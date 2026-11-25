"""The replaceable call transport keeps the current wire format and ownership."""
import base64

import pytest

from core.call_audio_transport import SocketIOCallAudioTransport


class Socket:
    def __init__(self):
        self.events = []

    def emit(self, event, payload):
        self.events.append((event, payload))


@pytest.mark.parametrize("epoch", ["", "a" * 32])
@pytest.mark.parametrize("rate", [16000, 48000])
def test_pcm_and_stop_keep_the_same_owner_and_stream(epoch, rate):
    socket = Socket()
    transport = SocketIOCallAudioTransport(socket, "account")
    pcm = b"\x00\x80\xff\x7f\x00\x00"
    transport.start()
    transport.send_microphone(pcm, sample_rate=rate, media_epoch=epoch, stream_epoch=123)
    transport.stop(media_epoch=epoch, stream_epoch=123)
    assert socket.events == [
        ("call:audio:start", {"session": "account"}),
        ("call:audio:mic", {
            "session": "account", "mediaProtocol": 1, "mediaEpoch": epoch,
            "streamEpoch": 123, "sampleRate": rate, "encoding": "base64",
            "pcm": base64.b64encode(pcm).decode("ascii"),
        }),
        ("call:audio:stop", {
            "session": "account", "mediaProtocol": 1, "mediaEpoch": epoch,
            "streamEpoch": 123,
        }),
    ]


def test_transport_does_not_cache_an_owner_across_device_or_call_changes():
    socket = Socket()
    transport = SocketIOCallAudioTransport(socket, "account")
    transport.send_microphone(b"\0\0", sample_rate=48000,
                              media_epoch="a" * 32, stream_epoch=1)
    transport.send_microphone(b"\1\0", sample_rate=48000,
                              media_epoch="b" * 32, stream_epoch=2)
    transport.stop(media_epoch="b" * 32, stream_epoch=2)
    assert [payload["mediaEpoch"] for _, payload in socket.events] == ["a" * 32, "b" * 32, "b" * 32]
    assert [payload["streamEpoch"] for _, payload in socket.events] == [1, 2, 2]


def test_delivery_failure_reaches_the_existing_session_error_policy():
    class BrokenSocket:
        def emit(self, *args):
            raise OSError("disconnected")

    transport = SocketIOCallAudioTransport(BrokenSocket(), "account")
    for action in (transport.start,
                   lambda: transport.send_microphone(b"\0\0", sample_rate=48000,
                                                      media_epoch="", stream_epoch=1),
                   lambda: transport.stop(media_epoch="", stream_epoch=1)):
        with pytest.raises(OSError, match="disconnected"):
            action()
