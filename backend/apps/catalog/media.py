"""Image validation and normalisation for product media.

Only real JPEG/PNG/WebP images within size limits are accepted. Images from
untrusted sources (staff uploads, future customer uploads) are re-encoded,
which drops metadata (e.g. phone GPS location) and anything appended to the
file, such as polyglot payloads.
"""

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from apps.core.exceptions import ValidationError

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000
FORMAT_CONTENT_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


@dataclass(frozen=True)
class ImageInfo:
    content_type: str
    width: int
    height: int
    pil_format: str


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inspect_image(data: bytes) -> ImageInfo:
    """Validate that `data` is an acceptable image; raise ValidationError otherwise."""
    if not data:
        raise ValidationError("Empty file.")
    if len(data) > MAX_BYTES:
        raise ValidationError(f"Image is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            pil_format = image.format
            width, height = image.size
            if pil_format not in FORMAT_CONTENT_TYPES:
                raise ValidationError(f"Unsupported image type {pil_format!r}; use JPEG, PNG or WebP.")
            if width * height > MAX_PIXELS:
                raise ValidationError("Image dimensions are too large.")
            image.verify()  # structural check without decoding all pixels
        # verify() leaves the image unusable; decode fully once to catch truncated data.
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValidationError("File is not a valid image.") from exc
    return ImageInfo(FORMAT_CONTENT_TYPES[pil_format], width, height, pil_format)


def reencode(data: bytes, info: ImageInfo) -> bytes:
    """Re-encode an already-validated image in the same format, without metadata."""
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if info.pil_format == "JPEG" and image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        out = io.BytesIO()
        options = {"JPEG": {"quality": 90, "optimize": True}, "PNG": {"optimize": True}, "WEBP": {"quality": 90}}
        image.save(out, format=info.pil_format, **options[info.pil_format])
        return out.getvalue()
