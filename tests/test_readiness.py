"""Tests for src.scraper.readiness - AsyncMock pages only, no real browser.

Sequencing never uses sleeps: hanging sub-waits block on a never-set
asyncio.Event and the gate's own bounded slices cut them off.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from unittest.mock import ANY, AsyncMock, call

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import src.scraper.readiness as readiness
from src.scraper.readiness import ReadyReport, wait_until_ready


def make_page(*, fonts: object = True, images: object = True, quiet: object = True) -> AsyncMock:
    """AsyncMock page whose evaluate dispatches on the module's static JS payloads."""
    page = AsyncMock()
    page.wait_for_load_state = AsyncMock(return_value=None)  # real API returns None

    async def fake_eval(js: str, arg: object = None) -> object:
        if js is readiness._JS_FONTS_READY:
            return fonts
        if js is readiness._JS_VIEWPORT_IMAGES:
            return images
        if js is readiness._JS_DOM_QUIET:
            return quiet
        raise AssertionError(f"unexpected evaluate payload: {js[:60]!r}")

    page.evaluate = AsyncMock(side_effect=fake_eval)
    return page


async def _hang(*args: Any, **kwargs: Any) -> None:
    await asyncio.Event().wait()  # never set - only the gate's bound can end this


def _load_states(page: AsyncMock) -> list[Any]:
    return [c.args[0] for c in page.wait_for_load_state.await_args_list]


async def test_dom_level_runs_only_dom_check() -> None:
    page = make_page()
    report = await wait_until_ready(page, level="dom", timeout=5.0)
    assert isinstance(report, ReadyReport)
    assert page.wait_for_load_state.await_args_list == [call("domcontentloaded", timeout=ANY)]
    page.evaluate.assert_not_awaited()
    page.wait_for_selector.assert_not_awaited()
    assert report.ready and report.dom and not report.timed_out
    assert report.level_reached == "dom"
    assert not report.load and not report.network_idle
    assert report.selector_found is None
    assert report.elapsed_ms >= 0 and report.detail == "ok"


async def test_load_level_runs_dom_and_load_only() -> None:
    page = make_page()
    report = await wait_until_ready(page, level="load", timeout=5.0)
    assert _load_states(page) == ["domcontentloaded", "load"]
    page.evaluate.assert_not_awaited()
    assert report.ready and report.dom and report.load
    assert not report.network_idle
    assert report.level_reached == "load"


async def test_network_level_adds_networkidle() -> None:
    page = make_page()
    report = await wait_until_ready(page, level="network", timeout=5.0)
    assert _load_states(page) == ["domcontentloaded", "load", "networkidle"]
    page.evaluate.assert_not_awaited()
    assert report.ready and report.network_idle
    assert report.level_reached == "network"


async def test_stable_level_runs_all_checks_in_order() -> None:
    page = make_page()
    report = await wait_until_ready(page, level="stable", timeout=5.0)
    assert _load_states(page) == ["domcontentloaded", "load", "networkidle"]
    js_payloads = [c.args[0] for c in page.evaluate.await_args_list]
    assert js_payloads == [readiness._JS_FONTS_READY, readiness._JS_VIEWPORT_IMAGES, readiness._JS_DOM_QUIET]
    assert report.ready and not report.timed_out
    assert report.fonts and report.images and report.dom_quiet
    assert report.level_reached == "stable" and report.level_requested == "stable"


async def test_hanging_networkidle_is_absorbed_and_reported() -> None:
    page = make_page()

    async def load_state(state: str = "load", timeout: float | None = None) -> None:
        if state == "networkidle":
            await _hang()  # ignores its timeout arg, like a websocket-y real site

    page.wait_for_load_state = AsyncMock(side_effect=load_state)
    t0 = time.monotonic()
    report = await wait_until_ready(page, level="network", timeout=1.0)
    assert time.monotonic() - t0 < 2.0  # absorbed within the gate's own bound
    assert report.dom and report.load and not report.network_idle
    assert report.timed_out and not report.ready
    assert report.level_reached == "load"  # page still usable at this level
    assert "network_idle" in report.detail


async def test_overall_budget_respected_when_every_subwait_hangs() -> None:
    page = make_page()
    page.wait_for_load_state = AsyncMock(side_effect=_hang)
    page.evaluate = AsyncMock(side_effect=_hang)
    t0 = time.monotonic()
    report = await wait_until_ready(page, level="stable", timeout=1.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0  # sliced budget: no sub-wait ate more than its cap
    assert report.timed_out and not report.ready
    assert report.level_reached == "none"
    assert not any((report.dom, report.load, report.network_idle, report.fonts, report.images, report.dom_quiet))


async def test_fonts_check_guards_pages_without_font_api() -> None:
    # The JS itself must tolerate a missing document.fonts (some engines/embeds).
    assert "document.fonts" in readiness._JS_FONTS_READY
    assert "return true" in readiness._JS_FONTS_READY  # guard path resolves, never rejects
    page = make_page(fonts=True)  # guard returning true surfaces as fonts=True
    report = await wait_until_ready(page, level="stable", timeout=5.0)
    assert report.fonts is True


async def test_evaluate_throw_is_contained() -> None:
    page = make_page()
    page.evaluate = AsyncMock(side_effect=RuntimeError("Execution context was destroyed"))
    report = await wait_until_ready(page, level="stable", timeout=5.0)
    assert report.dom and report.load and report.network_idle
    assert not report.fonts and not report.images and not report.dom_quiet
    assert not report.ready
    assert report.level_reached == "network"
    assert "RuntimeError" in report.detail


async def test_selector_gate_found() -> None:
    page = make_page()
    report = await wait_until_ready(page, level="dom", timeout=5.0, selector="#app .result")
    page.wait_for_selector.assert_awaited_once()
    args, kwargs = page.wait_for_selector.await_args
    assert args == ("#app .result",)
    assert kwargs["state"] == "visible"  # attached AND visible
    assert report.selector_found is True
    assert report.ready


async def test_selector_missing_is_reported_not_raised() -> None:
    page = make_page()
    page.wait_for_selector = AsyncMock(side_effect=PlaywrightTimeoutError("Timeout 1000ms exceeded"))
    report = await wait_until_ready(page, level="dom", timeout=2.0, selector=".never-mounts")
    assert report.selector_found is False
    assert report.dom and not report.ready  # page usable, gate honest
    assert report.timed_out
    assert "selector" in report.detail


async def test_selector_may_use_most_of_the_budget() -> None:
    """An element that mounts late but inside the caller's budget must be caught.

    The one place a sleep is unavoidable: "mounts at t=0.6s" IS the condition
    under test. A selector phase capped at half the budget missed it.
    """
    page = make_page()

    async def mounts_late(sel: str, *, state: str = "visible", timeout: float | None = None) -> object:
        await asyncio.sleep(0.6)
        return object()  # ElementHandle

    page.wait_for_selector = AsyncMock(side_effect=mounts_late)
    t0 = time.monotonic()
    report = await wait_until_ready(page, level="load", timeout=1.0, selector=".mounts-late")
    assert time.monotonic() - t0 < 1.3  # still bounded by the overall budget
    assert report.selector_found is True
    assert report.ready and not report.timed_out


async def test_dom_quiet_js_reclaims_a_stale_observer() -> None:
    # A cancelled run cannot reach finish(), so the next run must disconnect the
    # observer it left behind - otherwise long-lived session pages accumulate them.
    js = readiness._JS_DOM_QUIET
    assert "__wsmcpReadinessMO" in js
    assert js.index("stale.disconnect()") < js.index("observer.observe(")
    assert "window.__wsmcpReadinessMO = observer" in js


async def test_tiny_budget_degrades_and_zero_budget_returns_immediately() -> None:
    page = make_page()
    report = await wait_until_ready(page, level="stable", timeout=0.3)
    assert page.wait_for_load_state.await_args_list == [call("domcontentloaded", timeout=ANY)]
    page.evaluate.assert_not_awaited()
    assert "degraded" in report.detail
    assert report.level_requested == "stable" and report.level_reached == "dom"
    assert report.dom and not report.ready  # stable was requested but never verified

    page2 = make_page()
    report2 = await wait_until_ready(page2, level="stable", timeout=0.0)
    page2.wait_for_load_state.assert_not_awaited()
    page2.evaluate.assert_not_awaited()
    assert report2.timed_out and not report2.ready
    assert report2.level_reached == "none"


async def test_garbage_env_values_fall_back(monkeypatch: Any, caplog: Any) -> None:
    monkeypatch.setenv("READINESS_TIMEOUT", "banana")
    monkeypatch.setenv("READINESS_QUIET_MS", "inf")
    monkeypatch.setenv("READINESS_LEVEL", "warp9")
    page = make_page()
    with caplog.at_level(logging.WARNING, logger="src.scraper.readiness"):
        report = await wait_until_ready(page)
    assert report.level_requested == "stable"  # defaults despite garbage env
    assert report.ready
    warnings = [r for r in caplog.records if "READINESS" in r.getMessage()]
    assert len(warnings) == 3

    monkeypatch.setenv("READINESS_TIMEOUT", "nan")
    report2 = await wait_until_ready(make_page())
    assert report2.ready  # NaN never became a deadline


async def test_dom_quiet_receives_validated_args_never_interpolated() -> None:
    page = make_page()
    await wait_until_ready(page, level="stable", timeout=5.0, quiet_ms=250)
    quiet_call = page.evaluate.await_args_list[-1]
    assert quiet_call.args[0] is readiness._JS_DOM_QUIET  # static payload, not a built string
    cfg = quiet_call.args[1]
    assert cfg["quietMs"] == 250
    assert isinstance(cfg["maxMs"], int) and cfg["maxMs"] >= cfg["quietMs"]


async def test_dom_quiet_never_settling_is_reported() -> None:
    page = make_page(quiet=False)  # JS hard-stop fired: no quiet window in bound
    report = await wait_until_ready(page, level="stable", timeout=5.0)
    assert report.fonts and report.images and report.dom_quiet is False
    assert not report.ready and report.timed_out
    assert report.level_reached == "network"
    assert "dom_quiet" in report.detail
