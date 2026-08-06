"""Tests for the rate limit middleware: token bucket, burst, concurrency, quotas, reaping."""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from src.middleware.rate_limit import (
    RateLimitConfig,
    RateLimitMiddleware,
    rate_limit_stats,
    reset_rate_limits,
)


class _FakeClock:
    """Injectable monotonic clock so token refill can be tested without sleeping."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _KeyIdMiddleware(BaseHTTPMiddleware):
    """Stand-in for APIKeyAuthMiddleware — publishes request.state.api_key_id."""

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        key_id = request.headers.get("x-test-key-id")
        if key_id:
            request.state.api_key_id = int(key_id)
        return await call_next(request)


async def _ok(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _build_app(
    config: RateLimitConfig | None = None,
    clock: Callable[[], float] | None = None,
    handler=_ok,  # type: ignore[no-untyped-def]
) -> Starlette:
    app = Starlette(routes=[
        Route("/mcp", handler, methods=["GET", "POST"]),
        Route("/health", _ok),
        Route("/admin/api/stats", _ok),
        Route("/livez", _ok),
        Route("/readyz", _ok),
        Route("/metrics", _ok),
    ])
    app.add_middleware(RateLimitMiddleware, config=config, clock=clock)
    app.add_middleware(_KeyIdMiddleware)  # outermost — sets api_key_id first
    return app


def _client(**kwargs) -> TestClient:  # type: ignore[no-untyped-def]
    raise_exc = kwargs.pop("raise_server_exceptions", True)
    return TestClient(_build_app(**kwargs), raise_server_exceptions=raise_exc)


def _inflight() -> int:
    return sum(int(row["inflight"]) for row in rate_limit_stats()["keys"])


@pytest.fixture(autouse=True)
def _reset():
    reset_rate_limits()
    yield
    reset_rate_limits()


def test_burst_allowed_then_throttled():
    client = _client(config=RateLimitConfig(rps=1, burst=3), clock=_FakeClock())
    assert [client.get("/mcp").status_code for _ in range(3)] == [200, 200, 200]
    assert client.get("/mcp").status_code == 429
    assert rate_limit_stats()["throttled_total"] == 1


def test_429_carries_retry_after_and_ratelimit_headers():
    client = _client(config=RateLimitConfig(rps=2, burst=1), clock=_FakeClock())
    assert client.get("/mcp").status_code == 200

    resp = client.get("/mcp")
    assert resp.status_code == 429
    assert resp.json() == {"error": "Rate limit exceeded", "reason": "rate"}
    assert resp.headers["Retry-After"] == "1"
    assert resp.headers["X-RateLimit-Limit"] == "1"
    assert resp.headers["X-RateLimit-Remaining"] == "0"
    assert resp.headers["X-RateLimit-Reset"] == "1"


def test_success_carries_ratelimit_headers():
    client = _client(config=RateLimitConfig(rps=1, burst=5), clock=_FakeClock())
    first = client.get("/mcp")
    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "5"
    assert first.headers["X-RateLimit-Remaining"] == "4"
    assert client.get("/mcp").headers["X-RateLimit-Remaining"] == "3"


def test_tokens_refill_over_mocked_time():
    clock = _FakeClock()
    client = _client(config=RateLimitConfig(rps=2, burst=2), clock=clock)
    assert [client.get("/mcp").status_code for _ in range(2)] == [200, 200]
    assert client.get("/mcp").status_code == 429

    clock.advance(1.0)  # +2 tokens, capped at burst
    assert [client.get("/mcp").status_code for _ in range(2)] == [200, 200]
    assert client.get("/mcp").status_code == 429


def test_per_key_buckets_are_isolated():
    client = _client(config=RateLimitConfig(rps=1, burst=2), clock=_FakeClock())
    for _ in range(2):
        assert client.get("/mcp", headers={"x-test-key-id": "1"}).status_code == 200
    assert client.get("/mcp", headers={"x-test-key-id": "1"}).status_code == 429
    assert client.get("/mcp", headers={"x-test-key-id": "2"}).status_code == 200

    keys = {row["key"] for row in rate_limit_stats()["keys"]}
    assert keys == {"key:1", "key:2"}


def test_health_admin_and_probes_are_exempt():
    client = _client(config=RateLimitConfig(rps=1, burst=1), clock=_FakeClock())
    for _ in range(5):
        for path in ("/health", "/admin/api/stats", "/livez", "/readyz", "/metrics"):
            assert client.get(path).status_code == 200, path
    stats = rate_limit_stats()
    assert stats["active_buckets"] == 0
    assert stats["throttled_total"] == 0


def test_disabled_by_env_passes_through():
    with patch.dict(os.environ, {"RATE_LIMIT_ENABLED": "0", "RATE_LIMIT_BURST": "1"}):
        client = _client(clock=_FakeClock())  # config=None → read from env
        assert [client.get("/mcp").status_code for _ in range(10)] == [200] * 10
    assert rate_limit_stats()["active_buckets"] == 0


def test_env_defaults_applied():
    with patch.dict(os.environ, {}, clear=False):
        for name in ("RATE_LIMIT_ENABLED", "RATE_LIMIT_RPS", "RATE_LIMIT_BURST", "RATE_LIMIT_CONCURRENCY"):
            os.environ.pop(name, None)
        client = _client(clock=_FakeClock())
        assert client.get("/mcp").status_code == 200
    stats = rate_limit_stats()
    assert (stats["enabled"], stats["rps"], stats["burst"], stats["concurrency"]) == (True, 5.0, 10, 4)


def test_daily_quota_exceeded():
    client = _client(config=RateLimitConfig(rps=100, burst=100, daily_quota=2), clock=_FakeClock())
    assert client.get("/mcp").status_code == 200
    second = client.get("/mcp")
    assert second.status_code == 200
    assert second.headers["X-RateLimit-Limit"] == "2"  # quota is the nearest ceiling
    assert second.headers["X-RateLimit-Remaining"] == "0"

    resp = client.get("/mcp")
    assert resp.status_code == 429
    assert resp.json()["reason"] == "daily"
    assert resp.headers["Retry-After"] == "86400"


def test_inflight_released_when_handler_raises():
    async def boom(request: Request) -> Response:
        raise ValueError("handler exploded")

    client = _client(
        config=RateLimitConfig(rps=100, burst=100, concurrency=1),
        clock=_FakeClock(),
        handler=boom,
        raise_server_exceptions=False,
    )
    assert client.get("/mcp").status_code == 500
    assert _inflight() == 0
    # Slot was returned, so the next request is not starved by the concurrency cap.
    assert client.get("/mcp").status_code == 500


def test_idle_buckets_are_reaped():
    clock = _FakeClock()
    client = _client(config=RateLimitConfig(rps=100, burst=100, idle_ttl=30), clock=clock)
    assert client.get("/mcp", headers={"x-test-key-id": "1"}).status_code == 200
    assert rate_limit_stats()["active_buckets"] == 1

    clock.advance(400.0)  # past both idle_ttl and the reap interval
    assert client.get("/mcp", headers={"x-test-key-id": "2"}).status_code == 200

    stats = rate_limit_stats()
    assert stats["active_buckets"] == 1
    assert [row["key"] for row in stats["keys"]] == ["key:2"]
    assert stats["reaped_total"] == 1


def test_bucket_count_is_hard_capped():
    client = _client(config=RateLimitConfig(rps=100, burst=100, max_buckets=5), clock=_FakeClock())
    for key_id in range(30):
        assert client.get("/mcp", headers={"x-test-key-id": str(key_id)}).status_code == 200
    stats = rate_limit_stats()
    assert stats["active_buckets"] == 5
    assert stats["reaped_total"] == 25


async def test_concurrency_cap_enforced_and_released():
    gate = asyncio.Event()

    async def gated(request: Request) -> PlainTextResponse:
        await gate.wait()
        return PlainTextResponse("ok")

    app = _build_app(
        config=RateLimitConfig(rps=100, burst=100, concurrency=2),
        clock=_FakeClock(),
        handler=gated,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        held = [asyncio.create_task(client.get("/mcp")) for _ in range(2)]
        await _until(lambda: _inflight() == 2)

        rejected = await client.get("/mcp")  # never reaches the gated handler
        assert rejected.status_code == 429
        assert rejected.json()["reason"] == "concurrency"
        assert rejected.headers["Retry-After"] == "1"

        gate.set()
        assert [r.status_code for r in await asyncio.gather(*held)] == [200, 200]

    assert _inflight() == 0


async def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition never became true")
