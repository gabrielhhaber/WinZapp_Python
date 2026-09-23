import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_wppconnect_patch_exposes_voice_call_control_routes():
    routes = _source("client/api_patches/src/routes/index.ts")
    controller = _source("client/api_patches/src/controller/callController.ts")

    assert '/api/:session/call/accept' in routes
    assert '/api/:session/call/reject' in routes
    assert '/api/:session/call/end' in routes
    assert '/api/:session/call/offer' in routes
    assert '/api/:session/call/diagnostics' in routes
    assert "getVoipStackInterface" in controller
    assert "enableCallInterface" in controller
    assert "requireVoipJsBackend" in controller
    assert "initWAWebVoip" in controller
    assert "acceptCall(true" in controller
    assert "rejectCall()" in controller
    assert "endCall(2, true)" in controller
    assert "WPP.call.accept" in controller
    assert "WPP.call.rejectCall" in controller
    assert "WPP.call.end" in controller
    assert "WPP.call.offer" in controller
    assert "prepareAudioBridge(req)" in controller
    assert "isOutgoingOrLiveCall" in controller
    assert "without successful voipInit" in controller
    assert "installAudioBridge(req)" in controller
    assert "callDiagnostics" in controller


def test_call_media_bridge_replaces_browser_microphone_with_python_pcm():
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")

    assert "navigator.mediaDevices" in bridge
    assert "getUserMedia" in bridge
    assert "MediaStreamAudioDestinationNode" in bridge or "createMediaStreamDestination" in bridge
    assert "pushMicrophone" in bridge
    assert "decodePcm16" in bridge
    assert "call:audio:mic" in bridge
    assert "call:audio:remote" in bridge
    assert "RTCPeerConnection" in bridge
    assert "__winzappOnCallRemoteAudio" in bridge
    assert "if (!constraints?.audio && !constraints?.video) {" in bridge
    assert "new MediaStream([cameraTrack()])" in bridge
    assert "call:video:camera" in bridge
    assert "call:video:remote" in bridge
    assert "video-request-refused" not in bridge
    assert "microphone" in bridge
    assert "camera" in bridge
    assert "webkitGetUserMedia" in bridge
    assert "if (!state.enabled || !constraints?.audio)" not in bridge


def test_call_media_bridge_advertises_virtual_camera_on_headless_hosts():
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")

    assert "nativeEnumerateDevices" in bridge
    assert "bridgedEnumerateDevices" in bridge
    assert "deviceId: 'winzapp-camera'" in bridge
    assert "kind: 'videoinput'" in bridge
    assert "label: 'WinZapp Camera'" in bridge
    assert "devices.unshift(virtualCamera)" in bridge
    assert "cameraTrackRequests" in bridge
    assert "cameraFramesReceived" in bridge
    assert "call camera frames received from desktop=" in bridge
    # The re-install guard and the state literal must name the SAME version,
    # or a page that already carries an older bridge is never replaced (the
    # guard returns early) or is replaced on every call (it never matches).
    # Asserted as a pair rather than as a number, so a deliberate bump does
    # not have to be re-typed here — only a mismatch fails.
    guard = re.search(r"__winzappCallMediaBridge\?\.version === (\d+)", bridge)
    literal = re.search(r"^\s*version: (\d+),", bridge, re.M)
    assert guard and literal, "the bridge must carry a version at both sites"
    assert guard.group(1) == literal.group(1), (
        f"guard says v{guard.group(1)}, state literal says v{literal.group(1)}"
    )


def test_remote_audio_tap_prefers_an_audio_worklet_and_still_has_a_fallback():
    """Measured 2026-09-23 on a live call: the ScriptProcessor tap dropped ~1%
    of its own buffers (464 callbacks where 468.8 were due, longTaskCount 0),
    delivering 47,507 samples/s against the 48,000 it declares — which drains
    the Python jitter buffer forever. A worklet runs on the audio render
    thread and cannot be starved by the main thread.

    The fallback is not optional: addModule() needs a URL and WhatsApp Web's
    CSP may refuse a blob: script, and a call with no remote audio at all is
    far worse than a popping one. Assert on the shape of the guard — both taps
    reachable, and which one ran reported — not on the wording.
    """
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")

    assert "registerProcessor('winzapp-call-tap'" in bridge
    assert "audioWorklet" in bridge and "addModule" in bridge
    assert "new win.AudioWorkletNode(context, 'winzapp-call-tap'" in bridge
    # The fallback path survives, and is chosen only when the module is not.
    assert "startScriptProcessorTap" in bridge
    assert "createScriptProcessor(1024, 1, 1)" in bridge
    # The shape of the fallback, not its wording: the ScriptProcessor tap is
    # reachable from the module being refused AND from the node constructor
    # throwing. A track left with no tap at all is silence on the call, which
    # is worse than the popping this replaces.
    assert bridge.count("startScriptProcessorTap(") >= 2  # two reachable call sites
    assert re.search(
        r"catch \(error[^)]*\) \{.{0,600}?startScriptProcessorTap\(", bridge, re.S
    ), "a worklet node that throws must fall back, not leave the track untapped"
    assert re.search(
        r"if \(ready\)[^}]*startWorkletTap\(\)", bridge, re.S
    ), "the worklet tap is used only when the module actually registered"

    # Two guards that are the difference between a working call and a silent
    # or double-tapped one, and neither is visible in the happy path.
    assert "state.remotePipelines.get(id) !== pipeline" in bridge, (
        "a track that ended while addModule() was in flight must not be revived"
    )
    teardown = bridge[bridge.index("const disconnectRemotePipeline"):]
    teardown = teardown[: teardown.index("\n  };")]
    assert "pipeline.node?.disconnect()" in teardown, (
        "a worklet node outliving the call goes on sending frames to Python"
    )
    assert "pipeline.processor?.disconnect()" in teardown
    # Which tap ran has to be visible from outside: a silent fallback looks
    # fixed and is not, and finding that out costs another live call.
    assert "audioWorkletStatus" in bridge
    assert "tap=audio-worklet" in bridge
    assert "tap=script-processor" in bridge
    assert "remoteTapMode" in _source("client/api_patches/src/controller/callController.ts")
    # Batching happens in the worklet (128-sample quanta -> ~1024), never on
    # the main thread, which is the thread being taken off the audio path.
    assert "REMOTE_TAP_BATCH_SAMPLES = 1024" in bridge
    assert "batchSamples: REMOTE_TAP_BATCH_SAMPLES" in bridge
    # The real context rate goes with the frames; only the Linux relay, which
    # creates its own devices, may assume 48000.
    assert "callback(base64, context.sampleRate)" in bridge
    assert "REMOTE_TAP_MAX_FRAME_BYTES" in bridge


def test_microphone_tap_prefers_an_audio_worklet_and_can_swap_back():
    """The peer hears this side chop for the mirror-image reason.

    Measured the same way as the remote tap: the mic ScriptProcessor missed
    0.37% of its callbacks against the remote tap's 1.01%. That was judged
    tolerable when the microphone was a separate device; with the headset's
    own microphone the people on the other end reported the chop, so the
    producer moves to the audio render thread too.

    It is NOT a mirror of the remote tap: zero inputs, and what has to leave
    the main thread is the PULL, so the frame queue moves INTO the worklet.
    Assert on the shape — the queue's home, both producers reachable, one at
    a time, on one destination — never on reason wording.
    """
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")

    assert "registerProcessor('winzapp-call-mic'" in bridge
    assert "new win.AudioWorkletNode(context, 'winzapp-call-mic'" in bridge
    # Zero inputs: a mirror of the remote tap's `numberOfInputs: 1` would make
    # the node wait for a source that does not exist.
    mic_node = bridge[bridge.index("'winzapp-call-mic', {"):]
    mic_node = mic_node[: mic_node.index("});")]
    assert "numberOfInputs: 0" in mic_node

    # The queue lives in the worklet, and so does the drop policy: a queue
    # left on the main thread would put main-thread scheduling straight back
    # on the audio path, which is the whole defect being fixed.
    worklet = bridge[bridge.index("class WinzappCallMicProcessor"):]
    worklet = worklet[: worklet.index("registerProcessor('winzapp-call-mic'")]
    assert "this.queue.push(data)" in worklet
    assert "this.queue.shift()" in worklet
    assert "this.dropped" in worklet, "the latency drop policy moved in here too"
    # Counters cross as DELTAS, so a mid-call swap cannot make the totals jump
    # or go backwards.
    assert "consumed: this.consumed" in worklet and "this.consumed = 0" in worklet

    # Both producers exist, exactly one is connected, and both feed the same
    # destination — WhatsApp cloned that track once and never asks again.
    assert "createScriptProcessor(1024, 0, 1)" in bridge
    # Exact, not a lower bound: a lower bound against N real call sites lets
    # any one of them be deleted with the test still green. Every terminal
    # fallback goes through swapMicProducerToScriptProcessor (watchdog, node
    # constructor throwing, upgrade-in-place throwing); the ScriptProcessor
    # itself is started from that wrapper and from the not-yet-ready branch.
    assert bridge.count("startMicScriptProcessorTap(") == 2
    assert bridge.count("swapMicProducerToScriptProcessor(") == 3
    teardown = bridge[bridge.index("const disconnectMicProducer"):]
    teardown = teardown[: teardown.index("\n  };")]
    assert "state.micNode = null" in teardown and "state.micProcessor = null" in teardown
    assert "state.micNode?.disconnect()" in teardown
    assert re.search(
        r"catch \(error[^)]*\) \{.{0,400}?swapMicProducerToScriptProcessor\(", bridge, re.S
    ), "a mic worklet node that throws must fall back, not leave the call mute"

    # A silent microphone is discovered by the PEER, so the worklet gets a
    # deadline rather than trust, and the swap goes back to the same
    # destination.
    assert "MIC_WORKLET_WATCHDOG_MS" in bridge
    # The watchdog has to be ARMED where the worklet starts — asserting the
    # name alone is satisfied by its own definition, so deleting the call
    # would leave an unwatched worklet and a green test.
    start_worklet = bridge[bridge.index("const startMicWorkletTap"):]
    start_worklet = start_worklet[: start_worklet.index("\n  };")]
    assert "armMicWorkletWatchdog();" in start_worklet
    watchdog = bridge[bridge.index("const armMicWorkletWatchdog"):]
    watchdog = watchdog[: watchdog.index("\n  };")]
    assert "state.micFramesPushed === pushedAtArm" in watchdog, (
        "silence proves nothing until frames have actually been handed over"
    )
    assert "state.micSamplesConsumed > consumedAtArm" in watchdog, (
        "a worklet that IS consuming must not be swapped out"
    )
    assert "swapMicProducerToScriptProcessor(" in watchdog, (
        "firing without handing the destination back leaves the call mute"
    )
    # The watchdog is a MICROPHONE verdict. Writing audioWorkletStatus here
    # would condemn the remote tap, which branches on that same flag, and put
    # the blind user's own audio back on the ScriptProcessor #281 replaced.
    # The ASSIGNMENT, not the word: the comment in there explains at length
    # why this flag must not be touched, and prose must not satisfy the test.
    assert "state.audioWorkletStatus =" not in watchdog
    assert "state.micWorkletUnscheduled = true" in watchdog
    # The terminal fallback is wrapped: if it throws, both producers are null
    # on a track that still reads 'live', so ensureMicTrack() would early
    # return it forever and the microphone would be dead for the session.
    swap_body = bridge[bridge.index("const swapMicProducerToScriptProcessor"):]
    swap_body = swap_body[: swap_body.index("\n  };")]
    assert "try {" in swap_body and "state.micDestination = null" in swap_body

    # The upgrade-in-place guard, both halves: the wrong destination means a
    # newer track owns the page, and an existing node means two producers on
    # one destination — the peer would hear doubled audio.
    assert "state.micDestination !== destination || state.micNode" in bridge

    # Counters are ADDED on this side too, not assigned.
    assert "state.micSamplesConsumed += data.consumed" in bridge
    # The partially-consumed head frame is kept while older complete frames
    # are dropped; dropping the head instead cuts a frame mid-sample.
    assert worklet.count("const dropIndex = this.offset > 0 ? 1 : 0;") == 2

    # pushMicrophone hands frames to the worklet when it is live, and the
    # queue below it is the fallback's, not a second copy.
    assert "state.micNode.port.postMessage(samples)" in bridge
    # reset() has to reach the queue that now lives on the audio thread.
    reset = bridge[bridge.index("state.reset = () => {"):]
    reset = reset[: reset.index("\n  };")]
    assert "postMessage({ type: 'clear' })" in reset

    # Which producer is live is visible from outside, same reason as the
    # remote tap — and here the user cannot hear the failure himself.
    assert "micTapMode" in bridge
    assert "micTapMode" in _source("client/api_patches/src/controller/callController.ts")
    # The synthetic track is still registered as local, or the bridge loops
    # our own microphone back into the speaker pipeline.
    assert "state.localTrackIds.add(track.id)" in bridge


def test_chromium_does_not_disable_voice_input_for_python_call_bridge():
    start_js = _source("client/api_patches/start.js")
    config = _source("client/api_patches/src/config.ts")
    session_util = _source("client/api_patches/src/util/sessionUtil.ts")

    assert "'--disable-voice-input'," not in start_js
    assert "--disable-voice-input" not in config
    assert "--disable-voice-input" not in session_util
    assert "--mute-audio" not in config
    assert "\n  '--mute-audio'," not in start_js
    assert "--mute-audio" not in session_util
    assert "ignoreDefaultArgs: ['--mute-audio']" in start_js


def test_headless_chromium_auto_grants_webrtc_media_permission():
    start_js = _source("client/api_patches/start.js")
    config = _source("client/api_patches/src/config.ts")
    session_util = _source("client/api_patches/src/util/sessionUtil.ts")

    # Current WhatsApp Web initializes the native VoIP backend only after the
    # browser-level media permission gate passes.  The Python bridge replaces
    # getUserMedia with a synthetic track, while fake-ui removes Chromium's
    # permission UI (which cannot be answered in headless mode). Never launch
    # a fake capture device: WhatsApp's native VoIP backend can bypass the JS
    # wrapper and would then transmit Chromium's silent test microphone.
    for source in (start_js, config, session_util):
        assert "--use-fake-ui-for-media-stream" in source
        assert "--use-fake-device-for-media-stream" not in source


def test_runtime_user_agent_matches_the_launched_chromium_for_voip():
    start_js = _source("client/api_patches/start.js")

    assert "WAuserAgente" in start_js
    assert "uaModule.useragentOverride" in start_js
    assert "Chromium user-agent aligned" in start_js


def test_pinned_document_preserves_cross_origin_isolation_for_voip_wasm():
    start_js = _source("client/api_patches/start.js")

    assert "Cross-Origin-Opener-Policy" in start_js
    assert "Cross-Origin-Embedder-Policy" in start_js
    assert "require-corp" in start_js
    assert "Origin-Agent-Cluster" in start_js
    assert "--disable-web-security" not in start_js


def test_chromium_keeps_rendering_backend_available_for_voip_runtime():
    session_util = "\n".join(
        line
        for line in _source("client/api_patches/src/util/sessionUtil.ts").splitlines()
        if not line.lstrip().startswith("//")
    )

    assert "--disable-software-rasterizer" not in session_util
    assert "--disable-3d-apis" not in session_util
    assert "--disable-webgl" not in session_util


def test_cdp_permission_grant_includes_voip_capture_permissions():
    create_session = _source("client/api_patches/src/util/createSessionUtil.ts")
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")

    granted = [
        line for line in create_session.splitlines()
        if "permissions: [" in line and not line.lstrip().startswith("//")
    ]
    assert len(granted) == 1, granted
    assert "'audioCapture'" in granted[0]
    assert "'videoCapture'" in granted[0]
    assert "name === 'microphone' || name === 'camera'" in bridge
    assert "new MediaStream([cameraTrack()])" in bridge
    assert "video-request-refused" not in bridge

def test_setup_api_copies_call_patch_files_into_runtime_api():
    setup_api = _source("setup_api.py")

    assert "src/util/callMediaBridge.ts" in setup_api
    assert "src/controller/callController.ts" in setup_api


def test_outgoing_call_allows_native_voip_more_than_generic_http_timeout():
    main_py = _source("client/main.py")

    offer = main_py[main_py.index("def _start_individual_call"):]
    offer = offer[: offer.index("threading.Thread(target=_worker")]
    assert '"offer",' in offer
    assert "timeout=75," in offer
    assert "dial_jid = self._resolve_jid_for_send(peer_jid) or peer_jid" in offer
    assert '{"to": dial_jid, "isVideo": is_video}' in offer

def test_outgoing_call_prefers_new_active_call_over_stale_collection_model():
    controller = _source("client/api_patches/src/controller/callController.ts")

    assert "const preexistingIds = new Set(" in controller
    assert "const previousActiveId = callIdOf(storeBeforeOffer?.activeCall)" in controller
    assert "const active = store?.activeCall" in controller
    assert "activeId !== previousActiveId" in controller
    assert "!preexistingIds.has(offeredId)" in controller
    assert "!preexistingIds.has(modelId)" in controller




def test_active_call_poll_tolerates_transient_active_call_gaps():
    source = _source("client/api_patches/src/util/createSessionUtil.ts")

    assert "const ACTIVE_CALL_MISSING_GRACE_MS = 5000" in source
    assert "let activeCallMissingSince = 0" in source
    assert "if (previousId) activeCall = findCall(previousId)" in source
    assert "now - activeCallMissingSince < ACTIVE_CALL_MISSING_GRACE_MS" in source
    assert "emitCallState('ended', lastActiveCall, 'ENDED')" in source
    # The regression was a one-poll null immediately becoming ENDED.
    assert "if (!activeCall) {\n                if (lastActiveCall) {" not in source

def test_handled_incoming_call_cannot_fire_stale_120_second_timeout():
    create_session = _source("client/api_patches/src/util/createSessionUtil.ts")
    controller = _source("client/api_patches/src/controller/callController.ts")

    # Valmir's log showed offer -> accept -> NOT_ANSWERED exactly 120 seconds
    # after the original offer. Newer WA keeps accepted calls in activeCall,
    # so the incoming tracker must see that slot instead of its stale ring model.
    assert "const active = store?.activeCall || store?.get?.('activeCall')" in create_session
    assert "if (active && callIdOf(active) === id) return active" in create_session

    # Successful WinZapp actions also retire the incoming watchdog explicitly,
    # covering builds where activeCall changes identity during the transition.
    assert "__winzappForgetIncomingCall" in create_session
    assert "trackedCalls.delete(id)" in create_session
    assert "forgetIncomingCall(callId || callIdOf(call))" in controller
    assert "forgetIncomingCall(callId)" in controller
    assert "forgetIncomingCall(handledCallId)" in controller

def test_session_prewarms_lazy_whatsapp_voip_runtime():
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")
    create_session = _source("client/api_patches/src/util/createSessionUtil.ts")

    assert "export async function warmCallVoipRuntime" in bridge
    assert "requireVoipJsBackend" in bridge
    assert "initWAWebVoip" in bridge
    assert "getVoipStackInterface" in bridge
    assert "void warmCallVoipRuntime(client, req.logger)" in create_session
def test_native_call_actions_wait_for_lazy_voip_rpc_initialization():
    controller = _source("client/api_patches/src/controller/callController.ts")

    assert "const maxAttempts = 8" in controller
    assert "isVoipInitError(error)" in controller
    assert "Math.min(1500, 300 * (attempt + 1))" in controller
    assert "winzapp_call_action" in controller
    assert "getIsVoipInited" in controller
    assert "retryWAWebVoipInitAfterFailure" in controller


def test_voip_initialization_can_retry_after_lazy_backend_failure():
    """A failed lazy VoIP initialization must be retried by WinZapp's bridge."""
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")

    assert "WPP?.call?.enableCallInterface" in bridge
    assert "getDidVoipInitError" in bridge
    assert "retryWAWebVoipInitAfterFailure" in bridge
    assert "attempt < 10" in bridge

def test_voip_runtime_warmup_is_deduplicated_per_session():
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")

    assert "__winzappVoipWarmupPromise" in bridge
    assert "if (pending) return pending" in bridge
    assert "delete (client as any).__winzappVoipWarmupPromise" in bridge
    assert "winzapp_session_warmup" in bridge
    assert "getDidVoipInitError" in bridge


def test_page_native_audio_mutes_message_ping_but_preserves_call_end_chime():
    """Page audio is muted by default, except for the real terminal-call chime."""
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")

    # The old loop-only rule was the regression: it muted the ringtone but
    # allowed WhatsApp Web's short incoming-message ping through.
    assert "let allowCallEndChimeUntil = 0;" in bridge
    assert "let callWasActive = false;" in bridge
    assert "const silencePageAudio = (el: HTMLMediaElement)" in bridge
    assert "silencePageAudio(el);" in bridge
    assert "return !(el.srcObject instanceof MediaStream) && el.loop === true;" in bridge

    # A live/ringing -> terminal transition opens the short-audio exception,
    # but only for a call that was actually CONNECTED -- a merely-ringing call
    # being cancelled/rejected must not open it, or a coincident missed-call
    # message ping slips through unmuted. "Connected" comes from the page's
    # CallStore state, keyed by call id, never from state.enabled (enabled too
    # early on outgoing calls, never on the Linux path). Its behaviour is
    # executed for real in tests/test_call_end_chime_policy.py.
    assert "let answeredPageCallKey: string | null = null;" in bridge
    assert "if (active && isConnectedPageCall(call)) answeredPageCallKey = key;" in bridge
    assert "if (lastCallWasAnswered()) allowCallEndChime();" in bridge
    assert "state.enabled) allowCallEndChime" not in bridge
    assert "callWasAnswered" not in bridge
    assert "pageAudioNow() + 2500" in bridge
    assert "if (pageAudioNow() <= allowCallEndChimeUntil)" in bridge
    assert "restorePageAudio(el);" in bridge

    # An element carrying the call's own live MediaStream is muted like any
    # other page-native media (fix(calls) 810acca5): WinZapp's separate PCM
    # tap reads the raw MediaStreamTrack directly, so this cannot affect what
    # Python receives, and skipping it here let WhatsApp Web's own native
    # playback double up with WinZapp's relayed copy.
    assert "if (el.srcObject instanceof MediaStream) {" not in bridge

    # HTMLMediaElement.play is checked synchronously; periodic scanning covers
    # autoplay/property changes and refreshes call lifecycle even with no media.
    assert "win.HTMLMediaElement.prototype.play = function" in bridge
    assert "const silenced = applyPageAudioPolicy(el);" in bridge
    scan = bridge[bridge.index("const scanMediaElements = ()"):]
    scan = scan[: scan.index("\n  };")]
    assert "refreshCallAudioPolicy();" in scan
    assert "applyPageAudioPolicy(element);" in scan

    # Newly-created Audio objects enter the same policy, so message pings do
    # not escape through a separate constructor path.
    audio_proxy = bridge[bridge.index("const NativeAudio = win.Audio"):]
    audio_proxy = audio_proxy[: audio_proxy.index("win.__winzappPageAudioMuteAudioWrapped = true;")]
    assert "applyPageAudioPolicy(instance);" in audio_proxy

    assert "if (mutedLogCount > 40) return;" in bridge



def test_remote_linux_call_audio_relay_can_start_while_call_is_still_ringing():
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")

    assert "socket.on('call:audio:start'" in bridge
    assert "ensureLinuxCallAudio(session, socket, logger)" in bridge
    assert "Linux call speaker monitor started before answer" in bridge


def test_call_media_bridge_bounds_microphone_backlog_to_live_audio():
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")

    # Regression: all three microphone queues were raised to 75 x 20 ms,
    # allowing roughly 1.5 seconds of old speech to be replayed after a stall.
    assert "const MAX_MIC_QUEUE_FRAMES = 12;" in bridge
    assert "const MIC_TARGET_BACKLOG_FRAMES = 3;" in bridge
    assert "queue.length > MIC_TARGET_BACKLOG_FRAMES" in bridge
    assert "queue.splice(0, dropped)" in bridge

    # The final WebAudio queue inside the WhatsApp page has its own bound and
    # preserves only a partially-consumed head plus the freshest frames.
    assert "const PAGE_MIC_QUEUE_FRAMES = 4;" in bridge
    assert "const PAGE_MIC_TARGET_BACKLOG_FRAMES = 2;" in bridge
    assert "state.micFramesDroppedForLatency" in bridge
    assert "const dropIndex = state.micOffset > 0 ? 1 : 0;" in bridge


def test_turning_video_off_reaches_the_page_and_blanks_the_canvas():
    """REGRESSION: the page draws our camera frames onto a canvas and hands
    WhatsApp a captureStream() of it, which keeps emitting whatever that
    canvas last holds at 10 fps. Stopping the desktop-side ffmpeg capture
    therefore froze the user's last frame and went on transmitting that
    picture of them -- for the rest of the call, and into the next one,
    because reset() never cleared the canvas or the track either.

    Asserted on the shape of the mechanism rather than on one string: a
    stopCamera() the socket can reach, a blank actually painted, and a
    teardown wired into reset().
    """
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")

    # Reachable from the Node layer, which is how "turn video off" travels.
    assert "socket.on('call:video:camera:stop'" in bridge
    assert "__winzappCallMediaBridge?.stopCamera?.(stoppedEpoch, nativeMute)" in bridge

    # WhatsApp Web sends our camera through its own call engine, so turning
    # video off and on goes through that engine's camera toggle -- the same
    # one WhatsApp's UI button uses.
    assert "socket.on('call:video:camera:start'" in bridge
    assert "__winzappCallMediaBridge?.resumeCamera?.()" in bridge
    assert "stack?.setCallVideoMute" in bridge
    assert "if (native) setNativeVideoMute(true);" in bridge
    assert "setNativeVideoMute(false);" in bridge
    assert "pushCameraFrame?.(frame, frameEpoch)" in bridge

    # The blank is what actually stops the transmission; the track stays live
    # so the peer connection is not torn down mid-call.
    assert "state.stopCamera = (epoch?: number, native?: boolean) =>" in bridge
    assert "blankCameraCanvas" in bridge
    assert "context.fillRect(0, 0, canvas.width, canvas.height)" in bridge

    # A decode that lands after the stop must not repaint the frame the blank
    # just erased.
    assert "generation === state.cameraGeneration" in bridge

    # A frame from a capture the desktop already stopped is dropped, keyed on
    # the desktop's own epochs -- NOT on state.enabled, which never becomes
    # true on the Linux/PulseAudio path and is cleared by a mid-call reset().
    assert "epoch <= state.cameraStoppedEpoch" in bridge
    push_body = bridge[
        bridge.index("state.pushCameraFrame = "):bridge.index("const blankCameraCanvas")
    ]
    assert "state.enabled" not in push_body

    # reset() blanks the SAME canvas and never replaces it: it also runs
    # mid-call on an audio device restart, and WhatsApp keeps its clone of the
    # original track, so replacing the canvas left the peer watching black for
    # the rest of the call while WinZapp said video was on.
    assert "blankCamera();" in bridge
    assert "teardownCamera" not in bridge
    assert "state.cameraCanvas = null;" not in bridge
    assert "state.cameraTrack = null;" not in bridge


def test_the_desktop_side_has_a_camera_stop_channel():
    websocket_client = _source("client/core/websocket_client.py")
    assert "def send_call_camera_stop(self, epoch: int | None = None, native: bool = False)" in websocket_client
    assert "def send_call_camera_start(self)" in websocket_client
    assert '"call:video:camera:start"' in websocket_client
    assert '"call:video:camera:stop"' in websocket_client

