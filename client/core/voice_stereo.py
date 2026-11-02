"""Stereo voice messages (issue #82).

There is one record button and no mode: what the input device delivers decides.
A device that opens with two channels sends a stereo voice message, anything
else a mono one. The two channels are never compared for likeness: an input
carrying a mix of mono and stereo sources (a virtual device such as BlackHole)
is exactly the case that comparison would get wrong. A device with more channels
is opened with its first two. The one exception is a side that carries nothing
at all, see drop_empty_side().

WhatsApp on iPhone cannot play a stereo OGG/Opus message, voice message or
file, at any bitrate, but it does play stereo AAC-LC M4A, also as a voice
message. So a stereo recording is AAC and a mono one OGG/Opus, both through
/send-voice-base64 as push-to-talk.
"""

#: Opus rate of a mono voice message.
MONO_BITRATE = "256k"
#: AAC rate of a stereo voice message. The encoder treats it as a ceiling, and
#: speech usually lands below it.
STEREO_BITRATE = "256k"
#: Sent in place of Opus's mimetype for a stereo voice message.
STEREO_MIMETYPE = "audio/mp4"
OPUS_MIMETYPE = "audio/ogg;codecs=opus"
#: How far below the other a side must sit to count as empty: 30 dB, in power.
#: Two sides of one voice never sit that far apart.
EMPTY_SIDE_POWER_RATIO = 1000
#: A level needs no more than every 17th frame, which keeps a long recording
#: cheap: the paused preview calls drop_empty_side() on the UI thread.
LEVEL_FRAME_STEP = 17


def recording_configs_preferring(configs, stereo: bool) -> list:
    """*configs* ((rate, channels) pairs, in order) with two channels first when
    *stereo* is wanted, keeping the order within each group."""
    configs = list(configs or ())
    if not stereo:
        return configs
    return ([c for c in configs if c[1] >= 2]
            + [c for c in configs if c[1] < 2])


def encode_as_stereo(captured_channels) -> bool:
    """Whether the recording goes out in stereo: the device delivered it."""
    try:
        return int(captured_channels or 0) >= 2
    except (TypeError, ValueError):
        return False


def drop_empty_side(pcm: bytes, channels) -> tuple:
    """(*pcm*, *channels*) without a side that carries nothing (issue #679).

    A two-input audio interface opens with two channels whatever is plugged
    into it, so a microphone on input 1 arrives on the left alone and would go
    out with a silent right. Such a recording becomes mono, taken from the live
    side only: mixing in the empty one would halve its level.

    Recorded sound on both sides stays stereo, however alike, and so does a
    recording with no signal anywhere, which says nothing about either side.
    """
    if channels != 2 or len(pcm) < 4:
        return pcm, channels
    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2", count=len(pcm) // 4 * 2)
    stride = 2 * LEVEL_FRAME_STEP
    # Variance, not raw power: a dead input can still sit at a DC offset.
    left, right = samples[0::stride].var(), samples[1::stride].var()
    if max(left, right) <= 0 or min(left, right) * EMPTY_SIDE_POWER_RATIO > max(left, right):
        return pcm, channels
    return samples[0 if left > right else 1::2].tobytes(), 1


def opus_encode_args() -> list:
    """The ffmpeg arguments of a mono voice message: channel count, codec, bitrate."""
    return ["-ac", "1", "-c:a", "libopus", "-b:a", MONO_BITRATE]


def voice_data_uri(audio_b64: str, mimetype: str = "") -> str:
    """The base64Ptt value of /send-voice-base64."""
    return f"data:{mimetype or OPUS_MIMETYPE};base64,{audio_b64}"
