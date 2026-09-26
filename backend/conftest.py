import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """Throttle counters live in the cache; isolate tests from each other."""
    cache.clear()
    yield
    cache.clear()
