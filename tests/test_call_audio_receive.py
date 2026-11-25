"""Wire decoding and direct PCM delivery share the same ownership-safe sink."""
import base64
from types import SimpleNamespace as N
import types

import pytest

from core.call_audio_socketio import decode_remote_audio, socketio_binary_to_bytes
from core.call_audio_transport import CallAudioFrame
from tests.test_call_media_epoch import socket_methods
from tests.test_call_audio_lifecycle_ownership import methods


A, B = "a" * 32, "b" * 32
PCM = b"\x00\x80\xff\x7f\x00\x00"


@pytest.mark.parametrize("wire", [
    PCM, bytearray(PCM), list(PCM), {"type": "Buffer", "data": list(PCM)},
    base64.b64encode(PCM).decode("ascii"),
])
def test_socketio_representations_become_the_same_pcm_frame(wire):
    frame = decode_remote_audio({"pcm": wire, "sampleRate": "16000", "mediaEpoch": A})
    assert frame == CallAudioFrame(PCM, 16000, A)
    assert isinstance(frame.pcm, bytes)


@pytest.mark.parametrize("wire", [
    None, b"", "not base64!", [-1], [256], [None], object(),
    {"type": "Buffer", "data": "not a list"},
    {"type": "Other", "data": list(PCM)},
])
def test_malformed_and_empty_wire_audio_is_not_delivered(wire):
    assert socketio_binary_to_bytes(wire) == b""
    assert decode_remote_audio({"pcm": wire}) is None


@pytest.mark.parametrize("epoch", [None, "", "wrong", 123, False])
def test_malformed_explicit_owner_cannot_be_downgraded_to_legacy(epoch):
    assert decode_remote_audio({"pcm": PCM, "mediaEpoch": epoch}) is None


@pytest.mark.parametrize("rate", [None, 0, "invalid", [], {}])
def test_legacy_rate_fallback_stays_48000(rate):
    assert decode_remote_audio({"pcm": PCM, "sampleRate": rate}) == CallAudioFrame(PCM, 48000)


def test_legacy_receiver_keeps_its_two_argument_call_contract():
    delivered = []
    frame = decode_remote_audio({"pcm": PCM})
    frame.deliver_to(lambda pcm, rate: delivered.append((pcm, rate)))
    assert delivered == [(PCM, 48000)]


def test_tagged_frame_keeps_its_owner_at_the_receiver_boundary():
    delivered = []
    frame = decode_remote_audio({"pcm": PCM, "mediaEpoch": A})
    frame.deliver_to(lambda *a, **kw: delivered.append((a, kw)))
    assert delivered == [((PCM, 48000), {"media_epoch": A})]


def test_mutable_socket_buffer_is_copied_before_delivery():
    wire = bytearray(PCM)
    frame = decode_remote_audio({"pcm": wire, "mediaEpoch": A})
    wire[:] = bytes(len(wire))
    assert frame.pcm == PCM


@pytest.mark.parametrize("data", [None, [], b"", "invalid"])
def test_non_object_envelope_is_rejected(data):
    assert decode_remote_audio(data) is None


def audio_session(epoch):
    from core.call_audio import CallAudioConfig, CallAudioSession
    audio = CallAudioSession(None, CallAudioConfig("synthetic"))
    audio.set_media_epoch(epoch)
    return audio


def test_direct_transport_can_deliver_pcm_without_a_socket_or_wire_decoder():
    audio = audio_session(A)
    frame = CallAudioFrame(PCM, 16000, A)
    frame.deliver_to(audio.enqueue_remote_audio)
    queued = audio._output_queue.get_nowait()
    assert (queued.pcm, queued.rate, queued.epoch) == (PCM, 16000, A)


@pytest.mark.parametrize("notify_bridge", [False, True])
def test_late_delivery_to_stopped_receiver_cannot_reach_a_replacement(notify_bridge):
    old, new = audio_session(A), audio_session(B)
    delayed_receiver = old.enqueue_remote_audio
    old.stop(notify_bridge=notify_bridge)
    CallAudioFrame(PCM, 48000, A).deliver_to(delayed_receiver)
    CallAudioFrame(PCM, 48000, A).deliver_to(new.enqueue_remote_audio)
    CallAudioFrame(PCM, 48000).deliver_to(new.enqueue_remote_audio)
    assert old._output_queue.empty() and new._output_queue.empty()
    CallAudioFrame(PCM, 48000, B).deliver_to(new.enqueue_remote_audio)
    assert new._output_queue.get_nowait().epoch == B


def test_device_switch_in_the_same_call_keeps_only_the_replacement_receiver():
    old, new = audio_session(A), audio_session(A)
    old.stop(notify_bridge=False)
    frame = CallAudioFrame(PCM, 48000, A)
    frame.deliver_to(old.enqueue_remote_audio)
    frame.deliver_to(new.enqueue_remote_audio)
    assert old._output_queue.empty()
    assert new._output_queue.get_nowait().pcm == PCM


def window_with_audio(audio):
    window = N(_active_voice_call={"media_epoch": A}, _call_audio_session=audio)
    window.on_call_remote_audio = types.MethodType(methods("on_call_remote_audio")["on_call_remote_audio"], window)
    return window


def test_reconnection_during_wire_decode_is_rechecked_by_the_actual_pcm_sink():
    old, new = audio_session(A), audio_session(B)
    window = window_with_audio(old)
    scope = socket_methods("on_call_audio_remote")

    def reconnect(data):
        window._active_voice_call = {"media_epoch": B}
        window._call_audio_session = new
        return decode_remote_audio(data)

    scope["decode_remote_audio"] = reconnect
    ws = N(main_window=window, _belongs_to_this_session=lambda data: True)
    scope["on_call_audio_remote"](ws, {"pcm": PCM, "mediaEpoch": A})
    assert old._output_queue.empty() and new._output_queue.empty()


def test_wire_owner_is_captured_before_payload_conversion_can_relabel_it():
    audio = audio_session(B)
    data = {"mediaEpoch": A}

    class RelabelingPayload(list):
        def __iter__(self):
            data["mediaEpoch"] = B
            return super().__iter__()

    data["pcm"] = RelabelingPayload(PCM)
    frame = decode_remote_audio(data)
    assert data["mediaEpoch"] == B and frame.media_epoch == A
    frame.deliver_to(audio.enqueue_remote_audio)
    assert audio._output_queue.empty()


def test_foreign_account_is_rejected_before_decoding():
    scope = socket_methods("on_call_audio_remote")
    scope["decode_remote_audio"] = lambda data: pytest.fail("decoded a foreign account")
    ws = N(main_window=N(), _belongs_to_this_session=lambda data: False)
    scope["on_call_audio_remote"](ws, {"session": "other", "pcm": PCM})
