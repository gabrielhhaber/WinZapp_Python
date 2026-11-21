import pytest

from core.attachment_types import classify_attachment_media_type


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("photo.jpg", "image"),
        ("photo.heic", "image"),
        ("photo.heif", "image"),
        ("photo.avif", "image"),
        ("photo.bmp", "image"),
        ("photo.tiff", "image"),
        ("movie.mp4", "video"),
        ("movie.webm", "video"),
        ("movie.m4v", "video"),
        ("movie.3g2", "video"),
        ("voice.mp3", "audio"),
        ("voice.opus", "audio"),
        ("voice.oga", "audio"),
        ("voice.mka", "audio"),
        ("archive.zip", "document"),
        ("report.pdf", "document"),
        ("program.exe", "document"),
        ("README", "document"),
    ],
)
def test_classifies_modern_media_and_falls_back_to_document(filename, expected):
    assert classify_attachment_media_type(filename) == expected


def test_signature_detects_image_without_extension(tmp_path):
    photo = tmp_path / "clipboard-file"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    assert classify_attachment_media_type(photo) == "image"


def test_signature_overrides_misleading_document_extension(tmp_path):
    photo = tmp_path / "actually-a-photo.txt"
    photo.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    assert classify_attachment_media_type(photo) == "image"


def test_signature_distinguishes_riff_media(tmp_path):
    wav = tmp_path / "unknown.bin"
    wav.write_bytes(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 32)
    assert classify_attachment_media_type(wav) == "audio"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le"])
def test_utf16_le_text_with_bom_is_a_document_not_audio(tmp_path, encoding):
    # The UTF-16LE BOM (FF FE) is also an MPEG audio sync word (issue #838).
    report = tmp_path / "msinfo.txt"
    data = "System Information report\r\nOS Name: Windows\r\n".encode(encoding)
    if encoding == "utf-16-le":
        data = b"\xff\xfe" + data
    report.write_bytes(data)
    assert data.startswith(b"\xff\xfe")
    assert classify_attachment_media_type(report) == "document"


def test_utf16_text_longer_than_the_sniffed_header_is_a_document(tmp_path):
    # 😀 is a surrogate pair; the 4096-byte read can cut one in half.
    report = tmp_path / "long.txt"
    report.write_bytes(("a" * 2046 + "😀" * 10).encode("utf-16"))
    assert classify_attachment_media_type(report) == "document"


def test_pasted_utf16_text_stays_a_document_with_the_default_audio_mode(tmp_path):
    from core.attachment_types import pasted_attachment_media_type

    report = tmp_path / "msinfo.txt"
    report.write_bytes("Hello world".encode("utf-16"))
    assert pasted_attachment_media_type(report, "audio") == "document"


def test_real_mpeg_audio_frame_is_still_audio(tmp_path):
    mp3 = tmp_path / "frame.bin"
    mp3.write_bytes(b"\xff\xfb\x90\x00" + bytes(range(1, 200)) * 4)
    assert classify_attachment_media_type(mp3) == "audio"
