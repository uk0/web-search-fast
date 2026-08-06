"""Prometheus metrics + request correlation IDs.

Zero third-party deps: implements just enough of the Prometheus text
exposition format (version 0.0.4) for counters, gauges and histograms.
Every metric is a process-lifetime singleton created at import time; call
sites use the ``record_*`` / ``set_*`` helpers rather than the primitives.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
import uuid
from bisect import bisect_left
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
REQUEST_ID_HEADER = "X-Request-ID"

# Sane spread for a 0.1s-30s search: dense where p50 lives, coarse in the tail.
DEFAULT_LATENCY_BUCKETS: tuple[float, ...] = (
    0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 30.0, math.inf,
)

# A runaway label value (a query string, an IP) would leak memory and blow up
# the scrape payload, so every family is hard-capped and folds into one series.
MAX_SERIES_PER_METRIC = 128
OVERFLOW_LABEL = "__other__"
_MAX_LABEL_LEN = 48

_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_RESERVED_LABELS = frozenset({"le", "quantile"})

_KNOWN_ENGINES = frozenset({"google", "bing", "duckduckgo"})
_KNOWN_OUTCOMES = frozenset({"success", "blocked", "error", "timeout"})
OTHER = "other"


# --------------------------------------------------------------------------
# text-format primitives
# --------------------------------------------------------------------------

def _fmt_float(value: float) -> str:
    """Render a float the way Go/Prometheus does (`+Inf`, early exponents)."""
    value = float(value)
    if value == math.inf:
        return "+Inf"
    if value == -math.inf:
        return "-Inf"
    if math.isnan(value):
        return "NaN"
    s = repr(value)
    dot = s.find(".")
    # Go switches to exponent notation sooner than Python does.
    if value > 0 and dot > 6:
        mantissa = f"{s[0]}.{s[1:dot]}{s[dot + 1:]}".rstrip("0.")
        return f"{mantissa}e+0{dot - 1}"
    return s


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _sanitize_label_value(value: Any) -> str:
    """Bound the length of a label value; escaping happens at render time."""
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text[:_MAX_LABEL_LEN]


def _fmt_labels(pairs: Sequence[tuple[str, str]]) -> str:
    # An empty label set renders as a bare metric name, no braces.
    if not pairs:
        return ""
    body = ",".join(f'{name}="{_escape_label_value(value)}"' for name, value in pairs)
    return "{" + body + "}"


# --------------------------------------------------------------------------
# metric types
# --------------------------------------------------------------------------

Sample = tuple[str, tuple[tuple[str, str], ...], float]


class _Metric:
    """Shared label handling, locking and cardinality guard."""

    _type = "untyped"

    def __init__(self, name: str, documentation: str, labelnames: Sequence[str] = ()) -> None:
        if not _NAME_RE.match(name):
            raise ValueError(f"invalid metric name: {name!r}")
        for label in labelnames:
            if not _LABEL_RE.match(label) or label in _RESERVED_LABELS:
                raise ValueError(f"invalid label name: {label!r}")
        self.name = name
        self.documentation = documentation
        self.labelnames: tuple[str, ...] = tuple(labelnames)
        self._lock = threading.Lock()
        self._values: dict[tuple[str, ...], Any] = {}
        if not self.labelnames:
            self._values[()] = self._zero()

    def _zero(self) -> Any:
        return 0.0

    def _key(self, labels: Mapping[str, Any]) -> tuple[str, ...]:
        """Build the series key. Caller must hold ``self._lock``."""
        if len(labels) != len(self.labelnames) or set(labels) != set(self.labelnames):
            raise ValueError(f"{self.name} expects labels {self.labelnames}, got {tuple(labels)}")
        key = tuple(_sanitize_label_value(labels[name]) for name in self.labelnames)
        if key not in self._values and len(self._values) >= MAX_SERIES_PER_METRIC:
            return (OVERFLOW_LABEL,) * len(self.labelnames)
        return key

    def _label_pairs(self, key: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        return tuple(zip(self.labelnames, key))

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            if not self.labelnames:
                self._values[()] = self._zero()

    def collect(self) -> list[Sample]:  # pragma: no cover - overridden
        raise NotImplementedError


class Counter(_Metric):
    """Monotonic counter."""

    _type = "counter"

    def __init__(self, name: str, documentation: str, labelnames: Sequence[str] = ()) -> None:
        super().__init__(name, documentation, labelnames)
        self._synced: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, /, **labels: Any) -> None:
        if amount < 0:
            raise ValueError("counter increments must be non-negative")
        with self._lock:
            key = self._key(labels)
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def sync(self, total: float, /, **labels: Any) -> None:
        """Mirror an externally-owned running total (e.g. a cache's own stats).

        Reset-aware: if the source restarts and its total rewinds, the new
        total is added as a delta instead of dragging this counter backwards.
        """
        total = float(total)
        with self._lock:
            key = self._key(labels)
            previous = self._synced.get(key, 0.0)
            delta = total - previous if total >= previous else total
            self._synced[key] = total
            self._values[key] = self._values.get(key, 0.0) + max(delta, 0.0)

    def value(self, **labels: Any) -> float:
        with self._lock:
            return float(self._values.get(self._key(labels), 0.0))

    def clear(self) -> None:
        super().clear()
        with self._lock:
            self._synced.clear()

    def collect(self) -> list[Sample]:
        with self._lock:
            items = sorted(self._values.items())
        return [(self.name, self._label_pairs(key), float(value)) for key, value in items]


class Gauge(_Metric):
    """Instantaneous value that can go up or down."""

    _type = "gauge"

    def set(self, value: float, /, **labels: Any) -> None:
        with self._lock:
            self._values[self._key(labels)] = float(value)

    def inc(self, amount: float = 1.0, /, **labels: Any) -> None:
        with self._lock:
            key = self._key(labels)
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def dec(self, amount: float = 1.0, /, **labels: Any) -> None:
        self.inc(-amount, **labels)

    def value(self, **labels: Any) -> float:
        with self._lock:
            return float(self._values.get(self._key(labels), 0.0))

    def collect(self) -> list[Sample]:
        with self._lock:
            items = sorted(self._values.items())
        return [(self.name, self._label_pairs(key), float(value)) for key, value in items]


@dataclass
class _Bucketed:
    counts: list[int]
    total: float = 0.0
    count: int = 0


class Histogram(_Metric):
    """Cumulative histogram with `_bucket` / `_sum` / `_count` series."""

    _type = "histogram"

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_LATENCY_BUCKETS,
    ) -> None:
        bounds = [float(b) for b in buckets]
        if not bounds:
            raise ValueError("histogram needs at least one bucket")
        if any(b <= a for a, b in zip(bounds, bounds[1:])):
            raise ValueError("histogram buckets must be strictly ascending")
        if bounds[-1] != math.inf:
            bounds.append(math.inf)
        self.upper_bounds: tuple[float, ...] = tuple(bounds)
        super().__init__(name, documentation, labelnames)

    def _zero(self) -> _Bucketed:
        return _Bucketed(counts=[0] * len(self.upper_bounds))

    def observe(self, value: float, /, **labels: Any) -> None:
        value = float(value)
        # A NaN would poison _sum forever; drop it rather than raise on a hot path.
        if math.isnan(value):
            return
        index = bisect_left(self.upper_bounds, value)
        with self._lock:
            key = self._key(labels)
            entry = self._values.get(key)
            if entry is None:
                entry = self._zero()
                self._values[key] = entry
            entry.counts[index] += 1
            entry.total += value
            entry.count += 1

    def sample_count(self, **labels: Any) -> int:
        with self._lock:
            entry = self._values.get(self._key(labels))
        return entry.count if entry else 0

    def sample_sum(self, **labels: Any) -> float:
        with self._lock:
            entry = self._values.get(self._key(labels))
        return entry.total if entry else 0.0

    def collect(self) -> list[Sample]:
        with self._lock:
            items = [(key, _Bucketed(list(e.counts), e.total, e.count)) for key, e in sorted(self._values.items())]
        samples: list[Sample] = []
        for key, entry in items:
            pairs = self._label_pairs(key)
            cumulative = 0
            for bound, count in zip(self.upper_bounds, entry.counts):
                cumulative += count
                samples.append((f"{self.name}_bucket", pairs + (("le", _fmt_float(bound)),), float(cumulative)))
            samples.append((f"{self.name}_sum", pairs, entry.total))
            samples.append((f"{self.name}_count", pairs, float(entry.count)))
        return samples


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

class MetricsRegistry:
    """Ordered collection of metric families plus scrape-time collectors."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}
        self._collectors: list[Callable[[], None]] = []

    def _get_or_create(self, metric: _Metric) -> _Metric:
        with self._lock:
            existing = self._metrics.get(metric.name)
            if existing is None:
                self._metrics[metric.name] = metric
                return metric
            if type(existing) is not type(metric) or existing.labelnames != metric.labelnames:
                raise ValueError(f"metric {metric.name!r} already registered with a different shape")
            return existing

    def counter(self, name: str, documentation: str, labelnames: Sequence[str] = ()) -> Counter:
        metric = self._get_or_create(Counter(name, documentation, labelnames))
        assert isinstance(metric, Counter)
        return metric

    def gauge(self, name: str, documentation: str, labelnames: Sequence[str] = ()) -> Gauge:
        metric = self._get_or_create(Gauge(name, documentation, labelnames))
        assert isinstance(metric, Gauge)
        return metric

    def histogram(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_LATENCY_BUCKETS,
    ) -> Histogram:
        metric = self._get_or_create(Histogram(name, documentation, labelnames, buckets))
        assert isinstance(metric, Histogram)
        return metric

    def register_collector(self, fn: Callable[[], None]) -> None:
        """Run `fn` immediately before each render — for pull-style gauges."""
        with self._lock:
            if fn not in self._collectors:
                self._collectors.append(fn)

    def unregister_collector(self, fn: Callable[[], None]) -> None:
        with self._lock:
            if fn in self._collectors:
                self._collectors.remove(fn)

    def reset(self) -> None:
        """Drop all sample values but keep registrations (test helper)."""
        with self._lock:
            metrics = list(self._metrics.values())
        for metric in metrics:
            metric.clear()

    def render(self) -> str:
        with self._lock:
            collectors = list(self._collectors)
            metrics = list(self._metrics.values())
        for collector in collectors:
            try:
                collector()
            except Exception as exc:  # a broken collector must not break the scrape
                logger.debug("[metrics] collector %r failed: %s", collector, exc)
        out: list[str] = []
        for metric in metrics:
            out.append(f"# HELP {metric.name} {_escape_help(metric.documentation)}\n")
            out.append(f"# TYPE {metric.name} {metric._type}\n")
            for name, pairs, value in metric.collect():
                out.append(f"{name}{_fmt_labels(pairs)} {_fmt_float(value)}\n")
        return "".join(out)


REGISTRY = MetricsRegistry()

SEARCH_REQUESTS = REGISTRY.counter(
    "wsm_search_requests_total",
    "Search requests by engine and outcome.",
    ("engine", "outcome"),
)
SEARCH_LATENCY = REGISTRY.histogram(
    "wsm_search_latency_seconds",
    "End-to-end search latency in seconds.",
    ("engine",),
    DEFAULT_LATENCY_BUCKETS,
)
CACHE_HITS = REGISTRY.counter("wsm_cache_hits_total", "Cache lookups served from cache.", ("cache",))
CACHE_MISSES = REGISTRY.counter("wsm_cache_misses_total", "Cache lookups that missed.", ("cache",))
POOL_ACTIVE_TABS = REGISTRY.gauge("wsm_browser_pool_active_tabs", "Browser tabs currently open.")
POOL_SIZE = REGISTRY.gauge("wsm_browser_pool_size", "Browser pool concurrency slots in use as the current limit.")
POOL_MAX_SIZE = REGISTRY.gauge("wsm_browser_pool_max_size", "Upper bound the pool may auto-scale to.")
POOL_GENERATION = REGISTRY.gauge("wsm_browser_pool_generation", "Browser instance generation, bumped on restart.")
RATE_LIMIT_THROTTLED = REGISTRY.counter(
    "wsm_rate_limit_throttled_total",
    "Requests rejected by the rate limiter.",
    ("scope",),
)
CIRCUIT_BREAKER_TRIPS = REGISTRY.counter(
    "wsm_engine_circuit_breaker_trips_total",
    "Engine circuit-breaker trips.",
    ("engine",),
)


# --------------------------------------------------------------------------
# app-facing recorders
# --------------------------------------------------------------------------

def _norm_engine(engine: Any) -> str:
    value = getattr(engine, "value", engine)
    text = str(value).strip().lower()
    return text if text in _KNOWN_ENGINES else OTHER


def _norm_outcome(outcome: Any) -> str:
    value = getattr(outcome, "value", outcome)
    text = str(value).strip().lower()
    return text if text in _KNOWN_OUTCOMES else OTHER


def record_search(engine: Any, outcome: str, duration_seconds: float | None = None) -> None:
    """Count one finished search; also record its latency when supplied."""
    normalized = _norm_engine(engine)
    SEARCH_REQUESTS.inc(1.0, engine=normalized, outcome=_norm_outcome(outcome))
    if duration_seconds is not None:
        SEARCH_LATENCY.observe(float(duration_seconds), engine=normalized)


def observe_search_latency(engine: Any, seconds: float) -> None:
    SEARCH_LATENCY.observe(float(seconds), engine=_norm_engine(engine))


def record_cache_hit(cache: str = "search", amount: int = 1) -> None:
    CACHE_HITS.inc(float(amount), cache=_sanitize_label_value(cache))


def record_cache_miss(cache: str = "search", amount: int = 1) -> None:
    CACHE_MISSES.inc(float(amount), cache=_sanitize_label_value(cache))


def set_cache_stats(hits: float, misses: float, cache: str = "search") -> None:
    """Publish a cache's own running totals. Do not mix with record_cache_*."""
    label = _sanitize_label_value(cache)
    CACHE_HITS.sync(float(hits), cache=label)
    CACHE_MISSES.sync(float(misses), cache=label)


def set_browser_pool_stats(stats: Mapping[str, Any]) -> None:
    """Publish a `BrowserPool.stats` snapshot into the pool gauges."""
    for gauge, key in (
        (POOL_ACTIVE_TABS, "active_tabs"),
        (POOL_SIZE, "pool_size"),
        (POOL_MAX_SIZE, "max_pool_size"),
        (POOL_GENERATION, "generation"),
    ):
        value = stats.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            gauge.set(float(value))


def record_rate_limit_throttled(scope: str = "global", amount: int = 1) -> None:
    RATE_LIMIT_THROTTLED.inc(float(amount), scope=_sanitize_label_value(scope))


def record_circuit_breaker_trip(engine: Any, amount: int = 1) -> None:
    CIRCUIT_BREAKER_TRIPS.inc(float(amount), engine=_norm_engine(engine))


@dataclass
class SearchTracker:
    """In-flight search handle: the call site may correct `engine` when the
    fallback chain switches, or `outcome` (e.g. "blocked") before exit."""

    engine: str
    outcome: str = "success"


@asynccontextmanager
async def track_search(engine: Any) -> AsyncIterator[SearchTracker]:
    """Time a search and record engine/outcome on the way out."""
    tracker = SearchTracker(engine=_norm_engine(engine))
    started = time.perf_counter()
    try:
        yield tracker
    except (asyncio.TimeoutError, TimeoutError):
        tracker.outcome = "timeout"
        raise
    except Exception:
        tracker.outcome = "error"
        raise
    finally:
        record_search(tracker.engine, tracker.outcome, time.perf_counter() - started)


def register_collector(fn: Callable[[], None]) -> None:
    REGISTRY.register_collector(fn)


def unregister_collector(fn: Callable[[], None]) -> None:
    REGISTRY.unregister_collector(fn)


def reset_metrics() -> None:
    """Zero every series, keeping registrations (test helper)."""
    REGISTRY.reset()


def render_metrics() -> str:
    """Full Prometheus 0.0.4 exposition payload."""
    return REGISTRY.render()


async def metrics_endpoint(request: Request) -> Response:
    """Starlette handler for GET /metrics."""
    return Response(render_metrics(), media_type=CONTENT_TYPE_LATEST)


# --------------------------------------------------------------------------
# request correlation
# --------------------------------------------------------------------------

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_request_id: ContextVar[str] = ContextVar("wsm_request_id", default="")


def new_request_id() -> str:
    """Short id — 48 bits is ample for correlating within a retention window."""
    return uuid.uuid4().hex[:12]


def normalize_request_id(value: str | None) -> str:
    """Reuse a caller-supplied id when it is safe, otherwise mint one.

    The id is echoed back in a response header, so anything outside the
    conservative charset is discarded rather than risking header injection.
    """
    if value and _REQUEST_ID_RE.match(value):
        return value
    return new_request_id()


def get_request_id() -> str:
    """Current request id, or "" outside a request scope."""
    return _request_id.get()


def set_request_id(value: str) -> Token[str]:
    return _request_id.set(value)


def reset_request_id(token: Token[str]) -> None:
    _request_id.reset(token)


@contextmanager
def request_id_scope(value: str | None = None) -> Iterator[str]:
    """Bind a (possibly inbound) request id for the duration of the block."""
    request_id = normalize_request_id(value)
    token = _request_id.set(request_id)
    try:
        yield request_id
    finally:
        _request_id.reset(token)


class RequestIdFilter(logging.Filter):
    """Expose `%(request_id)s` to formatters without threading it around."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


class RequestIDMiddleware:
    """Pure ASGI middleware: bind the inbound/generated id, echo it back."""

    def __init__(self, app: ASGIApp, header: str = REQUEST_ID_HEADER) -> None:
        self.app = app
        self.header = header
        self._raw_header = header.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = Headers(scope=scope).get(self.header)
        with request_id_scope(inbound) as request_id:
            raw_value = request_id.encode("latin-1")

            async def send_wrapper(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers") or [])
                    headers.append((self._raw_header, raw_value))
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, send_wrapper)
