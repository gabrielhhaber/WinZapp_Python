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
