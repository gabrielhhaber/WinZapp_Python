"""Stereo voice messages (issue #82).

There is one record button and no mode: what the input device delivers decides.
A device that opens with two channels sends a stereo voice message, anything
else a mono one. The two channels are never compared: an input carrying a mix
of mono and stereo sources (a virtual device such as BlackHole) is exactly the
case a comparison would get wrong. A device with more channels is opened with
its first two.

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


def opus_encode_args() -> list:
    """The ffmpeg arguments of a mono voice message: channel count, codec, bitrate."""
    return ["-ac", "1", "-c:a", "libopus", "-b:a", MONO_BITRATE]


def voice_data_uri(audio_b64: str, mimetype: str = "") -> str:
    """The base64Ptt value of /send-voice-base64."""
    return f"data:{mimetype or OPUS_MIMETYPE};base64,{audio_b64}"
