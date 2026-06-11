"""IP-derived fingerprint resolution for per-proxy browser contexts.

Under proxy rotation Camoufox can't set a launch-level geoip fingerprint
(the browser launches proxy-less, bridges are per-request). Without it the
browser's timezone/locale don't match the proxy's exit IP — an obvious
fingerprint inconsistency. This module resolves each proxy's exit IP, maps
it to geolocation/timezone/locale via Camoufox's bundled GeoLite2 db, and
caches the result so BrowserPool can apply it per context.

Resolution runs in the background (warm-the-cache) so it never adds latency
to the request path; geo is applied from the second request through a proxy
onward, once the cache is populated.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Exit-IP echo endpoints, tried in order. Plain-text body = the IP.
_IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://icanhazip.com",
)

# original_url -> resolved fingerprint dict (success only)
_resolved: dict[str, dict] = {}
# original_url -> last failure monotonic time (retry after cooldown)
_failed: dict[str, float] = {}
# original_url currently being resolved (avoid duplicate in-flight tasks)
_inflight: set[str] = set()

_FAILURE_RETRY_SECS = 600.0  # re-attempt a failed proxy after 10 min


def peek(original_url: str) -> dict | None:
    """Return the cached fingerprint for a proxy, or None if not ready."""
    return _resolved.get(original_url)


def should_resolve(original_url: str) -> bool:
    """True if this proxy has no cached geo and isn't in cooldown/in-flight."""
    if original_url in _resolved or original_url in _inflight:
        return False
    last_fail = _failed.get(original_url)
    if last_fail is not None and (time.monotonic() - last_fail) < _FAILURE_RETRY_SECS:
        return False
    return True


def _httpx_proxy_url(proxy_config: dict) -> str:
    """Build an httpx proxy URL from a Playwright proxy config.

    SOCKS5-auth proxies arrive as a local bridge URL (already an HTTP CONNECT
    proxy, no creds). HTTP(S) proxies with auth carry username/password — fold
    them back into the URL so httpx authenticates.
    """
    server = proxy_config.get("server", "")
    user = proxy_config.get("username")
    pwd = proxy_config.get("password", "")
    if user:
        p = urlparse(server)
        return f"{p.scheme or 'http'}://{user}:{pwd}@{p.hostname}:{p.port}"
    return server


async def _fetch_exit_ip(proxy_url: str, timeout: float) -> str | None:
    from camoufox.ip import validate_ip

    async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout, verify=False) as client:
        for url in _IP_ENDPOINTS:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                ip = resp.text.strip()
                validate_ip(ip)
                return ip
            except Exception:
                continue
    return None


def _geo_for_ip(ip: str) -> dict | None:
    """Map an IP to a context fingerprint via Camoufox's GeoLite2 db."""
    try:
        from camoufox.locale import get_geolocation

        g = get_geolocation(ip)  # raises UnknownIPLocation if data is missing
        return {
            "ip": ip,
            "locale": g.locale.as_string,
            "timezone_id": g.timezone,
            "latitude": g.latitude,
            "longitude": g.longitude,
        }
    except Exception as exc:
        logger.debug("[geo] no geolocation for %s: %s", ip, exc)
        return None


async def resolve(original_url: str, proxy_config: dict, timeout: float = 5.0) -> dict | None:
    """Resolve and cache a proxy's exit-IP fingerprint. Best-effort.

    Safe to fire-and-forget: populates the module cache; returns the
    fingerprint (or None) for callers that want to await it directly.
    """
    if original_url in _resolved:
        return _resolved[original_url]
    _inflight.add(original_url)
    try:
        proxy_url = _httpx_proxy_url(proxy_config)
        ip = await _fetch_exit_ip(proxy_url, timeout)
        fp = _geo_for_ip(ip) if ip else None
        if fp:
            _resolved[original_url] = fp
            _failed.pop(original_url, None)
            logger.info("[geo] proxy %s...  exit_ip=%s locale=%s tz=%s",
                        original_url[:24], fp["ip"], fp["locale"], fp["timezone_id"])
        else:
            _failed[original_url] = time.monotonic()
            logger.debug("[geo] could not resolve fingerprint for %s...", original_url[:24])
        return fp
    except Exception as exc:
        _failed[original_url] = time.monotonic()
        logger.debug("[geo] resolve error for %s...: %s", original_url[:24], exc)
        return None
    finally:
        _inflight.discard(original_url)


def context_geo_kwargs(fp: dict) -> dict:
    """Turn a resolved fingerprint into new_context() kwargs."""
    return {
        "locale": fp["locale"],
        "timezone_id": fp["timezone_id"],
        "geolocation": {
            "latitude": fp["latitude"],
            "longitude": fp["longitude"],
            "accuracy": 100,
        },
        "permissions": ["geolocation"],
    }


def clear_cache() -> None:
    """Reset all cached state — used by tests and on proxy hot-reload."""
    _resolved.clear()
    _failed.clear()
    _inflight.clear()
