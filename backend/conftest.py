import io

import pytest
from django.core.cache import cache
from PIL import Image

from apps.core.actors import Actor


@pytest.fixture(autouse=True)
def _clear_cache():
    """Throttle counters live in the cache; isolate tests from each other."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _media_root(settings, tmp_path):
    """Uploaded files go to a per-test temporary directory."""
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def system_actor():
    return Actor.system("test")


@pytest.fixture
def staff_user(django_user_model):
    return django_user_model.objects.create_user(
        email="staff@dewmix.example", password="pw-for-tests-123", is_staff=True
    )


@pytest.fixture
def superuser(django_user_model):
    return django_user_model.objects.create_superuser(email="owner@dewmix.example", password="pw-for-tests-123")


def make_image_bytes(fmt="JPEG", size=(40, 30), color=(200, 30, 30), **save_kwargs) -> bytes:
    image = Image.new("RGB", size, color)
    out = io.BytesIO()
    image.save(out, format=fmt, **save_kwargs)
    return out.getvalue()


@pytest.fixture
def image_bytes():
    return make_image_bytes
