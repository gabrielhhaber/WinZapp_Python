"""Attachment media-type classification shared by paste/upload flows."""

from __future__ import annotations

import mimetypes
import os


_FALLBACK_EXTENSION_TYPES = {
    # Images
    ".jpg": "image",
    ".jpeg": "image",
    ".jfif": "image",
    ".png": "image",
    ".gif": "image",
    ".webp": "image",
    ".bmp": "image",
    ".dib": "image",
    ".tif": "image",
    ".tiff": "image",
    ".heic": "image",
    ".heif": "image",
    ".avif": "image",
    ".jxl": "image",
    # Video
    ".mp4": "video",
    ".m4v": "video",
    ".mov": "video",
    ".avi": "video",
    ".mkv": "video",
    ".webm": "video",
    ".3gp": "video",
    ".3g2": "video",
    ".mpg": "video",
    ".mpeg": "video",
    ".m2v": "video",
    ".mts": "video",
    ".m2ts": "video",
    ".wmv": "video",
    ".flv": "video",
    ".ogv": "video",
    # Audio
    ".mp3": "audio",
    ".ogg": "audio",
    ".oga": "audio",
    ".opus": "audio",
    ".wav": "audio",
    ".wave": "audio",
    ".m4a": "audio",
    ".aac": "audio",
    ".flac": "audio",
    ".wma": "audio",
    ".mka": "audio",
    ".amr": "audio",
    ".aiff": "audio",
    ".aif": "audio",
    ".ac3": "audio",
}

_IMAGE_FTYP_BRANDS = {
    b"avif", b"avis", b"heic", b"heix", b"hevc", b"hevx",
    b"heim", b"heis", b"mif1", b"msf1",
}
_AUDIO_FTYP_BRANDS = {b"M4A ", b"M4B ", b"M4P "}
_VIDEO_FTYP_PREFIXES = (b"3gp", b"3g2")
_VIDEO_FTYP_BRANDS = {
    b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"avc1",
    b"mp41", b"mp42", b"M4V ", b"qt  ", b"dash",
}


def _sniff_media_type(path: str) -> str | None:
    """Identify common media containers from their file signature."""
    try:
        with open(path, "rb") as source:
            header = source.read(4096)
    except (OSError, TypeError, ValueError):
        return None

    if header.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM")):
        return "image"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "image"
    if header.startswith(b"RIFF") and len(header) >= 12:
        riff_kind = header[8:12]
        if riff_kind == b"WEBP":
            return "image"
        if riff_kind == b"WAVE":
            return "audio"
        if riff_kind == b"AVI ":
            return "video"
    if header.startswith((b"fLaC", b"ID3", b"#!AMR\n", b"FORM")):
        return "audio"
    if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
        return "audio"
    if header.startswith(b"OggS"):
        if b"theora" in header.lower():
            return "video"
        if b"OpusHead" in header or b"vorbis" in header.lower():
            return "audio"
    if header.startswith(b"FLV"):
        return "video"

    if len(header) >= 12 and header[4:8] == b"ftyp":
        brands = {header[i:i + 4] for i in range(8, min(len(header), 64), 4)}
        if brands & _IMAGE_FTYP_BRANDS:
            return "image"
        if brands & _AUDIO_FTYP_BRANDS:
            return "audio"
        if brands & _VIDEO_FTYP_BRANDS or any(
            brand.startswith(_VIDEO_FTYP_PREFIXES) for brand in brands
        ):
            return "video"

    return None


def classify_attachment_media_type(path: str | os.PathLike[str]) -> str:
    """Return ``image``, ``video``, ``audio`` or ``document`` for *path*.

    File signatures are checked first when the file exists, so copied media
    still classifies correctly when its extension is missing or misleading.
    The platform/Python MIME database then covers registered media formats,
    followed by a broad extension fallback. Anything not positively identified
    as media is deliberately treated as a document.
    """
    path_text = os.fspath(path)

    sniffed_type = _sniff_media_type(path_text)
    if sniffed_type:
        return sniffed_type

    mime, _encoding = mimetypes.guess_type(path_text, strict=False)
    if mime:
        major_type = mime.split("/", 1)[0].lower()
        if major_type in {"image", "video", "audio"}:
            # Python classifies .3g2 as audio/3gpp2 on some platforms even
            # though WinZapp's media sender treats the container as video.
            ext = os.path.splitext(path_text)[1].lower()
            if ext == ".3g2":
                return "video"
            return major_type

    ext = os.path.splitext(path_text)[1].lower()
    return _FALLBACK_EXTENSION_TYPES.get(ext, "document")
