"""Liveness / readiness / deep health probes for enterprise (K8s) deployments.

Three deliberately distinct levels:

- ``liveness``  — the process itself answers. Synchronous, zero I/O, never
  touches a dependency: a wedged Redis must not get the pod killed.
- ``readiness`` — can this replica serve a search *right now*? Browser pool
  started, SQLite reachable, Redis reachable when configured. ``degraded``
  still serves traffic (``ready: true``); only ``unavailable`` should pull the
  pod out of the load balancer.
- ``deep``      — per-dependency status, latency and last error, for humans.

Every dependency probe is timeout-guarded and swallows its own exceptions, so
a hung or broken dependency can never hang the probe endpoint.

The module never imports the running server: dependencies are reached through
injectable providers whose defaults look the process up in ``sys.modules``,
which keeps the whole thing unit-testable with fakes.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Union

# Status vocabulary. "skipped" is per-check only (an unconfigured optional
# dependency) and never propagates into the aggregate.
OK = "ok"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"
SKIPPED = "skipped"
_VALID_STATUSES = frozenset({OK, DEGRADED, UNAVAILABLE, SKIPPED})

# Per-check budget. Kubelet probe timeouts are usually 1-3s, so stay under it.
DEFAULT_TIMEOUT = 2.0

_STARTED_AT = time.monotonic()

ProbeResult = Union[bool, None, Mapping[str, Any]]
ProbeFn = Callable[[], Union[ProbeResult, Awaitable[ProbeResult]]]

_VERSION_RE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def resolve_version(explicit: str | None = None) -> str:
    """Service version: explicit arg > ``WSM_VERSION`` > pyproject > metadata.

    The Nuitka image ships neither pyproject nor package metadata, hence the
    env override.
    """
    if explicit:
        return explicit
    env_version = os.environ.get("WSM_VERSION", "").strip()
    if env_version:
        return env_version
    try:
        text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        match = _VERSION_RE.search(text)
        if match:
            return match.group(1)
    except OSError:
        pass
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("web-search-mcp")
    except Exception:
        return "unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    """Coerce to something ``json.dumps`` accepts — a probe endpoint must never
    500 because a foreign object leaked into a detail dict."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return repr(value)


async def _resolve(value: Any) -> Any:
    """Await ``value`` when a provider handed back a coroutine/future.

    Deliberately narrower than ``inspect.isawaitable``: an ``aiosqlite.Connection``
    is itself awaitable, so a sync provider returning the live connection
    (``db_provider=lambda: conn``) would get it re-awaited and raise
    ``RuntimeError: threads can only be started once`` — reporting the database
    permanently unavailable. Only ``async def`` providers need awaiting.
    """
    if inspect.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


@dataclass(frozen=True)
class DependencyCheck:
    """One named dependency probe.

    ``required=False`` marks a dependency the service can lose and keep
    serving: its failure degrades the aggregate instead of failing readiness.
    """

    name: str
    probe: ProbeFn
    required: bool = True
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class CheckReport:
    """Outcome of one check run, plus the last error ever seen for it."""

    name: str
    status: str
    required: bool
    latency_ms: float
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None

    @property
    def healthy(self) -> bool:
        return self.status in (OK, SKIPPED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "required": self.required,
            "latency_ms": self.latency_ms,
            "detail": _jsonable(self.detail),
            "error": self.error,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
        }


def aggregate_status(reports: Sequence[CheckReport]) -> str:
    """Fold per-check statuses into ``ok`` | ``degraded`` | ``unavailable``.

    A required dependency that is down means we cannot serve → ``unavailable``.
    Anything else that is not fully healthy → ``degraded`` (still serving).
    """
    status = OK
    for report in reports:
        if report.healthy:
            continue
        if report.required and report.status == UNAVAILABLE:
            return UNAVAILABLE
        status = DEGRADED
    return status


def _normalize(raw: Any) -> tuple[str, dict[str, Any]]:
    """Map a probe's return value onto ``(status, detail)``."""
    if raw is None or raw is True:
        return OK, {}
    if raw is False:
        return UNAVAILABLE, {}
    if isinstance(raw, Mapping):
        detail = {str(k): v for k, v in raw.items() if k != "status"}
        status = str(raw.get("status", OK))
        if status not in _VALID_STATUSES:
            detail["reported_status"] = status
            return UNAVAILABLE, detail
        return status, detail
    return OK, {"value": raw}


async def _invoke(probe: ProbeFn) -> Any:
    result = probe()
    return await result if inspect.isawaitable(result) else result


# ---------------------------------------------------------------------------
# Default providers — locate live objects without importing the server
# ---------------------------------------------------------------------------

# mcp_server is ``__main__`` in the Nuitka binary and ``src.mcp_server`` under
# ``python -m``; the FastAPI entrypoint keeps its pool in ``src.api.routes``.
_POOL_MODULES = ("__main__", "src.mcp_server", "src.api.routes")
_POOL_ATTRS = ("_pool_instance", "_pool")


def default_pool_provider() -> Any | None:
    """Return the live BrowserPool, or None if the process has none.

    Reads ``sys.modules`` instead of importing: importing ``src.mcp_server``
    would build a second FastMCP app as a side effect.
    """
    for mod_name in _POOL_MODULES:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for attr in _POOL_ATTRS:
            pool = getattr(mod, attr, None)
            if pool is not None:
                return pool
    return None


async def default_db_provider() -> Any:
    """Return the shared aiosqlite connection (raises when uninitialized)."""
    from src.admin.database import get_db

    return await get_db()


def default_redis_provider() -> Any | None:
    """Return the repository's Redis client, or None when it never connected."""
    mod = sys.modules.get("src.admin.repository")
    return getattr(mod, "_redis", None) if mod is not None else None


# ---------------------------------------------------------------------------
# Dependency probes
# ---------------------------------------------------------------------------


async def check_browser_pool(provider: Callable[[], Any]) -> dict[str, Any]:
    """Pool readiness from counters only.

    Deliberately does NOT call ``pool.is_healthy()``: that opens a real tab, and
    a kubelet probing every few seconds would burn pool slots and RAM.
    """
    pool = await _resolve(provider())
    if pool is None:
        return {"status": UNAVAILABLE, "reason": "browser pool not initialized"}
    stats = dict(getattr(pool, "stats", None) or {})
    if not stats.get("started"):
        return {"status": UNAVAILABLE, "reason": "browser pool not started", "pool": stats}
    if getattr(pool, "needs_restart", False):
        return {"status": DEGRADED, "reason": "consecutive failures pending browser restart", "pool": stats}
    return {"status": OK, "pool": stats}


async def check_database(provider: Callable[[], Any]) -> dict[str, Any]:
    """``SELECT 1`` against the shared SQLite connection.

    Cheap on purpose — ``PRAGMA integrity_check`` belongs in /admin/api/db-health,
    not on a probe the kubelet hits every few seconds.
    """
    db = await _resolve(provider())
    if db is None:
        return {"status": UNAVAILABLE, "reason": "database not initialized"}
    await db.execute_fetchall("SELECT 1")
    return {"status": OK}


async def check_redis(provider: Callable[[], Any]) -> dict[str, Any]:
    """Optional cache probe.

    Unconfigured Redis is ``skipped`` (it never affects the aggregate). A
    configured-but-unreachable Redis reports a failure that only degrades the
    service, because every Redis path in the repository falls back to SQLite.
    """
    client = await _resolve(provider())
    if client is None:
        if os.environ.get("REDIS_URL", "").strip():
            return {"status": DEGRADED, "reason": "REDIS_URL set but client unavailable — using SQLite fallback"}
        return {"status": SKIPPED, "reason": "REDIS_URL not configured"}
    await client.ping()
    return {"status": OK}


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class HealthMonitor:
    """Runs dependency checks and renders the three probe payloads."""

    def __init__(
        self,
        *,
        version: str | None = None,
        checks: Sequence[DependencyCheck] | None = None,
        pool_provider: Callable[[], Any] | None = None,
        db_provider: Callable[[], Any] | None = None,
        redis_provider: Callable[[], Any] | None = None,
        default_timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.version = resolve_version(version)
        self._default_timeout = default_timeout
        self._checks: list[DependencyCheck] = (
            list(checks)
            if checks is not None
            else self._build_default_checks(pool_provider, db_provider, redis_provider)
        )
        # name -> last observed failure, kept across runs so `deep` can show the
        # most recent error even while the dependency is currently healthy.
        self._last_errors: dict[str, dict[str, str]] = {}

    def _build_default_checks(
        self,
        pool_provider: Callable[[], Any] | None,
        db_provider: Callable[[], Any] | None,
        redis_provider: Callable[[], Any] | None,
    ) -> list[DependencyCheck]:
        pool = pool_provider or default_pool_provider
        db = db_provider or default_db_provider
        redis = redis_provider or default_redis_provider
        timeout = self._default_timeout
        return [
            DependencyCheck("browser_pool", lambda: check_browser_pool(pool), timeout=timeout),
            DependencyCheck("database", lambda: check_database(db), timeout=timeout),
            DependencyCheck("redis", lambda: check_redis(redis), required=False, timeout=timeout),
        ]

    @property
    def checks(self) -> list[DependencyCheck]:
        return list(self._checks)

    def liveness(self) -> dict[str, Any]:
        """Process-level probe: synchronous, no I/O, no dependency access."""
        return {
            "status": OK,
            "live": True,
            "version": self.version,
            "pid": os.getpid(),
            "uptime_s": round(time.monotonic() - _STARTED_AT, 3),
        }

    async def run_checks(self) -> list[CheckReport]:
        """Run every check concurrently. Never raises."""
        return list(await asyncio.gather(*(self._run_check(c) for c in self._checks)))

    async def _run_check(self, check: DependencyCheck) -> CheckReport:
        started = time.monotonic()
        detail: dict[str, Any] = {}
        error: str | None = None
        try:
            raw = await asyncio.wait_for(_invoke(check.probe), timeout=check.timeout)
        except asyncio.TimeoutError:
            status = UNAVAILABLE
            error = f"timeout after {check.timeout:g}s"
        except Exception as exc:  # a broken probe must not break the endpoint
            status = UNAVAILABLE
            error = f"{type(exc).__name__}: {exc}"
        else:
            status, detail = _normalize(raw)
            if status in (DEGRADED, UNAVAILABLE):
                error = str(detail.get("reason") or status)
        latency_ms = round((time.monotonic() - started) * 1000, 2)
        if error:
            self._last_errors[check.name] = {"error": error, "at": _utc_now()}
        last = self._last_errors.get(check.name, {})
        return CheckReport(
            name=check.name,
            status=status,
            required=check.required,
            latency_ms=latency_ms,
            detail=detail,
            error=error,
            last_error=last.get("error"),
            last_error_at=last.get("at"),
        )

    async def readiness(self) -> dict[str, Any]:
        """Compact serve/don't-serve verdict for the kubelet."""
        reports = await self.run_checks()
        status = aggregate_status(reports)
        return {
            "status": status,
            "ready": status != UNAVAILABLE,
            "version": self.version,
            "uptime_s": round(time.monotonic() - _STARTED_AT, 3),
            "checks": {r.name: r.status for r in reports},
        }

    async def deep(self) -> dict[str, Any]:
        """Full diagnostic payload — per-dependency detail, latency, last error."""
        reports = await self.run_checks()
        status = aggregate_status(reports)
        payload = self.liveness()
        payload.update(
            {
                "status": status,
                "ready": status != UNAVAILABLE,
                "checks": [r.to_dict() for r in reports],
            }
        )
        return payload


# ---------------------------------------------------------------------------
# Process-wide default monitor + wiring helpers
# ---------------------------------------------------------------------------

_monitor: HealthMonitor | None = None


def get_monitor() -> HealthMonitor:
    """Process-wide monitor wired to the default providers."""
    global _monitor
    if _monitor is None:
        _monitor = HealthMonitor()
    return _monitor


def liveness() -> dict[str, Any]:
    return get_monitor().liveness()


async def readiness() -> dict[str, Any]:
    return await get_monitor().readiness()


async def deep_health() -> dict[str, Any]:
    return await get_monitor().deep()


def http_status(payload: Mapping[str, Any]) -> int:
    """200 while serving (ok or degraded), 503 once readiness says we cannot."""
    return 200 if payload.get("ready", True) else 503


def starlette_routes(prefix: str = "/health") -> list[Any]:
    """Build ``<prefix>/live``, ``<prefix>/ready`` and ``<prefix>/deep`` routes.

    Starlette is imported lazily so the module stays framework-free for tests.
    ``deep`` always answers 200 — it is a diagnostic page, and a 503 would make
    ``curl -f`` hide the body an operator came for.
    """
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def _live(request: Any) -> Any:
        return JSONResponse(liveness())

    async def _ready(request: Any) -> Any:
        payload = await readiness()
        return JSONResponse(payload, status_code=http_status(payload))

    async def _deep(request: Any) -> Any:
        return JSONResponse(await deep_health())

    return [
        Route(f"{prefix}/live", _live, methods=["GET"]),
        Route(f"{prefix}/ready", _ready, methods=["GET"]),
        Route(f"{prefix}/deep", _deep, methods=["GET"]),
    ]
