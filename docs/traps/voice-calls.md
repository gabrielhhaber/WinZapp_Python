# Voice calls — Python owns the audio, the page owns everything else

## Reinstall is not proof of a fresh catalogue (2026-10-22)

A report from a live v3.0.0.4676alpha session, supplied by the user after
Antigravity's CDP diagnosis, identified `getVoipInitFailureClass()` as
`wasm_contract_mismatch`, `getDidVoipInitError()` true and readiness false.
The report says changing wa-version 1.5.5049 to 1.5.5086 restored calls. Those
live measurements were not repeated by the source-fix session. They fit the
main-document/live-worker skew measured below, but do not establish every
worker/version detail independently.

The source confirms a separate defect: setup rebuilds node_modules, whereas
the refresh verdict used to contain only a timestamp. Reinstalling a lower
catalogue inside that six-hour window could therefore keep it lower. Do not
repeat the supplied claim that upstream v2.10.61 ships a package-lock locked
to 1.5.5049: its source tree has yarn.lock (wa-version 1.5.4472), not that
package-lock. The actual origin of the installed 1.5.5049 remains unverified.

`wa_version_lifecycle` identifies the WPPConnect-resolved package (nested
before hoisted), manifests and installation identity. The six-hour verdict
applies only to that identity and a usable catalogue. State replacement is
atomic; API swap/rollback and catalogue publish/apply/setup share a gate
beside api/, which survives its rename. Downloads extract outside api/ and
recheck identity under the gate before publishing, so an old download cannot
write its verdict into a replacement. A completed startup task may run again
when the installation changes; a live task is never duplicated.

`wa_version_setup` snapshots a valid old package before npm can remove it,
preserves a newer copy only if compatible with the new runtime/dependencies,
then performs a private fresh check and apply before setup succeeds. The
same helper is used by first/modules-only/full/staged setup and setup_api.py.
An early in-place cancellation after npm also preserves a compatible newer
copy. This is not a transaction for the entire in-place npm installation;
the staged API updater retains its existing whole-server rollback.
Offline, unsupported latest, missing dependencies and failed checks are
explicit unconfirmed-freshness outcomes, never evidence that 'latest' is
installed. A tree with no usable catalogue cannot report setup success.
The resolver still reads /latest only: an incompatible latest is unsupported,
not a search result proving the newest compatible historical release.

`voipInitPolicy.ts` is serialized into the page before both warmup and call
actions. Optional getters are defensive and failure classes are allowlisted
before crossing into diagnostics. A confirmed wasm_contract_mismatch with
failed=true and canRetry=false aborts immediately, including before another
init call; missing getters and unknown/pending states retain bounded polling.
One in-flight initialization and at most one allowed/legacy retry are shared
per page. An ordinary failure releases the promise; 'more than once' retains
the no-second-WASM-load guard. No automatic reload, Node restart, account
closure or resending of a call is introduced. Updating files cannot repair
WASM already loaded in the current page.

Synthetic coverage: test_wa_version_reinstall.py and test_voip_init_policy.py,
plus the existing refresh, staging and controller contracts. No live device,
provider, CDP, wx window or call is needed. Windows copies use extended paths
because a catalogue's descendants can exceed MAX_PATH even with a short root.

## Streaming device-rate conversion (2026-10-19)

The call ends now use `core/call_audio_resampling.py` rather than per-packet
linear interpolation. Rounding 1024 samples from 48 kHz to 44.1 kHz separately
produced 941 samples each time instead of preserving the cumulative 940.8;
this is a source-derived count defect, not a measured live-call diagnosis.
The causal windowed-sinc FIR keeps integer rational clock position and sample
history, suppressing alias/image frequencies. Its fixed delay is at most 4 ms
at the supported 8–384 kHz rates; the equal-rate path does no filtering.

Microphone callbacks copy float32 only; the sender does conversion and PCM16
encoding. Converted input is packetized at 960 samples so dropping a stale
20 ms capture still has the existing call-frame meaning. Discarding backlog
also discards FIR/partial-frame history; the audio processor is not told,
because AEC3 re-estimates the delay itself. The remote player's FIR, rate snapshot and
reservoir push share the output-swap lock; reopen/stop discard old FIR history,
even when the new stream's rate is unchanged. Do not reset the FIR at every
packet or flush its old tail into a replacement stream. Device/rate selection,
exclusive access, buffer targets and all enhancement settings are unchanged.

Synthetic contracts are in `tests/test_call_audio_resampling.py`. They do not
establish live-provider, hardware, phone or packaged-NVDA acceptance.

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
that object by identity. It also must **not** emit `call:audio:stop`: that
event makes the page bridge `reset()`, which sets `enabled = false`, and only
`/call/audio/enable` turns it back on (the new session's `call:audio:start`
touches the Linux path alone). Every switch used to emit it twice
(`CallAudioSession.stop()` and `stop_call_audio_stream()`), so Python kept
sending microphone frames that `pushMicrophone()` dropped and the other person
stopped hearing the user for the rest of the call, voice and video alike.
`stop(notify_bridge=False)` is the switch's path; the end of a call, and a
switch that cannot open any device, still reset the bridge. The switch carries
the microphone mute into the new session before it opens, since a fresh
session starts unmuted.

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

**The microphone tap has the same defect and is now a worklet too — and the
judgement that it could wait was wrong in practice.** Measured the same way
(`micProcessor.onaudioprocess` wrapped over CDP, 2026-09-23): it missed 0.37%
of its callbacks against the remote tap's 1.01%, consuming 47,817 samples/s
against the 48,096 Python pushes, with the surplus discarded by the latency
drop policy. That was judged tolerable — a tenth of the remote tap's incident
rate, no downstream amplifier, and the peer's own jitter buffer concealing
what little escapes. Then Gabriel switched from a separate microphone to his
headset's own and the people on the other end heard the chop. **The peer is
the only one who can hear this side**, so the evidence for a mic fault never
arrives from the user running the build; weight it accordingly next time.

Its worklet (`winzapp-call-mic`) is deliberately NOT a mirror of the remote
one. It has zero inputs and exists to *produce*, so what had to leave the main
thread is the **pull**: the frame queue and the drop policy now live inside
the worklet, fed by `postMessage` from `pushMicrophone()`, and `process()`
drains them without asking the main thread for anything. A queue left on the
main thread would have put main-thread scheduling straight back on the audio
path. Both processors are registered by one `addModule()` — the only thing
that can refuse them, a CSP refusing `blob:`, refuses both or neither.

Two mechanisms are worth knowing before touching this. **Both producers feed
the same `MediaStreamDestination`**: WhatsApp cloned that track once at
`getUserMedia` time and never asks for another, so swapping the worklet for
the ScriptProcessor underneath it needs no renegotiation — which is what makes
an *upgrade in place* possible when the module finishes registering after the
track was already handed out. And the worklet is on a **watchdog**
(`MIC_WORKLET_WATCHDOG_MS`), not on trust: if frames were handed over and none
were consumed when it fires, the ScriptProcessor takes the destination back.
The watchdog re-arms rather than firing while nothing has been pushed yet —
before the call is answered there is no microphone audio to consume and
silence proves nothing. `micTapMode` in `/call/diagnostics` and the `mic-tap`
log line say which producer is live.

Three details that are easy to get wrong when touching this. The watchdog's
verdict is `micWorkletUnscheduled`, **not** `audioWorkletStatus`: that flag
answers only "did `addModule()` register the module", the remote tap branches
on it, and a microphone starved for a second and a half by a suspended
context would otherwise put every remote track for the life of the page back
on the ScriptProcessor — the audio the user actually hears, degraded as a side
effect of a microphone heuristic. The terminal fallback is **wrapped**, and
drops `micDestination` if even the ScriptProcessor will not start: with both
producers null on a track that still reads `live`, `ensureMicTrack()` early
returns it forever and the microphone is dead for the session. And the drop
policy moved into the worklet is the same arithmetic but not quite the same
behaviour: `offset > 0` is now sampled against a 128-sample quantum instead of
a 1024-sample block, against 20 ms frames, so the head is partially consumed
in 7 quanta out of 8 and the effective cap is `targetFrames + 1` nearly
always, where on the main thread it was usually `targetFrames`. That is ~20 ms
more steady-state microphone backlog than before — accepted, not overlooked.

The worklet reports its counters every `PAGE_MIC_REPORT_QUANTA` (~128 ms), so
a swap or a `reset()` discards up to one report's worth of `consumed` and
`dropped`. `micSamplesConsumed` therefore under-reports by up to ~128 ms per
swap; it never jumps or goes backwards, which is the invariant the deltas
exist for, but do not read it over a window of a few hundred milliseconds and
conclude anything.

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

**A catalogue that is old by a few weeks fails the same way, and the cause is
skew between the pinned page and Meta's live workers (measured 2026-10-01,
over CDP on a live session).** The symptom is the one above, but this time the
newest entry was 2.3000.1048298845 (23 Sep) and the page's pthread VoIP workers
died with `Aborted(No EM_ASM constant found at address 1367423)`, so
`WAWebVoipInit` never became ready. start.js pins only the main document;
WhatsApp serves the workers and the glue they load live, and Meta had changed
the worker bundle since that build. The pinned main glue's `EM_ASM` table ended
at 1367423, the worker asked for an address beyond it, and the abort is the
mismatch, not a wa-js or WinZapp defect. After `npm install
@wppconnect/wa-version@1.5.4964` (newest build 2.3000.1048960956) VoIP
initialised and calls worked. So "the catalogue's newest entry reaches
`CALLS_MINIMUM_BUILD`" is necessary and **not sufficient**: the newest entry
has to keep up with what Meta serves.

That is why `core/wa_version_refresh.py` exists. The catalogue used to move only
when `node_modules` was rebuilt, so testers kept an old one silently. At every
launch a daemon thread asks the registry for `@wppconnect/wa-version/latest`
(3 KB; never the packument, 7.6 MB abbreviated) at most every 6 hours, and
**stages** a newer same-major, in-range, non-prerelease package in
`wa-version.staged-<version>` beside the live one: the tarball is 42.6 MB (about
35 s on a 10 Mbit link), so Node must not wait for it. The swap, with rollback,
happens right before this process spawns Node, on a worker (never the UI
thread: `_start_wpp_background_after_catalogue()`), under an OS lock separate
from the download's, with the 161 MB old copy only renamed aside and deleted on
a background thread. It is skipped ("busy", package left staged) while another
account holds a live node-lease, because `start.js` re-reads
`html/<build>.html` from this package every time a session's page is created,
not once per Node, so a rename under a live Node can fail a session start. A
staged package must also carry the html of the newest build in its own
`versions.json`, or it is never promoted. Consequences to remember: the first update lands one launch after it was downloaded (unless the
download beat the 3 s wait), a launch that is closed mid-download wastes the
download, and a package whose declared dependencies the install lacks is skipped
(the in-app reinstall remains the fallback). The failure the user sees when it
has not caught up is classified by `core/call_voip_errors.py` and spoken as
`voice_call_voip_unavailable` instead of an HTTP code.

**A call placed without an entry trust waits for a popup nobody can answer.**
From WhatsApp Web 2.3000.1048x, `startWAWebVoipCall(peer, isVideo, fromUi,
?, callId, options)` first awaits
`WAWebVoipOutgoingCallConsent.hasOutgoingCallConsent(entryTrustOf(options), peer, isVideo)`,
and a missing `options.entryTrust` means `"deep_link"`: a confirmation popup,
"Start a WhatsApp call with <name>?". wa-js 4.6.0's `WPP.call.offer()` passes
only four arguments, so in the hidden page every offer hung: `page.evaluate`
never returned, the HTTP request was aborted at 75 s, `Runtime.callFunctionOn`
timed out minutes later, and each attempt left one more popup open (two
found over CDP on 2026-09-23, on build 1048298845, with an up-to-date
catalogue and server 2.10.27, so reinstalling could not help). The controller
now calls `startWAWebVoipCall` itself with `{ entryTrust: 'user_gesture' }`,
which is what a keystroke in WinZapp is, and keeps `WPP.call.offer()` only as
the fallback where the function is not exposed. wa-js 4.6.1's
`WPP.call.offer()` makes the same call natively (same arguments, then polls
`CallStore`), so the fallback works there too; the direct path stays primary
because it behaves identically on 4.6.0 and 4.6.1. If offers hang again, look
for `[role="dialog"]` in the page over CDP before anything else.

**Other notices can still stop an offer the same way.** Calling a WhatsApp
Business account for the first time opens "About this call — This business
uses a secure service from Meta to manage this call…" with *Learn more* and
*Continue* (seen 2026-09-29): same symptom, the offer aborted at 75 s and the
call never rang; the next call to that business goes through, which makes it
look intermittent. `offerCall()` now runs `watchOutgoingCallNotices()` while
the offer is pending: an informational dialog that appears during it (one
with a "Learn more" link and at most three buttons) gets its last (primary)
button pressed — the user asked for this call, and the position, not the
wording, keeps it working in every UI language. A dialog with no link is a
decision (e.g. "Unblock X to call?" with Cancel/Unblock, whose last button
would unblock someone) and is left alone, as are dialogs already open before
the call. The log line `[call-notice]` records only the button count, never the
dialog text, which can hold a contact or business name. The watcher stops after
60 s. If a new notice still stalls an offer, look at its DOM over CDP: the link
test is a guess at how WhatsApp marks "informational", not a guarantee.

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
in.

**The cancel path's `end` POST is gated on three things, because `end` is
unscoped on the Node side.** `callController.ts`'s `'end'` branch uses the
requested `callId` only to set `userEndedCall` on the model; the action itself
is `voipStack.endCall(2, true)` (or `WPP.call.end()`), which hangs up whatever
call the page currently holds — passing an id does not scope it. A stalled
offer therefore cannot post `end` on "the attempt was cancelled" alone: after
the hang-up, `_stop_voice_call_audio(grace_seconds=1.25)` leaves the record up
for another 1.25 s and the user is free to dial again or answer an incoming
call long before a 75-second offer gives up, so a late `end` would kill that
second, live call a few seconds in with nothing spoken. It is sent only when
the attempt is still the current `_outgoing_call_attempt` **and** either its
own record is still active or there is no active call at all — all three
re-read inside `_call_action_lock` after the POST returns, never snapshotted
before it. That second lock block is also why every *other* call-control POST in the
file is issued under the lock (`_attach_audio_to_browser_call()` takes it
too). **The offer POST is the one exception, deliberately** — holding the
lock across its 75-second timeout is the whole of #275, and the paragraph
above is why. Moving it back under the lock to make this sentence uniform
would reintroduce that bug. Two later POSTs are outside it for the same
reason: `upgrade-video` (Ctrl+P, 20 s timeout; the call record is re-checked
under the lock afterwards) and the read-only `status` asked before an end is
announced (see "Voice → video upgrade" below).

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

## Voice → video upgrade, and "ended" must mean ended (2026-09-26)

- **An upgrade is not a new call.** Read from WhatsApp Web's bundle over CDP:
  the A/V switch keeps the same `CallModel` and call id, and
  `WAWebVoipBridgeCallStateHandlers.handleVideoStateChange` only sets
  `isVideo = hasActiveVideo()` on it when the engine reports
  `callMediaStateChanged`. The `activeCall` poll's signature did not include
  `isVideo`, so the upgrade never reached Python; it does now, and
  `on_voice_call_state_event()` treats the flip as an upgrade (one way:
  `isVideo` drops back to false when both cameras are off, which is still a
  video call to the user). The call window turns into the video window in
  place -- NOT hidden and re-shown, which raised it and took focus while the
  user was reading a conversation and cut the announcement off (the
  focus-once rule above); focus moves only off the promote button it hides.
  The camera is only *probed* (`_probe_call_camera()`, transmit off, and it
  stops only a capture it started): when the other person upgrades, this
  side's camera stays off until the user turns it on, as in WhatsApp.
- **Upgrading from WinZapp** (Ctrl+P, the button after the microphone) is
  `getVoipStackInterface().requestVideoUpgrade()` — arity 0, resolves 0 on
  success — which is exactly what WhatsApp's own camera button does in an
  audio call. `acceptPeerVideo({jid})` exists for the unknown-contact banner.
  That POST runs OUTSIDE `_call_action_lock`, like the offer: held across
  its 20 s timeout, Ctrl+Shift+Q right after Ctrl+P blocked in silence
  (#275 again). The button appears only once the call is ACTIVE.
- **The chime window used to unmute the live call.** `reset()` →
  `allowCallEndChime()` restored every muted page element, the `<audio>`
  playing the call's `MediaStream` included, and WhatsApp keeps those
  elements out of the DOM, where the 250 ms scan never reaches them again. A
  terminal event for a call the page had NOT ended therefore put the other
  person straight onto the default speaker for the rest of the call.
  `restorePageAudio()` now refuses any `srcObject` MediaStream element, and
  `applyPageAudioPolicy()` silences one BEFORE the chime window (one starting
  to play inside those 2.5 s was otherwise left audible): the chime is an
  audio file, never a stream.
- **A terminal event is confirmed before it is announced.** The source of the
  terminal event WinZapp received during the upgrade was never measured (no
  test partner was available on 2026-09-26), so the fix does not depend on
  it: `_confirm_call_ended()` asks `/call/status` about that call id first
  (via `findCall()`, the same fallback the poll uses across a brief
  `activeCall` swap; read-only, so outside the lock). A second terminal
  event for a call already being checked is not dropped -- each is emitted
  once -- it makes a "live" answer be asked again.
  Only "the page's `activeCall` is that id, connected, and `getCallInfo()`
  does not say none/ending" keeps the call (logged as `ignored a terminal
  event: the page still holds this call live`); any other answer, including
  none at all, ends it — a call wrongly kept up can still be hung up from the
  window, a live call wrongly declared over cannot. If that log line shows up,
  capture the call over CDP (wrap `__winzappOnCallState` and record
  `new Error().stack`) to find which path emits it.
- **A declared end is enforced.** After "Ligação encerrada" is spoken (and 2.5 s
  after the user hangs up), `_ensure_page_call_ended()` posts
  `/call/ensure-ended` with the call id. Unlike `end`, it is scoped: it ends
  the page's call only if `activeCall` is that exact id and still connected
  or dialling -- checked again inside the native callback right before the
  unscoped `endCall()` -- so it can never kill a newer call. `page still held a call
  WinZapp had ended` in `log.log` is the defect's signature — if it appears,
  some terminal event was spurious and its source is worth finding.

**The call media bridge must never reload WhatsApp Web to install itself.** Its hooks (synthetic `getUserMedia`, the `RTCPeerConnection` tap) have to exist before WhatsApp's VoIP modules evaluate. They used to be registered with `evaluateOnNewDocument` only once WPPConnect handed over an already-loaded page, followed by a "priming" `page.reload()`. On 2026-09-24, right after an alpha update, that reload wedged the page on two consecutive starts. WhatsApp Web had reloaded itself right after its first load (history entry `transitionType: reload`, not served by the pin), and the priming reload landed on top of it.
- **What the renderer did:** it stopped answering everything, including CDP `Runtime.evaluate`, `Debugger.enable`, `Page.getFrameTree`, and even the trace-start task. It was idle, not suspended, with no JS dialog, not frozen (`Page.setWebLifecycleState active` changed nothing), and `Page.stopLoading` changed nothing either.
- **What the user saw:** the session sat in `INITIALIZING` until `Navigation timeout of 120000 ms exceeded`, and never recovered, so WinZapp went offline.
- **The proof:** removing the reload alone connected on the next start.

`start.js` now calls `registerCallMediaBridgeBeforeLoad(page)` from its `initWhatsapp` wrapper, before WPPConnect's first `goto()`. `ensureCallMediaBridge()` only installs into the running page, and if the early registration is missing it logs a warning instead of reloading. The page script looks the `__winzappOnCall*` bindings up at use time, so running before `exposeFunction` is safe. If a session is ever stuck in `INITIALIZING` with an unresponsive page, the first thing to check is whether anything reloads it during startup.

## Echo cancellation and noise suppression on the microphone (2026-09-30, rebuilt 2026-10-19)

A tester with speakers reported the other person hearing their own voice, only in WinZapp (the official app and Meet were fine: they run WebRTC's own AEC, which never sees WinZapp's synthetic microphone track, so **nothing on the page can do this — Python owns the signal and must**). The setting existed, and did close to nothing.

- **Now WebRTC's own module does it** (`core/call_audio_processing.py`, `CallAudioProcessor`, through `livekit.rtc.AudioProcessingModule`): AEC3 with its residual echo suppressor and delay estimator, plus WebRTC's noise suppressor and high-pass filter, in exactly 10 ms frames at 48 kHz. The sender thread feeds the speaker reference (`push_reference`, from the output-callback tap) before each microphone frame (`process`). Measured on the same synthetic speech: the hand-written numpy canceller gave **5-11 dB**, AEC3 gives **~32 dB in the first seconds and ~70 dB converged**, at under 1% of one core. With a laptop speaker or a mixer loopback the echo is nearly as loud as the voice, so 10 dB was "it cancels nothing" to testers (2026-10-19). Do not go back to a hand-written AEC; tune or replace the module instead.
- **History, so nobody rebuilds it:** an NLMS filter tested on white noise (52 dB) gave 1-4 dB on speech; a frequency-domain Kalman filter with GCC-PHAT alignment and a correlation gate reached ~20 dB after several seconds on SAPI speech and 5-11 dB on the test signals, with no residual-echo suppressor. Do not "fix" an AEC by testing it on noise; the tests in `tests/test_call_audio_microphone_processing.py` and `tests/test_call_audio_processing.py` assert on what leaves through `call:audio:mic`.
- **The reference still starts with the microphone.** The speaker opens at the first ring and the microphone only on answer; `_restart_echo_reference()` empties the tap and builds a fresh processor when the microphone opens, so a device switch never carries an echo path learned on the old speaker. The delay hint (`set_stream_delay_ms`) is output + input stream latency; AEC3 finds the real delay itself, so dropped stale microphone frames need no bookkeeping.
- **Packaging:** the module is `livekit/rtc/resources/livekit_ffi.dll` (~25 MB), loaded through `importlib.resources`, so `build.py` and `macos/build_app.py` `--collect-all livekit`. If it cannot load, `CallAudioProcessor` logs once and the microphone goes out unprocessed rather than failing the call. `[call_audio] microphone processing: {...}` in `log.log` (at mic open and every ~5 s) shows `engine` (`webrtc` / `unavailable`), the delay hint and frame/error counts. Every ~5 s it also carries `core/call_echo_measure.py`'s measure over the last 2 s: `echo_in` / `echo_out` (0..1, how much of the speaker reference is in the microphone before and after the module) with their lags, and `speaker_dbfs` / `mic_dbfs` / `sent_dbfs` (measured on the 8 kHz copy: compare them with each other, not as absolute levels). Read it only while the other person talks. High in, low out: working. High out: the module is not removing an echo it has the reference for. Low in while the other person hears themselves: the echo comes through a path the reference does not carry. Added after a mixer-loopback tester still heard an echo (2026-10-19) and the log could not tell which; a reference-lead theory from that log (620 ms of speaker audio queued before the microphone opened) was simulated and is harmless: AEC3 still gave 52 dB.
- **AEC3 only reaches ~0.5 s.** Simulated on a loopback copy: ~70 dB of reduction up to 450 ms late, 1-2 dB from 600 ms, whatever the gain (even 4x and clipping). A mixer loopback can return later than that (a tester heard his whole voice back, 2026-10-19). `CallAudioProcessor` therefore measures the echo's delay itself once a second (`echo_coupling` up to 1.5 s, coupling >= 0.4, two estimates within 20 ms) and, past `DIRECT_ECHO_MAX_MS` (350), holds the reference back so AEC3 sees the echo ~150 ms after it (`reference_held_ms` in the log, plus an info line on every change). 900 ms late: 1 dB -> ~75 dB. Below the threshold nothing is held, and a headset never moves it.
- **Defaults:** echo cancellation is on (`migrate_echo_cancellation_default()` turned it on once for existing installs, `settings-migrations.md`); noise suppression stays off. With a headset AEC3 finds no echo and leaves the voice alone.

**Answering from any window: two Windows hotkeys held only while they can act.** `main_window/call_hotkeys.py` registers them with `_HotkeyManager`: *answer* (shipped Ctrl+Alt+Shift+Enter) while an offer rings, *end* (shipped Ctrl+Alt+Shift+Backspace) while any offer rings or a call is in progress. *Answer* accepts the call, then brings the call window in front of any program (`core/dialog_foreground.force_foreground(topmost=True)`, the helper the incoming-call popup uses too) and focuses its first button; a video call is answered like its Answer button, camera on. A group offer takes *answer* like a one-to-one one (`incoming_call_can_answer()` is always true; the page accepts it through the same native `acceptCall`), in oldest-first order. *End* rejects the first ringing offer, a group one included, before it would hang up the call in progress: a group call ringing during a one-to-one call must not cost the user the call they are on. Both are settings (`general.call_answer_hotkey` / `call_end_hotkey`, `{"vk": 0, "mod": 0}` = removed) edited as `global` rows of Settings > Keyboard shortcuts next to the show-WinZapp key; an import that puts one on the show-WinZapp key or on the other call key keeps the old value. A show-WinZapp key saved on a call key's combination (possible before the call keys shipped) keeps it: `call_hotkey_setting()` then reads that call key as removed, and `saved_call_hotkey()` is the raw value. The call keys retry a failed registration every second, not every 15 s, since another account lets go of the key as soon as its call is decided. `_sync_incoming_call_bar()` and `_sync_voice_call_bar()` (which `_stop_voice_call_audio()` queues at the end of a call) ask for a sync with `_request_call_hotkey_sync()`, which queues one `_sync_call_hotkeys()` behind a pending flag. Never sync in place: `accept_incoming_call()` stops the alert before it sets `_active_voice_call`, and a sync in between dropped the end key and took it again on every answer, each `stop(wait=True)` holding the UI thread. `_perform_shutdown()` calls `_release_call_hotkeys()` first, and both keys do nothing once `_shutting_down` is set: answering into a server being stopped opens call audio for nothing. Why they are not permanent: a system-wide Ctrl+Alt+Shift+Enter held all day would take that key from every other program. Several accounts open: each account is its own process and Windows gives a combination to one registration, so while account A holds *end* for its call, account B's ringing call cannot take it and the press meant for B reaches A. With nothing ringing locally, A therefore asks every other running account first (`ipc.request_reject_ringing_call`, off the wx thread) and hangs up only when none had a call ringing, and only if the same call is still in progress by then.
