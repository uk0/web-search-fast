"""Per-engine health tracking and circuit breaking.

Google is CAPTCHA-blocked on some egress IPs: every attempt still burns ~2-3s
navigating before the block is detected and the chain falls back. This module
remembers that verdict so the next request drops the doomed engine from the
chain outright, then probes it again once a cooldown expires so recovery is
automatic when the IP (or Google's mood) changes.

Pure in-process state — no Redis, no I/O, no background tasks. The clock is
injectable (``set_clock``) so tests never sleep.
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Literal, TypeVar

Outcome = Literal["success", "error", "blocked"]
BreakerState = Literal["closed", "open", "half_open"]

KNOWN_ENGINES: tuple[str, ...] = ("google", "bing", "duckduckgo")

# A generic error is worth 1.0 and three of them trip the breaker. A CAPTCHA /
# bot-block is a definitive verdict about this egress IP rather than a flake, so
# it carries the whole threshold on its own and trips on the first occurrence.
FAILURE_THRESHOLD = 3.0
BLOCK_WEIGHT = 3.0

BASE_COOLDOWN_S = 60.0
COOLDOWN_FACTOR = 2.0
MAX_COOLDOWN_S = 900.0
# Blocks almost never clear inside a minute — start them one step up the curve.
BLOCK_COOLDOWN_MULTIPLIER = 2.0

# A probe whose caller never reports back must not wedge the breaker shut.
PROBE_LEASE_S = 60.0

EWMA_ALPHA = 0.3
WINDOW_SIZE = 50

EngineT = TypeVar("EngineT", bound=str)

_clock: Callable[[], float] = time.monotonic
_LOCK = RLock()


@dataclass
class _EngineHealth:
    consecutive_failures: int = 0
    failure_score: float = 0.0
    open_streak: int = 0  # trips since the last close — drives the exponential backoff
    open_until: float | None = None  # None == closed
    cooldown_s: float = 0.0
    probe_until: float | None = None  # probe lease; not None == a probe is in flight
    ewma_latency_ms: float | None = None
    total_success: int = 0
    total_error: int = 0
    total_blocked: int = 0
    last_outcome: Outcome | None = None
    last_outcome_at: float | None = None
    window: deque[tuple[float, Outcome]] = field(default_factory=lambda: deque(maxlen=WINDOW_SIZE))


_STATES: dict[str, _EngineHealth] = {}


def _key(engine: str) -> str:
    """Normalise an engine id — accepts plain strings and ``str``-mixin enums."""
    return str(getattr(engine, "value", engine)).strip().lower()


def _state(name: str) -> _EngineHealth:
    st = _STATES.get(name)
    if st is None:
        st = _STATES[name] = _EngineHealth()
    return st


def _derive(st: _EngineHealth, now: float) -> BreakerState:
    if st.open_until is None:
        return "closed"
    return "open" if now < st.open_until else "half_open"


def _success_rate(st: _EngineHealth) -> float | None:
    if not st.window:
        return None
    hits = sum(1 for _, outcome in st.window if outcome == "success")
    return hits / len(st.window)


def _trip(st: _EngineHealth, now: float, blocked: bool) -> None:
    st.open_streak += 1
    # Clamp the exponent. The cooldown saturates at MAX_COOLDOWN_S after ~5 trips
    # anyway, and an unclamped streak overflows float pow (2.0 ** 1024 raises
    # OverflowError) on an engine that has been down for ~10 days straight.
    cooldown = BASE_COOLDOWN_S * (COOLDOWN_FACTOR ** min(st.open_streak - 1, 32))
    if blocked:
        cooldown *= BLOCK_COOLDOWN_MULTIPLIER
    st.cooldown_s = min(cooldown, MAX_COOLDOWN_S)
    st.open_until = now + st.cooldown_s
    st.failure_score = 0.0


def set_clock(clock: Callable[[], float] | None) -> None:
    """Swap the time source (tests). ``None`` restores ``time.monotonic``."""
    global _clock
    _clock = clock or time.monotonic


def reset_health() -> None:
    """Drop all recorded health state. Leaves the injected clock alone."""
    with _LOCK:
        _STATES.clear()


def record_success(engine: str, latency_ms: float) -> None:
    """Record a successful search — closes the breaker and resets the backoff."""
    with _LOCK:
        now = _clock()
        st = _state(_key(engine))
        st.consecutive_failures = 0
        st.failure_score = 0.0
        st.open_until = None
        st.open_streak = 0
        st.cooldown_s = 0.0
        st.probe_until = None
        st.total_success += 1
        st.last_outcome = "success"
        st.last_outcome_at = now
        st.window.append((now, "success"))
        st.ewma_latency_ms = (
            latency_ms
            if st.ewma_latency_ms is None
            else EWMA_ALPHA * latency_ms + (1.0 - EWMA_ALPHA) * st.ewma_latency_ms
        )


def record_failure(engine: str, *, blocked: bool = False) -> None:
    """Record a failed search. ``blocked=True`` (CAPTCHA / bot wall) trips instantly."""
    with _LOCK:
        now = _clock()
        st = _state(_key(engine))
        outcome: Outcome = "blocked" if blocked else "error"
        st.consecutive_failures += 1
        st.failure_score += BLOCK_WEIGHT if blocked else 1.0
        st.total_blocked += 1 if blocked else 0
        st.total_error += 0 if blocked else 1
        st.last_outcome = outcome
        st.last_outcome_at = now
        st.window.append((now, outcome))

        state = _derive(st, now)
        st.probe_until = None  # release the lease either way
        if state == "half_open":
            # The trial request failed: straight back to open, one step longer.
            _trip(st, now, blocked)
        elif state == "closed" and st.failure_score >= FAILURE_THRESHOLD:
            _trip(st, now, blocked)
        # state == "open": a caller ignored the breaker (forced head-of-chain
        # attempt). Recorded, but the running cooldown is left alone so
        # concurrent forced attempts cannot escalate it several steps at once.


def should_skip(engine: str) -> bool:
    """True when the breaker says don't bother trying this engine right now.

    Admission gate, not a pure predicate: once the cooldown expires this hands
    out exactly one half-open probe lease, so concurrent callers don't all pile
    onto an engine that is probably still broken.
    """
    with _LOCK:
        now = _clock()
        st = _state(_key(engine))
        state = _derive(st, now)
        if state == "closed":
            return False
        if state == "open":
            return True
        if st.probe_until is not None and now < st.probe_until:
            return True  # someone else is already probing
        st.probe_until = now + PROBE_LEASE_S
        return False


def order_engines(preferred: EngineT, fallbacks: Sequence[EngineT]) -> list[EngineT]:
    """Build the engine chain to try, healthiest first.

    Open (cooling) engines are dropped. An explicitly preferred engine keeps the
    head of the chain whenever its breaker isn't open; fallbacks are ranked by
    breaker state, then success rate, then EWMA latency. If *every* candidate is
    open the full chain is returned soonest-to-recover first — callers must
    always have something to try, so this never returns an empty list.
    """
    chain: list[EngineT] = []
    index: dict[str, int] = {}
    for engine in (preferred, *fallbacks):
        name = _key(engine)
        if name not in index:
            index[name] = len(chain)
            chain.append(engine)
    if not chain:
        return []

    with _LOCK:
        now = _clock()
        states = {name: _derive(_state(name), now) for name in index}

        def healthy_key(engine: EngineT) -> tuple[int, float, float, int]:
            name = _key(engine)
            st = _state(name)
            rate = _success_rate(st)
            return (
                0 if states[name] == "closed" else 1,
                -round(rate if rate is not None else 1.0, 1),
                st.ewma_latency_ms if st.ewma_latency_ms is not None else float("inf"),
                index[name],
            )

        def recovery_key(engine: EngineT) -> tuple[float, float, float, int]:
            name = _key(engine)
            st = _state(name)
            rate = _success_rate(st)
            remaining = (st.open_until or now) - now
            return (
                remaining,
                -round(rate if rate is not None else 1.0, 1),
                st.ewma_latency_ms if st.ewma_latency_ms is not None else float("inf"),
                index[name],
            )

        live = [engine for engine in chain if states[_key(engine)] != "open"]
        if not live:
            return sorted(chain, key=recovery_key)
        if _key(preferred) in {_key(engine) for engine in live}:
            rest = [engine for engine in live if _key(engine) != _key(preferred)]
            return [chain[index[_key(preferred)]], *sorted(rest, key=healthy_key)]
        return sorted(live, key=healthy_key)


def engine_health_stats() -> dict[str, dict[str, Any]]:
    """Per-engine breaker snapshot for the admin dashboard."""
    with _LOCK:
        now = _clock()
        names = list(KNOWN_ENGINES) + sorted(set(_STATES) - set(KNOWN_ENGINES))
        stats: dict[str, dict[str, Any]] = {}
        for name in names:
            st = _state(name)
            state = _derive(st, now)
            stats[name] = {
                "state": state,
                "consecutive_failures": st.consecutive_failures,
                "failure_score": round(st.failure_score, 2),
                "open_streak": st.open_streak,
                "cooldown_s": round(st.cooldown_s, 1),
                "cooldown_remaining_s": round(max(0.0, (st.open_until or now) - now), 1) if state == "open" else 0.0,
                "probe_in_flight": st.probe_until is not None and now < st.probe_until,
                "last_outcome": st.last_outcome,
                "last_outcome_at": st.last_outcome_at,
                "last_outcome_age_s": (
                    None if st.last_outcome_at is None else round(now - st.last_outcome_at, 1)
                ),
                "ewma_latency_ms": None if st.ewma_latency_ms is None else round(st.ewma_latency_ms, 1),
                "success_rate": None if _success_rate(st) is None else round(_success_rate(st) or 0.0, 3),
                "samples": len(st.window),
                "totals": {
                    "success": st.total_success,
                    "error": st.total_error,
                    "blocked": st.total_blocked,
                },
            }
        return stats


_BLOCK_MARKERS = (
    "/sorry/",
    "captcha",
    "unusual traffic",
    "are you a robot",
    "access denied",
    "403",
    "429",
    "too many requests",
    "rate limit",
)


def is_block_signal(exc: BaseException | None = None, *, text: str | None = None) -> bool:
    """Heuristic: does this failure look like a bot wall rather than a flake?

    Lets call sites map an exception (or a page URL / body snippet) onto the
    ``blocked=True`` fast trip without duplicating the marker list.
    """
    haystack = " ".join(part for part in (str(exc) if exc is not None else None, text) if part).lower()
    return any(marker in haystack for marker in _BLOCK_MARKERS)
