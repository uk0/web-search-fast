"""Two-tier search-result cache: in-process TTL+LRU in front of an optional Redis tier.

Identical queries are the common case for LLM clients, and each one costs
seconds of scraping. The memory tier is always on and dependency-free; Redis
only adds sharing across workers/replicas and is never required — every Redis
path degrades to memory-only instead of raising.

Values are the JSON-serialized ``SearchResponse.model_dump()``. Storing the
serialized form in both tiers keeps them symmetric and hands every caller a
fresh object, so a mutated result can never poison the cache.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

# Bump the version segment whenever the payload shape changes — old entries
# then simply stop being addressable instead of deserializing into garbage.
KEY_PREFIX = "wsm:search:v1:"

DEFAULT_TTL = 300
DEFAULT_MAX = 512

_WS_RE = re.compile(r"\s+")

# key -> (expiry_deadline, serialized_payload). Insertion order is LRU order.
# No lock: every mutation runs on the event loop between awaits, and a module
# global (rather than a loop-bound primitive) survives loop teardown in tests.
_MEM: OrderedDict[str, tuple[float, str]] = OrderedDict()

_redis: Any = None

_stats: dict[str, int] = {
    "memory_hits": 0,
    "redis_hits": 0,
    "misses": 0,
    "sets": 0,
    "evictions": 0,
    "expired": 0,
    "errors": 0,
}


def _now() -> float:
    """Clock indirection so tests can drive expiry without sleeping."""
    return time.monotonic()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def cache_enabled() -> bool:
    """Whether caching is active. Read per call so env flips take effect live."""
    return os.environ.get("SEARCH_CACHE_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def _ttl() -> int:
    return _env_int("SEARCH_CACHE_TTL", DEFAULT_TTL)


def _maxsize() -> int:
    return max(1, _env_int("SEARCH_CACHE_MAX", DEFAULT_MAX))


# ---------------------------------------------------------------------------
# Keying
# ---------------------------------------------------------------------------


def make_key(query: str, engine: str, depth: int, max_results: int) -> str:
    """Stable key over every input that changes the result.

    The query is normalized (strip + collapse whitespace + casefold) so
    trivially different spellings share an entry; depth and max_results are
    part of the digest so they can never collide.
    """
    norm_query = _WS_RE.sub(" ", query.strip()).casefold()
    # Accept a SearchEngine member or a plain string interchangeably.
    norm_engine = str(getattr(engine, "value", engine)).strip().casefold()
    raw = "\x1f".join((norm_query, norm_engine, str(int(depth)), str(int(max_results))))
    return KEY_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Memory tier
# ---------------------------------------------------------------------------


def _mem_get(key: str) -> str | None:
    entry = _MEM.get(key)
    if entry is None:
        return None
    expires_at, raw = entry
    if expires_at <= _now():
        del _MEM[key]
        _stats["expired"] += 1
        return None
    _MEM.move_to_end(key)
    return raw


def _mem_put(key: str, raw: str, ttl: int) -> None:
    _MEM[key] = (_now() + ttl, raw)
    _MEM.move_to_end(key)  # a re-set of an existing key keeps its old position
    maxsize = _maxsize()
    while len(_MEM) > maxsize:
        _MEM.popitem(last=False)
        _stats["evictions"] += 1


def _decode(raw: str, key: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        logger.debug("[cache] corrupt entry dropped: %s", exc)
        _MEM.pop(key, None)
        return None
    return value if isinstance(value, dict) else None


def purge_expired() -> int:
    """Sweep expired entries. Reads already evict lazily; this bounds idle memory."""
    now = _now()
    dead = [k for k, (expires_at, _) in _MEM.items() if expires_at <= now]
    for key in dead:
        del _MEM[key]
    _stats["expired"] += len(dead)
    return len(dead)


# ---------------------------------------------------------------------------
# Redis tier (optional)
# ---------------------------------------------------------------------------


async def init_cache_redis(url: str | None = None) -> bool:
    """Attach the shared Redis tier. Falls back to memory-only on any problem.

    Defaults to SEARCH_CACHE_REDIS_URL, then REDIS_URL. Returns True when the
    tier is live.
    """
    global _redis
    target = url or os.environ.get("SEARCH_CACHE_REDIS_URL") or os.environ.get("REDIS_URL", "")
    if not target:
        return False
    client: Any = None
    try:
        import redis.asyncio as aioredis  # optional dependency — may be absent

        client = aioredis.from_url(target, decode_responses=True)
        await client.ping()
    except Exception as exc:
        # Only the class name: the message can echo back the connection URL.
        logger.warning("[cache] redis unavailable (%s) — memory-only", type(exc).__name__)
        await _close_client(client)  # drop the half-open pool instead of leaking it
        await close_cache_redis()  # detach (and release) any previously-attached client
        return False
    await close_cache_redis()  # a re-init must not orphan the previous client
    _redis = client
    logger.info("[cache] redis tier enabled")
    return True


async def _close_client(client: Any) -> None:
    """Best-effort release of a client's connection pool. Never raises."""
    if client is None:
        return
    try:
        closer: Any = getattr(client, "aclose", None) or getattr(client, "close", None)
        if closer is not None:
            await closer()
    except Exception as exc:
        logger.debug("[cache] redis close failed: %s", type(exc).__name__)


async def close_cache_redis() -> None:
    """Detach the Redis tier (shutdown hook). Never raises."""
    global _redis
    client, _redis = _redis, None
    await _close_client(client)


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


async def _redis_get(key: str) -> str | None:
    if _redis is None:
        return None
    try:
        return _as_text(await _redis.get(key))
    except Exception as exc:
        _stats["errors"] += 1
        logger.debug("[cache] redis get failed: %s", exc)
        return None


async def _redis_set(key: str, raw: str, ttl: int) -> None:
    if _redis is None:
        return
    try:
        await _redis.setex(key, ttl, raw)
    except Exception as exc:
        _stats["errors"] += 1
        logger.debug("[cache] redis set failed: %s", exc)


async def _redis_clear() -> int:
    if _redis is None:
        return 0
    removed = 0
    batch: list[str] = []
    try:
        async for key in _redis.scan_iter(match=f"{KEY_PREFIX}*", count=256):
            text = _as_text(key)
            if text is not None:
                batch.append(text)
            if len(batch) >= 256:
                removed += int(await _redis.delete(*batch) or 0)
                batch.clear()
        if batch:
            removed += int(await _redis.delete(*batch) or 0)
    except Exception as exc:
        _stats["errors"] += 1
        logger.debug("[cache] redis clear failed: %s", exc)
    return removed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_cached(query: str, engine: str, depth: int, max_results: int) -> dict[str, Any] | None:
    """Return the cached payload, or None on miss/expiry/disabled."""
    if not cache_enabled():
        return None
    key = make_key(query, engine, depth, max_results)

    raw = _mem_get(key)
    if raw is not None:
        payload = _decode(raw, key)
        if payload is not None:
            _stats["memory_hits"] += 1
            return payload

    raw = await _redis_get(key)
    if raw is not None:
        payload = _decode(raw, key)
        if payload is not None:
            # Promote so repeat hits stay in-process. The local copy uses the
            # configured TTL, so it can outlive the Redis entry by at most one
            # TTL — acceptable drift for search results.
            _mem_put(key, raw, _ttl())
            _stats["redis_hits"] += 1
            return payload

    _stats["misses"] += 1
    return None


async def set_cached(
    query: str,
    engine: str,
    depth: int,
    max_results: int,
    payload: dict[str, Any],
    ttl: int | None = None,
) -> None:
    """Store a payload in both tiers. No-op when disabled or TTL <= 0."""
    if not cache_enabled():
        return
    effective_ttl = _ttl() if ttl is None else ttl
    if effective_ttl <= 0:
        return
    try:
        raw = json.dumps(payload, default=str, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        logger.debug("[cache] payload not serializable: %s", exc)
        return
    key = make_key(query, engine, depth, max_results)
    _mem_put(key, raw, effective_ttl)
    _stats["sets"] += 1
    await _redis_set(key, raw, effective_ttl)


async def invalidate(query: str, engine: str, depth: int, max_results: int) -> bool:
    """Drop one entry from both tiers. True when something was removed."""
    key = make_key(query, engine, depth, max_results)
    removed = _MEM.pop(key, None) is not None
    if _redis is not None:
        try:
            removed = bool(await _redis.delete(key)) or removed
        except Exception as exc:
            _stats["errors"] += 1
            logger.debug("[cache] redis delete failed: %s", exc)
    return removed


async def clear_cache() -> int:
    """Drop every entry from both tiers. Returns the number removed."""
    removed = len(_MEM)
    _MEM.clear()
    removed += await _redis_clear()
    return removed


def cache_stats() -> dict[str, Any]:
    """Counters for the admin dashboard. Cumulative; unaffected by clear_cache()."""
    now = _now()
    hits = _stats["memory_hits"] + _stats["redis_hits"]
    total = hits + _stats["misses"]
    return {
        "enabled": cache_enabled(),
        "backend": "memory+redis" if _redis is not None else "memory",
        "hits": hits,
        "memory_hits": _stats["memory_hits"],
        "redis_hits": _stats["redis_hits"],
        "misses": _stats["misses"],
        "hit_rate": round(hits / total, 4) if total else 0.0,
        "size": sum(1 for expires_at, _ in _MEM.values() if expires_at > now),
        "max_size": _maxsize(),
        "ttl": _ttl(),
        "sets": _stats["sets"],
        "evictions": _stats["evictions"],
        "expired": _stats["expired"],
        "errors": _stats["errors"],
    }
