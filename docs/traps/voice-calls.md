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
lifecycle. Video calls are deliberately out of scope: the camera path fails
closed, and it takes THREE things rather than one: no `videoCapture` in the CDP
grant, the patched `navigator.permissions.query` claiming the microphone only,
and `bridgedGetUserMedia` serving a synthetic stream for any request naming
audio **or video**. The grant governs the Permissions API; the prompt is
governed by `--use-fake-ui-for-media-stream`, which accepts the request itself
— so dropping the grant closes nothing on its own. The first attempt at this
put the video refusal *below* an early return that fell through to the real
device whenever `audio` was falsy, which left `getUserMedia({video: true})`
opening the webcam with no prompt and no indicator, in a page the user never
sees, while a test asserting on the removed expression passed. Assert on the
shape of the guard, not on the absence of a string.

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
