"""Audio format conversion helpers used by the in-app player."""

import logging
import mimetypes
import os
import subprocess
import sys
import tempfile


def transcode_audio_to_wav(ffmpeg: str, source_path: str) -> str | None:
    """Convert any ffmpeg-readable audio stream to PCM WAV for reliable BASS playback.

    Originally MP4/M4A-only (hence the old name), this is also the fallback for
    any other container BASS can't open directly — notably an OGG whose codec
    isn't Opus (or whose bassopus.dll plugin failed to register), which BASS
    rejects with error 41 "unsupported file format" for both the decode+Tempo
    stream and the plain direct stream. Re-encoding through ffmpeg sidesteps
    BASS's codec support entirely rather than depending on it.
    """
    if not ffmpeg or not os.path.isfile(ffmpeg):
        return None

    output_path = source_path + ".wav"
    creationflags = 0
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(
            [
                ffmpeg, "-y", "-i", source_path, "-vn",
                "-c:a", "pcm_s16le", output_path,
            ],
            capture_output=True,
            timeout=60,
            creationflags=creationflags,
        )
        if (
            result.returncode == 0
            and os.path.isfile(output_path)
            and os.path.getsize(output_path) > 44
        ):
            return output_path
        logging.error(
            "[UI Audio Playback] audio→WAV conversion failed (rc=%s): %s",
            result.returncode,
            (result.stderr or b"").decode("utf-8", errors="replace")[-800:],
        )
    except Exception:
        logging.exception("[UI Audio Playback] audio→WAV conversion failed")

    try:
        os.unlink(output_path)
    except OSError:
        pass
    return None


def encode_system_audio_to_m4a(ffmpeg: str, source_wav: str,
                               bitrate: str = "192k") -> str | None:
    """Encode a stereo capture as 48 kHz AAC-LC, independently of voice PTT.

    Used by microphone + computer audio and by a stereo voice message: iPhone
    plays this, but not stereo OGG/Opus.

    The caller owns the returned temporary M4A until delivery/cancellation.
    Never fall back to the mono Opus attachment/voice conversion on failure.
    """
    if not ffmpeg or not os.path.isfile(ffmpeg):
        return None
    output_path = None
    try:
        output_fd, output_path = tempfile.mkstemp(prefix="winzapp-mixed-", suffix=".m4a")
        os.close(output_fd)
        creationflags = 0
        if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        timeout = max(120, min(1800, os.path.getsize(source_wav) // (512 * 1024)))
        result = subprocess.run(
            [ffmpeg, "-y", "-i", source_wav, "-vn", "-ac", "2", "-ar", "48000",
             "-c:a", "aac", "-profile:a", "aac_low", "-b:a", bitrate,
             "-movflags", "+faststart", output_path],
            capture_output=True, timeout=timeout, creationflags=creationflags,
        )
        if (result.returncode == 0 and os.path.isfile(output_path)
                and os.path.getsize(output_path) > 0):
            return output_path
        logging.error(
            "[mixed_audio] AAC conversion failed (rc=%s): %s",
            result.returncode,
            (result.stderr or b"").decode("utf-8", errors="replace")[-800:],
        )
    except Exception:
        logging.exception("[mixed_audio] AAC conversion failed")
    if output_path:
        try:
            os.unlink(output_path)
        except OSError:
            pass
    return None


# AAC-LC's own sample-rate table (ISO/IEC 14496-3; ffmpeg's encoder refuses
# anything else), stopping at 48 kHz: Android only has to decode AAC-LC up to
# 48 kHz, so a 96 kHz file might not play on the recipient's phone.
_AAC_SAMPLE_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100,
                     48000)
# Per channel, so stereo lands on the 192k encode_system_audio_to_m4a already
# uses and 5.1 on 576k — never below the old 64k mono Opus for any channel.
_AAC_BITRATE_PER_CHANNEL_K = 96
# When the header cannot be read, budget for 7.1 rather than guess low: the
# encoder clamps an excessive request to its own per-channel ceiling, so the
# only cost of over-asking is a larger file, never a worse one.
_AAC_FALLBACK_CHANNELS = 8
# AAC-LC's channel ceiling (7.1); ffmpeg's encoder exits 234 beyond it.
_AAC_MAX_CHANNELS = 8
# What prepare_audio_for_whatsapp() may have to convert, once it reads the header.
_REENCODED_EXTENSIONS = {".ogg", ".opus", ".wav", ".wave"}


def read_audio_header_format(header: bytes) -> tuple[int, int] | None:
    """Return ``(channels, sample_rate)`` from a WAV, Ogg Vorbis or Ogg Opus
    header.

    Reads only the bytes already in memory: the bundled ffmpeg ships without
    ffprobe, and these are the only formats prepare_audio_for_whatsapp() ever
    re-encodes.
    """
    # BW64 (ITU-R BS.2088) is the broadcast WAV of ADM/Atmos beds, the usual
    # source of a .wav with more than AAC's 8 channels.
    if len(header) >= 12 and header[:4] in (b"RIFF", b"RF64", b"BW64") and header[8:12] == b"WAVE":
        # Walk the chunks: a broadcast WAV puts bext/JUNK/ds64 before fmt.
        offset = 12
        while offset + 8 <= len(header):
            chunk_id = header[offset:offset + 4]
            chunk_size = int.from_bytes(header[offset + 4:offset + 8], "little")
            if chunk_id == b"fmt ":
                body = header[offset + 8:offset + 16]
                if len(body) < 8:
                    return None
                channels = int.from_bytes(body[2:4], "little")
                sample_rate = int.from_bytes(body[4:8], "little")
                if channels and sample_rate:
                    return channels, sample_rate
                return None
            offset += 8 + chunk_size + (chunk_size & 1)
        return None
    vorbis = header.find(b"\x01vorbis")
    if vorbis >= 0 and len(header) >= vorbis + 16:
        # Identification header: 4-byte version, then channels and rate.
        channels = header[vorbis + 11]
        sample_rate = int.from_bytes(header[vorbis + 12:vorbis + 16], "little")
        if channels and sample_rate:
            return channels, sample_rate
    opus = header.find(b"OpusHead")
    if opus >= 0 and len(header) >= opus + 16:
        # 8-byte magic, version, channels, pre-skip, then the input rate, which
        # an encoder may leave at 0: Opus itself always decodes at 48 kHz.
        channels = header[opus + 9]
        sample_rate = int.from_bytes(header[opus + 12:opus + 16], "little") or 48000
        if channels:
            return channels, sample_rate
    return None


def _is_mono_opus(header: bytes) -> bool:
    """Opus WhatsApp plays everywhere, sent as it is. iPhone cannot play Opus
    with more channels (core/voice_stereo.py); a header that cannot be read is
    left alone too, as it always was."""
    if b"OpusHead" not in header:
        return False
    source_format = read_audio_header_format(header)
    return source_format is None or source_format[0] <= 1


def exceeds_aac_channel_limit(source_path: str) -> bool:
    """True for a WAV/Vorbis attachment with more channels than AAC can carry.

    prepare_audio_for_whatsapp() would have to downmix such a file, and the
    rule is that an attachment never loses a channel, so the caller sends the
    original untouched as a document instead.
    """
    extension = os.path.splitext(source_path)[1].lower()
    if extension not in _REENCODED_EXTENSIONS:
        return False
    try:
        with open(source_path, "rb") as source:
            header = source.read(65536)
    except OSError:
        return False
    if _is_mono_opus(header):
        return False
    source_format = read_audio_header_format(header)
    return bool(source_format and source_format[0] > _AAC_MAX_CHANNELS)


def aac_encode_args(channels: int | None, sample_rate: int | None) -> list[str]:
    """ffmpeg output arguments for an ordinary-audio AAC-LC attachment.

    There is deliberately no ``-ac``: the source's channel count and layout
    survive (5.1 stays 5.1). The sample rate is kept when AAC supports it and
    otherwise raised to the next rate it does (ffmpeg's own negotiation would
    pick the nearest one *below*); only rates above 48 kHz go down, to 48 kHz,
    the highest rate every phone has to decode.
    """
    bitrate_channels = channels or _AAC_FALLBACK_CHANNELS
    args = ["-c:a", "aac", "-profile:a", "aac_low",
            "-b:a", f"{_AAC_BITRATE_PER_CHANNEL_K * bitrate_channels}k"]
    if sample_rate:
        target = next(
            (rate for rate in _AAC_SAMPLE_RATES if rate >= sample_rate),
            _AAC_SAMPLE_RATES[-1],
        )
        args += ["-ar", str(target)]
    return args + ["-movflags", "+faststart"]


def prepare_audio_for_whatsapp(ffmpeg: str, source_path: str) -> tuple[str, str] | None:
    """Return a WhatsApp-compatible audio path and MIME type.

    WhatsApp accepts OGG audio through sendFile only when its stream is Opus,
    while WAV reaches its media worker but is rejected after upload. Generic
    ``.ogg`` files may also contain Vorbis, which WPPConnect rejects with
    ``InvalidMediaCheckRepairFailedType``, and stereo Opus does not play on
    iPhone. Convert only those incompatible inputs; formats already handled by WhatsApp (MP3, M4A, FLAC, etc.) keep
    passing through unchanged.

    The conversion target is AAC-LC in M4A, keeping every channel. It used to
    be ``-ac 1`` 64k OGG/Opus — the voice-message format — so a 5.1 WAV picked
    through Attach arrived as a mono, voice-note-like clip. Voice recordings
    never come through here (they use /send-voice-base64).
    """
    mime = mimetypes.guess_type(source_path)[0] or "application/octet-stream"
    extension = os.path.splitext(source_path)[1].lower()
    if extension == ".m4a":
        # Windows registry associations may call this audio/x-m4a. WhatsApp's
        # ordinary-audio upload contract uses audio/mp4; keep the AAC untouched.
        return source_path, "audio/mp4"
    if extension not in _REENCODED_EXTENSIONS:
        return source_path, mime

    try:
        with open(source_path, "rb") as source:
            header = source.read(65536)
    except OSError:
        return None
    if _is_mono_opus(header):
        return source_path, "audio/ogg; codecs=opus"
    if not ffmpeg or not os.path.isfile(ffmpeg):
        return None
    source_format = read_audio_header_format(header)
    channels, sample_rate = source_format or (None, None)

    try:
        output_fd, output_path = tempfile.mkstemp(
            prefix=os.path.basename(source_path) + ".",
            suffix=".whatsapp.m4a",
        )
        os.close(output_fd)
    except OSError:
        logging.exception("[send_media] could not create temporary M4A file")
        return None

    creationflags = 0
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        source_size = os.path.getsize(source_path)
    except OSError:
        source_size = 0
    timeout = max(120, min(1800, source_size // (512 * 1024)))
    logging.info(
        "[send_media] converting %s audio to AAC/M4A with %s "
        "(channels=%s, rate=%s; temporary output: %s)",
        extension or "unknown",
        ffmpeg,
        channels or "unknown",
        sample_rate or "unknown",
        output_path,
    )
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-i", source_path, "-vn"]
            + aac_encode_args(channels, sample_rate)
            + [output_path],
            capture_output=True,
            timeout=timeout,
            creationflags=creationflags,
        )
        if (
            result.returncode == 0
            and os.path.isfile(output_path)
            and os.path.getsize(output_path) > 0
        ):
            return output_path, "audio/mp4"
        logging.error(
            "[send_media] audio conversion to AAC/M4A failed (rc=%s): %s",
            result.returncode,
            (result.stderr or b"").decode("utf-8", errors="replace")[-800:],
        )
    except Exception:
        logging.exception("[send_media] audio conversion to AAC/M4A failed")
    try:
        os.unlink(output_path)
    except OSError:
        pass
    return None
