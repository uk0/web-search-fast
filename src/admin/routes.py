"""Admin REST API routes — /admin/api/* endpoints.

Auth is handled by APIKeyAuthMiddleware (supports ADMIN_TOKEN env var
and database API keys with wsm_ prefix). No per-route auth checks needed here.
"""
from __future__ import annotations

import logging
import os

import psutil
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.admin import models, repository

logger = logging.getLogger(__name__)


# --- Stats ---

async def get_stats(request: Request) -> JSONResponse:
    stats = await repository.get_stats()
    return JSONResponse(stats.model_dump())


# --- Search Logs ---

async def list_search_logs(request: Request) -> JSONResponse:
    page = int(request.query_params.get("page", "1"))
    page_size = int(request.query_params.get("page_size", "20"))
    result = await repository.list_search_logs(
        page=page, page_size=page_size,
        query_filter=request.query_params.get("query"),
        ip_filter=request.query_params.get("ip"),
        key_filter=request.query_params.get("key_id"),
    )
    return JSONResponse({
        "items": [item.model_dump() for item in result.items],
        "total": result.total, "page": result.page, "page_size": result.page_size,
    })


# --- API Keys ---

async def list_keys(request: Request) -> JSONResponse:
    keys = await repository.list_api_keys()
    return JSONResponse([k.model_dump() for k in keys])


async def create_key(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        data = models.APIKeyCreate(**body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    key = await repository.create_api_key(
        name=data.name, call_limit=data.call_limit, expires_at=data.expires_at,
    )
    return JSONResponse(key.model_dump(), status_code=201)


async def delete_key(request: Request) -> JSONResponse:
    key_id = request.path_params["key_id"]
    ok = await repository.revoke_api_key(key_id)
    if not ok:
        return JSONResponse({"error": "Key not found"}, status_code=404)
    return JSONResponse({"ok": True})


async def update_key(request: Request) -> JSONResponse:
    key_id = request.path_params["key_id"]
    try:
        body = await request.json()
        is_active = bool(body.get("is_active"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    ok = await repository.set_api_key_active(key_id, is_active)
    if not ok:
        return JSONResponse({"error": "Key not found"}, status_code=404)
    return JSONResponse({"ok": True, "is_active": is_active})


# --- IP Bans ---

async def list_ip_bans(request: Request) -> JSONResponse:
    bans = await repository.list_bans()
    return JSONResponse([b.model_dump() for b in bans])


async def create_ip_ban(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        data = models.IPBanCreate(**body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    ban = await repository.ban_ip(ip=data.ip, reason=data.reason)
    return JSONResponse(ban.model_dump(), status_code=201)


async def delete_ip_ban(request: Request) -> JSONResponse:
    ip = request.path_params["ip"]
    ok = await repository.unban_ip(ip)
    if not ok:
        return JSONResponse({"error": "IP not found in ban list"}, status_code=404)
    return JSONResponse({"ok": True})


# --- System Info ---

async def get_system(request: Request) -> JSONResponse:
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    proc = psutil.Process(os.getpid())
    proc_mem = proc.memory_info()

    # Read pool stats from Redis (real-time), fallback to direct import
    pool_stats = await repository.get_pool_stats()
    if pool_stats is None:
        pool_stats = {"started": False, "pool_size": 0, "active_tabs": 0,
                      "total_requests": 0, "total_failures": 0,
                      "consecutive_failures": 0, "restart_count": 0}
        try:
            import sys
            main_mod = sys.modules.get("__main__")
            pool = getattr(main_mod, "_pool_instance", None) if main_mod else None
            if pool is None:
                from src.mcp_server import _pool_instance as pool
            if pool:
                pool_stats = pool.stats
        except Exception:
            pass

    return JSONResponse({
        "cpu_percent": cpu,
        "memory": {
            "total_gb": round(mem.total / (1024**3), 2),
            "used_gb": round(mem.used / (1024**3), 2),
            "percent": mem.percent,
        },
        "process": {
            "rss_mb": round(proc_mem.rss / (1024**2), 1),
            "vms_mb": round(proc_mem.vms / (1024**2), 1),
        },
        "pool": pool_stats,
    })


# --- Analytics ---

async def get_analytics(request: Request) -> JSONResponse:
    hours = int(request.query_params.get("hours", "24"))
    if hours not in (24, 168):
        hours = 24
    data = await repository.get_analytics(hours=hours)
    return JSONResponse(data)


# --- Proxies ---

async def list_proxies(request: Request) -> JSONResponse:
    active_only = request.query_params.get("active_only", "").lower() == "true"
    proxies = await repository.list_proxies(active_only=active_only)
    return JSONResponse([p.model_dump() for p in proxies])


async def import_proxies(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        data = models.ProxyCreate(**body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    added = await repository.add_proxies(data.urls, default_scheme=data.scheme)
    # Hot-reload proxies into BrowserPool
    await _reload_proxies()
    return JSONResponse({"added": added}, status_code=201)


async def get_proxy_stats(request: Request) -> JSONResponse:
    stats = await repository.get_proxy_stats()
    return JSONResponse(stats.model_dump())


async def delete_proxy(request: Request) -> JSONResponse:
    proxy_id = int(request.path_params["id"])
    ok = await repository.delete_proxy(proxy_id)
    if not ok:
        return JSONResponse({"error": "Proxy not found"}, status_code=404)
    await _reload_proxies()
    return JSONResponse({"ok": True})


async def toggle_proxy(request: Request) -> JSONResponse:
    proxy_id = int(request.path_params["id"])
    try:
        body = await request.json()
        is_active = body.get("is_active", True)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    ok = await repository.toggle_proxy(proxy_id, is_active)
    if not ok:
        return JSONResponse({"error": "Proxy not found"}, status_code=404)
    await _reload_proxies()
    return JSONResponse({"ok": True})


async def test_proxy(request: Request) -> JSONResponse:
    from src.scraper.proxy import test_proxy as run_test

    proxy_id = int(request.path_params["id"])
    proxy = await repository.get_proxy(proxy_id)
    if not proxy:
        return JSONResponse({"error": "Proxy not found"}, status_code=404)

    try:
        timeout = float(request.query_params.get("timeout", "8"))
    except ValueError:
        timeout = 8.0
    timeout = max(1.0, min(timeout, 20.0))

    result = await run_test(proxy.url, timeout=timeout)
    return JSONResponse(result)


async def _reload_proxies() -> None:
    """Reload active proxies from DB into the running BrowserPool."""
    try:
        urls = await repository.get_active_proxy_urls()
        # Get pool instance
        import sys
        main_mod = sys.modules.get("__main__")
        pool = getattr(main_mod, "_pool_instance", None) if main_mod else None
        if pool is None:
            from src.mcp_server import _pool_instance as pool
        if pool and hasattr(pool, "update_proxies"):
            await pool.update_proxies(urls)
            logger.info("[admin] hot-reloaded %d proxies into BrowserPool", len(urls))
    except Exception as exc:
        logger.warning("[admin] proxy hot-reload failed: %s", exc)


# --- Starlette sub-app ---

admin_routes = [
    Route("/admin/api/stats", get_stats, methods=["GET"]),
    Route("/admin/api/search-logs", list_search_logs, methods=["GET"]),
    Route("/admin/api/keys", list_keys, methods=["GET"]),
    Route("/admin/api/keys", create_key, methods=["POST"]),
    Route("/admin/api/keys/{key_id}", delete_key, methods=["DELETE"]),
    Route("/admin/api/keys/{key_id}", update_key, methods=["PATCH"]),
    Route("/admin/api/ip-bans", list_ip_bans, methods=["GET"]),
    Route("/admin/api/ip-bans", create_ip_ban, methods=["POST"]),
    Route("/admin/api/ip-bans/{ip:path}", delete_ip_ban, methods=["DELETE"]),
    Route("/admin/api/system", get_system, methods=["GET"]),
    Route("/admin/api/analytics", get_analytics, methods=["GET"]),
    Route("/admin/api/proxies", list_proxies, methods=["GET"]),
    Route("/admin/api/proxies", import_proxies, methods=["POST"]),
    Route("/admin/api/proxies/stats", get_proxy_stats, methods=["GET"]),
    Route("/admin/api/proxies/{id:int}", delete_proxy, methods=["DELETE"]),
    Route("/admin/api/proxies/{id:int}", toggle_proxy, methods=["PATCH"]),
    Route("/admin/api/proxies/{id:int}/test", test_proxy, methods=["POST"]),
]


def create_admin_app() -> Starlette:
    """Create the admin API Starlette sub-application."""
    return Starlette(routes=admin_routes)
