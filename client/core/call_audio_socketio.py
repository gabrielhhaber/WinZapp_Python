"""Decode Socket.IO call envelopes before delivering transport-independent PCM."""
import base64

from core.call_audio_transport import CallAudioFrame
from core.call_media_ownership import valid_media_epoch


def socketio_binary_to_bytes(value):
    """Normalize Node Buffer, base64 and Socket.IO binary values into bytes."""
    if isinstance(value, str):
        try:
            return base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, dict) and value.get("type") == "Buffer":
        value = value.get("data")
    if isinstance(value, list):
        try:
            return bytes(value)
        except (TypeError, ValueError):
            return b""
    return b""


def decode_remote_audio(data) -> CallAudioFrame | None:
    """Keep wire decoding separate from session selection and ownership checks.

    The event handler checks account/owner before decoding. The eventual
    PCM sink checks ownership again, including after a call/device change.
    A malformed explicit owner is rejected rather than turned into legacy
    audio that an unbound sink would accept.
    """
    if not isinstance(data, dict):
        return None
    epoch = data.get("mediaEpoch")
    if "mediaEpoch" in data and not valid_media_epoch(epoch):
        return None
    pcm = socketio_binary_to_bytes(data.get("pcm"))
    if not pcm:
        return None
    try:
        sample_rate = int(data.get("sampleRate") or 48000)
    except (TypeError, ValueError):
        sample_rate = 48000
    return CallAudioFrame(pcm, sample_rate, epoch)
