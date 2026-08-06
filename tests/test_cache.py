from __future__ import annotations

import asyncio
import fnmatch
import sys
from typing import Any, AsyncIterator, Callable, Coroutine

import pytest

from src.api.schemas import SearchMetadata, SearchResponse, SearchResult, SubLink
from src.config import SearchEngine
from src.core import cache


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test: empty tiers, zeroed counters, no cache env vars."""
    cache._MEM.clear()
    cache._INFLIGHT.clear()
    for key in cache._stats:
        cache._stats[key] = 0
    monkeypatch.setattr(cache, "_redis", None)
    for var in ("SEARCH_CACHE_ENABLED", "SEARCH_CACHE_TTL", "SEARCH_CACHE_MAX",
                "SEARCH_CACHE_REDIS_URL", "SEARCH_CACHE_INFLIGHT_WAIT"):
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


class TestSingleFlight:
    """get_or_compute: N concurrent identical cold queries must cost ONE compute."""

    @pytest.mark.asyncio
    async def test_burst_coalesces_to_single_compute(self) -> None:
        payload = _payload()
        calls = 0
        entered, release = asyncio.Event(), asyncio.Event()

        async def compute() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return payload

        tasks = [asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute)) for _ in range(8)]
        await entered.wait()  # leader is inside compute; FIFO scheduling has attached the other 7
        assert cache._stats["coalesced"] == 7
        assert cache.cache_stats()["inflight"] == 1 and calls == 1

        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)

        assert calls == 1
        assert all(r == payload for r in results)
        # Isolation: mutating one caller's copy must leak into no other caller
        # and never into the cache (results[1] is a follower's copy).
        assert results[1] is not None and results[2] is not None
        results[1]["total"] = 999
        assert results[2]["total"] == 1 and payload["total"] == 1
        hit = await cache.get_cached("q", "google", 1, 10)
        assert hit is not None and hit["total"] == 1

        assert cache._INFLIGHT == {} and cache.cache_stats()["inflight"] == 0  # no leaked slot
        # Result was cached: a late identical caller never invokes compute.
        assert await cache.get_or_compute("q", "google", 1, 10, compute) == payload
        assert calls == 1

    @pytest.mark.asyncio
    async def test_different_keys_are_not_coalesced(self) -> None:
        entered_a, entered_b, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = {"a": 0, "b": 0}

        def gated(name: str, entered: asyncio.Event) -> Callable[[], Coroutine[Any, Any, dict[str, Any]]]:
            async def compute() -> dict[str, Any]:
                calls[name] += 1
                entered.set()
                await release.wait()
                return _payload(name)
            return compute

        task_a = asyncio.create_task(cache.get_or_compute("qa", "google", 1, 10, gated("a", entered_a)))
        task_b = asyncio.create_task(cache.get_or_compute("qb", "google", 1, 10, gated("b", entered_b)))
        # Both computes run CONCURRENTLY — proof the two keys never shared a slot.
        await asyncio.wait_for(asyncio.gather(entered_a.wait(), entered_b.wait()), timeout=2)
        assert cache.cache_stats()["inflight"] == 2

        release.set()
        result_a, result_b = await asyncio.gather(task_a, task_b)

        assert calls == {"a": 1, "b": 1} and cache._stats["coalesced"] == 0
        assert result_a is not None and result_a["query"] == "a"
        assert result_b is not None and result_b["query"] == "b"
        assert cache._INFLIGHT == {}

    @pytest.mark.asyncio
    async def test_leader_exception_propagates_to_every_follower(self) -> None:
        calls = 0
        entered, release = asyncio.Event(), asyncio.Event()

        async def compute() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            raise RuntimeError("scrape blew up")

        tasks = [asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute)) for _ in range(4)]
        await entered.wait()
        assert cache._stats["coalesced"] == 3

        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)

        assert calls == 1  # one compute — the failure fanned out, no retries piled on
        assert all(isinstance(r, RuntimeError) and str(r) == "scrape blew up" for r in results)
        assert cache._INFLIGHT == {}  # registry freed on failure — key is not wedged
        assert await cache.get_cached("q", "google", 1, 10) is None  # failures are never cached
        with pytest.raises(RuntimeError):  # next caller gets a fresh compute
            await cache.get_or_compute("q", "google", 1, 10, compute)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_leader_cancellation_reelects_instead_of_hanging_followers(self) -> None:
        payload = _payload()
        calls = 0
        entered = asyncio.Event()
        stall = asyncio.Event()  # never set: the first leader hangs until cancelled

        async def compute() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await stall.wait()
            return payload

        leader = asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute))
        followers = [asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute)) for _ in range(3)]
        await entered.wait()
        assert cache._stats["coalesced"] == 3

        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        # Followers must not hang: one re-elects and computes, the rest are served.
        results = await asyncio.wait_for(asyncio.gather(*followers), timeout=2)

        assert all(r == payload for r in results)
        assert calls == 2  # original + exactly one re-elected leader
        assert cache._INFLIGHT == {}

    @pytest.mark.asyncio
    async def test_follower_cancellation_leaves_leader_and_others_intact(self) -> None:
        payload = _payload()
        calls = 0
        entered, release = asyncio.Event(), asyncio.Event()

        async def compute() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return payload

        leader = asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute))
        keeper = asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute))
        dropped = asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute))
        await entered.wait()
        assert cache._stats["coalesced"] == 2

        dropped.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dropped
        assert cache.cache_stats()["inflight"] == 1  # shared future survived the follower's cancel

        release.set()
        assert await leader == payload
        assert await keeper == payload
        assert calls == 1 and cache._INFLIGHT == {}

    @pytest.mark.asyncio
    async def test_follower_cancelled_with_the_leader_stays_cancelled(self) -> None:
        """Both cancelled in one tick: the follower must die, not re-elect itself.

        Re-electing here would resurrect a task its caller already abandoned
        (client hangup / shutdown) and start a second scrape nobody consumes.
        """
        calls = 0
        entered = asyncio.Event()
        stall = asyncio.Event()  # never set

        async def compute() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            entered.set()
            await stall.wait()
            return _payload()

        leader = asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute))
        follower = asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute))
        await entered.wait()
        assert calls == 1

        follower.cancel()
        leader.cancel()
        for task in (leader, follower):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)

        assert follower.cancelled()
        assert calls == 1  # the abandoned follower must NOT have started a new compute
        assert cache._INFLIGHT == {}

    @pytest.mark.asyncio
    async def test_none_result_releases_followers_and_is_not_cached(self) -> None:
        calls = 0
        entered, release = asyncio.Event(), asyncio.Event()

        async def compute() -> dict[str, Any] | None:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return None

        tasks = [asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute)) for _ in range(4)]
        await entered.wait()
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)

        assert results == [None, None, None, None]  # every waiter released
        assert calls == 1
        assert cache._MEM == {} and cache.cache_stats()["sets"] == 0  # empty SERPs never cached
        assert cache._INFLIGHT == {}
        # Nothing cached ⇒ the next call elects a fresh leader.
        assert await cache.get_or_compute("q", "google", 1, 10, compute) is None
        assert calls == 2

    @pytest.mark.asyncio
    async def test_follower_wait_is_bounded_by_explicit_timeout(self) -> None:
        entered = asyncio.Event()
        stall = asyncio.Event()  # never set: leader out-lives the follower's bound

        async def compute() -> dict[str, Any]:
            entered.set()
            await stall.wait()
            return _payload()

        leader = asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute))
        await entered.wait()

        with pytest.raises(asyncio.TimeoutError):
            await cache.get_or_compute("q", "google", 1, 10, compute, wait_timeout=0.05)
        # The bound freed only the follower — leader and shared future still run.
        assert cache.cache_stats()["inflight"] == 1 and not leader.done()

        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        assert cache._INFLIGHT == {}

    @pytest.mark.asyncio
    async def test_follower_wait_bound_defaults_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEARCH_CACHE_INFLIGHT_WAIT", "0.05")
        entered = asyncio.Event()
        stall = asyncio.Event()  # never set

        async def compute() -> dict[str, Any]:
            entered.set()
            await stall.wait()
            return _payload()

        leader = asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute))
        await entered.wait()

        with pytest.raises(asyncio.TimeoutError):
            await cache.get_or_compute("q", "google", 1, 10, compute)  # no explicit bound — env applies

        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        assert cache._INFLIGHT == {}

    @pytest.mark.asyncio
    async def test_disabled_cache_disables_coalescing_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEARCH_CACHE_ENABLED", "0")
        release = asyncio.Event()
        entered = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def compute() -> dict[str, Any]:
            nonlocal calls
            calls += 1
            entered[calls - 1].set()
            await release.wait()
            return _payload()

        task_1 = asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute))
        task_2 = asyncio.create_task(cache.get_or_compute("q", "google", 1, 10, compute))
        # Disabled means fresh work per caller: BOTH computes run concurrently.
        await asyncio.wait_for(asyncio.gather(entered[0].wait(), entered[1].wait()), timeout=2)
        assert calls == 2 and cache.cache_stats()["inflight"] == 0

        release.set()
        await asyncio.gather(task_1, task_2)
        assert cache._MEM == {} and cache._stats["coalesced"] == 0

    @pytest.mark.asyncio
    async def test_warm_key_never_calls_compute(self) -> None:
        payload = _payload()
        await cache.set_cached("q", "google", 1, 10, payload)

        async def compute() -> dict[str, Any]:
            raise AssertionError("compute must not run on a warm key")

        assert await cache.get_or_compute("q", "google", 1, 10, compute) == payload

    def test_stats_expose_inflight_and_coalesced(self) -> None:
        stats = cache.cache_stats()
        assert stats["inflight"] == 0 and stats["coalesced"] == 0
