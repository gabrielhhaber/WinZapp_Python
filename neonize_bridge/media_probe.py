"""What the bridge measures in a media file, without ffprobe.

neonize's `build_audio_message` and `build_video_message` run `ffprobe` for the
duration (and, for video, again before the thumbnail). WinZapp ships
`ffmpeg.exe` (client/lib) but not ffprobe, so every voice note and every video
failed with FileNotFoundError.

Here the duration comes from the file itself: Ogg (the voice note WinZapp
records is Ogg/Opus) by the last page's granule position, MP4/M4A/MOV (video,
stereo voice, converted attachments) by the `mvhd` box. Any other format asks
the bundled `ffmpeg -i`, whose stderr prints "Duration: ..."; when that is
missing too the message goes out with 0 seconds instead of failing. The video
thumbnail is one `ffmpeg` run, which also prints the duration.

Tested in test_media_probe.py.
"""
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

log = logging.getLogger("bridge")

OPUS_RATE = 48000                    # an Opus granule position always counts at 48 kHz
THUMBNAIL_SIDE = 200                 # the longest side of a video thumbnail, as neonize's
_FFMPEG_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


def ogg_seconds(data):
    """Duration of an Ogg Opus/Vorbis file in seconds, or None if it is not one."""
    if not data.startswith(b"OggS") or len(data) < 28:
        return None
    first_payload = 27 + data[26]
    head = data[first_payload:first_payload + 30]
    if head.startswith(b"OpusHead") and len(head) >= 12:
        rate, skip = OPUS_RATE, struct.unpack_from("<H", head, 10)[0]
    elif head.startswith(b"\x01vorbis") and len(head) >= 16:
        rate, skip = struct.unpack_from("<I", head, 12)[0], 0
    else:
        return None
    end = len(data)
    while rate > 0:
        start = data.rfind(b"OggS", 0, end)
        if start < 0 or start + 14 > len(data):
            return None
        granule = struct.unpack_from("<q", data, start + 6)[0]
        if granule >= 0:          # -1: a page where no packet ends; look further back
            return max(granule - skip, 0) / rate
        end = start
    return None


def _boxes(data, start, end):
    """(type, payload start, payload end) of the MP4 boxes between start and end."""
    while start + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, start)
        header = 8
        if size == 1 and start + 16 <= end:
            size, header = struct.unpack_from(">Q", data, start + 8)[0], 16
        elif size == 0:
            size = end - start
        if size < header:
            return
        yield kind, start + header, min(start + size, end)
        start += size


def mp4_seconds(data):
    """Duration of an MP4/M4A/MOV file in seconds (moov/mvhd), or None."""
    for kind, start, end in _boxes(data, 0, len(data)):
        if kind != b"moov":
            continue
        for inner, body, body_end in _boxes(data, start, end):
            if inner != b"mvhd" or body_end - body < 20:
                continue
            if data[body] == 1:
                if body_end - body < 32:
                    return None
                scale, length = struct.unpack_from(">IQ", data, body + 20)
            else:
                scale, length = struct.unpack_from(">II", data, body + 12)
            return length / scale if scale else None
    return None


def ffmpeg_output_seconds(stderr):
    """Seconds from the "Duration: HH:MM:SS.xx" line of `ffmpeg -i`, or None."""
    found = _FFMPEG_DURATION.search(stderr or "")
    if not found:
        return None
    hours, minutes, seconds = found.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def run_ffmpeg(file, *args):
    """`ffmpeg -hide_banner -i <file> <args>` -> (stdout bytes, stderr text), or
    None when ffmpeg is not on the PATH or did not run. `file` is a path or the
    bytes (written to a temporary file). A non-zero exit is not an error here:
    `ffmpeg -i` alone always ends that way, after printing what is wanted."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    temporary = None
    if isinstance(file, (bytes, bytearray)):
        fd, temporary = tempfile.mkstemp(suffix=".media")
        with os.fdopen(fd, "wb") as f:
            f.write(file)
        file = temporary
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run([ffmpeg, "-hide_banner", "-i", file, *args],
                                capture_output=True, timeout=60, creationflags=flags)
        return result.stdout, result.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        log.exception("ffmpeg")
        return None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _ffmpeg_seconds(data):
    output = run_ffmpeg(data)
    return None if output is None else ffmpeg_output_seconds(output[1])


def whole_seconds(seconds):
    """Rounded seconds; 0 when nothing could measure them."""
    if seconds is None:
        log.info("media duration unknown: sent with 0 seconds")
        return 0
    return int(round(seconds))


def duration_seconds(data, ffmpeg_seconds=None):
    """Whole seconds of the audio or video in `data`; 0 when nothing could measure it."""
    seconds = ogg_seconds(data)
    if seconds is None:
        seconds = mp4_seconds(data)
    if seconds is None:
        seconds = (ffmpeg_seconds or _ffmpeg_seconds)(data)
    return whole_seconds(seconds)


def thumbnail_and_seconds(file):
    """(JPEG of the first frame, seconds from ffmpeg's stderr or None). The
    thumbnail is b"" when ffmpeg is missing or found no frame."""
    side = THUMBNAIL_SIDE
    output = run_ffmpeg(
        file, "-vframes", "1", "-an",
        "-vf", f"scale='if(gt(iw,ih),{side},-2)':'if(gt(iw,ih),-2,{side})'",
        "-f", "mjpeg", "-")
    if output is None:
        return b"", None
    return output[0] or b"", ffmpeg_output_seconds(output[1])


SIGNATURES = (          # (offset, bytes, mimetype): what libmagic says for the common media
    (0, b"OggS", "audio/ogg"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"GIF8", "image/gif"),
    (0, b"RIFF", "audio/x-wav"),        # refined below: WEBP/AVI/WAVE
    (0, b"ID3", "audio/mpeg"),
    (0, b"\xff\xfb", "audio/mpeg"),
    (0, b"\xff\xf3", "audio/mpeg"),
    (0, b"\xff\xf1", "audio/aac"),
    (0, b"\xff\xf9", "audio/aac"),
    (0, b"fLaC", "audio/flac"),
    (0, b"#!AMR", "audio/amr"),
    (0, b"%PDF", "application/pdf"),
    (0, b"\x1a\x45\xdf\xa3", "video/webm"),
)


def sniff_mimetype(data):
    """The mimetype of `data` from its first bytes alone, "" when unknown. A
    stand-in for libmagic, which cannot load its database on some machines
    (issue: every voice note failed with "could not find any valid magic
    files!")."""
    head = bytes(data[:16])
    if head[4:8] == b"ftyp":              # ISO base media: M4A/MP4/MOV
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return "audio/mp4"
        return "video/quicktime" if brand == b"qt  " else "video/mp4"
    if head[:4] == b"RIFF":
        return {b"WAVE": "audio/x-wav", b"WEBP": "image/webp",
                b"AVI ": "video/x-msvideo"}.get(head[8:12], "")
    for offset, signature, mime in SIGNATURES:
        if head[offset:offset + len(signature)] == signature:
            return mime
    return ""


def mimetype(data):
    """libmagic's mimetype of `data`; the signature check when libmagic cannot
    run, so one broken database never stops a send."""
    try:
        import magic
        return magic.from_buffer(data, mime=True)
    except Exception:
        log.warning("libmagic failed; using the file signature", exc_info=True)
        return sniff_mimetype(data)


def media_type_of(data, mimetype_of=None):
    """neonize's MediaType for `data` (what its `MediaType.from_magic` answers,
    without needing libmagic): by the first part of the mimetype, a document
    when it is none of image, video or audio."""
    from neonize.utils.enum import MediaType
    kind = ((mimetype_of or mimetype)(data) or "").split("/")[0]
    return {"image": MediaType.MediaImage, "video": MediaType.MediaVideo,
            "audio": MediaType.MediaAudio}.get(kind, MediaType.MediaDocument)


def install_safe_from_magic():
    """Makes neonize's own `MediaType.from_magic` (called by its `upload`, its
    `send_image`, `send_document` and sticker code) survive a libmagic that
    cannot load: it answers from the file signature instead of raising."""
    from neonize.utils.enum import MediaType
    original = MediaType.from_magic.__func__

    def from_magic(cls, fn_or_bytes):
        try:
            return original(cls, fn_or_bytes)
        except Exception:
            log.warning("libmagic failed; using the file signature", exc_info=True)
            if isinstance(fn_or_bytes, str):
                with open(fn_or_bytes, "rb") as f:
                    fn_or_bytes = f.read(16)
            return media_type_of(fn_or_bytes, sniff_mimetype)
    MediaType.from_magic = classmethod(from_magic)


def read(file):
    """The bytes of `file`, a path or the bytes themselves."""
    if isinstance(file, (bytes, bytearray)):
        return bytes(file)
    with open(file, "rb") as f:
        return f.read()
