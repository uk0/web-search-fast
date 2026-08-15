"""Unit tests for the browser action library + batch executor (src/scraper/actions.py).

Pure unit tests: the Playwright Page is an AsyncMock — no network, no browser.
``page.locator`` is a sync MagicMock (as on a real Page) returning a locator
mock whose ``.first`` chains back to itself, so call assertions are direct.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.scraper import actions


def make_page() -> tuple[Any, Any]:
    locator = AsyncMock()
    locator.first = locator  # .first is a sync property on real Locator
    locator.count = AsyncMock(return_value=1)
    locator.inner_text = AsyncMock(return_value="Done")
    locator.get_attribute = AsyncMock(return_value="https://x/")
    locator.select_option = AsyncMock(return_value=["v1"])
    page = AsyncMock()
    page.locator = MagicMock(return_value=locator)  # sync method on real Page
    page.url = "https://example.com/"
    page.title = AsyncMock(return_value="Example")
    page.goto = AsyncMock(return_value=MagicMock(status=200))
    return page, locator


# ---------------------------------------------------------------- single actions

async def test_click_calls_playwright_api():
    page, locator = make_page()
    res = await actions.click(page, "#btn", button="right", click_count=2)
    assert res["ok"] is True
    page.locator.assert_called_once_with("#btn")
    locator.click.assert_awaited_once_with(button="right", click_count=2, timeout=actions._DEFAULT_TIMEOUT_MS)


async def test_click_missing_element_returns_ok_false():
    page, locator = make_page()
    locator.click.side_effect = PlaywrightTimeoutError("Timeout 5000ms exceeded")
    res = await actions.click(page, "#gone")  # must not raise
    assert res["ok"] is False
    assert "timed out" in res["detail"]


async def test_double_click_uses_dblclick():
    page, locator = make_page()
    res = await actions.double_click(page, "text=Open")
    assert res["ok"] is True
    page.locator.assert_called_once_with("text=Open")  # text= engine passes through
    locator.dblclick.assert_awaited_once_with(timeout=actions._DEFAULT_TIMEOUT_MS)


async def test_type_text_types_like_a_human():
    page, locator = make_page()
    res = await actions.type_text(page, "#q", "hi there")
    assert res["ok"] is True
    locator.press_sequentially.assert_awaited_once_with(
        "hi there", delay=actions._DEFAULT_DELAY_MS, timeout=actions._TYPE_TIMEOUT_MS,
    )


async def test_fill_and_form_controls():
    page, locator = make_page()
    assert (await actions.fill(page, "#q", "value"))["ok"] is True
    locator.fill.assert_awaited_once_with("value", timeout=actions._DEFAULT_TIMEOUT_MS)

    res = await actions.select_option(page, "#sel", label="Deutschland")
    assert res["ok"] is True and res["value"] == ["v1"]
    locator.select_option.assert_awaited_once_with(label="Deutschland", timeout=actions._DEFAULT_TIMEOUT_MS)
    both = await actions.select_option(page, "#sel", value="a", label="b")
    assert both["ok"] is False and "exactly one" in both["detail"]

    assert (await actions.check(page, "#cb"))["ok"] is True
    locator.check.assert_awaited_once()
    assert (await actions.uncheck(page, "#cb"))["ok"] is True
    locator.uncheck.assert_awaited_once()


async def test_press_key_normalizes_scopes_and_rejects():
    page, locator = make_page()
    res = await actions.press_key(page, "ctrl+a")
    assert res["ok"] is True
    page.keyboard.press.assert_awaited_once_with("Control+a")

    res = await actions.press_key(page, "enter", selector="#q")
    assert res["ok"] is True
    locator.press.assert_awaited_once_with("Enter", timeout=actions._DEFAULT_TIMEOUT_MS)

    bad = await actions.press_key(page, "Enter; rm -rf /")
    assert bad["ok"] is False and "key combo" in bad["detail"]
    page.keyboard.press.assert_awaited_once()  # no second press happened


async def test_navigate_rejects_dangerous_schemes():
    page, _ = make_page()
    for url in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "ftp://h/x", "not-a-url"):
        res = await actions.navigate(page, url)
        assert res["ok"] is False, url
        assert "http" in res["detail"]
    page.goto.assert_not_called()

    ok = await actions.navigate(page, "https://example.com/a")
    assert ok["ok"] is True and "HTTP 200" in ok["detail"]
    page.goto.assert_awaited_once_with(
        "https://example.com/a", wait_until="domcontentloaded", timeout=actions._NAV_TIMEOUT_MS,
    )


async def test_scroll_variants():
    page, locator = make_page()
    assert (await actions.scroll(page, direction="down", pixels=300))["ok"] is True
    page.mouse.wheel.assert_awaited_once_with(0, 300)
    assert (await actions.scroll(page, direction="up", pixels=300))["ok"] is True
    page.mouse.wheel.assert_awaited_with(0, -300)

    res = await actions.scroll(page, to_selector="#footer")
    assert res["ok"] is True
    locator.scroll_into_view_if_needed.assert_awaited_once()

    both = await actions.scroll(page, direction="down", to_selector="#x")
    assert both["ok"] is False and "exactly one" in both["detail"]


async def test_wait_for_selector_load_state_and_sleep():
    page, locator = make_page()
    res = await actions.wait_for(page, selector="#done")
    assert res["ok"] is True
    locator.wait_for.assert_awaited_once_with(state="visible", timeout=actions._NAV_TIMEOUT_MS)

    res = await actions.wait_for(page, load_state="domcontentloaded")
    assert res["ok"] is True
    page.wait_for_load_state.assert_awaited_once_with("domcontentloaded", timeout=actions._NAV_TIMEOUT_MS)

    res = await actions.wait_for(page, timeout_ms=10)  # pure sleep, bounded
    assert res["ok"] is True and "waited" in res["detail"]

    none = await actions.wait_for(page)
    assert none["ok"] is False


async def test_getters_return_values_not_failures():
    page, locator = make_page()
    got = await actions.get_text(page, "#msg")
    assert got["ok"] is True and got["value"] == "Done"

    locator.get_attribute.return_value = None  # attribute absent — still ok
    attr = await actions.get_attribute(page, "a", "href")
    assert attr["ok"] is True and attr["value"] is None

    locator.count.return_value = 0  # zero matches is data, not failure
    exists = await actions.element_exists(page, "#nope")
    assert exists["ok"] is True and exists["value"] is False


async def test_get_text_truncates_long_text():
    page, locator = make_page()
    locator.inner_text.return_value = "x" * (actions.MAX_TEXT_RETURN + 1000)
    res = await actions.get_text(page, "body")
    assert res["ok"] is True
    assert len(res["value"]) == actions.MAX_TEXT_RETURN
    assert "truncated" in res["detail"]


async def test_history_actions():
    page, _ = make_page()
    page.go_back = AsyncMock(return_value=None)  # no history
    back = await actions.go_back(page)
    assert back["ok"] is False and "history" in back["detail"]
    page.go_back.assert_awaited_once_with(wait_until="domcontentloaded", timeout=actions._NAV_TIMEOUT_MS)

    assert (await actions.go_forward(page))["ok"] is True
    assert (await actions.reload(page))["ok"] is True
    page.reload.assert_awaited_once_with(wait_until="domcontentloaded", timeout=actions._NAV_TIMEOUT_MS)


# ---------------------------------------------------------------- batch executor

async def test_run_actions_happy_path_runs_in_order():
    page, locator = make_page()
    order: list[str] = []
    page.goto = AsyncMock(side_effect=lambda *a, **k: order.append("goto"))
    locator.fill = AsyncMock(side_effect=lambda *a, **k: order.append("fill"))
    locator.click = AsyncMock(side_effect=lambda *a, **k: order.append("click"))

    result = await actions.run_actions(page, [
        {"action": "navigate", "url": "https://example.com"},
        {"action": "fill", "selector": "#q", "value": "camoufox"},
        {"action": "click", "selector": "#go"},
    ], timeout=10.0)

    assert order == ["goto", "fill", "click"]
    assert result["ok"] is True and result["error"] is None
    assert result["completed"] == 3 and result["total"] == 3
    assert [s["index"] for s in result["steps"]] == [0, 1, 2]
    assert all(s["ok"] and isinstance(s["elapsed_ms"], int) for s in result["steps"])
    assert result["url"] == "https://example.com/" and result["title"] == "Example"


async def test_run_actions_smoke_every_action_dispatches():
    page, _ = make_page()
    steps = [
        {"action": "navigate", "url": "https://example.com"},
        {"action": "wait_for", "load_state": "domcontentloaded"},
        {"action": "element_exists", "selector": "#q"},
        {"action": "click", "selector": "#q"},
        {"action": "double_click", "selector": "#q"},
        {"action": "type_text", "selector": "#q", "text": "hi", "delay_ms": 0},
        {"action": "fill", "selector": "#q", "value": "hello"},
        {"action": "press_key", "key": "Enter"},
        {"action": "hover", "selector": "#r"},
        {"action": "scroll", "direction": "down", "pixels": 250},
        {"action": "scroll", "to_selector": "#footer"},
        {"action": "select_option", "selector": "#sel", "value": "v1"},
        {"action": "check", "selector": "#cb"},
        {"action": "uncheck", "selector": "#cb"},
        {"action": "wait_for", "selector": "#done"},
        {"action": "get_text", "selector": "#done"},
        {"action": "get_attribute", "selector": "a", "name": "href"},
        {"action": "go_back"},
        {"action": "go_forward"},
        {"action": "reload"},
    ]
    assert {s["action"] for s in steps} == set(actions.ACTION_NAMES)  # full coverage
    result = await actions.run_actions(page, steps, timeout=30.0)
    assert result["ok"] is True, [s for s in result["steps"] if not s["ok"]]
    assert result["completed"] == len(steps)


async def test_run_actions_stops_on_first_failure_by_default():
    page, locator = make_page()
    locator.click = AsyncMock(side_effect=[None, PlaywrightTimeoutError("nope")])
    result = await actions.run_actions(page, [
        {"action": "click", "selector": "#a"},
        {"action": "click", "selector": "#b"},
        {"action": "click", "selector": "#c"},
    ], timeout=10.0)
    assert result["ok"] is False
    assert result["completed"] == 2 and result["total"] == 3
    assert result["steps"][0]["ok"] is True
    assert result["steps"][1]["ok"] is False
    assert result["steps"][2]["detail"].startswith("skipped:")
    assert "step 1" in result["error"]
    assert locator.click.await_count == 2  # step 2 never ran


async def test_run_actions_continue_on_error_runs_all():
    page, locator = make_page()
    locator.click = AsyncMock(side_effect=[None, PlaywrightTimeoutError("nope"), None])
    result = await actions.run_actions(page, [
        {"action": "click", "selector": "#a"},
        {"action": "click", "selector": "#b"},
        {"action": "click", "selector": "#c"},
    ], timeout=10.0, continue_on_error=True)
    assert result["completed"] == 3
    assert [s["ok"] for s in result["steps"]] == [True, False, True]
    assert result["ok"] is False and result["error"] is None


async def test_run_actions_deadline_truncates_and_reports():
    page, locator = make_page()

    async def hang(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()  # blocks until cancelled by the deadline

    locator.click = AsyncMock(side_effect=hang)
    t0 = time.monotonic()
    result = await actions.run_actions(page, [
        {"action": "click", "selector": "#slow"},
        {"action": "get_text", "selector": "#a"},
        {"action": "get_text", "selector": "#b"},
    ], timeout=0.3)
    wall = time.monotonic() - t0

    assert wall < 2.5  # hard-stopped, did not run to the mock's natural end
    assert result["ok"] is False
    assert result["completed"] == 1  # only the hanging step ran
    assert result["steps"][0]["ok"] is False
    assert result["steps"][1]["detail"].startswith("skipped:")
    assert result["steps"][2]["detail"].startswith("skipped:")
    locator.inner_text.assert_not_called()


async def test_validation_rejects_before_anything_runs():
    page, _ = make_page()
    result = await actions.run_actions(page, [
        {"action": "navigate", "url": "https://example.com"},
        {"action": "warp_ten", "selector": "#x"},
        {"action": "click"},
    ], timeout=10.0)
    assert result["ok"] is False and result["completed"] == 0 and result["steps"] == []
    joined = " | ".join(result["errors"])
    assert "unknown action" in joined and "missing required arg" in joined
    # fail fast: the valid first step must NOT have run
    page.goto.assert_not_called()
    page.locator.assert_not_called()
    page.title.assert_not_called()


async def test_validation_caps_and_absurd_values():
    over_text = actions.validate_actions([{"action": "type_text", "selector": "#q", "text": "x" * 501}])
    assert any("too long" in e for e in over_text)

    over_fill = actions.validate_actions([{"action": "fill", "selector": "#q", "value": "y" * 2001}])
    assert any("too long" in e for e in over_fill)

    over_batch = actions.validate_actions([{"action": "reload"}] * 31)
    assert any("too many actions" in e for e in over_batch)

    absurd = actions.validate_actions([
        {"action": "click", "selector": "#a", "click_count": 9},
        {"action": "click", "selector": "#a", "timeout_ms": 10_000_000},
        {"action": "scroll", "direction": "down", "pixels": 10_000_000},
        {"action": "type_text", "selector": "#q", "text": "x" * 400, "delay_ms": 250},  # 100s of typing
        {"action": "click", "selector": "#a", "warp": True},
    ])
    assert len(absurd) == 5

    page, _ = make_page()
    bad_timeout = await actions.run_actions(page, [{"action": "reload"}], timeout=0)
    assert bad_timeout["ok"] is False and "timeout" in bad_timeout["error"]
    page.reload.assert_not_called()


async def test_validation_errors_do_not_echo_caller_strings_unbounded():
    # A bad batch must not turn a huge caller string into a huge tool result.
    huge = "Z" * 200_000
    for errs in (actions.validate_actions([{"action": huge}]),
                 actions.validate_actions([{"action": "reload", huge: 1}])):
        assert len(errs[0]) < 500, len(errs[0])
    page, _ = make_page()
    res = await actions.run_actions(page, [{"action": huge}] * actions.MAX_ACTIONS_PER_BATCH, timeout=5.0)
    assert res["ok"] is False
    assert len(str(res)) < 50_000  # bounded regardless of input size


async def test_select_option_bounds_page_supplied_values():
    page, locator = make_page()
    locator.select_option.return_value = ["v" * (actions.MAX_TEXT_RETURN + 1000)] * 100
    res = await actions.select_option(page, "#sel", label="X")
    assert res["ok"] is True
    assert len(res["value"]) == actions.MAX_SELECTED_OPTIONS
    assert all(len(v) == actions.MAX_TEXT_RETURN for v in res["value"])


async def test_no_eval_surface_exists():
    # An eval action would be an RCE/exfiltration surface — it must not exist
    # under any of the usual names, and unknown actions are rejected.
    for name in ("evaluate", "eval", "execute_js", "run_js", "js", "evaluate_handle"):
        assert name not in actions.ACTION_NAMES
        assert not hasattr(actions, name)
        errs = actions.validate_actions([{"action": name}])
        assert errs and "unknown action" in errs[0]
