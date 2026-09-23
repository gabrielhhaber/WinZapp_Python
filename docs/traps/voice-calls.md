# Voice calls — Python owns the audio, the page owns everything else

> The audio bridge, peer matching across JID forms, why the sync stand-down is bounded, call controls, PulseAudio for remote hosts, Socket.IO auth.
>
> Moved verbatim out of `CLAUDE.md` so it is read when the area is touched, not on every session. Keep it here: this is measured history, not a summary.

## Voice calls — Python owns the audio, the page owns everything else

WhatsApp signaling, encryption and WebRTC stay inside WhatsApp Web. What
WinZapp replaces is only the two ends of the audio: `client/core/call_audio.py`
(`CallAudioSession`) captures PCM from the local microphone and plays the PCM
extracted from the remote track, and `client/api_patches/src/util/callMediaBridge.ts`
injects a page script that hands those frames to and from WhatsApp's own
`RTCPeerConnection` through a synthetic `getUserMedia` track. The page never
opens a physical device. `callController.ts` exposes
`/api/:session/call/{accept,reject,end,offer,audio/enable,diagnostics}`, and
`createSessionUtil.ts` re-emits both `incomingcall` and the full `callstate`
lifecycle.

**Which Windows host API a call device opens on is decided by NAME, and the
first name match used to win.** PortAudio enumerates MME, then DirectSound,
then WASAPI, then WDM-KS, each listing the same physical device; measured on a
Realtek device, 90 ms of input buffering on MME and 120 ms on DirectSound
against 3 ms on WASAPI, and DirectSound's burst-and-starve pacing at 20 ms
blocks is the choppy audio of issue #272. `CallAudioSession._candidate_devices()`
now tries the WASAPI twin of the chosen device (exact name before the
31-character MME prefix match) first, shared mode. Exclusive mode is a
per-direction opt-in (`exclusive_input`, `exclusive_output`; the latter warns
that it silences the screen reader for the call) with three rules that each
come from a failure: the exclusive pass opens only devices exclusive mode
applies to (a non-WASAPI entry would "succeed" there and skip shared WASAPI);
it never leaves the device the user chose — not for the all-devices sweep (it
opened a virtual audio cable, the peer heard silence) and not for the system
default (a refusing headset put the call exclusive on the default speakers);
and the output opened while an incoming call RINGS is always shared, made
exclusive only on answer, so the ring tone and the announcement of who is
calling are still heard. A device switch mid-call
(`_restart_active_voice_call_audio`) restarts the audio only — the camera and
the `_active_voice_call` object stay, because `_start_call_camera()` compares
that object by identity.

**Playback is callback driven, behind a bounded jitter buffer, and the two
halves of that sentence are both load-bearing.** `_play_remote_loop()` used to
write each arriving packet straight into a blocking `OutputStream` opened at a
20 ms `blocksize` with `latency="low"` — one packet in, one write out, with no
reservoir at all. The remote track is tapped in the page by a
`createScriptProcessor(1024)`, a deprecated main-thread node that delivers
~21.3 ms chunks in bursts, and the bytes then cross Socket.IO and a Python
socket thread, so a few milliseconds of arrival jitter is routine. A device
buffer holding barely two periods underflows on it, and every underflow is an
audible click: hardware with a deep driver buffer hides it, a low-latency USB
headset (CORSAIR HS80, reported 2026-09-22 as constant popping through both
exclusive settings and after the WASAPI device-selection work) does not. The
output stream is now opened with `blocksize=0` (PortAudio serves the device's
own period, which is the only size WASAPI can serve without another conversion
buffer) and a `callback=` that drains `_OutputJitterBuffer`: ~60 ms of
prebuffer on cold start, originally only ~20 ms to resume after an underrun
(requiring the full target again would chop a jittery-but-adequate line into a
~50% duty cycle, which is worse than the clicks — that threshold is adaptive
now, see the next paragraph), a ~200 ms hard cap that drops the
**oldest** samples, and ~5 ms fades on starvation and resumption because the
discontinuity clicks even between two stretches of silence.

**The reservoir was never the fault — the SOURCE runs slow, and that was
measured, so do not re-derive it.** The paragraph above implies the remaining
popping was a tuning problem in the buffer. It was not. Over CDP against a
live, popping call (2026-09-23, CORSAIR HS80, WASAPI device 14, page bridge
version 7):

- The page's audio clock is exact — `ctx.currentTime` advanced 10.0000 s over
  10.0014 s of wall clock (ratio 0.99986). **Not clock drift.**
- The `createScriptProcessor(1024, 1, 1)` remote tap fired **464 callbacks
  where the context clock says 468.8 were due** (ratio 0.98987). It drops ~1%
  of its buffers outright — 1024 samples, 21.3 ms of audio, gone, ~0.5 times a
  second — with `longTaskCount` 0. Not a long-task stall: the deprecated
  main-thread node simply misses its deadline under ordinary scheduling.
- Net delivery to Python was **47,507 samples/s against the 48,000 it
  declares**. Arrival gaps were unremarkable (p50 20.08 ms, p90 29.76 ms, max
  32.43 ms).
- Python's side of the same call: `remote audio starved underruns=` grew by 50
  every ~18 s from the first seconds — **~2.8 underruns per second, steady,
  for 35 minutes** (6101 underruns, 26144 dropped samples).

A source delivering under real time drains **any fixed reservoir forever**, and
the 20 ms resume threshold then re-starved within a packet or two, so the
buffer lived permanently at the edge and every underrun was a fade-out/fade-in
chop. The choppy WhatsApp ringback fits: it arrives through the same relay.

Two fixes, and the first is the real one. The remote tap is now an
**AudioWorklet** (`winzapp-call-tap`, registered from a Blob URL, batching
128-sample quanta to ~1024 inside the worklet) which runs on the audio render
thread and cannot be starved by main-thread scheduling. `addModule()` needs a
URL and the page's CSP is entitled to refuse a `blob:` script, so the
ScriptProcessor path is kept as a fallback and **which one ran is reported** —
`call media remote-tap: ... tap=audio-worklet|tap=script-processor` in
`wppconnect.log`, plus `remoteTapMode`/`audioWorkletStatus` in
`/api/:session/call/diagnostics`. A silent fallback would look fixed and cost
another live call with a blind user to discover. Second, the Python resume
threshold is no longer fixed: `_OutputJitterBuffer` keeps an **adaptive
target** that starts at `CALL_OUTPUT_PREBUFFER_MS`, grows a step per recurring
underrun up to `CALL_OUTPUT_TARGET_MAX_MS` (120 ms, well under the 200 ms hard
cap, so drop-oldest still bounds latency) and decays back while playback stays
clean. Simulated against a 1%-slow source over 90 s: 44 underruns with the
fixed 20 ms threshold, 8 with the adaptive target (15 → 2 in the last 30 s).

**That floor at `CALL_OUTPUT_PREBUFFER_MS` is a deliberate reversal of PR
#278's review decision, and here is the condition to revisit it.** That review
chose 20 ms precisely so a jittery-but-adequate line would not be chopped into
a ~50% duty cycle; the target now never resumes on less than the full 60 ms.
The reversal is right for the failure that was actually measured — a permanent
20 ms of slack is what left the buffer sitting at the edge, and fewer underruns
from a deeper reservoir serves the jittery line better than a shallower resume
does. But it is a trade, not a free win: once the worklet lands and the source
is no longer slow, the underruns that remain ARE the jitter case, and each now
costs 60 ms of silence where it used to cost 20 ms. So if the logs then still
show `remote audio starved` recurring on a call whose tap reports
`tap=audio-worklet`, reconsider letting the target START at `CALL_FRAME_MS` and
grow from there, using the cold-start value only for the very first prime.
Growing is what makes that safe now and was not available before.

The adaptive target also survives `_reopen_output_exclusive()` — the same
device at the same rate on answer — and is dropped only when the rate actually
changes, because it is a sample count. Discarding it on the answer reopen would
hand the first minute of the conversation back to the chopping the ringing
phase had just learned its way out of.

**A reservoir in front of the output is the exact thing the 2026-09-20
bisection blamed, and it is back on purpose — so be accurate about what that
bisection established.** It showed that swapping this file for main's version
fixed severely choppy audio where three targeted fixes had not, which isolates
the defect to the reservoir rewrite *as a whole*; c78fe4c7 states outright that
the root cause was never pinned down further. The reverted version (0f11af9c)
was already hard capped at `CALL_OUTPUT_MAX_BUFFER_MS` and already dropped its
oldest samples, so neither of those is what makes this one different. What is
different is that nothing blocks and nothing paces: that version drained its
reservoir through blocking `stream.write()` calls, and on a device whose buffer
turned out to be five frames deep those writes did not block, so frames landed
in bursts (3d507ce1 added wall-clock pacing on top and that did not fix it
either). Here there is no write call anywhere — PortAudio asks for one device
period when it needs one, and that request rate is the only clock left. Treat
this as a deliberate re-test of the suspect component with the write path
removed: if choppiness returns, the next step is data (underrun counts, device
period, reported latency), not another guess.

Two consequences worth stating. The prebuffer is ~60 ms of one-way latency the
listener pays on every call. And with the blocking write gone, `_output_queue`
(40 packets, ~850 ms, drop-oldest) is effectively depth-1 — the player thread
empties it into the reservoir as fast as it arrives — so the 200 ms cap is now
the only bound in the path: a ~300 ms stall drops ~100 ms of speech where it
used to play late but whole. That is the intended trade (latency that never
creeps), not an oversight.

The callback never raises, never blocks and never
logs (all three are audible on a realtime thread); starvation is a counter, and
the player thread logs a rate-limited summary. Every failed candidate in
`_open_input_stream()`/`_open_output_stream()` is now logged at INFO too: the
same headset opened on WASAPI (device 14) for one call and on MME (device 6,
100 ms) for the next with nothing changed in between, and the reason the WASAPI
twin lost was swallowed into `last_error`.

**Video calls used to be out of scope, and are not any more — read this before
touching the camera path.** While they were, the camera failed closed through
THREE independent things rather than one: no `videoCapture` in the CDP grant,
the patched `navigator.permissions.query` claiming the microphone only, and
`bridgedGetUserMedia` serving a synthetic stream for any request naming audio
**or video**. The grant governs the Permissions API; the prompt is governed by
`--use-fake-ui-for-media-stream`, which accepts the request itself — so
dropping the grant closes nothing *on its own*, but as an explicit allowlist it
does deny what it omits, which is what made it a real second layer. The first
attempt at this put the video refusal *below* an early return that fell through
to the real device whenever `audio` was falsy, which left
`getUserMedia({video: true})` opening the webcam with no prompt and no
indicator, in a page the user never sees, while a test asserting on the removed
expression passed. Assert on the shape of the guard, not on the absence of a
string.

**One-to-one video calls are now supported, and TWO of those three layers are
gone.** `createSessionUtil.ts` grants `videoCapture`, and
`navigator.permissions.query` now answers `granted` for `'camera'` as well as
`'microphone'` — WhatsApp's VoIP bootstrap gates on it, so video does not work
without that one. What still holds the line is `bridgedGetUserMedia`, which
returns `cameraTrack()` (a canvas `captureStream`, fed by the Python-owned
ffmpeg capture in `client/core/call_video.py`) for any request naming video and
never calls `nativeGetUserMedia` with a video constraint. That is now a
**single** layer: any path that escapes the override — a reference to
`getUserMedia` captured before the patch ran, an iframe with its own
`navigator.mediaDevices`, a worker — reaches the physical webcam with no prompt
and no indicator. `videoCapture` in the grant appears to be unnecessary for the
feature (nothing in the bridge consults it), so removing just that one would
restore a layer without costing anything; it was left in deliberately and is
worth revisiting. The guard test is
`tests/test_call_control_api_patch.py::test_cdp_permission_grant_includes_voip_capture_permissions`,
which now asserts the grant *does* contain `videoCapture` — if you restore the
denial, restore that half of the assertion with it.

**Calls depend on the WhatsApp Web build, which is chosen by the age of the
install's catalogue — so "works for some testers, not others" is the expected
shape of a stale catalogue, not of a bug.** Measured on the first alpha: one
install failed every call with `WhatsApp VoIP initialization failed: WhatsApp
VoIP initializer completed without becoming ready`, and never received a single
`incomingcall` event either — a companion whose VoIP never initialises is not
offered calls by the server, so "cannot call" and "does not ring" are one
defect. It was pinned to `2.3000.1046948731-alpha` from a 417-entry catalogue.
An in-app API reinstall moved it to `2.3000.1047835881-alpha` (430 entries) and
the same profile logged `VoIP runtime warmed (accept=true, reject=true,
end=true)` within seconds. The server went 2.10.24 -> 2.10.27 in the same
reinstall, and that is **not** what fixed it: upstream changed only
express/sharp/minimatch between those tags, and wa-js stayed 4.6.0. The
catalogue is `@wppconnect/wa-version`, a *transitive* dependency of wppconnect
that is deliberately left updateable, so it only refreshes when `node_modules`
is rebuilt from scratch — which the in-app update does (`ApiSetupDialog` deletes
everything but `tokens`, `userDataDir` and the log) and a plain `npm install`
over a preserved tree does not. Bumping `wpp_minimum_version.txt` is therefore
the lever that reaches every tester, even when the server bump itself carries
nothing: it trips `ensure_wpp_version()`, whose update wipes `node_modules`.
Two limits worth knowing: that prompt can be declined, and it never appears in
`--background` mode, so an install started with Windows keeps its old build
until someone opens WinZapp in the foreground. When a tester reports calls not
working, the `Pinning WhatsApp Web to ...` line at the top of `wppconnect.log`
is the first thing to compare against a working install.

**A call event names the peer in whichever address form its source happened to
hold, and the two sources disagree.** The offer arrives through
`call.incoming_call` carrying whatever WhatsApp signalled with; the page's own
`CallStore.activeCall` poll (every 250 ms) reports the form the store holds. On
a LID-addressed account those are `<digits>@lid` and `<phone>@s.whatsapp.net`.
Compared raw, one call's `ACTIVE` and its terminal `ENDED` looked like two
different calls and both were discarded: nothing was announced, the call window
stayed up, and `CallAudioSession` went on reading the microphone for the rest of
the session. `core/call_matching.py` owns that decision — extracted from
`main.py` precisely so it is reachable from a test — with the peer comparison
injected (`_chat_jids_equivalent`) because the `@lid` caches live on
`MainWindow`. Once the held call has a *real* WhatsApp id, only that id matches,
so a delayed terminal event from an earlier call with the same person cannot
tear down the current one. Outgoing calls go through `_resolve_jid_for_send()`
like every send, for the same reason: an `@lid` is an identifier, not a number
to dial.

**That `activeCall` poll can lose its timer, and then no call state reaches
Python at all.** The listener installs as soon as WA-JS appears, which can be
before WhatsApp's bundle replaces `window.setInterval`/`clearInterval` with its
`JSScheduler` wrappers; measured 2026-09-21, the poll's native timer (id 4) had
silently stopped — a non-pausing Debugger logpoint on the tick never fired
while a fresh interval ticked normally — so a rejected outgoing call left the
call window up and the microphone capturing. Each tick now stamps
`window.__winzappCallStatePollLastTick`, and a Node-side watchdog in
`onIncomingCallDirect()` calls `reviveStalledCallStatePoll()` every 3 s, which
re-creates only the timer through `__winzappRearmCallStatePoll` (the closure,
and the call it was tracking, survive). `call-state poll had stopped ticking;
re-armed` in `wppconnect.log` means it happened. To check a live page, count
reads of `require('WAWebCallCollection').activeCall` for two seconds with an
accessor that returns the same value (then restore the data property): the
poll's reads should be among them. The incoming-offer poll
(`__winzappIncomingCallPoll`) is created in the same instant, so it has its
own heartbeat and is re-armed with it.

When `activeCall` disappears, the same poll asks the VoIP engine once:
`getCallInfo()` empty or `call_ending` ends the call at once, anything else
keeps the 5 s grace. "The engine names a different call_id" is deliberately
NOT used: whether that id has the CallStore id's form was never measured, and
a mismatch of form alone would end healthy calls on a brief `activeCall`
swap. Probe answers carry a generation, so one that resolves after its
absence is over cannot decide the next.

**Standing the background sync down during a call must happen where a round is
*decided*, never inside one.** `sync_remote_chats()` returns the set of chats
that FAILED, and `sync_chat_messages()` reports failure only by returning
`False` — so an early return from either reads as *every chat succeeded*:
`message_sync_ok` stays true, `_persist_successful_sync_state()` clears the
`force_full_pending` latch and the list-chats snapshot is committed for chats
nothing read. An account can be marked fully synced having fetched nothing, and
only F5 repairs that (see the sync-completion trap above — this is the same
trap from the other side). The gate therefore lives in the 60-second poll, the
backfill loop, the deep-history walk and the media sweep, all four of which
simply skip a cycle or do fewer chats in this one, and it is
`_voice_call_in_progress()` — **bounded**, because `_active_voice_call` is
cleared only by a terminal `callstate` event or by hanging up in WinZapp, and a
call torn down on the phone producing neither would otherwise pause every
background pass for the rest of the session. A paused sync is invisible: the
user just stops receiving messages. Losing the connection also ends the call,
since no terminal event can arrive over a dead socket.

That predicate is reached from four background paths, so **every test stub that
runs one of them binds it from the real class**. A stub whose `__getattr__`
invents a truthy answer makes the pause permanent, and the backfill loop then
sleeps a second per iteration until its whole deadline elapses — one guard left
to a default turned `tests/test_deep_history_backfill.py` into a 2h20m CI run
reporting 184 failures, most of them unrelated collateral from hours in one
process (dialog creation failing, `TextCtrl` returning `''`: the signature of
USER-object exhaustion).

**The call controls live in exactly one place**, the modeless
`voice_call_window`. An in-frame bar was tried alongside it and removed: two
copies of the same three buttons have to be kept in step by hand, and the
frame-level copy was never shown anyway, so `refresh_labels` was relabelling
the dead set. Focus is put on that window's mute button **once, when the window
appears** — `_sync_voice_call_bar()` runs again on every call-state change, so
focusing unconditionally yanked focus back while the user was reading the
conversation or had Tabbed to Desligar, and NVDA read the button over the call
audio. Same class of defect as the played-row repaint above. Failures are
spoken, so they go through `_raise_for_call_response()`/`_call_error_text()`:
the status code is what a user can act on, while the API's body — up to 500
characters of JSON wrapping a minified browser stack trace, which used to be
read aloud in full — stays in `log.log`.

**`_call_action_lock` serialises state transitions, never a blocking POST —
and the outgoing offer was the last place that broke the rule.** The camera
work was moved out of the lock because holding it across a device enumeration
made Ctrl+Shift+Q (hang up) sit there with nothing spoken; the offer POST kept
doing exactly that with a 75-second timeout, so a stalled offer left "end
call" dead for over a minute (issue #275). It now runs outside the lock, which
buys the race the lock was hiding: the user can hang up while the offer is in
flight, that `end` POST names a call WhatsApp has not created yet, and the
offer then lands and rings the peer for a call WinZapp thinks is over.
`_start_individual_call()` therefore keeps a per-attempt token
(`_outgoing_call_attempt`, a plain dict) that `end_active_call()` marks
`cancelled` **on the UI thread, before its worker starts**; an offer that
lands cancelled ends the call it just created and never starts the camera.
The token lives on the window and not on the call record because
`_start_voice_call_audio()` replaces `_active_voice_call` wholesale, so a flag
written onto the record could land on a dict nobody reads again.

**An offer that never goes live clears its own state, in a `finally`, guarded
by identity.** The failure path used to leave `_active_voice_call` set; the
stand-down bound above is what stopped that from being unbounded, so the cost
was up to two hours of paused background work plus a call window that stayed
up and a `voice_call_already_active` refusal of every later call — the safety
net worked, and the defect was still worth fixing. `_abandon_outgoing_call()`
is the one place that teardown lives, and it compares `_active_voice_call` by
identity the way `_start_call_camera()` does: a terminal `callstate` event or
an incoming call answered while the offer was in flight leaves a *different*
record in place, and tearing that one down would hang up a call the user is
in. For the same reason the cancel path's `end` POST is sent only on an
explicit cancel — it carries no call id, so it would end whichever call the
page holds now.

Call devices are their own pair (`settings["call_audio_devices"]`), separate
from Settings > Dispositivos de áudio on purpose: a headset chosen for calls
must not silently become the microphone that records voice messages.

**PulseAudio, and why it is not dead Linux code.** WinZapp itself always runs on
Windows, where the page bridge above is the whole mechanism. But with local API
mode off the user points WinZapp at a WPPConnect Server on their own Linux host,
and there the Chrome holding the session has no audio device at all — the page
bridge has nothing to hand PCM to. So `prepareLinuxCallAudioEnvironment()` gives
each session its own devices (`winzapp_<kind>_<session>`, scoped by name so
nothing can touch another application's) — at module level two null sinks plus
a `module-remap-source`, because Chromium leaves raw monitor sources out of
`enumerateDevices()`, and the module count is what the sweep has to clean —
binds that Chrome to them
through `PULSE_SOURCE`/`PULSE_SINK`, and `pacat`/`parec` move the bytes to and
from the Socket.IO stream the Windows client is on. Every function in that block
refuses to run unless `process.platform` is `linux` **and** `PULSE_SERVER` is
the WinZapp socket, which is what makes it read like dead code on a Windows
checkout. Deleting it would remove calls from remote-API installs while they go
on *appearing* to work, since signaling, ringing and the call window all come
from the page. `pulse_audio_lifecycle.py` is the other half: a `load-module`
outlives the process that asked for it, so a crashed Node leaves its devices
behind and the next session stacks another pair on top — hence the sweep, from
the start script as well as the stop ones.

**Socket.IO is authenticated now, and that is part of the same feature.**
`socketAuth.ts` binds every socket to the session encoded in the same
`<session>:<bcrypt>` token the REST API uses, and call events are emitted to
`session:<name>` rooms rather than broadcast. The server historically trusted
every connection because it only ever listened on localhost; that stops being
true the moment custom-API mode points at a remote host, which is exactly the
deployment the Pulse path above exists for. Note the ordinary message/ACK
traffic is deliberately left as a broadcast — this change is call-only — and
that is the reason **one shared remote server serving more than one account is
not supported yet**: every authenticated socket still receives every session's
`received-message`, `chats-update`, `onack` and even `qrCode`/`phoneCode`
(around twenty emit sites). `_belongs_to_this_session()` discards them on the
client, but the plaintext crosses first. Harmless on the default, where each
account has its own Node on its own port. Related, and also not solved: with a
user-supplied `http://`/`ws://` server the live microphone PCM crosses that
network in the clear, which `settings_import_api_confirm` does not warn about
— it covers the token.

## How WhatsApp Web actually consumes our camera (measured 2026-09-21)

Established with live instrumentation over a day of calls; each point cost at
least one real call to learn, so read before touching the video path.

- **There is no video sender to inspect.** Every RTCPeerConnection WhatsApp
  creates reports `getSenders()` empty for video and no `outbound-rtp` video
  stats. Its WASM call engine encodes and transmits on its own. Anything that
  reasons about "the WebRTC sender" is reasoning about something that does
  not exist.
- **It plays our track in hidden `<video>` elements** (not in the DOM) and
  snapshots them with `new VideoFrame(video)`. Our canvas, our own track and
  those elements all carried the real picture; the loss people reported was
  never on the sending side.
- **The bridge's media scan sees those elements too.** `scanMediaElements()`
  hands every `<video>` with a stream to `attachRemoteVideo()`, so without a
  guard the "remote" picture in WinZapp's call window was the user's OWN
  camera. Two WinZapp users calling each other each saw themselves (or black
  with the camera off) while a phone on the other end saw them fine.
  `attachRemoteVideo()` therefore refuses `isOurCameraTrack()`, and
  `MediaStreamTrack.prototype.clone` is hooked so a clone WhatsApp makes of
  our track stays marked. The microphone always had the equivalent guard
  (`localTrackIds`).
- **Video off/on must go through the engine's own toggle,**
  `getVoipStackInterface().setCallVideoMute(muted)` (arity 1, resolves 0) --
  the same as WhatsApp's camera button. With it, WhatsApp re-requests
  `getUserMedia({video: true})` on resume. The canvas is still painted black
  on "off" as the privacy guarantee if that native call ever fails.
  `requestKeyFrame` exists but wants the peer JID and is not needed.
- **Test video with a phone on the other end,** not a second WinZapp, until
  you have confirmed WinZapp's own remote rendering on that build: a broken
  receiver is indistinguishable, from the sender's chair, from a broken
  sender. A sighted-assistance app describing the call window is how the own-
  camera bug was finally seen.
- **The other person's video is never a track or a `<video>` the page can
  read.** Measured over CDP during a live call with a phone: the engine's
  `getShortStatisticString()` reported `Decoding 640x432 (H264) @ 19 fps`
  while nothing on the page carried a single frame of it -- no track event,
  no foreign `<video>`, no `VideoDecoder`, no canvas draw. The engine hands
  decoded frames only to canvases registered with
  `WAWebVoipVideoRendererRegistry` (`registerVideoCanvas(canvas, false)` then
  `assignSourceToCanvas({canvas, mirror: false, source})`, with
  `source = WAWebVoipVideoRenderSource.peer(peerJid, CAMERA)`), which is what
  WhatsApp's own call UI does for the peer tile. `syncPeerVideo()` registers
  one such canvas per video call and captures it into
  `__winzappOnCallRemoteVideo`, released when the call ends. The registry keeps
  a set of canvases per source, so this coexists with WhatsApp's own. Before
  it, WinZapp had never shown the peer at all: the only "remote" video the
  bridge ever found was the user's own camera.
- **Debugging this live is far cheaper over CDP than by restarting WinZapp.**
  WinZapp's Chrome is `HeadlessChrome` on a loopback remote-debugging port
  (find it with `netstat` against the `chrome.exe` PIDs, confirm with
  `/json/version` before listing targets so a personal Chrome's tabs are
  never read). `getShortStatisticString()` and `getCallInfo()` on the VoIP
  interface answer "is media arriving at all" in one call; `getCallInfo()`
  carries account identifiers, so filter it before printing.
