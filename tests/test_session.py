"""Leak-proof tests for interactive browser sessions (src/scraper/session.py).

The FakePool records acquire() enter/exit counts so every test can assert the
one invariant that matters: enters == exits — the tab is always released, and
released exactly once. No network, no real browser, no timed sleeps: fake
clock for TTLs, asyncio.Event for sequencing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from src.scraper.session import (
    SessionExpiredError,
    SessionLimitError,
    SessionManager,
    SessionNotFoundError,
    get_session_manager,
)


def make_page() -> AsyncMock:
    page = AsyncMock()
    page.is_closed = Mock(return_value=False)  # sync in Playwright
    return page


class FakePool:
    """Stand-in for BrowserPool.acquire() that counts context enters/exits."""

    def __init__(self, page_factory: Callable[[], AsyncMock] | None = None, fail_enter: bool = False) -> None:
        self.enters = 0
        self.exits = 0
        self.pages: list[AsyncMock] = []
        self.labels: list[str | None] = []
        self.fail_enter = fail_enter
        self.enter_gate: asyncio.Event | None = None  # when set, block inside __aenter__
        self.enter_reached = asyncio.Event()
        self._page_factory = page_factory or make_page

    @asynccontextmanager
    async def acquire(self, *, label: str | None = None, session: str | None = None) -> AsyncIterator[AsyncMock]:
        self.enter_reached.set()
        if self.fail_enter:
            raise RuntimeError("pool exhausted")
        if self.enter_gate is not None:
            await self.enter_gate.wait()
        self.enters += 1
        page = self._page_factory()
        self.pages.append(page)
        self.labels.append(label)
        try:
            yield page
        finally:
            self.exits += 1
            page.is_closed = Mock(return_value=True)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


@pytest.fixture
async def mgr_factory() -> AsyncIterator[Callable[..., tuple[SessionManager, FakeClock]]]:
    created: list[SessionManager] = []

    def _make(**kw: Any) -> tuple[SessionManager, FakeClock]:
        clock = FakeClock()
        mgr = SessionManager(
            max_sessions=kw.pop("max_sessions", 2),
            idle_ttl=kw.pop("idle_ttl", 120.0),
            max_lifetime=kw.pop("max_lifetime", 80.0),
            sweep_interval=kw.pop("sweep_interval", 3600.0),  # background sweeper stays inert in tests
            clock=clock,
        )
        created.append(mgr)
        return mgr, clock

    yield _make
    for mgr in created:  # always stop sweepers / release any leftover tabs
        await mgr.close_all()


async def test_open_get_close_roundtrip(mgr_factory) -> None:
    mgr, _ = mgr_factory()
    pool = FakePool()
    sid = await mgr.open_session(pool)
    assert pool.enters == 1
    assert pool.labels == [f"session:{sid}"]

    page = await mgr.get_page(sid)
    assert page is pool.pages[0]

    info = mgr.session_info(sid)
    assert info is not None and info["session_id"] == sid and info["alive"] is True
    assert [s["session_id"] for s in mgr.list_sessions()] == [sid]

    assert await mgr.close_session(sid) is True
    assert pool.enters == pool.exits == 1  # released exactly once
    assert mgr.session_info(sid) is None
    assert mgr.list_sessions() == []


async def test_open_with_url_navigates(mgr_factory) -> None:
    mgr, _ = mgr_factory()
    pool = FakePool()
    sid = await mgr.open_session(pool, url="https://example.com")
    page = pool.pages[0]
    page.goto.assert_awaited_once()
    assert page.goto.await_args.args[0] == "https://example.com"
    info = mgr.session_info(sid)
    assert info is not None and info["url"] == "https://example.com"


async def test_double_close_is_harmless(mgr_factory) -> None:
    mgr, _ = mgr_factory()
    pool = FakePool()
    sid = await mgr.open_session(pool)
    assert await mgr.close_session(sid) is True
    assert await mgr.close_session(sid) is False  # second close: no-op
    assert pool.enters == pool.exits == 1  # not released twice


async def test_idle_ttl_expiry_sweep_releases_tab(mgr_factory) -> None:
    mgr, clock = mgr_factory(idle_ttl=100.0, max_lifetime=10_000.0)
    pool = FakePool()
    sid = await mgr.open_session(pool)

    clock.advance(99)
    assert await mgr.sweep_expired() == 0  # not yet idle-expired

    clock.advance(2)  # idle 101s >= 100s
    assert await mgr.sweep_expired() == 1
    assert pool.enters == pool.exits == 1
    with pytest.raises(SessionNotFoundError):
        await mgr.get_page(sid)
    assert mgr.stats()["expired_total"] == 1


async def test_get_page_refreshes_idle_ttl(mgr_factory) -> None:
    mgr, clock = mgr_factory(idle_ttl=100.0, max_lifetime=10_000.0)
    pool = FakePool()
    sid = await mgr.open_session(pool)

    for _ in range(3):  # cumulative age 270s > ttl, but each use refreshes
        clock.advance(90)
        assert await mgr.get_page(sid) is pool.pages[0]

    clock.advance(101)
    with pytest.raises(SessionExpiredError):
        await mgr.get_page(sid)
    assert pool.enters == pool.exits == 1  # expiry released the tab


async def test_max_lifetime_caps_continuously_active_session(mgr_factory) -> None:
    mgr, clock = mgr_factory(idle_ttl=10_000.0, max_lifetime=80.0)
    pool = FakePool()
    sid = await mgr.open_session(pool)

    clock.advance(50)
    await mgr.get_page(sid)  # active use — lifetime must still bind
    clock.advance(50)  # age 100s >= 80s, idle only 50s
    with pytest.raises(SessionExpiredError):
        await mgr.get_page(sid)
    assert pool.enters == pool.exits == 1
    assert mgr.stats()["expired_total"] == 1


async def test_session_cap_rejects_cleanly_then_frees(mgr_factory) -> None:
    mgr, _ = mgr_factory(max_sessions=1)
    pool = FakePool()
    sid = await mgr.open_session(pool)

    with pytest.raises(SessionLimitError):
        await mgr.open_session(pool)  # fails fast — never queues
    assert pool.enters == 1  # rejected before touching the pool
    assert mgr.stats()["rejected_total"] == 1
    assert await mgr.get_page(sid) is pool.pages[0]  # existing session unaffected

    await mgr.close_session(sid)
    sid2 = await mgr.open_session(pool)  # slot freed by the close
    assert sid2 != sid
    assert pool.enters == 2


async def test_unknown_session_id(mgr_factory) -> None:
    mgr, _ = mgr_factory()
    with pytest.raises(SessionNotFoundError):
        await mgr.get_page("nope")
    assert await mgr.close_session("nope") is False
    assert mgr.session_info("nope") is None


async def test_get_page_never_returns_dead_page(mgr_factory) -> None:
    mgr, _ = mgr_factory()
    pool = FakePool()
    sid = await mgr.open_session(pool)

    pool.pages[0].is_closed = Mock(return_value=True)  # pool reaper killed the tab
    with pytest.raises(SessionExpiredError) as ei:
        await mgr.get_page(sid)
    assert "open a new session" in str(ei.value)
    assert pool.enters == pool.exits == 1  # detection released the pool slot
    assert mgr.session_info(sid) is None
    assert mgr.stats()["dead_page_total"] == 1


async def test_sweep_detects_externally_closed_tab(mgr_factory) -> None:
    mgr, _ = mgr_factory()
    pool = FakePool()
    await mgr.open_session(pool)

    pool.pages[0].is_closed = Mock(return_value=True)
    assert await mgr.sweep_expired() == 1
    assert pool.enters == pool.exits == 1
    assert mgr.list_sessions() == []


async def test_open_failure_when_pool_acquire_fails(mgr_factory) -> None:
    mgr, _ = mgr_factory()
    pool = FakePool(fail_enter=True)
    with pytest.raises(RuntimeError, match="pool exhausted"):
        await mgr.open_session(pool)
    assert pool.enters == 0 and pool.exits == 0
    assert mgr.list_sessions() == []
    assert mgr.stats()["active_sessions"] == 0


async def test_open_navigation_failure_releases_tab(mgr_factory) -> None:
    def bad_page() -> AsyncMock:
        page = make_page()
        page.goto = AsyncMock(side_effect=RuntimeError("nav failed"))
        return page

    mgr, _ = mgr_factory()
    pool = FakePool(page_factory=bad_page)
    with pytest.raises(RuntimeError, match="nav failed"):
        await mgr.open_session(pool, url="https://example.com")
    assert pool.enters == 1 and pool.exits == 1  # tab acquired, then released on failure
    assert mgr.list_sessions() == []


async def test_close_all_releases_everything(mgr_factory) -> None:
    mgr, _ = mgr_factory(max_sessions=3)
    pool = FakePool()
    for _ in range(3):
        await mgr.open_session(pool)

    assert await mgr.close_all() == 3
    assert pool.enters == 3 and pool.exits == 3
    assert mgr.list_sessions() == []
    assert await mgr.close_all() == 0  # idempotent


async def test_cancellation_during_navigation_releases_tab(mgr_factory) -> None:
    started = asyncio.Event()
    hang = asyncio.Event()

    def slow_page() -> AsyncMock:
        page = make_page()

        async def goto(*args: Any, **kwargs: Any) -> None:
            started.set()
            await hang.wait()

        page.goto = goto
        return page

    mgr, _ = mgr_factory()
    pool = FakePool(page_factory=slow_page)
    task = asyncio.create_task(mgr.open_session(pool, url="https://example.com"))
    await started.wait()  # tab acquired, navigation in flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.enters == 1 and pool.exits == 1  # cancelled open still released the tab
    assert mgr.list_sessions() == []


async def test_cancellation_during_acquire_enter_unwinds(mgr_factory) -> None:
    mgr, _ = mgr_factory()
    pool = FakePool()
    pool.enter_gate = asyncio.Event()  # block inside acquire()'s __aenter__

    task = asyncio.create_task(mgr.open_session(pool))
    await pool.enter_reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.enters == 0 and pool.exits == 0  # unwound before a tab ever existed
    assert mgr.list_sessions() == []


async def test_wedged_tab_teardown_never_blocks_shutdown(monkeypatch, mgr_factory) -> None:
    """A page.close() that never returns must not hang close/sweep/shutdown."""
    import src.scraper.session as session_mod

    monkeypatch.setattr(session_mod, "_TERMINATE_WAIT_SECS", 0.05)

    class WedgePool(FakePool):
        @asynccontextmanager
        async def acquire(self, *, label: str | None = None, session: str | None = None) -> AsyncIterator[AsyncMock]:
            self.enters += 1
            page = self._page_factory()
            self.pages.append(page)
            try:
                yield page
            finally:
                await asyncio.shield(asyncio.sleep(30))  # wedged, uncancellable
                self.exits += 1

    mgr, _ = mgr_factory()
    pool = WedgePool()
    sid = await mgr.open_session(pool)
    await asyncio.wait_for(mgr.close_session(sid), timeout=1.0)  # would hang without the bound
    assert mgr.list_sessions() == []
    await asyncio.wait_for(mgr.close_all(), timeout=1.0)


async def test_sweeper_interval_is_floored(mgr_factory) -> None:
    """A 0/negative interval must not turn the sweeper into a hot loop."""
    mgr, _ = mgr_factory(sweep_interval=0.0)
    sweeps = 0
    real_sweep = mgr.sweep_expired

    async def counting() -> int:
        nonlocal sweeps
        sweeps += 1
        return await real_sweep()

    mgr.sweep_expired = counting  # type: ignore[method-assign]
    mgr._ensure_sweeper()
    await asyncio.sleep(0.2)
    assert sweeps < 50, f"sweeper spun {sweeps} times in 0.2s — interval floor missing"


async def test_stats_ledger_and_default_singleton(mgr_factory) -> None:
    mgr, _ = mgr_factory(max_sessions=1)
    pool = FakePool()
    sid = await mgr.open_session(pool)
    s = mgr.stats()
    assert s["active_sessions"] == 1 and s["opened_total"] == 1 and s["max_sessions"] == 1

    await mgr.close_session(sid)
    s = mgr.stats()
    assert s["active_sessions"] == 0 and s["closed_total"] == 1
    assert s["opened_total"] - s["closed_total"] == s["active_sessions"]

    assert get_session_manager() is get_session_manager()  # wiring singleton
