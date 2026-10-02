"""Decode only bounded still images; orient, re-encode and strip all metadata."""
from dataclasses import dataclass
from io import BytesIO
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from .config import MAX_PIXELS, MAX_SOURCE_BYTES
from .errors import DescriptionError


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    mime: str
    width: int
    height: int


def prepare_image(data, profile="balanced"):
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise DescriptionError("image_size")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format not in ("JPEG", "PNG", "WEBP") or getattr(source, "n_frames", 1) != 1:
                    raise DescriptionError("image_format")
                if source.width * source.height > MAX_PIXELS:
                    raise DescriptionError("image_size")
                source.verify()
            with Image.open(BytesIO(data)) as source:
                oriented = ImageOps.exif_transpose(source)
                oriented.thumbnail((2048, 2048) if profile == "fast" else (3072, 3072))
                # Composite transparency onto white instead of turning it black.
                rgba = oriented.convert("RGBA")
                result = Image.new("RGB", rgba.size, "white")
                result.paste(rgba, mask=rgba.getchannel("A"))
                output = BytesIO()
                result.save(output, format="JPEG", quality=92, subsampling=0)
                encoded = output.getvalue()
                if len(encoded) > 8 * 1024 * 1024:
                    raise DescriptionError("image_size")
                return ImageInput(encoded, "image/jpeg", *result.size)
    except DescriptionError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise DescriptionError("image_size") from None
    except (OSError, ValueError, UnidentifiedImageError):
        raise DescriptionError("image_format") from None
