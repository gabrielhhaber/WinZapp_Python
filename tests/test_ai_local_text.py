"""Reading text on this computer: a PDF's text layer and Windows OCR."""
import io

import pytest

from core.ai_media import local_text
from core.ai_media.errors import DescriptionError


def text_pdf(*pages):
    """A tiny PDF whose pages each show the given text (None = no text)."""
    objects = []

    def add(body):
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids, content_ids = [], []
    for text in pages:
        stream = b"" if text is None else b"BT /F1 12 Tf 72 700 Td (" + text.encode() + b") Tj ET"
        content_ids.append(add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
        page_ids.append(None)
    pages_id = len(objects) + len(pages) + 1
    for index, content in enumerate(content_ids):
        page_ids[index] = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
            b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (pages_id, content, font))
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    assert add(b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(pages)) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objects) + 1, catalog, xref))
    return out.getvalue()


def test_pdf_with_a_text_layer_is_read_locally():
    result = local_text.pdf_native_text(text_pdf("Invoice number 12345", "Total due today"))
    assert result.usable and result.complete
    assert result.pages == 2 and result.pages_without_text == 0
    assert "Invoice number 12345" in result.text and "Total due today" in result.text


def test_a_scanned_pdf_has_no_usable_text():
    result = local_text.pdf_native_text(text_pdf(None, None))
    assert not result.usable
    assert result.pages == 2 and result.pages_without_text == 2


def test_mixed_pdf_is_usable_and_reports_the_missing_pages():
    result = local_text.pdf_native_text(text_pdf("Readable page text", None))
    assert result.usable and result.pages_without_text == 1 and result.pages == 2


def test_cancel_check_runs_between_pages():
    calls = []
    local_text.pdf_native_text(text_pdf("Page one text", "Page two text"), lambda: calls.append(1))
    assert len(calls) == 2


def test_garbage_is_a_format_error_not_a_crash():
    with pytest.raises(DescriptionError, match="ai_error_media_format"):
        local_text.pdf_native_text(b"%PDF-1.4 nothing here")


def test_ocr_refuses_cleanly_without_windows_ocr(monkeypatch):
    monkeypatch.setattr(local_text, "ocr_available", lambda: False)
    with pytest.raises(DescriptionError, match="ai_error_ocr_unavailable"):
        local_text.ocr_image(b"anything")


@pytest.mark.skipif(not local_text.ocr_available(), reason="Windows OCR is not available here")
def test_windows_ocr_reads_a_rendered_sentence():
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (700, 160), "white")
    try:
        font = ImageFont.truetype("arial.ttf", 48)
    except OSError:
        pytest.skip("no TrueType font to render the test picture")
    ImageDraw.Draw(image).text((20, 40), "Hello Winzapp 2026", fill="black", font=font)
    out = io.BytesIO()
    image.save(out, "PNG")
    try:
        text = local_text.ocr_image(out.getvalue())
    except DescriptionError as exc:
        if exc.category == "ocr_language":
            pytest.skip("no OCR language installed")
        raise
    assert "Winzapp" in text


def test_a_file_that_is_not_a_picture_is_a_format_error():
    if not local_text.ocr_available():
        pytest.skip("Windows OCR is not available here")
    with pytest.raises(DescriptionError, match="ai_error_media_format"):
        local_text.ocr_image(b"not an image")


def test_offline_ocr_kind_offers_pictures_stickers_and_videos_when_ocr_is_available(monkeypatch):
    def msg(kind, **info):
        return {"messageType": kind, "key": {"id": "m"}, "message": {kind: info}}

    monkeypatch.setattr(local_text, "ocr_available", lambda: True)
    assert local_text.offline_ocr_kind(msg("imageMessage")) == "image"
    assert local_text.offline_ocr_kind(msg("stickerMessage")) == "sticker"
    assert local_text.offline_ocr_kind(msg("videoMessage")) == "video"
    assert local_text.offline_ocr_kind(msg("imageMessage", viewOnce=True)) is None
    assert local_text.offline_ocr_kind(msg("audioMessage")) is None
    assert local_text.offline_ocr_kind(msg("documentMessage", mimetype="application/pdf")) is None

    monkeypatch.setattr(local_text, "ocr_available", lambda: False)
    assert local_text.offline_ocr_kind(msg("imageMessage")) is None
