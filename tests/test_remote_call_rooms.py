from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_remote_call_audio_is_emitted_only_to_authenticated_session_room():
    source = (ROOT / "client/api_patches/src/util/callMediaBridge.ts").read_text(encoding="utf-8")
    assert "io.to(`session:${client.session}`).emit('call:audio:remote'" in source
    assert "io.emit('call:audio:remote'" not in source


def test_remote_call_signaling_is_emitted_only_to_session_room():
    source = (ROOT / "client/api_patches/src/util/createSessionUtil.ts").read_text(encoding="utf-8")
    assert source.count("req.io.to(`session:${client.session}`).emit('incomingcall'") == 2
    assert "req.io.to(`session:${client.session}`).emit('callstate'" in source


def test_unrelated_socket_events_keep_existing_behavior():
    source = (ROOT / "client/api_patches/src/util/createSessionUtil.ts").read_text(encoding="utf-8")
    # This change is deliberately call-only: normal message ACK traffic remains untouched.
    assert "req.io.emit('onack', { ...ack, session: client.session });" in source


def test_remote_audio_recovers_tracks_that_arrive_without_a_track_event():
    source = (ROOT / "client/api_patches/src/util/callMediaBridge.ts").read_text(encoding="utf-8")
    assert "pc.getReceivers?.()" in source
    assert "Promise.resolve(result).then(attachRemoteReceivers)" in source
    assert "call media ${event}: ${details}" in source


def test_remote_audio_also_observes_media_streams_assigned_to_audio_elements():
    source = (ROOT / "client/api_patches/src/util/callMediaBridge.ts").read_text(encoding="utf-8")
    assert "const attachRemoteStream" in source
    assert "Object.getOwnPropertyDescriptor(mediaProto, 'srcObject')" in source
    assert "new MutationObserver(scanMediaElements)" in source


def test_remote_audio_observes_the_web_audio_playback_graph():
    source = (ROOT / "client/api_patches/src/util/callMediaBridge.ts").read_text(encoding="utf-8")
    assert "createMediaStreamSource" in source
    assert "createMediaStreamTrackSource" in source
    assert "__winzappCallMediaSourceWrapped" in source


def test_audio_graph_hook_cannot_recursively_attach_the_same_remote_track():
    source = (ROOT / "client/api_patches/src/util/callMediaBridge.ts").read_text(encoding="utf-8")
    assert "remoteTrackIds: new Set<string>()" in source
    assert "if (state.remoteTrackIds.has(id)) return;" in source
    assert "state.remoteTrackIds.add(id);" in source


def test_audio_graph_hook_never_routes_the_synthetic_microphone_to_speakers():
    source = (ROOT / "client/api_patches/src/util/callMediaBridge.ts").read_text(encoding="utf-8")
    assert "localTrackIds: new Set<string>()" in source
    assert "if (state.localTrackIds.has(id)) return;" in source
    assert source.count("state.localTrackIds.add(micTrack.id)") == 1


def test_microphone_bridge_does_not_replace_webrtc_tracks_after_call_setup():
    source = (ROOT / "client/api_patches/src/util/callMediaBridge.ts").read_text(encoding="utf-8")
    assert "pc.addTrack =" not in source
    assert "Do not replace pc.addTrack here" in source
