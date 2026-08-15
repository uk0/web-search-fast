"""Bounded readiness gate for JS-rendered pages.

``screenshot_page``, ``browser_actions`` and session ``open`` all navigate with
``wait_until="domcontentloaded"`` and then act immediately — on a JS-rendered
page that screenshots a skeleton and clicks elements that have not mounted.
``wait_until_ready`` closes the gap: it composes individually bounded waits
(DOM / load event / network idle / fonts / viewport images / DOM-quiet /
selector) under ONE overall budget and NEVER raises. A page that never goes
network-idle (websockets, long-polling, analytics beacons) is treated as
"good enough, proceed": the timeout is *reported* in the :class:`ReadyReport`,
never thrown at the caller — a gate that turns a working screenshot into an
error is worse than no gate.

Levels (escalating):
  ``dom``     — document reached ``interactive`` (what callers already have).
  ``load``    — the load event fired (markup subresources fetched).
  ``network`` — plus network idle (Playwright's 500ms quiet window).
  ``stable``  — plus fonts ready, viewport <img> decoded, and a DOM-quiet
                window (no mutations), i.e. hydration finished. The load event
                also guarantees ``document.readyState === "complete"``.

Env-tunable defaults: ``READINESS_LEVEL``, ``READINESS_TIMEOUT`` (seconds),
``READINESS_QUIET_MS``. Garbage / NaN / inf values log a warning and fall back.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

__all__ = ["LEVELS", "ReadyReport", "wait_until_ready"]

LEVELS: tuple[str, ...] = ("dom", "load", "network", "stable")

_DEFAULT_LEVEL = "stable"
_DEFAULT_TIMEOUT_S = 8.0
_DEFAULT_QUIET_MS = 500
_MIN_QUIET_MS = 50
_MAX_QUIET_MS = 10_000
_MIN_TIMEOUT_S = 0.05
_MAX_TIMEOUT_S = 300.0
# Below this budget only the "dom" check is worth running - degrade, don't overrun.
_DEGRADE_BUDGET_S = 0.5

# Per-phase cap as a fraction of the TOTAL budget, so no single sub-wait
# (networkidle is the notorious never-resolver) can eat the whole window.
_PHASE_FRACTION: dict[str, float] = {
    "dom": 0.5,
    "load": 0.5,
    "network_idle": 0.6,
    # An explicit selector IS the caller's readiness condition (the click/act
    # recipe), so it may use nearly the whole budget - halving it silently made
    # a `timeout=14s` gate give up on an element that mounts at 10s. Still
    # capped by the remaining budget, so the overall deadline is unchanged.
    "selector": 0.9,
    "fonts": 0.35,
    "images": 0.45,
    "dom_quiet": 0.9,
}

# All JS below is STATIC. Runtime values travel through evaluate's structured
# `arg` parameter - caller data (URLs, selectors) is never spliced into JS.
_JS_FONTS_READY = """
async () => {
  const fonts = document.fonts;
  // Pages without the Font Loading API have nothing to wait for.
  if (!fonts || !fonts.ready || typeof fonts.ready.then !== "function") return true;
  await fonts.ready;
  return true;
}
"""

_JS_VIEWPORT_IMAGES = """
async () => {
  if (!document.images) return true;
  const vh = window.innerHeight || 0;
  const vw = window.innerWidth || 0;
  const inViewport = Array.from(document.images).filter((el) => {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw && r.width > 0 && r.height > 0;
  });
  // Listeners are attached in the same synchronous block as the .complete
  // check, so a load event cannot slip between the two.
  await Promise.all(inViewport.map((el) =>
    el.complete
      ? Promise.resolve()
      : new Promise((res) => {
          el.addEventListener("load", res, { once: true });
          el.addEventListener("error", res, { once: true });
        })
  ));
  return inViewport.every((el) => el.complete && el.naturalWidth > 0);
}
"""

_JS_DOM_QUIET = """
(cfg) => new Promise((resolve) => {
  const quietMs = Math.max(50, Number(cfg && cfg.quietMs) || 0);
  const maxMs = Math.max(quietMs, Number(cfg && cfg.maxMs) || 0);
  let settleTimer = null;
  let hardTimer = null;
  let observer = null;
  const finish = (ok) => {
    if (observer) { try { observer.disconnect(); } catch (e) {} }
    if (window.__wsmcpReadinessMO === observer) { window.__wsmcpReadinessMO = null; }
    clearTimeout(settleTimer);
    clearTimeout(hardTimer);
    resolve(ok);
  };
  try {
    // A run cancelled mid-flight (caller-side cancellation) cannot reach
    // finish(), so its observer stays attached. Reclaim it here: at most one
    // readiness observer is ever live on a page, however often the gate runs.
    const stale = window.__wsmcpReadinessMO;
    if (stale) { try { stale.disconnect(); } catch (e) {} }
    observer = new MutationObserver(() => {
      clearTimeout(settleTimer);
      settleTimer = setTimeout(() => finish(true), quietMs);
    });
    window.__wsmcpReadinessMO = observer;
    observer.observe(document.documentElement || document, {
      subtree: true, childList: true, attributes: true, characterData: true,
    });
  } catch (e) {
    finish(true);  // no MutationObserver: nothing to measure, do not block
    return;
  }
  settleTimer = setTimeout(() => finish(true), quietMs);
  hardTimer = setTimeout(() => finish(false), maxMs);
})
"""


@dataclass(slots=True)
class ReadyReport:
    """Which readiness conditions held when the gate returned (it never raises).

    ``ready`` is True only when every condition of the *requested* level (and
    the selector, if one was given) was met. ``timed_out`` True with a usable
    page is the expected outcome on never-idle sites - proceed and surface
    ``detail`` so the model knows how settled the page is.
    """

    ready: bool = False
    level_requested: str = _DEFAULT_LEVEL
    level_reached: str = "none"
    dom: bool = False
    load: bool = False
    network_idle: bool = False
    fonts: bool = False
    images: bool = False
    dom_quiet: bool = False
    selector_found: bool | None = None  # None = no selector was requested
    timed_out: bool = False
    elapsed_ms: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _warn_fallback(name: str, raw: object, fallback: object) -> None:
    logger.warning("READINESS: invalid %s=%r; falling back to %r", name, raw, fallback)


def _env_level() -> str:
    raw = os.environ.get("READINESS_LEVEL")
    if raw is None or not raw.strip():
        return _DEFAULT_LEVEL
    val = raw.strip().lower()
    if val in LEVELS:
        return val
    _warn_fallback("READINESS_LEVEL", raw, _DEFAULT_LEVEL)
    return _DEFAULT_LEVEL


def _resolve_level(level: str | None) -> str:
    if level is None:
        return _env_level()
    val = level.strip().lower()
    if val in LEVELS:
        return val
    _warn_fallback("level", level, _DEFAULT_LEVEL)
    return _DEFAULT_LEVEL


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw)
    except ValueError:
        _warn_fallback(name, raw, default)
        return default
    if not math.isfinite(val) or not (lo <= val <= hi):
        _warn_fallback(name, raw, default)
        return default
    return val


def _resolve_timeout(timeout: float | None) -> float:
    if timeout is None:
        return _env_float("READINESS_TIMEOUT", _DEFAULT_TIMEOUT_S, _MIN_TIMEOUT_S, _MAX_TIMEOUT_S)
    try:
        val = float(timeout)
    except (TypeError, ValueError):
        _warn_fallback("timeout", timeout, _DEFAULT_TIMEOUT_S)
        return _DEFAULT_TIMEOUT_S
    if not math.isfinite(val):
        _warn_fallback("timeout", timeout, _DEFAULT_TIMEOUT_S)
        return _DEFAULT_TIMEOUT_S
    # <= 0 is a legitimate "budget already spent" signal, not garbage: return
    # immediately upstream instead of silently substituting the default.
    return max(0.0, min(val, _MAX_TIMEOUT_S))


def _resolve_quiet_ms(quiet_ms: int | None) -> int:
    if quiet_ms is None:
        return int(_env_float("READINESS_QUIET_MS", float(_DEFAULT_QUIET_MS), _MIN_QUIET_MS, _MAX_QUIET_MS))
    try:
        val = float(quiet_ms)
    except (TypeError, ValueError):
        _warn_fallback("quiet_ms", quiet_ms, _DEFAULT_QUIET_MS)
        return _DEFAULT_QUIET_MS
    if not math.isfinite(val) or not (_MIN_QUIET_MS <= val <= _MAX_QUIET_MS):
        _warn_fallback("quiet_ms", quiet_ms, _DEFAULT_QUIET_MS)
        return _DEFAULT_QUIET_MS
    return int(val)


def _ms(seconds: float) -> float:
    # Playwright treats timeout=0 as "no timeout" - never hand it 0.
    return max(1.0, seconds * 1000.0)


def _quiet_cfg(quiet_ms: int, slice_s: float) -> dict[str, int]:
    # The JS hard-stop fires slightly before the Python guard so we get a
    # graceful `false` back instead of a cancelled evaluate.
    max_ms = max(_MIN_QUIET_MS, int(slice_s * 1000) - 50)
    return {"quietMs": max(_MIN_QUIET_MS, min(quiet_ms, max_ms)), "maxMs": max_ms}


async def wait_until_ready(
    page: Page,
    *,
    level: str | None = None,
    timeout: float | None = None,
    selector: str | None = None,
    quiet_ms: int | None = None,
) -> ReadyReport:
    """Wait until *page* is ready enough for the requested *level*, never raising.

    Args:
        page: A live Playwright page the CALLER owns (already navigated).
            This function never calls ``goto`` and never closes the page.
        level: ``"dom" | "load" | "network" | "stable"``. ``None`` -> env
            ``READINESS_LEVEL`` -> ``"stable"``. Unknown values fall back.
        timeout: Overall budget in SECONDS (the caller's remaining budget).
            ``None`` -> env ``READINESS_TIMEOUT`` -> 8.0. ``<= 0`` returns
            immediately; below 0.5s the level degrades to ``"dom"``.
        selector: Optional extra gate - wait for it to be attached AND
            visible. Missing at timeout is reported, never raised.
        quiet_ms: DOM-quiet settle window for ``"stable"``. ``None`` -> env
            ``READINESS_QUIET_MS`` -> 500.

    Returns:
        ReadyReport - which conditions held, which timed out, elapsed time.
        Callers should proceed regardless and surface ``detail``.
    """
    start = time.monotonic()
    requested = _resolve_level(level)
    total_s = _resolve_timeout(timeout)
    q_ms = _resolve_quiet_ms(quiet_ms)

    flags: dict[str, bool] = dict.fromkeys(("dom", "load", "network_idle", "fonts", "images", "dom_quiet"), False)
    notes: list[str] = []
    timed_out = False
    selector_found: bool | None = None

    def _finish() -> ReadyReport:
        reached = "none"
        if flags["dom"]:
            reached = "dom"
            if flags["load"]:
                reached = "load"
                if flags["network_idle"]:
                    reached = "network"
                    if flags["fonts"] and flags["images"] and flags["dom_quiet"]:
                        reached = "stable"
        required = ["dom"]
        if requested in ("load", "network", "stable"):
            required.append("load")
        if requested in ("network", "stable"):
            required.append("network_idle")
        if requested == "stable":
            required += ["fonts", "images", "dom_quiet"]
        ready = all(flags[k] for k in required) and (selector is None or selector_found is True)
        report = ReadyReport(
            ready=ready,
            level_requested=requested,
            level_reached=reached,
            dom=flags["dom"],
            load=flags["load"],
            network_idle=flags["network_idle"],
            fonts=flags["fonts"],
            images=flags["images"],
            dom_quiet=flags["dom_quiet"],
            selector_found=selector_found,
            timed_out=timed_out,
            elapsed_ms=int((time.monotonic() - start) * 1000),
            detail="; ".join(notes) if notes else "ok",
        )
        logger.debug("[readiness] %s", report)
        return report

    if total_s <= 0.0:
        timed_out = True
        notes.append("no budget: all checks skipped")
        return _finish()

    effective = requested
    if total_s < _DEGRADE_BUDGET_S and requested != "dom":
        # Tiny budget: run the cheapest check instead of overrunning. `ready`
        # stays honest - it is computed against the REQUESTED level.
        effective = "dom"
        notes.append(f"budget {total_s:.2f}s: degraded {requested}->dom")

    deadline = start + total_s

    def _remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    async def _phase(
        name: str,
        factory: Callable[[float], Awaitable[Any]],
        *,
        false_is_timeout: bool = False,
    ) -> bool:
        """Run one bounded sub-wait; absorb every failure into the report."""
        nonlocal timed_out
        rem = _remaining()
        if rem <= 0.01:
            timed_out = True
            notes.append(f"{name}: skipped (budget exhausted)")
            return False
        slice_s = max(0.001, min(rem, total_s * _PHASE_FRACTION[name]))
        try:
            # asyncio.wait_for is the authority; the Playwright-side timeout
            # (same value) exists so Playwright can clean up its own waiter.
            result = await asyncio.wait_for(factory(slice_s), timeout=slice_s)
        except (TimeoutError, asyncio.TimeoutError):
            timed_out = True
            notes.append(f"{name}: timed out after {int(slice_s * 1000)}ms")
            return False
        except Exception as exc:
            # Playwright TimeoutError/Error, destroyed contexts, JS throws -
            # absorbed. Only outer cancellation (BaseException) propagates.
            if "Timeout" in type(exc).__name__:
                timed_out = True
            notes.append(f"{name}: {type(exc).__name__}")
            return False
        ok = result is None or bool(result)  # load-state waits return None on success
        if not ok:
            if false_is_timeout:
                timed_out = True
            notes.append(f"{name}: not settled")
            return False
        return True

    flags["dom"] = await _phase("dom", lambda s: page.wait_for_load_state("domcontentloaded", timeout=_ms(s)))
    if effective in ("load", "network", "stable"):
        flags["load"] = await _phase("load", lambda s: page.wait_for_load_state("load", timeout=_ms(s)))
    if effective in ("network", "stable"):
        # networkidle frequently NEVER resolves on real sites (websockets,
        # beacons) - its slice is capped so a dead wait still leaves budget
        # for the stable checks below.
        flags["network_idle"] = await _phase(
            "network_idle", lambda s: page.wait_for_load_state("networkidle", timeout=_ms(s))
        )
    if selector is not None:
        sel: str = selector
        selector_found = await _phase(
            "selector", lambda s: page.wait_for_selector(sel, state="visible", timeout=_ms(s))
        )
    if effective == "stable":
        # Load event implies document.readyState === "complete" (readyState
        # flips to complete immediately before `load` fires and never reverts),
        # so `load` above already covers the readyState condition of "stable".
        flags["fonts"] = await _phase("fonts", lambda s: page.evaluate(_JS_FONTS_READY))
        flags["images"] = await _phase("images", lambda s: page.evaluate(_JS_VIEWPORT_IMAGES))
        flags["dom_quiet"] = await _phase(
            "dom_quiet",
            lambda s: page.evaluate(_JS_DOM_QUIET, _quiet_cfg(q_ms, s)),
            false_is_timeout=True,  # JS `false` == no quiet window within its bound
        )
    return _finish()
