"""Rate limit middleware — per-key token bucket, burst, concurrency cap and quotas.

Pure in-process (no Redis): every counter lives in this worker, so a multi-worker
deployment enforces each limit *per worker*. Sized for the single-uvicorn-process
setup this service ships with.

Environment:
    RATE_LIMIT_ENABLED      1/0                     (default 1)
    RATE_LIMIT_RPS          sustained refill rate   (default 5)
    RATE_LIMIT_BURST        bucket capacity         (default 10)
    RATE_LIMIT_CONCURRENCY  max in-flight per key   (default 4)
    RATE_LIMIT_DAILY        requests per 24h        (default 0 = unlimited)
    RATE_LIMIT_MONTHLY      requests per 30d        (default 0 = unlimited)
    RATE_LIMIT_IDLE_TTL     idle bucket ttl in s    (default 300)
    RATE_LIMIT_MAX_BUCKETS  hard bucket cap         (default 10000)

Env parsing never stops boot: unparseable or non-finite (NaN/inf) values warn and
fall back to the default; finite but out-of-range values warn and are clamped.

Billing rule: daily/monthly quotas charge only requests that complete without a
server error (2xx/3xx/4xx). 5xx responses and unhandled exceptions are not billed.
Rate-limit tokens are different — they are spent at admission regardless of outcome.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

_DAY = 86_400.0
_MONTH = 30.0 * _DAY

# Idle buckets are swept at most this often — a full scan per request would turn
# the limiter itself into the DoS vector it exists to prevent.
_REAP_INTERVAL = 60.0

# /health and /pool mirror the auth middleware's skip list. The whole admin panel is
# exempt on purpose: /admin/api/* already requires ADMIN_TOKEN and the dashboard polls
# on a timer, so throttling the operator's own console mid-incident costs more than it saves.
# /livez, /readyz and /metrics are unauthenticated, so behind a proxy they share the one
# IP bucket every other anonymous caller lands in — a 429 there restarts the pod or blinds
# the scrape, which is strictly worse than the load the probes themselves add.
_EXEMPT_PREFIXES = ("/health", "/pool", "/admin", "/livez", "/readyz", "/metrics")


def _is_exempt(path: str) -> bool:
    # Boundary-safe: "/health" exempts "/health" and "/health/deep" but never a
    # lookalike such as "/healthz-evil" — bare startswith would hand those a bypass.
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _EXEMPT_PREFIXES)

_REASON_MESSAGES = {
    "rate": "Rate limit exceeded",
    "concurrency": "Too many concurrent requests",
    "daily": "Daily quota exceeded",
    "monthly": "Monthly quota exceeded",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    logger.warning("[rate_limit] invalid %s=%r — using %s", name, raw, default)
    return default


def _env_num(name: str, default: float, lo: float, hi: float) -> float:
    """Parse a numeric env var. Garbage or NaN/inf warn and fall back; finite values clamp to [lo, hi]."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("[rate_limit] invalid %s=%r — using %s", name, raw, default)
        return default
    if not math.isfinite(value):
        logger.warning("[rate_limit] non-finite %s=%r — using %s", name, raw, default)
        return default
    if value < lo or value > hi:
        clamped = min(max(value, lo), hi)
        logger.warning("[rate_limit] out-of-range %s=%r — clamped to %s", name, raw, clamped)
        return clamped
    return value


@dataclass(frozen=True)
class RateLimitConfig:
    """Effective limits. Values are clamped so a bad env var can never disable refill."""

    enabled: bool = True
    rps: float = 5.0
    burst: int = 10
    concurrency: int = 4
    daily_quota: int = 0
    monthly_quota: int = 0
    idle_ttl: float = 300.0
    max_buckets: int = 10_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "rps", max(float(self.rps), 0.01))
        object.__setattr__(self, "burst", max(int(self.burst), 1))
        object.__setattr__(self, "concurrency", max(int(self.concurrency), 1))
        object.__setattr__(self, "daily_quota", max(int(self.daily_quota), 0))
        object.__setattr__(self, "monthly_quota", max(int(self.monthly_quota), 0))
        object.__setattr__(self, "idle_ttl", max(float(self.idle_ttl), 1.0))
        object.__setattr__(self, "max_buckets", max(int(self.max_buckets), 1))

    @classmethod
    def from_env(cls) -> RateLimitConfig:
        """Build from RATE_LIMIT_* env vars. Every value is sanitized — bad input warns, never crashes boot."""
        return cls(
            enabled=_env_bool("RATE_LIMIT_ENABLED", True),
            rps=_env_num("RATE_LIMIT_RPS", 5.0, 0.01, 10_000.0),
            burst=int(_env_num("RATE_LIMIT_BURST", 10, 1, 1_000_000)),
            concurrency=int(_env_num("RATE_LIMIT_CONCURRENCY", 4, 1, 10_000)),
            daily_quota=int(_env_num("RATE_LIMIT_DAILY", 0, 0, 1e12)),
            monthly_quota=int(_env_num("RATE_LIMIT_MONTHLY", 0, 0, 1e12)),
            idle_ttl=_env_num("RATE_LIMIT_IDLE_TTL", 300.0, 1.0, 7 * _DAY),
            max_buckets=int(_env_num("RATE_LIMIT_MAX_BUCKETS", 10_000, 1, 1_000_000)),
        )


@dataclass
class _Bucket:
    tokens: float
    updated: float
    last_seen: float
    day_start: float
    month_start: float
    inflight: int = 0
    allowed: int = 0
    throttled: int = 0
    day_count: int = 0
    month_count: int = 0


@dataclass(frozen=True)
class _Verdict:
    allowed: bool
    limit: int
    remaining: int
    reset: int
    retry_after: int = 0
    reason: str = ""


def _ceil(seconds: float) -> int:
    return max(0, int(math.ceil(seconds)))


class _Limiter:
    """Token buckets keyed by caller identity. All critical sections are pure CPU."""

    def __init__(self, config: RateLimitConfig, clock: Callable[[], float]) -> None:
        self.config = config
        self.clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()
        self._next_reap = 0.0
        self._throttled_total = 0
        self._reaped_total = 0

    # --- lifecycle ---

    def configure(self, config: RateLimitConfig, clock: Callable[[], float] | None = None) -> None:
        with self._lock:
            self.config = config
            if clock is not None:
                self.clock = clock

    def reset(self, config: RateLimitConfig, clock: Callable[[], float]) -> None:
        with self._lock:
            self._buckets.clear()
            self._next_reap = 0.0
            self._throttled_total = 0
            self._reaped_total = 0
            self.config = config
            self.clock = clock

    # --- internals, lock held ---

    def _bucket(self, key: str, now: float) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(
                tokens=float(self.config.burst),
                updated=now,
                last_seen=now,
                day_start=now,
                month_start=now,
            )
            self._buckets[key] = bucket
            self._evict()
        else:
            self._buckets.move_to_end(key)
            bucket.last_seen = now
        return bucket

    def _evict(self) -> None:
        # OrderedDict is kept in least-recently-used order, so the front is the coldest.
        while len(self._buckets) > self.config.max_buckets:
            victim = next((k for k, b in self._buckets.items() if b.inflight == 0), None)
            if victim is None:
                return  # everything busy — the cap is soft under extreme load
            del self._buckets[victim]
            self._reaped_total += 1

    def _maybe_reap(self, now: float) -> None:
        if now < self._next_reap:
            return
        self._next_reap = now + _REAP_INTERVAL
        ttl = self.config.idle_ttl
        stale = [k for k, b in self._buckets.items() if b.inflight == 0 and now - b.last_seen >= ttl]
        for key in stale:
            del self._buckets[key]
        self._reaped_total += len(stale)

    def _roll_windows(self, bucket: _Bucket, now: float) -> None:
        if now - bucket.day_start >= _DAY:
            bucket.day_start, bucket.day_count = now, 0
        if now - bucket.month_start >= _MONTH:
            bucket.month_start, bucket.month_count = now, 0

    def _quota_reject(self, bucket: _Bucket, now: float) -> _Verdict | None:
        cfg = self.config
        windows = (
            ("daily", cfg.daily_quota, bucket.day_count, bucket.day_start, _DAY),
            ("monthly", cfg.monthly_quota, bucket.month_count, bucket.month_start, _MONTH),
        )
        for reason, quota, count, start, window in windows:
            if quota and count >= quota:
                wait = max(1, _ceil(start + window - now))
                return _Verdict(False, quota, 0, wait, wait, reason)
        return None

    def _allow_verdict(self, bucket: _Bucket, now: float) -> _Verdict:
        cfg = self.config
        limit = cfg.burst
        remaining = int(bucket.tokens)
        reset = _ceil((cfg.burst - bucket.tokens) / cfg.rps)
        # Report whichever ceiling is nearest — that is the one the caller will actually hit.
        windows = (
            (cfg.daily_quota, bucket.day_count, bucket.day_start, _DAY),
            (cfg.monthly_quota, bucket.month_count, bucket.month_start, _MONTH),
        )
        for quota, count, start, window in windows:
            # count excludes this request (quota is billed post-response), so presume
            # the charge here to keep the advertised remaining consistent.
            if quota and quota - count - 1 < remaining:
                limit, remaining = quota, quota - count - 1
                reset = _ceil(start + window - now)
        return _Verdict(True, limit, remaining, reset)

    # --- public ---

    def acquire(self, key: str) -> _Verdict:
        cfg = self.config
        now = self.clock()
        with self._lock:
            self._maybe_reap(now)
            bucket = self._bucket(key, now)
            self._roll_windows(bucket, now)

            # Quotas first: once exhausted the answer is stable for the whole window,
            # and a caller sitting on an empty quota should not also be told "slow down".
            verdict = self._quota_reject(bucket, now)
            if verdict is None:
                elapsed = now - bucket.updated
                if elapsed > 0:
                    bucket.tokens = min(float(cfg.burst), bucket.tokens + elapsed * cfg.rps)
                    bucket.updated = now
                if bucket.tokens < 1.0:
                    wait = _ceil((1.0 - bucket.tokens) / cfg.rps)
                    verdict = _Verdict(False, cfg.burst, 0, wait, max(1, wait), "rate")
                else:
                    # Spend the token before the concurrency check so a client that
                    # hammers while saturated still burns down to a rate limit.
                    bucket.tokens -= 1.0
                    if bucket.inflight >= cfg.concurrency:
                        verdict = _Verdict(False, cfg.burst, int(bucket.tokens), 1, 1, "concurrency")

            if verdict is not None:
                bucket.throttled += 1
                self._throttled_total += 1
                return verdict

            bucket.inflight += 1
            bucket.allowed += 1
            # Quota is NOT charged here — the middleware calls commit_quota() once the
            # response status is known, so server errors never consume billed quota.
            return self._allow_verdict(bucket, now)

    def commit_quota(self, key: str) -> None:
        """Charge one request against the daily/monthly windows.

        Called post-response for billable outcomes only (2xx/3xx/4xx). Because the
        quota check in acquire() cannot see requests still in flight, a caller can
        overshoot a quota by at most `concurrency` requests — the accepted cost of
        not billing failures.
        """
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:  # reset/evicted mid-flight — nothing left to bill
                return
            self._roll_windows(bucket, self.clock())
            bucket.day_count += 1
            bucket.month_count += 1

    def release(self, key: str) -> None:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is not None and bucket.inflight > 0:
                bucket.inflight -= 1

    def snapshot(self, top: int) -> dict[str, Any]:
        cfg = self.config
        now = self.clock()
        with self._lock:
            hottest = sorted(self._buckets.items(), key=lambda kv: kv[1].throttled + kv[1].allowed, reverse=True)
            keys = [
                {
                    "key": key,
                    "tokens": round(b.tokens, 2),
                    "inflight": b.inflight,
                    "allowed": b.allowed,
                    "throttled": b.throttled,
                    "idle_s": round(now - b.last_seen, 1),
                    "day_count": b.day_count,
                    "month_count": b.month_count,
                }
                for key, b in hottest[:top]
            ]
            return {
                "enabled": cfg.enabled,
                "rps": cfg.rps,
                "burst": cfg.burst,
                "concurrency": cfg.concurrency,
                "daily_quota": cfg.daily_quota,
                "monthly_quota": cfg.monthly_quota,
                "idle_ttl": cfg.idle_ttl,
                "active_buckets": len(self._buckets),
                "max_buckets": cfg.max_buckets,
                "throttled_total": self._throttled_total,
                "reaped_total": self._reaped_total,
                "keys": keys,
            }


_limiter = _Limiter(RateLimitConfig(), time.monotonic)


def rate_limit_stats(top: int = 20) -> dict[str, Any]:
    """Limiter snapshot for the admin dashboard. `keys` is capped at `top` hottest buckets."""
    return _limiter.snapshot(top)


def reset_rate_limits(
    config: RateLimitConfig | None = None,
    clock: Callable[[], float] | None = None,
) -> None:
    """Drop every bucket and counter, restoring defaults unless overridden. For tests."""
    _limiter.reset(config or RateLimitConfig(), clock or time.monotonic)


def _client_key(request: Request) -> str:
    # api_key_id is set by APIKeyAuthMiddleware; ADMIN_TOKEN traffic leaves it None.
    api_key_id = getattr(request.state, "api_key_id", None)
    if api_key_id is not None:
        return f"key:{api_key_id}"
    # Deliberately not X-Forwarded-For: it is caller-controlled unless a trusted proxy
    # rewrites it, and honouring it would hand out a free per-request limit reset.
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


def _headers(verdict: _Verdict, *, retry_after: bool = False) -> dict[str, str]:
    headers = {
        "X-RateLimit-Limit": str(verdict.limit),
        "X-RateLimit-Remaining": str(max(0, verdict.remaining)),
        # Delta-seconds, not an epoch: the clock is monotonic and injectable.
        "X-RateLimit-Reset": str(verdict.reset),
    }
    if retry_after:
        headers["Retry-After"] = str(max(1, verdict.retry_after))
    return headers


async def _drain(source: AsyncIterator[Any], release: Callable[[], None]) -> AsyncIterator[Any]:
    try:
        async for chunk in source:
            yield chunk
    finally:
        release()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limit, burst control, concurrency cap and daily/monthly quotas.

    Keyed by `request.state.api_key_id` and falling back to the client IP for open
    deployments, so this MUST be installed *inside* APIKeyAuthMiddleware (added to the
    app before it) or every caller collapses onto one shared IP bucket.

    Quota billing: daily/monthly quotas are billing-shaped, so they charge only
    requests that complete without a server error — 2xx/3xx/4xx consume quota, while
    5xx responses and unhandled exceptions do not. Rate-limit tokens are spent at
    admission for every accepted request regardless of outcome.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        config: RateLimitConfig | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(app)
        cfg = config if config is not None else RateLimitConfig.from_env()
        _limiter.configure(cfg, clock)
        logger.info(
            "[rate_limit] enabled=%s rps=%s burst=%s concurrency=%s daily=%s monthly=%s",
            cfg.enabled, cfg.rps, cfg.burst, cfg.concurrency, cfg.daily_quota, cfg.monthly_quota,
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not _limiter.config.enabled or _is_exempt(request.url.path):
            return await call_next(request)

        key = _client_key(request)
        verdict = _limiter.acquire(key)
        if not verdict.allowed:
            # Allocation only — no DB, no Redis, no await: rejections must stay cheap.
            return JSONResponse(
                {"error": _REASON_MESSAGES[verdict.reason], "reason": verdict.reason},
                status_code=429,
                headers=_headers(verdict, retry_after=True),
            )

        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                _limiter.release(key)

        try:
            response = await call_next(request)
        except BaseException:
            # Slot returned, quota untouched — an unhandled exception surfaces as a 5xx.
            release()
            raise

        # Billable outcome (anything below 5xx): charge the daily/monthly quota now.
        # Runs before release(), so inflight>0 still pins the bucket against eviction.
        if response.status_code < 500:
            _limiter.commit_quota(key)

        response.headers.update(_headers(verdict))
        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is None:
            release()
        else:
            # Hold the slot until the body is fully streamed or the client vanishes,
            # otherwise the cap only bounds header latency instead of real work.
            response.body_iterator = _drain(body_iterator, release)  # type: ignore[attr-defined]
        return response
