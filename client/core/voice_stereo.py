"""Stereo voice messages (issue #82).

There is one record button. In automatic mode the input device decides;
explicit mono is mixed by core.voice_recording_mode before encoding.
A device that opens with two channels otherwise sends a stereo voice message,
anything else a mono one. The channels are never compared for likeness: an input
carrying a mix of mono and stereo sources (a virtual device such as BlackHole)
is exactly the case that comparison would get wrong. A device with more channels
is opened with its first two. The one exception, in automatic mode only, is a
side that carries nothing at all, see drop_empty_side() (#679).

WhatsApp on iPhone cannot play a stereo OGG/Opus message, voice message or
file, at any bitrate, but it does play stereo AAC-LC M4A, also as a voice
message. So a stereo recording is AAC and a mono one OGG/Opus, both through
/send-voice-base64 as push-to-talk.
"""

#: Rates the user can pick for a recorded voice message, in kbit/s. 64 is the
#: mono default: a minute of speech stays small, so it uploads quickly. 256
#: made recordings of over a minute slow to send. A stereo (AAC) recording has
#: its own setting, default 128: at 64 it sounded poor.
VOICE_BITRATES_KBPS = (32, 48, 64, 96, 128, 192, 256)
DEFAULT_VOICE_BITRATE_KBPS = 64
DEFAULT_STEREO_VOICE_BITRATE_KBPS = 128
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


def voice_bitrate_kbps(settings) -> int:
    """The recorded-voice-message bitrate chosen in *settings*, else the default."""
    general = settings.get("general", {}) if isinstance(settings, dict) else {}
    value = general.get("voice_recording_bitrate") if isinstance(general, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and value in VOICE_BITRATES_KBPS:
        return value
    return DEFAULT_VOICE_BITRATE_KBPS


def stereo_voice_bitrate_kbps(settings) -> int:
    """The stereo (AAC) voice-message bitrate chosen in *settings*, else 128."""
    general = settings.get("general", {}) if isinstance(settings, dict) else {}
    value = general.get("voice_recording_bitrate_stereo") if isinstance(general, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and value in VOICE_BITRATES_KBPS:
        return value
    return DEFAULT_STEREO_VOICE_BITRATE_KBPS


def migrate_stereo_voice_bitrate(settings) -> bool:
    """Give an existing install its stereo bitrate: 128, or the rate it chose.

    Until now one setting (voice_recording_bitrate) served both mono and stereo,
    and its default 64 made stereo AAC sound poor. An install still on 64 (the
    default, indistinguishable from a deliberate 64) moves to 128 for stereo;
    any other rate it chose is kept. Mono keeps the old setting. Idempotent:
    it only acts while the stereo key is absent and the old one present. Returns True if *settings*
    changed.
    """
    if not isinstance(settings, dict):
        return False
    general = settings.get("general")
    if (not isinstance(general, dict) or "voice_recording_bitrate_stereo" in general
            or "voice_recording_bitrate" not in general):
        return False  # no old setting: the backfill supplies the 128 default
    old = general.get("voice_recording_bitrate")
    chosen = old if (isinstance(old, int) and not isinstance(old, bool)
                     and old in VOICE_BITRATES_KBPS and old != DEFAULT_VOICE_BITRATE_KBPS) \
        else DEFAULT_STEREO_VOICE_BITRATE_KBPS
    general["voice_recording_bitrate_stereo"] = chosen
    return True


def bitrate_arg(kbps: int) -> str:
    """The ffmpeg ``-b:a`` value of *kbps*."""
    return f"{kbps}k"


def opus_encode_args(kbps: int = DEFAULT_VOICE_BITRATE_KBPS) -> list:
    """The ffmpeg arguments of a mono voice message: channel count, codec, bitrate."""
    return ["-ac", "1", "-c:a", "libopus", "-b:a", bitrate_arg(kbps)]


def voice_data_uri(audio_b64: str, mimetype: str = "") -> str:
    """The base64Ptt value of /send-voice-base64."""
    return f"data:{mimetype or OPUS_MIMETYPE};base64,{audio_b64}"
