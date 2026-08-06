"""Tests for the per-engine circuit breaker (src/engine/health.py)."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.config import SearchEngine
from src.engine.health import (
    BASE_COOLDOWN_S,
    BLOCK_COOLDOWN_MULTIPLIER,
    MAX_COOLDOWN_S,
    engine_health_stats,
    is_block_signal,
    order_engines,
    record_failure,
    record_success,
    reset_health,
    set_clock,
    should_skip,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def clock() -> Iterator[FakeClock]:
    fake = FakeClock()
    reset_health()
    set_clock(fake)
    yield fake
    set_clock(None)
    reset_health()


def test_unknown_engine_is_closed_and_never_skipped() -> None:
    assert should_skip("google") is False
    assert engine_health_stats()["google"]["state"] == "closed"


def test_breaker_opens_after_three_errors() -> None:
    record_failure("google")
    record_failure("google")
    assert should_skip("google") is False  # 2 < threshold

    record_failure("google")
    assert should_skip("google") is True
    stats = engine_health_stats()["google"]
    assert stats["state"] == "open"
    assert stats["consecutive_failures"] == 3
    assert stats["cooldown_s"] == BASE_COOLDOWN_S


def test_block_trips_faster_and_cools_longer_than_plain_error() -> None:
    record_failure("google", blocked=True)
    blocked_stats = engine_health_stats()["google"]
    assert blocked_stats["state"] == "open"  # one block is enough
    assert blocked_stats["last_outcome"] == "blocked"
    assert blocked_stats["cooldown_s"] == BASE_COOLDOWN_S * BLOCK_COOLDOWN_MULTIPLIER

    for _ in range(3):
        record_failure("bing")
    assert engine_health_stats()["bing"]["cooldown_s"] == BASE_COOLDOWN_S


def test_success_resets_failure_count(clock: FakeClock) -> None:
    record_failure("bing")
    record_failure("bing")
    record_success("bing", 250.0)
    stats = engine_health_stats()["bing"]
    assert stats["consecutive_failures"] == 0
    assert stats["failure_score"] == 0.0
    assert stats["state"] == "closed"


def test_cooldown_expiry_admits_exactly_one_probe(clock: FakeClock) -> None:
    record_failure("google", blocked=True)
    clock.advance(BASE_COOLDOWN_S * BLOCK_COOLDOWN_MULTIPLIER + 1)

    assert engine_health_stats()["google"]["state"] == "half_open"
    assert should_skip("google") is False  # first caller becomes the probe
    assert should_skip("google") is True  # concurrent callers are held back
    assert should_skip("google") is True


def test_probe_success_closes_breaker_and_resets_backoff(clock: FakeClock) -> None:
    record_failure("google", blocked=True)
    clock.advance(BASE_COOLDOWN_S * BLOCK_COOLDOWN_MULTIPLIER + 1)
    assert should_skip("google") is False
    record_success("google", 800.0)

    stats = engine_health_stats()["google"]
    assert stats["state"] == "closed"
    assert stats["open_streak"] == 0
    assert stats["probe_in_flight"] is False
    assert should_skip("google") is False

    # Backoff really did reset: the next trip starts at the base cooldown again.
    for _ in range(3):
        record_failure("google")
    assert engine_health_stats()["google"]["cooldown_s"] == BASE_COOLDOWN_S


def test_probe_failure_reopens_with_longer_cooldown(clock: FakeClock) -> None:
    for _ in range(3):
        record_failure("google")
    clock.advance(BASE_COOLDOWN_S + 1)
    assert should_skip("google") is False
    record_failure("google")

    stats = engine_health_stats()["google"]
    assert stats["state"] == "open"
    assert stats["cooldown_s"] == BASE_COOLDOWN_S * 2
    assert stats["cooldown_remaining_s"] == pytest.approx(BASE_COOLDOWN_S * 2, abs=0.1)
    assert should_skip("google") is True


def test_cooldown_growth_caps_at_max(clock: FakeClock) -> None:
    for _ in range(3):
        record_failure("google")
    for _ in range(8):
        clock.advance(engine_health_stats()["google"]["cooldown_s"] + 1)
        assert should_skip("google") is False
        record_failure("google")
    assert engine_health_stats()["google"]["cooldown_s"] == MAX_COOLDOWN_S


def test_long_outage_does_not_overflow_the_backoff(clock: FakeClock) -> None:
    """An engine down for weeks must keep tripping, not raise OverflowError."""
    for _ in range(3):
        record_failure("google")
    for _ in range(1_100):
        clock.advance(engine_health_stats()["google"]["cooldown_s"] + 1)
        assert should_skip("google") is False
        record_failure("google")
    stats = engine_health_stats()["google"]
    assert stats["state"] == "open"
    assert stats["cooldown_s"] == MAX_COOLDOWN_S


def test_stale_probe_lease_is_reclaimed(clock: FakeClock) -> None:
    record_failure("google", blocked=True)
    clock.advance(BASE_COOLDOWN_S * BLOCK_COOLDOWN_MULTIPLIER + 1)
    assert should_skip("google") is False  # probe taken, never reported back
    assert should_skip("google") is True

    clock.advance(61)  # lease expires
    assert should_skip("google") is False


def test_order_engines_drops_open_engines() -> None:
    assert order_engines("google", ["duckduckgo", "bing"]) == ["google", "duckduckgo", "bing"]

    record_failure("google", blocked=True)
    assert order_engines("google", ["duckduckgo", "bing"]) == ["duckduckgo", "bing"]

    record_failure("duckduckgo", blocked=True)
    assert order_engines("google", ["duckduckgo", "bing"]) == ["bing"]


def test_order_engines_never_empty_when_all_open(clock: FakeClock) -> None:
    record_failure("google", blocked=True)
    clock.advance(10)
    record_failure("bing", blocked=True)
    clock.advance(10)
    record_failure("duckduckgo", blocked=True)

    chain = order_engines("google", ["duckduckgo", "bing"])
    assert sorted(chain) == ["bing", "duckduckgo", "google"]
    assert chain[0] == "google"  # soonest to recover leads the forced chain


def test_order_engines_keeps_preferred_first_but_ranks_fallbacks_by_latency() -> None:
    record_success("google", 5_000.0)
    record_success("bing", 120.0)
    record_success("duckduckgo", 900.0)

    # Explicit preference wins even when slower.
    assert order_engines("google", ["duckduckgo", "bing"]) == ["google", "bing", "duckduckgo"]

    # Health beats speed: bing degraded, so the healthy (slower) ddg leads.
    record_failure("bing")
    record_failure("bing")
    assert order_engines("google", ["duckduckgo", "bing"])[1] == "duckduckgo"


def test_order_engines_accepts_search_engine_enum() -> None:
    record_failure(SearchEngine.GOOGLE, blocked=True)
    chain = order_engines(SearchEngine.GOOGLE, [SearchEngine.DUCKDUCKGO, SearchEngine.BING])
    assert chain == [SearchEngine.DUCKDUCKGO, SearchEngine.BING]
    assert should_skip(SearchEngine.GOOGLE) is True


def test_ewma_latency_updates() -> None:
    record_success("duckduckgo", 1_000.0)
    assert engine_health_stats()["duckduckgo"]["ewma_latency_ms"] == 1_000.0
    record_success("duckduckgo", 2_000.0)
    assert engine_health_stats()["duckduckgo"]["ewma_latency_ms"] == pytest.approx(1_300.0)


def test_stats_shape(clock: FakeClock) -> None:
    record_success("duckduckgo", 400.0)
    record_failure("google", blocked=True)
    clock.advance(5)

    stats = engine_health_stats()
    assert set(stats) == {"google", "bing", "duckduckgo"}
    google = stats["google"]
    assert set(google) == {
        "state",
        "consecutive_failures",
        "failure_score",
        "open_streak",
        "cooldown_s",
        "cooldown_remaining_s",
        "probe_in_flight",
        "last_outcome",
        "last_outcome_at",
        "last_outcome_age_s",
        "ewma_latency_ms",
        "success_rate",
        "samples",
        "totals",
    }
    assert google["last_outcome_age_s"] == 5.0
    assert google["success_rate"] == 0.0
    assert google["totals"] == {"success": 0, "error": 0, "blocked": 1}
    assert google["cooldown_remaining_s"] == pytest.approx(115.0, abs=0.1)
    assert stats["duckduckgo"]["success_rate"] == 1.0
    assert stats["bing"]["success_rate"] is None


def test_is_block_signal() -> None:
    assert is_block_signal(RuntimeError("redirected to https://www.google.com/sorry/index"))
    assert is_block_signal(text="Our systems have detected unusual traffic")
    assert is_block_signal(RuntimeError("HTTP 429 Too Many Requests"))
    assert not is_block_signal(RuntimeError("Timeout 10000ms exceeded"))
    assert not is_block_signal()
