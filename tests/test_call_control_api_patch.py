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
    assert "version === 7" in bridge
    assert "version: 7" in bridge

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
    # but only for a call that was actually answered (state.enabled at some
    # point) — a merely-ringing call being cancelled/rejected must not open it,
    # or a coincident missed-call message ping slips through unmuted.
    assert "let callWasAnswered = false;" in bridge
    assert "if (state.enabled) callWasAnswered = true;" in bridge
    assert "if (callWasAnswered) allowCallEndChime();" in bridge
    assert "if (state.enabled) allowCallEndChime();" in bridge
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
