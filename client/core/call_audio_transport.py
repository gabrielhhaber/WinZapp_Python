"""Call PCM delivery independent of the capture/playback implementation.

Socket.IO remains the default for both engines. A future native Neonize
transport can accept PCM directly without making the device pipeline encode
JSON. Media ownership and device-switch policy stay with CallAudioSession.
"""
import base64
from dataclasses import dataclass
from typing import Protocol

from core.call_media_ownership import media_fields


class CallAudioReceiver(Protocol):
    """PCM sink; it must recheck media ownership and whether it is stopped."""

    def __call__(self, pcm: bytes, sample_rate: int, *, media_epoch=None) -> None: ...


@dataclass(frozen=True)
class CallAudioFrame:
    """Transport-independent mono s16le PCM, with its original ownership tag.

    None denotes untagged legacy audio. An explicit invalid tag must never
    become legacy audio, so adapters must reject it before constructing this
    frame. Delivery does not bind a call or acquire playback devices.
    """

    pcm: bytes
    sample_rate: int
    media_epoch: str | None = None

    def deliver_to(self, receiver: CallAudioReceiver) -> None:
        if self.media_epoch is None:
            receiver(self.pcm, self.sample_rate)
        else:
            receiver(self.pcm, self.sample_rate, media_epoch=self.media_epoch)


class CallAudioTransport(Protocol):
    """Outgoing PCM and bridge controls; methods may raise on delivery failure.

    These run on the sender/control threads, never a PortAudio callback.
    Incoming PCM uses CallAudioFrame.deliver_to with a CallAudioReceiver,
    such as CallAudioSession.enqueue_remote_audio. The receiver rechecks the
    frame's original owner even when the transport already checked it.
    """

    def start(self) -> None: ...

    def send_microphone(self, pcm: bytes, *, sample_rate: int,
                        media_epoch: str, stream_epoch: int) -> None: ...

    def stop(self, *, media_epoch: str, stream_epoch: int) -> None: ...


class SocketIOCallAudioTransport:
    """Keep the existing WPPConnect/Neonize wire envelopes byte-compatible."""

    def __init__(self, sio, session: str):
        self._sio = sio
        self._session = session

    def start(self) -> None:
        self._sio.emit("call:audio:start", {"session": self._session})

    def send_microphone(self, pcm: bytes, *, sample_rate: int,
                        media_epoch: str, stream_epoch: int) -> None:
        # JSON-safe on polling and WebSocket alike: raw binary can arrive in
        # Node as an ArrayBuffer/placeholder rather than PCM.
        self._sio.emit("call:audio:mic", {
            "session": self._session,
            **media_fields(media_epoch, stream_epoch),
            "sampleRate": sample_rate,
            "encoding": "base64",
            "pcm": base64.b64encode(pcm).decode("ascii"),
        })

    def stop(self, *, media_epoch: str, stream_epoch: int) -> None:
        self._sio.emit("call:audio:stop", {
            "session": self._session,
            **media_fields(media_epoch, stream_epoch),
        })
