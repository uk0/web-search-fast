from __future__ import annotations

import asyncio
import inspect
import json
import time

import pytest

from src.observability.health import (
    DEGRADED,
    OK,
    SKIPPED,
    UNAVAILABLE,
    DependencyCheck,
    HealthMonitor,
    check_browser_pool,
    check_database,
    check_redis,
    http_status,
)


def _check(name: str, value, *, required: bool = True, timeout: float = 1.0) -> DependencyCheck:
    """Check whose probe returns `value` (or raises it, when it is an exception)."""

    async def probe():
        if isinstance(value, BaseException):
            raise value
        return value

    return DependencyCheck(name, probe, required=required, timeout=timeout)


class _FakePool:
    def __init__(self, *, started: bool = True, needs_restart: bool = False) -> None:
        self.stats = {"started": started, "pool_size": 4, "active_tabs": 0}
        self.needs_restart = needs_restart


class TestAggregateStatus:
    @pytest.mark.asyncio
    async def test_all_ok(self):
        monitor = HealthMonitor(version="1.2.3", checks=[_check("a", True), _check("b", {"status": OK})])
        payload = await monitor.readiness()
        assert payload["status"] == OK
        assert payload["ready"] is True
        assert payload["version"] == "1.2.3"
        assert payload["checks"] == {"a": OK, "b": OK}
        assert http_status(payload) == 200

    @pytest.mark.asyncio
    async def test_required_dependency_down_is_unavailable(self):
        monitor = HealthMonitor(checks=[_check("db", False), _check("pool", True)])
        payload = await monitor.readiness()
        assert payload["status"] == UNAVAILABLE
        assert payload["ready"] is False
        assert http_status(payload) == 503

    @pytest.mark.asyncio
    async def test_optional_dependency_down_degrades_but_keeps_serving(self):
        monitor = HealthMonitor(checks=[_check("db", True), _check("redis", False, required=False)])
        payload = await monitor.readiness()
        assert payload["status"] == DEGRADED
        assert payload["ready"] is True
        assert http_status(payload) == 200

    @pytest.mark.asyncio
    async def test_required_dependency_degraded_keeps_serving(self):
        monitor = HealthMonitor(checks=[_check("pool", {"status": DEGRADED, "reason": "restart pending"})])
        payload = await monitor.readiness()
        assert payload["status"] == DEGRADED
        assert payload["ready"] is True

    @pytest.mark.asyncio
    async def test_skipped_optional_does_not_affect_aggregate(self):
        monitor = HealthMonitor(
            checks=[_check("db", True), _check("redis", {"status": SKIPPED}, required=False)]
        )
        payload = await monitor.readiness()
        assert payload["status"] == OK
        assert payload["checks"]["redis"] == SKIPPED


class TestFailureContainment:
    @pytest.mark.asyncio
    async def test_hanging_check_does_not_hang_the_probe(self):
        async def hangs():
            await asyncio.sleep(30)

        monitor = HealthMonitor(
            checks=[DependencyCheck("wedged", hangs, timeout=0.05), _check("db", True)]
        )
        started = time.monotonic()
        payload = await monitor.deep()
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"probe blocked on a hung dependency for {elapsed:.1f}s"
        assert payload["status"] == UNAVAILABLE
        wedged = next(c for c in payload["checks"] if c["name"] == "wedged")
        assert wedged["status"] == UNAVAILABLE
        assert "timeout" in wedged["error"]
        # Sibling checks still run
        assert next(c for c in payload["checks"] if c["name"] == "db")["status"] == OK

    @pytest.mark.asyncio
    async def test_raising_check_is_contained(self):
        monitor = HealthMonitor(checks=[_check("db", ValueError("boom")), _check("pool", True)])
        payload = await monitor.deep()
        db = next(c for c in payload["checks"] if c["name"] == "db")
        assert db["status"] == UNAVAILABLE
        assert db["error"] == "ValueError: boom"
        assert next(c for c in payload["checks"] if c["name"] == "pool")["status"] == OK


class TestLiveness:
    def test_liveness_is_sync_cheap_and_dependency_free(self):
        calls: list[str] = []

        def boom(name: str):
            def provider():
                calls.append(name)
                raise RuntimeError(f"{name} touched by liveness")

            return provider

        monitor = HealthMonitor(
            version="9.9.9",
            pool_provider=boom("pool"),
            db_provider=boom("db"),
            redis_provider=boom("redis"),
        )
        assert not inspect.iscoroutinefunction(monitor.liveness)

        started = time.monotonic()
        payload = monitor.liveness()
        elapsed = time.monotonic() - started

        assert calls == []
        assert elapsed < 0.05
        assert payload["status"] == OK
        assert payload["live"] is True
        assert payload["version"] == "9.9.9"
        assert payload["uptime_s"] >= 0


class TestPayloads:
    @pytest.mark.asyncio
    async def test_all_payloads_are_json_serializable(self):
        monitor = HealthMonitor(
            version="1.0.0",
            checks=[_check("weird", {"status": OK, "handle": object()}), _check("db", True)],
        )
        json.dumps(monitor.liveness())
        json.dumps(await monitor.readiness())
        deep = json.loads(json.dumps(await monitor.deep()))
        weird = next(c for c in deep["checks"] if c["name"] == "weird")
        assert isinstance(weird["detail"]["handle"], str)

    @pytest.mark.asyncio
    async def test_deep_reports_latency_and_remembers_last_error(self):
        outcomes = [{"status": UNAVAILABLE, "reason": "connection refused"}, True]

        async def flaky():
            return outcomes.pop(0)

        monitor = HealthMonitor(checks=[DependencyCheck("db", flaky)])

        first = await monitor.deep()
        assert first["status"] == UNAVAILABLE
        assert first["checks"][0]["error"] == "connection refused"

        second = await monitor.deep()
        report = second["checks"][0]
        assert second["status"] == OK
        assert report["error"] is None
        assert report["last_error"] == "connection refused"
        assert report["last_error_at"]
        assert report["latency_ms"] >= 0


class TestDefaultProbes:
    @pytest.mark.asyncio
    async def test_browser_pool_states(self):
        assert (await check_browser_pool(lambda: None))["status"] == UNAVAILABLE
        assert (await check_browser_pool(lambda: _FakePool(started=False)))["status"] == UNAVAILABLE
        assert (await check_browser_pool(lambda: _FakePool(needs_restart=True)))["status"] == DEGRADED

        healthy = await check_browser_pool(lambda: _FakePool())
        assert healthy["status"] == OK
        assert healthy["pool"]["pool_size"] == 4

    @pytest.mark.asyncio
    async def test_sync_provider_returning_an_awaitable_object_is_not_re_awaited(self):
        """A live aiosqlite Connection is itself awaitable; re-awaiting one raises."""

        class _Conn:
            def __await__(self):
                raise RuntimeError("threads can only be started once")

            async def execute_fetchall(self, sql):
                return [(1,)]

        conn = _Conn()
        assert (await check_database(lambda: conn))["status"] == OK

        async def async_provider():
            return conn

        assert (await check_database(async_provider))["status"] == OK

    @pytest.mark.asyncio
    async def test_redis_is_skipped_when_unconfigured_and_degraded_when_dead(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        assert (await check_redis(lambda: None))["status"] == SKIPPED

        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        assert (await check_redis(lambda: None))["status"] == DEGRADED

        class _Client:
            async def ping(self) -> bool:
                return True

        assert (await check_redis(lambda: _Client()))["status"] == OK
