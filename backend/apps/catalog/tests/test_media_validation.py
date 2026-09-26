import io

import pytest
from PIL import Image

from apps.catalog import media, services
from apps.core.exceptions import ValidationError

pytestmark = pytest.mark.django_db


def test_accepts_jpeg_png_webp(image_bytes):
    for fmt, content_type in (("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")):
        info = media.inspect_image(image_bytes(fmt))
        assert info.content_type == content_type
        assert (info.width, info.height) == (40, 30)


@pytest.mark.parametrize(
    "data",
    [b"", b"not an image at all", b"%PDF-1.4 fake pdf", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"],
)
def test_rejects_non_images(data):
    with pytest.raises(ValidationError):
        media.inspect_image(data)


def test_rejects_gif(image_bytes):
    with pytest.raises(ValidationError, match="Unsupported image type"):
        media.inspect_image(image_bytes("GIF"))


def test_rejects_truncated_image(image_bytes):
    data = image_bytes(size=(400, 400))
    with pytest.raises(ValidationError):
        media.inspect_image(data[: len(data) // 2])


def test_rejects_oversized_file(monkeypatch, image_bytes):
    monkeypatch.setattr(media, "MAX_BYTES", 100)
    with pytest.raises(ValidationError, match="larger than"):
        media.inspect_image(image_bytes(size=(200, 200)))


def test_rejects_huge_dimensions(monkeypatch, image_bytes):
    monkeypatch.setattr(media, "MAX_PIXELS", 100)
    with pytest.raises(ValidationError, match="dimensions"):
        media.inspect_image(image_bytes(size=(20, 20)))


def test_untrusted_upload_is_reencoded_dropping_metadata_and_appended_payload(image_bytes):
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # Make
    original = image_bytes(exif=exif.tobytes())
    polyglot = original + b"<?php system($_GET['c']); ?>"
    asset, _ = services.store_image(polyglot, "evil.jpg", trusted=False)
    stored = asset.file.read()
    assert b"<?php" not in stored
    with Image.open(io.BytesIO(stored)) as image:
        assert 0x010F not in image.getexif()
    assert asset.content_type == "image/jpeg"
