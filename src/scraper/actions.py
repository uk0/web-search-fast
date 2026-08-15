"""Stateless browser action library + batch executor for caller-owned tabs.

Every action operates on a Playwright ``Page`` obtained by the caller (via
``BrowserPool.acquire()``). Nothing in this module ever closes the page — the
pool's context manager and its leak reaper own the tab lifecycle.

Contract:
- Ordinary failures (element not found, navigation timeout, detached page)
  return ``{"ok": False, "detail": ...}`` — actions never raise for them.
- Every action is timeout-guarded twice: Playwright's own ``timeout=`` for
  auto-waiting, plus an ``asyncio.wait_for`` hard stop so a wedged call can
  never hold a pool tab hostage past its budget.
- Selectors go verbatim into ``page.locator()`` — CSS by default, Playwright's
  ``text=`` / ``role=`` / ``xpath=`` engines pass through. Selectors are never
  interpolated into evaluated JavaScript (this module evaluates none at all).
- Multi-match selectors act on the FIRST match (``locator.first``) instead of
  raising Playwright's strict-mode error — predictable for LLM-driven input.

Security (deliberate omissions):
- There is NO ``evaluate`` / arbitrary-JS action BY DESIGN. This service takes
  untrusted input; an eval action would be a code-execution and exfiltration
  surface (cookies, storage, cross-origin frames). Scrolling therefore uses
  ``mouse.wheel`` / ``scroll_into_view_if_needed``, never injected JS. If some
  future feature seems to need an eval escape hatch, it needs its own security
  review — do not bolt one onto this module.
- ``navigate`` accepts absolute http/https URLs only, which blocks
  ``javascript:`` (eval via the address bar), ``file:`` (local file read) and
  ``data:`` URLs.

Batch executor: ``run_actions(page, actions, timeout=...)`` validates every
step BEFORE running anything (fail fast — a bad script never half-executes),
then runs steps in order under one overall deadline, and returns per-step
results plus the final page url/title.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

__all__ = [
    "ACTION_NAMES",
    "MAX_ACTIONS_PER_BATCH",
    "MAX_BATCH_TIMEOUT_SECS",
    "check",
    "click",
    "double_click",
    "element_exists",
    "fill",
    "get_attribute",
    "get_text",
    "go_back",
    "go_forward",
    "hover",
    "navigate",
    "press_key",
    "reload",
    "run_actions",
    "scroll",
    "select_option",
    "type_text",
    "uncheck",
    "validate_actions",
    "wait_for",
]

# --- caps (validation rejects anything beyond these BEFORE running) ---
MAX_ACTIONS_PER_BATCH = 30
# Pool reaper force-closes tabs at 90s — a batch must always finish inside that.
MAX_BATCH_TIMEOUT_SECS = 75.0
MAX_SELECTOR_LEN = 500
MAX_URL_LEN = 2048
MAX_FILL_LEN = 2_000     # fill() is instant, can afford form-sized values
MAX_TYPE_LEN = 500       # type_text() pays per keystroke — keep it short
MAX_TYPE_WALL_MS = 20_000  # len(text) * delay_ms must fit a sane wall-clock
MAX_OPTION_LEN = 500
MAX_KEY_LEN = 64
MAX_TEXT_RETURN = 4_000  # page-supplied values (text/attribute/option) are truncated to this
MAX_SELECTED_OPTIONS = 20
MAX_SCROLL_PIXELS = 20_000
MAX_STEP_TIMEOUT_MS = 30_000
_MIN_TIMEOUT_MS = 100
_MAX_DELAY_MS = 250
_MAX_CLICK_COUNT = 3

# --- defaults ---
_DEFAULT_TIMEOUT_MS = 5_000    # element interactions
_NAV_TIMEOUT_MS = 10_000       # navigations / load-state waits (project nav budget)
_TYPE_TIMEOUT_MS = 15_000      # human-paced typing needs room for the keystroke delay
_DEFAULT_DELAY_MS = 20         # per-keystroke delay for type_text
_DEFAULT_SCROLL_PIXELS = 600
# Extra slack for the asyncio hard stop so Playwright's own (better) timeout
# error fires first when the page is merely slow rather than wedged.
_HARD_GRACE_SECS = 0.5
# Below this remaining budget a step can't do anything useful — skip instead.
_MIN_REMAINING_MS = 50

_LOAD_STATES = ("domcontentloaded", "load", "networkidle")
_MOUSE_BUTTONS = ("left", "middle", "right")
_MouseButton = Literal["left", "middle", "right"]
_LoadState = Literal["domcontentloaded", "load", "networkidle"]

_ATTR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_:.-]{0,99}")
_KEY_PART_RE = re.compile(r"[A-Za-z0-9]{1,20}")
_F_KEY_RE = re.compile(r"[fF]([1-9]|1[0-2])")

# Friendly aliases → Playwright key names, so "ctrl+a" and "esc" just work.
_KEY_ALIASES = {
    "ctrl": "Control", "control": "Control",
    "cmd": "Meta", "command": "Meta", "meta": "Meta", "win": "Meta",
    "alt": "Alt", "opt": "Alt", "option": "Alt",
    "shift": "Shift",
    "enter": "Enter", "return": "Enter",
    "tab": "Tab",
    "esc": "Escape", "escape": "Escape",
    "space": "Space",
    "backspace": "Backspace",
    "delete": "Delete", "del": "Delete",
    "insert": "Insert",
    "home": "Home", "end": "End",
    "pageup": "PageUp", "pagedown": "PageDown",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
}


# ---------------------------------------------------------------- result plumbing

def _ok(detail: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True, "detail": detail}
    out.update(extra)
    return out


def _fail(detail: str) -> dict[str, Any]:
    return {"ok": False, "detail": detail}


def _short(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_line(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return (lines[0] if lines else type(exc).__name__)[:200]


async def _call(
    make: Callable[[], Awaitable[Any]],
    *,
    timeout_ms: int,
    what: str,
) -> tuple[str | None, Any]:
    """Run one guarded Playwright call: ``(error_detail, result)``.

    ``asyncio.wait_for`` backstops Playwright's own timeout so even a call that
    ignores ``timeout=`` (keyboard/mouse APIs, a wedged transport) is cut off.
    """
    try:
        result = await asyncio.wait_for(make(), timeout=timeout_ms / 1000 + _HARD_GRACE_SECS)
        return None, result
    except asyncio.TimeoutError:
        return f"{what}: hard-timed out after {timeout_ms}ms", None
    except PlaywrightTimeoutError:
        return f"{what}: timed out after {timeout_ms}ms (element not found / not actionable / page not ready)", None
    except PlaywrightError as exc:
        return f"{what}: {_first_line(exc)}", None
    except Exception as exc:  # actions must degrade to a result, never raise
        return f"{what}: {type(exc).__name__}: {_first_line(exc)}", None


def _t(timeout_ms: int | None, default: int) -> int:
    if timeout_ms is None:
        return default
    return max(1, min(int(timeout_ms), MAX_STEP_TIMEOUT_MS))


# ---------------------------------------------------------------- arg checkers

_Checker = Callable[[Any], str | None]


def _v_selector(v: Any) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return "must be a non-empty string"
    if len(v) > MAX_SELECTOR_LEN:
        return f"is too long ({len(v)} chars > {MAX_SELECTOR_LEN})"
    return None


def _v_url(v: Any) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return "must be a non-empty string"
    if len(v) > MAX_URL_LEN:
        return f"is too long ({len(v)} chars > {MAX_URL_LEN})"
    try:
        parts = urlsplit(v)
    except ValueError:
        return "is not a parseable URL"
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return "must be an absolute http(s) URL (javascript:/data:/file: are refused)"
    return None


def _v_timeout(v: Any) -> str | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return "must be an integer (milliseconds)"
    if not _MIN_TIMEOUT_MS <= v <= MAX_STEP_TIMEOUT_MS:
        return f"must be between {_MIN_TIMEOUT_MS} and {MAX_STEP_TIMEOUT_MS} ms"
    return None


def _int_between(lo: int, hi: int) -> _Checker:
    def check_int(v: Any) -> str | None:
        if isinstance(v, bool) or not isinstance(v, int):
            return "must be an integer"
        if not lo <= v <= hi:
            return f"must be between {lo} and {hi}"
        return None
    return check_int


def _str_max(max_len: int, *, allow_empty: bool = False) -> _Checker:
    def check_str(v: Any) -> str | None:
        if not isinstance(v, str):
            return "must be a string"
        if not allow_empty and not v:
            return "must not be empty"
        if len(v) > max_len:
            return f"is too long ({len(v)} chars > {max_len})"
        return None
    return check_str


def _choice(*options: str) -> _Checker:
    def check_choice(v: Any) -> str | None:
        if v not in options:
            return f"must be one of {options}"
        return None
    return check_choice


def _normalize_key(key: str) -> str | None:
    """'ctrl+a' → 'Control+a'; None if the combo is not a sane key chord."""
    if not isinstance(key, str) or not 0 < len(key) <= MAX_KEY_LEN:
        return None
    parts = key.split("+")
    if not 1 <= len(parts) <= 4:
        return None
    out: list[str] = []
    for part in parts:
        if not _KEY_PART_RE.fullmatch(part):
            return None
        alias = _KEY_ALIASES.get(part.lower())
        if alias is not None:
            out.append(alias)
        elif _F_KEY_RE.fullmatch(part):
            out.append(part.upper())
        else:
            out.append(part)
    return "+".join(out)


def _v_key(v: Any) -> str | None:
    if not isinstance(v, str):
        return "must be a string"
    if _normalize_key(v) is None:
        return "is not a valid key combo (examples: 'Enter', 'Tab', 'ctrl+a', 'Control+Shift+T')"
    return None


def _v_attr_name(v: Any) -> str | None:
    if not isinstance(v, str) or not _ATTR_NAME_RE.fullmatch(v):
        return "must be a valid attribute name"
    return None


def _arg_err(action: str, arg: str, value: Any, checker: _Checker) -> str | None:
    msg = checker(value)
    return f"{action}: {arg!r} {msg}" if msg else None


# ---------------------------------------------------------------- actions

async def navigate(page: Page, url: str, *, timeout_ms: int | None = None) -> dict[str, Any]:
    """Go to an absolute http(s) URL. Other schemes are refused (see module docs)."""
    if (e := _arg_err("navigate", "url", url, _v_url)):
        return _fail(e)
    t = _t(timeout_ms, _NAV_TIMEOUT_MS)
    err, resp = await _call(
        lambda: page.goto(url, wait_until="domcontentloaded", timeout=t),
        timeout_ms=t, what=f"navigate to {_short(url)}",
    )
    if err:
        return _fail(err)
    status = getattr(resp, "status", None)
    suffix = f" (HTTP {status})" if isinstance(status, int) else ""
    return _ok(f"navigated to {_short(url)}{suffix}")


async def click(
    page: Page,
    selector: str,
    *,
    button: str = "left",
    click_count: int = 1,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Click the first element matching ``selector``."""
    if (e := _arg_err("click", "selector", selector, _v_selector)):
        return _fail(e)
    if (e := _arg_err("click", "button", button, _choice(*_MOUSE_BUTTONS))):
        return _fail(e)
    if (e := _arg_err("click", "click_count", click_count, _int_between(1, _MAX_CLICK_COUNT))):
        return _fail(e)
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    err, _ = await _call(
        lambda: page.locator(selector).first.click(
            button=cast(_MouseButton, button), click_count=click_count, timeout=t,
        ),
        timeout_ms=t, what=f"click {_short(selector)!r}",
    )
    return _fail(err) if err else _ok(f"clicked {_short(selector)!r}")


async def double_click(page: Page, selector: str, *, timeout_ms: int | None = None) -> dict[str, Any]:
    if (e := _arg_err("double_click", "selector", selector, _v_selector)):
        return _fail(e)
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    err, _ = await _call(
        lambda: page.locator(selector).first.dblclick(timeout=t),
        timeout_ms=t, what=f"double_click {_short(selector)!r}",
    )
    return _fail(err) if err else _ok(f"double-clicked {_short(selector)!r}")


async def type_text(
    page: Page,
    selector: str,
    text: str,
    *,
    delay_ms: int = _DEFAULT_DELAY_MS,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Type like a human — focuses the element, fires per-char key events.

    Appends at the current caret; use ``fill`` with ``""`` first to replace.
    """
    if (e := _arg_err("type_text", "selector", selector, _v_selector)):
        return _fail(e)
    if (e := _arg_err("type_text", "text", text, _str_max(MAX_TYPE_LEN))):
        return _fail(e)
    if (e := _arg_err("type_text", "delay_ms", delay_ms, _int_between(0, _MAX_DELAY_MS))):
        return _fail(e)
    if len(text) * delay_ms > MAX_TYPE_WALL_MS:
        return _fail(f"type_text: len(text) x delay_ms exceeds the {MAX_TYPE_WALL_MS}ms typing budget")
    t = _t(timeout_ms, _TYPE_TIMEOUT_MS)
    err, _ = await _call(
        lambda: page.locator(selector).first.press_sequentially(text, delay=delay_ms, timeout=t),
        timeout_ms=t, what=f"type into {_short(selector)!r}",
    )
    return _fail(err) if err else _ok(f"typed {len(text)} chars into {_short(selector)!r}")


async def fill(page: Page, selector: str, value: str, *, timeout_ms: int | None = None) -> dict[str, Any]:
    """Fast form set (no key events). ``value=""`` clears the field."""
    if (e := _arg_err("fill", "selector", selector, _v_selector)):
        return _fail(e)
    if (e := _arg_err("fill", "value", value, _str_max(MAX_FILL_LEN, allow_empty=True))):
        return _fail(e)
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    err, _ = await _call(
        lambda: page.locator(selector).first.fill(value, timeout=t),
        timeout_ms=t, what=f"fill {_short(selector)!r}",
    )
    return _fail(err) if err else _ok(f"filled {_short(selector)!r} ({len(value)} chars)")


async def press_key(
    page: Page,
    key: str,
    *,
    selector: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Press a key / chord ('Enter', 'Tab', 'ctrl+a'), page-wide or on an element."""
    normalized = _normalize_key(key) if isinstance(key, str) else None
    if normalized is None:
        return _fail(f"press_key: 'key' is not a valid key combo: {_short(str(key))!r}")
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    if selector is not None:
        if (e := _arg_err("press_key", "selector", selector, _v_selector)):
            return _fail(e)
        sel = selector
        err, _ = await _call(
            lambda: page.locator(sel).first.press(normalized, timeout=t),
            timeout_ms=t, what=f"press {normalized!r} on {_short(sel)!r}",
        )
        return _fail(err) if err else _ok(f"pressed {normalized!r} on {_short(sel)!r}")
    err, _ = await _call(
        lambda: page.keyboard.press(normalized),
        timeout_ms=t, what=f"press {normalized!r}",
    )
    return _fail(err) if err else _ok(f"pressed {normalized!r}")


async def hover(page: Page, selector: str, *, timeout_ms: int | None = None) -> dict[str, Any]:
    if (e := _arg_err("hover", "selector", selector, _v_selector)):
        return _fail(e)
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    err, _ = await _call(
        lambda: page.locator(selector).first.hover(timeout=t),
        timeout_ms=t, what=f"hover {_short(selector)!r}",
    )
    return _fail(err) if err else _ok(f"hovering {_short(selector)!r}")


async def scroll(
    page: Page,
    *,
    direction: str | None = None,
    to_selector: str | None = None,
    pixels: int | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Scroll by wheel (direction + pixels) or bring an element into view.

    Implemented via ``mouse.wheel`` / ``scroll_into_view_if_needed`` — never
    evaluated JS (see module security notes).
    """
    if (direction is None) == (to_selector is None):
        return _fail("scroll: give exactly one of 'direction' or 'to_selector'")
    if pixels is not None and direction is None:
        return _fail("scroll: 'pixels' is only valid together with 'direction'")
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    if to_selector is not None:
        if (e := _arg_err("scroll", "to_selector", to_selector, _v_selector)):
            return _fail(e)
        sel = to_selector
        err, _ = await _call(
            lambda: page.locator(sel).first.scroll_into_view_if_needed(timeout=t),
            timeout_ms=t, what=f"scroll to {_short(sel)!r}",
        )
        return _fail(err) if err else _ok(f"scrolled to {_short(sel)!r}")
    if direction not in ("up", "down"):
        return _fail("scroll: 'direction' must be one of ('up', 'down')")
    px = _DEFAULT_SCROLL_PIXELS if pixels is None else pixels
    if (e := _arg_err("scroll", "pixels", px, _int_between(1, MAX_SCROLL_PIXELS))):
        return _fail(e)
    dy = px if direction == "down" else -px
    err, _ = await _call(
        lambda: page.mouse.wheel(0, dy),
        timeout_ms=t, what=f"scroll {direction}",
    )
    return _fail(err) if err else _ok(f"scrolled {direction} {px}px")


async def select_option(
    page: Page,
    selector: str,
    *,
    value: str | None = None,
    label: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Select a ``<select>`` option by value or by visible label (exactly one)."""
    if (e := _arg_err("select_option", "selector", selector, _v_selector)):
        return _fail(e)
    if (value is None) == (label is None):
        return _fail("select_option: give exactly one of 'value' or 'label'")
    chosen = value if value is not None else label
    if (e := _arg_err("select_option", "value" if value is not None else "label",
                      chosen, _str_max(MAX_OPTION_LEN))):
        return _fail(e)
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    if value is not None:
        err, res = await _call(
            lambda: page.locator(selector).first.select_option(value=value, timeout=t),
            timeout_ms=t, what=f"select_option in {_short(selector)!r}",
        )
    else:
        err, res = await _call(
            lambda: page.locator(selector).first.select_option(label=label, timeout=t),
            timeout_ms=t, what=f"select_option in {_short(selector)!r}",
        )
    if err:
        return _fail(err)
    # Option values come from the page — bound them like the other getters do.
    selected = [str(v)[:MAX_TEXT_RETURN] for v in res[:MAX_SELECTED_OPTIONS]] if isinstance(res, list) else []
    return _ok(f"selected option in {_short(selector)!r}", value=selected)


async def check(page: Page, selector: str, *, timeout_ms: int | None = None) -> dict[str, Any]:
    if (e := _arg_err("check", "selector", selector, _v_selector)):
        return _fail(e)
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    err, _ = await _call(
        lambda: page.locator(selector).first.check(timeout=t),
        timeout_ms=t, what=f"check {_short(selector)!r}",
    )
    return _fail(err) if err else _ok(f"checked {_short(selector)!r}")


async def uncheck(page: Page, selector: str, *, timeout_ms: int | None = None) -> dict[str, Any]:
    if (e := _arg_err("uncheck", "selector", selector, _v_selector)):
        return _fail(e)
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    err, _ = await _call(
        lambda: page.locator(selector).first.uncheck(timeout=t),
        timeout_ms=t, what=f"uncheck {_short(selector)!r}",
    )
    return _fail(err) if err else _ok(f"unchecked {_short(selector)!r}")


async def wait_for(
    page: Page,
    *,
    selector: str | None = None,
    load_state: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Wait for a selector to be visible, a load state, or plain milliseconds.

    A missing selector FAILS the step — this is the gate to use before acting
    on dynamic content (contrast with ``element_exists``, which reports
    absence as data and never fails the batch).
    """
    if selector is not None and load_state is not None:
        return _fail("wait_for: 'selector' and 'load_state' are mutually exclusive")
    if selector is not None:
        if (e := _arg_err("wait_for", "selector", selector, _v_selector)):
            return _fail(e)
        sel = selector
        t = _t(timeout_ms, _NAV_TIMEOUT_MS)
        err, _ = await _call(
            lambda: page.locator(sel).first.wait_for(state="visible", timeout=t),
            timeout_ms=t, what=f"wait_for {_short(sel)!r}",
        )
        return _fail(err) if err else _ok(f"{_short(sel)!r} is visible")
    if load_state is not None:
        if (e := _arg_err("wait_for", "load_state", load_state, _choice(*_LOAD_STATES))):
            return _fail(e)
        t = _t(timeout_ms, _NAV_TIMEOUT_MS)
        state = cast(_LoadState, load_state)
        err, _ = await _call(
            lambda: page.wait_for_load_state(state, timeout=t),
            timeout_ms=t, what=f"wait_for load_state {load_state!r}",
        )
        return _fail(err) if err else _ok(f"load state {load_state!r} reached")
    if timeout_ms is None:
        return _fail("wait_for: give one of 'selector', 'load_state' or 'timeout_ms'")
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    await asyncio.sleep(t / 1000)
    return _ok(f"waited {t}ms")


async def go_back(page: Page, *, timeout_ms: int | None = None) -> dict[str, Any]:
    t = _t(timeout_ms, _NAV_TIMEOUT_MS)
    err, resp = await _call(
        lambda: page.go_back(wait_until="domcontentloaded", timeout=t),
        timeout_ms=t, what="go_back",
    )
    if err:
        return _fail(err)
    if resp is None:
        return _fail("go_back: no previous page in history")
    return _ok("went back")


async def go_forward(page: Page, *, timeout_ms: int | None = None) -> dict[str, Any]:
    t = _t(timeout_ms, _NAV_TIMEOUT_MS)
    err, resp = await _call(
        lambda: page.go_forward(wait_until="domcontentloaded", timeout=t),
        timeout_ms=t, what="go_forward",
    )
    if err:
        return _fail(err)
    if resp is None:
        return _fail("go_forward: no next page in history")
    return _ok("went forward")


async def reload(page: Page, *, timeout_ms: int | None = None) -> dict[str, Any]:
    t = _t(timeout_ms, _NAV_TIMEOUT_MS)
    err, _ = await _call(
        lambda: page.reload(wait_until="domcontentloaded", timeout=t),
        timeout_ms=t, what="reload",
    )
    return _fail(err) if err else _ok("reloaded")


async def get_text(page: Page, selector: str, *, timeout_ms: int | None = None) -> dict[str, Any]:
    """Visible text of the first match, truncated to MAX_TEXT_RETURN chars."""
    if (e := _arg_err("get_text", "selector", selector, _v_selector)):
        return _fail(e)
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    err, res = await _call(
        lambda: page.locator(selector).first.inner_text(timeout=t),
        timeout_ms=t, what=f"get_text {_short(selector)!r}",
    )
    if err:
        return _fail(err)
    text = res if isinstance(res, str) else str(res)
    truncated = len(text) > MAX_TEXT_RETURN
    if truncated:
        text = text[:MAX_TEXT_RETURN]
    note = ", truncated" if truncated else ""
    return _ok(f"got text ({len(text)} chars{note})", value=text)


async def get_attribute(
    page: Page,
    selector: str,
    name: str,
    *,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Attribute of the first match. Absent attribute is ok=True, value=None."""
    if (e := _arg_err("get_attribute", "selector", selector, _v_selector)):
        return _fail(e)
    if (e := _arg_err("get_attribute", "name", name, _v_attr_name)):
        return _fail(e)
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    err, res = await _call(
        lambda: page.locator(selector).first.get_attribute(name, timeout=t),
        timeout_ms=t, what=f"get_attribute {name!r} of {_short(selector)!r}",
    )
    if err:
        return _fail(err)
    if res is None:
        return _ok(f"attribute {name!r} is absent", value=None)
    val = res if isinstance(res, str) else str(res)
    return _ok(f"attribute {name!r} read", value=val[:MAX_TEXT_RETURN])


async def element_exists(page: Page, selector: str, *, timeout_ms: int | None = None) -> dict[str, Any]:
    """Instant existence probe. Zero matches is ok=True with value=False —
    existence is data, not failure, so it never stops a batch (use ``wait_for``
    to gate)."""
    if (e := _arg_err("element_exists", "selector", selector, _v_selector)):
        return _fail(e)
    t = _t(timeout_ms, _DEFAULT_TIMEOUT_MS)
    err, res = await _call(
        lambda: page.locator(selector).count(),
        timeout_ms=t, what=f"element_exists {_short(selector)!r}",
    )
    if err:
        return _fail(err)
    count = res if isinstance(res, int) else 0
    return _ok(f"{count} match(es) for {_short(selector)!r}", value=count > 0)


# ---------------------------------------------------------------- batch validation

@dataclass(frozen=True)
class _Spec:
    required: dict[str, _Checker]
    optional: dict[str, _Checker]
    # Cross-arg rule, run only after all per-arg checks passed for the step.
    cross: Callable[[dict[str, Any]], str | None] | None = None


def _cross_type_text(step: dict[str, Any]) -> str | None:
    wall = len(step["text"]) * int(step.get("delay_ms", _DEFAULT_DELAY_MS))
    if wall > MAX_TYPE_WALL_MS:
        return f"len(text) x delay_ms is ~{wall}ms — over the {MAX_TYPE_WALL_MS}ms typing budget"
    return None


def _cross_scroll(step: dict[str, Any]) -> str | None:
    has_dir, has_sel = "direction" in step, "to_selector" in step
    if has_dir == has_sel:
        return "requires exactly one of 'direction' or 'to_selector'"
    if "pixels" in step and not has_dir:
        return "'pixels' is only valid together with 'direction'"
    return None


def _cross_select_option(step: dict[str, Any]) -> str | None:
    if ("value" in step) == ("label" in step):
        return "requires exactly one of 'value' or 'label'"
    return None


def _cross_wait_for(step: dict[str, Any]) -> str | None:
    if "selector" in step and "load_state" in step:
        return "'selector' and 'load_state' are mutually exclusive"
    if not ({"selector", "load_state", "timeout_ms"} & step.keys()):
        return "requires one of 'selector', 'load_state' or 'timeout_ms'"
    return None


_SPECS: dict[str, _Spec] = {
    "navigate": _Spec(required={"url": _v_url}, optional={}),
    "click": _Spec(
        required={"selector": _v_selector},
        optional={"button": _choice(*_MOUSE_BUTTONS), "click_count": _int_between(1, _MAX_CLICK_COUNT)},
    ),
    "double_click": _Spec(required={"selector": _v_selector}, optional={}),
    "type_text": _Spec(
        required={"selector": _v_selector, "text": _str_max(MAX_TYPE_LEN)},
        optional={"delay_ms": _int_between(0, _MAX_DELAY_MS)},
        cross=_cross_type_text,
    ),
    "fill": _Spec(
        required={"selector": _v_selector, "value": _str_max(MAX_FILL_LEN, allow_empty=True)},
        optional={},
    ),
    "press_key": _Spec(required={"key": _v_key}, optional={"selector": _v_selector}),
    "hover": _Spec(required={"selector": _v_selector}, optional={}),
    "scroll": _Spec(
        required={},
        optional={
            "direction": _choice("up", "down"),
            "to_selector": _v_selector,
            "pixels": _int_between(1, MAX_SCROLL_PIXELS),
        },
        cross=_cross_scroll,
    ),
    "select_option": _Spec(
        required={"selector": _v_selector},
        optional={"value": _str_max(MAX_OPTION_LEN), "label": _str_max(MAX_OPTION_LEN)},
        cross=_cross_select_option,
    ),
    "check": _Spec(required={"selector": _v_selector}, optional={}),
    "uncheck": _Spec(required={"selector": _v_selector}, optional={}),
    "wait_for": _Spec(
        required={},
        optional={"selector": _v_selector, "load_state": _choice(*_LOAD_STATES)},
        cross=_cross_wait_for,
    ),
    "go_back": _Spec(required={}, optional={}),
    "go_forward": _Spec(required={}, optional={}),
    "reload": _Spec(required={}, optional={}),
    "get_text": _Spec(required={"selector": _v_selector}, optional={}),
    "get_attribute": _Spec(required={"selector": _v_selector, "name": _v_attr_name}, optional={}),
    "element_exists": _Spec(required={"selector": _v_selector}, optional={}),
}
# Every action accepts a per-step timeout override.
for _spec in _SPECS.values():
    _spec.optional.setdefault("timeout_ms", _v_timeout)

ACTION_NAMES: tuple[str, ...] = tuple(sorted(_SPECS))

# Actions whose default budget differs from _DEFAULT_TIMEOUT_MS (used by the
# batch executor when a step carries no explicit timeout_ms).
_DEFAULT_STEP_TIMEOUTS: dict[str, int] = {
    "navigate": _NAV_TIMEOUT_MS,
    "go_back": _NAV_TIMEOUT_MS,
    "go_forward": _NAV_TIMEOUT_MS,
    "reload": _NAV_TIMEOUT_MS,
    "wait_for": _NAV_TIMEOUT_MS,
    "type_text": _TYPE_TIMEOUT_MS,
}


def validate_actions(actions: Any) -> list[str]:
    """Validate a batch WITHOUT touching the page. Empty list == valid.

    Rejects unknown actions, unknown/missing/mistyped args, absurd values and
    oversized batches — so a bad script fails fast instead of half-executing.
    """
    if not isinstance(actions, list):
        return ["actions must be a list of step objects"]
    if not actions:
        return ["actions list is empty"]
    if len(actions) > MAX_ACTIONS_PER_BATCH:
        return [f"too many actions: {len(actions)} > max {MAX_ACTIONS_PER_BATCH}"]
    errors: list[str] = []
    for i, step in enumerate(actions):
        if not isinstance(step, dict):
            errors.append(f"step {i}: must be an object, got {type(step).__name__}")
            continue
        name = step.get("action")
        if not isinstance(name, str) or name not in _SPECS:
            # _short: the name is caller-supplied — never echo it back unbounded.
            errors.append(f"step {i}: unknown action {_short(repr(name), 60)} (allowed: {', '.join(ACTION_NAMES)})")
            continue
        spec = _SPECS[name]
        before = len(errors)
        allowed = {"action", *spec.required, *spec.optional}
        for key in step:
            if key not in allowed:
                errors.append(f"step {i} ({name}): unknown arg {_short(repr(key), 60)}")
        for arg, checker in spec.required.items():
            if arg not in step:
                errors.append(f"step {i} ({name}): missing required arg {arg!r}")
            elif (msg := checker(step[arg])):
                errors.append(f"step {i} ({name}): {arg!r} {msg}")
        for arg, checker in spec.optional.items():
            if arg in step and (msg := checker(step[arg])):
                errors.append(f"step {i} ({name}): {arg!r} {msg}")
        if spec.cross is not None and len(errors) == before and (msg := spec.cross(step)):
            errors.append(f"step {i} ({name}): {msg}")
    return errors


# ---------------------------------------------------------------- batch executor

_REGISTRY: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "navigate": navigate,
    "click": click,
    "double_click": double_click,
    "type_text": type_text,
    "fill": fill,
    "press_key": press_key,
    "hover": hover,
    "scroll": scroll,
    "select_option": select_option,
    "check": check,
    "uncheck": uncheck,
    "wait_for": wait_for,
    "go_back": go_back,
    "go_forward": go_forward,
    "reload": reload,
    "get_text": get_text,
    "get_attribute": get_attribute,
    "element_exists": element_exists,
}


async def _page_state(page: Page) -> tuple[str | None, str | None]:
    """Best-effort final url/title — never lets a dead page break the report."""
    url_attr = getattr(page, "url", None)
    url = url_attr if isinstance(url_attr, str) else None
    try:
        t = await asyncio.wait_for(page.title(), timeout=2.0)
        title = t if isinstance(t, str) else None
    except Exception:
        title = None
    return url, title


def _validation_reply(errors: list[str], total: int) -> dict[str, Any]:
    head = "; ".join(errors[:8]) + (" …" if len(errors) > 8 else "")
    return {
        "ok": False, "completed": 0, "total": total, "steps": [],
        "url": None, "title": None, "elapsed_ms": 0,
        "error": f"validation failed: {head}", "errors": errors,
    }


async def run_actions(
    page: Page,
    actions: list[dict[str, Any]],
    *,
    timeout: float = 20.0,
    continue_on_error: bool = False,
) -> dict[str, Any]:
    """Run a validated sequence of actions on one page under one deadline.

    - Validates EVERY step first; any error means nothing runs at all.
    - Runs steps in order. A failed step stops the batch unless
      ``continue_on_error=True`` (deadline exhaustion always stops it).
    - Per-step budget = min(step ``timeout_ms`` or the action default,
      remaining batch budget) — no step can overdraw the batch.
    - Never closes the page; the pool's ``acquire()`` context owns the tab.

    Returns ``{ok, completed, total, steps, url, title, elapsed_ms, error}``
    where each entry of ``steps`` is ``{index, action, ok, detail, elapsed_ms}``
    (+ ``value`` for getters). Steps that never ran are listed with detail
    ``"skipped: …"`` so the caller can see exactly which steps executed.
    """
    total = len(actions) if isinstance(actions, list) else 0
    errors = validate_actions(actions)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not 0 < float(timeout) <= MAX_BATCH_TIMEOUT_SECS:
        errors = [*errors, f"timeout must be a number in (0, {MAX_BATCH_TIMEOUT_SECS:g}] seconds"]
    if errors:
        return _validation_reply(errors, total)

    batch_start = time.monotonic()
    deadline = batch_start + float(timeout)
    steps_out: list[dict[str, Any]] = []
    completed = 0
    stopped: str | None = None

    for i, step in enumerate(actions):
        name = str(step["action"])
        if stopped is not None:
            steps_out.append({"index": i, "action": name, "ok": False,
                              "detail": f"skipped: {stopped}", "elapsed_ms": 0})
            continue
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms < _MIN_REMAINING_MS:
            stopped = "batch deadline exceeded"
            steps_out.append({"index": i, "action": name, "ok": False,
                              "detail": f"skipped: {stopped}", "elapsed_ms": 0})
            continue

        kwargs = {k: v for k, v in step.items() if k != "action"}
        requested = kwargs.get("timeout_ms")
        base = requested if isinstance(requested, int) else _DEFAULT_STEP_TIMEOUTS.get(name, _DEFAULT_TIMEOUT_MS)
        kwargs["timeout_ms"] = max(1, min(base, remaining_ms))

        t0 = time.monotonic()
        try:
            # Outer stop: the step (incl. its internal grace) must land inside
            # the batch budget even if its own guards misbehave.
            result = await asyncio.wait_for(
                _REGISTRY[name](page, **kwargs), timeout=remaining_ms / 1000 + 0.25,
            )
        except asyncio.TimeoutError:
            result = _fail(f"{name}: cut off by batch deadline")
        except Exception as exc:  # defensive — an action bug must not kill the batch
            result = _fail(f"{name}: {type(exc).__name__}: {_first_line(exc)}")
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        completed += 1

        entry: dict[str, Any] = {
            "index": i, "action": name, "ok": bool(result.get("ok")),
            "detail": str(result.get("detail", "")), "elapsed_ms": elapsed_ms,
        }
        if "value" in result:
            entry["value"] = result["value"]
        steps_out.append(entry)

        if not entry["ok"] and not continue_on_error:
            stopped = f"stopped at step {i} ({name}): {_short(entry['detail'], 120)}"

    url, title = await _page_state(page)
    return {
        "ok": stopped is None and all(s["ok"] for s in steps_out),
        "completed": completed,
        "total": total,
        "steps": steps_out,
        "url": url,
        "title": title,
        "elapsed_ms": int((time.monotonic() - batch_start) * 1000),
        "error": stopped,
    }
