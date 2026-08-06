from __future__ import annotations

import fnmatch
import sys
from typing import Any, AsyncIterator

import pytest

from src.api.schemas import SearchMetadata, SearchResponse, SearchResult, SubLink
from src.config import SearchEngine
from src.core import cache


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test: empty tiers, zeroed counters, no cache env vars."""
    cache._MEM.clear()
    for key in cache._stats:
        cache._stats[key] = 0
    monkeypatch.setattr(cache, "_redis", None)
    for var in ("SEARCH_CACHE_ENABLED", "SEARCH_CACHE_TTL", "SEARCH_CACHE_MAX", "SEARCH_CACHE_REDIS_URL"):
        monkeypatch.delenv(var, raising=False)


class _Clock:
    """Monkeypatchable clock — TTL tests must never sleep."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeRedis:
    """Minimal async stand-in exercising the exact calls the cache makes."""

    def __init__(self, fail: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.fail = fail
        self.setex_calls = 0

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError("redis down")

    async def get(self, key: str) -> str | None:
        self._check()
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._check()
        self.setex_calls += 1
        self.store[key] = value

    async def delete(self, *keys: str) -> int:
        self._check()
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    async def scan_iter(self, match: str = "*", count: int = 10) -> AsyncIterator[str]:
        self._check()
        for key in list(self.store):
            if fnmatch.fnmatchcase(key, match):
                yield key


def _payload(query: str = "python asyncio") -> dict[str, Any]:
    return SearchResponse(
        query=query,
        engine=SearchEngine.DUCKDUCKGO,
        depth=2,
        total=1,
        results=[
            SearchResult(
                title="Result",
                url="https://example.com",
                snippet="snippet",
                content="body",
                sub_links=[SubLink(url="https://example.com/a", title="A", content="c")],
            )
        ],
        metadata=SearchMetadata(elapsed_ms=1234, timestamp="2026-01-01T00:00:00+00:00",
                                engine=SearchEngine.DUCKDUCKGO, depth=2),
    ).model_dump(mode="json")


class TestKey:
    def test_normalizes_whitespace_and_case(self) -> None:
        assert cache.make_key("  Hello   World ", "google", 1, 10) == cache.make_key("hello world", "google", 1, 10)
        assert cache.make_key("hello world", "google", 1, 10) != cache.make_key("hello  worlds", "google", 1, 10)

    def test_depth_max_results_and_engine_never_collide(self) -> None:
        keys = {
            cache.make_key("q", "google", 1, 10),
            cache.make_key("q", "google", 2, 10),
            cache.make_key("q", "google", 1, 20),
            cache.make_key("q", "bing", 1, 10),
        }
        assert len(keys) == 4

    def test_accepts_engine_enum_or_string(self) -> None:
        assert cache.make_key("q", SearchEngine.GOOGLE, 1, 10) == cache.make_key("q", "google", 1, 10)


class TestMemoryTier:
    @pytest.mark.asyncio
    async def test_miss_then_hit_roundtrips_to_equal_model(self) -> None:
        payload = _payload()
        assert await cache.get_cached("python asyncio", "duckduckgo", 2, 10) is None

        await cache.set_cached("python asyncio", "duckduckgo", 2, 10, payload)
        hit = await cache.get_cached("python asyncio", "duckduckgo", 2, 10)

        assert hit == payload
        assert SearchResponse.model_validate(hit) == SearchResponse.model_validate(payload)

    @pytest.mark.asyncio
    async def test_equivalent_query_hits_but_different_params_miss(self) -> None:
        await cache.set_cached("Python  Asyncio ", "google", 1, 10, _payload())

        assert await cache.get_cached("python asyncio", "google", 1, 10) is not None
        assert await cache.get_cached("python asyncio", "google", 2, 10) is None
        assert await cache.get_cached("python asyncio", "google", 1, 25) is None

    @pytest.mark.asyncio
    async def test_returned_payload_is_isolated_from_the_store(self) -> None:
        await cache.set_cached("q", "google", 1, 10, _payload())
        first = await cache.get_cached("q", "google", 1, 10)
        assert first is not None
        first["total"] = 999

        second = await cache.get_cached("q", "google", 1, 10)
        assert second is not None and second["total"] == 1

    @pytest.mark.asyncio
    async def test_ttl_expiry_without_sleeping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(cache, "_now", clock)
        monkeypatch.setenv("SEARCH_CACHE_TTL", "60")

        await cache.set_cached("q", "google", 1, 10, _payload())
        clock.advance(59)
        assert await cache.get_cached("q", "google", 1, 10) is not None

        clock.advance(2)
        assert await cache.get_cached("q", "google", 1, 10) is None
        assert cache._MEM == {}  # expired entries are evicted on read
        assert cache.cache_stats()["expired"] == 1

    @pytest.mark.asyncio
    async def test_purge_expired_sweeps_idle_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(cache, "_now", clock)

        await cache.set_cached("old", "google", 1, 10, _payload(), ttl=10)
        await cache.set_cached("new", "google", 1, 10, _payload(), ttl=600)
        clock.advance(30)

        assert cache.cache_stats()["size"] == 1  # stats never count expired entries
        assert cache.purge_expired() == 1
        assert len(cache._MEM) == 1

    @pytest.mark.asyncio
    async def test_lru_eviction_at_capacity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEARCH_CACHE_MAX", "3")
        for query in ("a", "b", "c"):
            await cache.set_cached(query, "google", 1, 10, _payload(query))

        # Touch "a" so the least-recently-used entry is "b", not the oldest insert.
        assert await cache.get_cached("a", "google", 1, 10) is not None
        await cache.set_cached("d", "google", 1, 10, _payload("d"))

        assert len(cache._MEM) == 3
        assert await cache.get_cached("b", "google", 1, 10) is None
        for query in ("a", "c", "d"):
            assert await cache.get_cached(query, "google", 1, 10) is not None
        assert cache.cache_stats()["evictions"] == 1


class TestStatsAndClear:
    @pytest.mark.asyncio
    async def test_stats_accounting(self) -> None:
        assert cache.cache_stats()["hit_rate"] == 0.0

        await cache.get_cached("q", "google", 1, 10)  # miss
        await cache.set_cached("q", "google", 1, 10, _payload())
        await cache.get_cached("q", "google", 1, 10)  # hit
        await cache.get_cached("q", "google", 1, 10)  # hit

        stats = cache.cache_stats()
        assert (stats["hits"], stats["memory_hits"], stats["redis_hits"], stats["misses"]) == (2, 2, 0, 1)
        assert stats["hit_rate"] == pytest.approx(2 / 3, abs=1e-4)
        assert (stats["size"], stats["sets"], stats["backend"], stats["enabled"]) == (1, 1, "memory", True)

    @pytest.mark.asyncio
    async def test_clear_and_invalidate(self) -> None:
        await cache.set_cached("q1", "google", 1, 10, _payload("q1"))
        await cache.set_cached("q2", "google", 1, 10, _payload("q2"))

        assert await cache.invalidate("q1", "google", 1, 10) is True
        assert await cache.invalidate("q1", "google", 1, 10) is False
        assert await cache.clear_cache() == 1
        assert cache.cache_stats()["size"] == 0


class TestDisabled:
    @pytest.mark.asyncio
    async def test_disabled_by_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEARCH_CACHE_ENABLED", "0")

        await cache.set_cached("q", "google", 1, 10, _payload())
        assert cache._MEM == {}
        assert await cache.get_cached("q", "google", 1, 10) is None

        stats = cache.cache_stats()
        assert stats["enabled"] is False
        assert (stats["hits"], stats["misses"], stats["sets"]) == (0, 0, 0)

    @pytest.mark.asyncio
    async def test_non_positive_ttl_skips_storage(self) -> None:
        await cache.set_cached("q", "google", 1, 10, _payload(), ttl=0)
        assert await cache.get_cached("q", "google", 1, 10) is None


class TestRedisTier:
    @pytest.mark.asyncio
    async def test_absent_redis_is_memory_only(self) -> None:
        await cache.set_cached("q", "google", 1, 10, _payload())

        assert await cache.get_cached("q", "google", 1, 10) is not None
        assert cache.cache_stats()["backend"] == "memory"
        assert await cache.clear_cache() == 1

    @pytest.mark.asyncio
    async def test_shared_entry_is_promoted_into_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        writer = _FakeRedis()
        monkeypatch.setattr(cache, "_redis", writer)
        await cache.set_cached("q", "google", 1, 10, _payload())
        assert writer.setex_calls == 1

        cache._MEM.clear()  # simulate a second worker with a cold memory tier
        assert await cache.get_cached("q", "google", 1, 10) is not None
        assert await cache.get_cached("q", "google", 1, 10) is not None

        stats = cache.cache_stats()
        assert (stats["redis_hits"], stats["memory_hits"], stats["backend"]) == (1, 1, "memory+redis")
        assert await cache.clear_cache() == 2 and writer.store == {}

    @pytest.mark.asyncio
    async def test_failing_redis_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cache, "_redis", _FakeRedis(fail=True))

        await cache.set_cached("q", "google", 1, 10, _payload())
        assert await cache.get_cached("q", "google", 1, 10) is not None  # memory tier still serves
        cache._MEM.clear()
        assert await cache.get_cached("q", "google", 1, 10) is None
        assert await cache.invalidate("q", "google", 1, 10) is False
        assert await cache.clear_cache() == 0
        assert cache.cache_stats()["errors"] == 4

    @pytest.mark.asyncio
    async def test_init_is_safe_when_redis_is_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "redis", None)
        monkeypatch.setitem(sys.modules, "redis.asyncio", None)

        assert await cache.init_cache_redis("redis://localhost:6379/0") is False
        assert cache._redis is None

        await cache.set_cached("q", "google", 1, 10, _payload())
        assert await cache.get_cached("q", "google", 1, 10) is not None

    @pytest.mark.asyncio
    async def test_init_without_url_stays_memory_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REDIS_URL", raising=False)

        assert await cache.init_cache_redis() is False
        await cache.close_cache_redis()
        assert cache.cache_stats()["backend"] == "memory"
