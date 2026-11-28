"""Reading text out of media on this computer, with no network and no key.

* ``ocr_image``: the text of a picture through Windows' own OCR
  (``Windows.Media.Ocr``), in the languages the person has installed in Windows.
* ``pdf_native_text``: the text layer of a PDF, read with pypdf.

Nothing here sends anything anywhere, so none of it needs the consent window.
Both raise ``DescriptionError`` with the window's usual keys, and never log
what they read (it is the content of a private conversation).
"""
import asyncio
import io
import sys

from core.ai_media.errors import DescriptionError

#: A PDF page counts as having a text layer when it yields at least this many
#: letters or digits; scanned pages give none (or stray marks).
MIN_PAGE_CHARS = 8

#: Pages read from one PDF: a bound on the work, never on the answer. A longer
#: document is reported as incomplete rather than silently cut.
MAX_PDF_PAGES = 400

#: Media kinds Windows OCR can read text from on this computer (a video's
#: frames are prepared as a picture before OCR runs).
OFFLINE_OCR_KINDS = frozenset({"image", "sticker", "video"})


def offline_ocr_kind(message):
    """The kind Windows OCR can read text from in ``message``, or None.

    Independent of online AI settings and keys: nothing leaves this computer.
    """
    from core.ai_media.config import eligible_kind
    kind = eligible_kind(message)
    if kind in OFFLINE_OCR_KINDS and ocr_available():
        return kind
    return None


def ocr_available() -> bool:
    """Whether Windows OCR can be reached from this process."""
    if sys.platform != "win32":
        return False
    try:
        from winrt.windows.media.ocr import OcrEngine  # noqa: F401
    except Exception:
        return False
    return True


def _png_for_ocr(data: bytes, max_dimension: int) -> bytes:
    """The picture as a PNG small enough for the engine (it refuses a side over
    ``OcrEngine.max_image_dimension``); transparency flattened on white so a
    sticker's text is not lost on black."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGBA")
                flat = Image.new("RGB", image.size, "white")
                flat.paste(image, mask=image.split()[-1])
                image = flat
            else:
                image = image.convert("RGB")
            if max(image.size) > max_dimension:
                scale = max_dimension / max(image.size)
                image = image.resize((max(1, int(image.width * scale)),
                                      max(1, int(image.height * scale))))
            out = io.BytesIO()
            image.save(out, format="PNG")
            return out.getvalue()
    except Exception:
        raise DescriptionError("media_format") from None


async def _recognize(png: bytes) -> str:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise DescriptionError("ocr_language")
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    lines = [line.text for line in result.lines]
    return "\n".join(line for line in lines if line.strip())


def ocr_image(data: bytes) -> str:
    """The text Windows OCR reads in the picture, one line per line of text.

    "" when the picture has no readable text. Blocks (call it from a worker).
    Raises ``DescriptionError``: ``ocr_unavailable`` without Windows OCR,
    ``ocr_language`` when no OCR language is installed, ``media_format`` for a
    file that is not a picture.
    """
    if not ocr_available():
        raise DescriptionError("ocr_unavailable")
    try:
        from winrt.windows.media.ocr import OcrEngine
        limit = int(OcrEngine.max_image_dimension)
    except Exception:
        limit = 4096
    png = _png_for_ocr(data, limit)
    try:
        return asyncio.run(_recognize(png)).strip()
    except DescriptionError:
        raise
    except Exception:
        raise DescriptionError("ocr_unavailable") from None


class PdfText:
    """What the text layer of a PDF held."""

    def __init__(self, text: str, pages: int, pages_without_text: int, complete: bool):
        self.text = text
        self.pages = pages
        self.pages_without_text = pages_without_text
        #: False when the document had more pages than were read.
        self.complete = complete

    @property
    def usable(self) -> bool:
        """Some page has real text. A mix of text and scanned pages is still
        usable, and ``pages_without_text`` tells the person what is missing."""
        return bool(self.text.strip()) and self.pages_without_text < self.pages


def _letters(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


def pdf_native_text(data: bytes, cancel_check=None) -> PdfText:
    """The text layer of a PDF, page by page, in reading order as the file
    states it. An encrypted or damaged file raises ``media_format``;
    ``cancel_check`` is called between pages."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DescriptionError("media_format")
        total = len(reader.pages)
        parts, blank = [], 0
        for index in range(min(total, MAX_PDF_PAGES)):
            if cancel_check is not None:
                cancel_check()
            page_text = (reader.pages[index].extract_text() or "").strip()
            if _letters(page_text) >= MIN_PAGE_CHARS:
                parts.append(page_text)
            else:
                blank += 1
        read = min(total, MAX_PDF_PAGES)
        return PdfText("\n\n".join(parts), read, blank, total <= MAX_PDF_PAGES)
    except DescriptionError:
        raise
    except Exception:
        raise DescriptionError("media_format") from None
