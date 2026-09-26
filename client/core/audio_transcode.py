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


def encode_system_audio_to_m4a(ffmpeg: str, source_wav: str) -> str | None:
    """Encode mixed capture as stereo 48 kHz AAC-LC, independently of voice PTT.

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
             "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "192k",
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


def prepare_audio_for_whatsapp(ffmpeg: str, source_path: str) -> tuple[str, str] | None:
    """Return a WhatsApp-compatible audio path and MIME type.

    WhatsApp accepts OGG audio through sendFile only when its stream is Opus,
    while WAV reaches its media worker but is rejected after upload. Generic
    ``.ogg`` files may also contain Vorbis, which WPPConnect rejects with
    ``InvalidMediaCheckRepairFailedType``. Convert only those incompatible
    inputs; formats already handled by WhatsApp (MP3, M4A, FLAC, etc.) keep
    passing through unchanged.
    """
    mime = mimetypes.guess_type(source_path)[0] or "application/octet-stream"
    extension = os.path.splitext(source_path)[1].lower()
    if extension == ".m4a":
        # Windows registry associations may call this audio/x-m4a. WhatsApp's
        # ordinary-audio upload contract uses audio/mp4; keep the AAC untouched.
        return source_path, "audio/mp4"
    if extension not in {".ogg", ".wav", ".wave"}:
        return source_path, mime

    if extension == ".ogg":
        try:
            with open(source_path, "rb") as source:
                header = source.read(4096)
        except OSError:
            return None
        if b"OpusHead" in header:
            return source_path, "audio/ogg; codecs=opus"
    if not ffmpeg or not os.path.isfile(ffmpeg):
        return None

    try:
        output_fd, output_path = tempfile.mkstemp(
            prefix=os.path.basename(source_path) + ".",
            suffix=".whatsapp.opus.ogg",
        )
        os.close(output_fd)
    except OSError:
        logging.exception("[send_media] could not create temporary Opus file")
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
        "[send_media] converting %s audio to OGG/Opus with %s (temporary output: %s)",
        extension or "unknown",
        ffmpeg,
        output_path,
    )
    try:
        result = subprocess.run(
            [
                ffmpeg, "-y", "-i", source_path, "-vn", "-ac", "1",
                "-c:a", "libopus", "-b:a", "64k", output_path,
            ],
            capture_output=True,
            timeout=timeout,
            creationflags=creationflags,
        )
        if (
            result.returncode == 0
            and os.path.isfile(output_path)
            and os.path.getsize(output_path) > 0
        ):
            return output_path, "audio/ogg; codecs=opus"
        logging.error(
            "[send_media] audio conversion to OGG/Opus failed (rc=%s): %s",
            result.returncode,
            (result.stderr or b"").decode("utf-8", errors="replace")[-800:],
        )
    except Exception:
        logging.exception("[send_media] audio conversion to OGG/Opus failed")
    try:
        os.unlink(output_path)
    except OSError:
        pass
    return None
