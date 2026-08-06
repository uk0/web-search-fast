"""Shared test fixtures.

The search cache and engine-health breaker are process-global by design (they
must persist across requests in production). In tests that means state leaks
between cases — a cached SearchResponse or an open breaker from an earlier test
would silently change the next one's result. Reset both around every test.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_global_state():
    from src.core import cache as cache_mod
    from src.engine import health as health_mod

    cache_mod._MEM.clear()
    for counter in cache_mod._stats:
        cache_mod._stats[counter] = 0
    health_mod.reset_health()
    yield
    cache_mod._MEM.clear()
    health_mod.reset_health()
