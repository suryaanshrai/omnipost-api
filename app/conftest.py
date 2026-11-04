import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    """Rate-limit counters live in the cache; start every test from zero."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()
