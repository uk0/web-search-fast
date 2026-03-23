"""MCP server mode — exposes web search as MCP tools for LLM clients."""
from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

if TYPE_CHECKING:
    from src.scraper.browser import BrowserPool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton browser pool — shared across all SSE sessions
# ---------------------------------------------------------------------------

_pool_instance: BrowserPool | None = None
_pool_started: bool = False


async def _ensure_pool() -> BrowserPool:
    """Return the global BrowserPool, starting it on first call."""
    global _pool_instance, _pool_started
    if _pool_instance is not None and _pool_started:
        logger.debug("[pool] already started, returning singleton")
        return _pool_instance
    logger.info("[pool] cold start — initializing BrowserPool ...")
    import time as _t
    t0 = _t.monotonic()
    from src.config import get_config
    from src.scraper.browser import BrowserPool

    config = get_config()
    # Load proxy list: DB first, then file, merge and deduplicate
    proxy_list: list[str] | None = None
    db_proxies: list[str] = []
    try:
        from src.admin.repository import get_active_proxy_urls
        db_proxies = await get_active_proxy_urls()
        if db_proxies:
            logger.info("[pool] loaded %d proxies from database", len(db_proxies))
    except Exception as exc:
        logger.debug("[pool] no DB proxies available: %s", exc)

    file_proxies: list[str] = []
    if config.browser.proxy_list_file:
        try:
            from src.scraper.proxy import ProxyRotator
            rotator = ProxyRotator.from_file(config.browser.proxy_list_file)
            file_proxies = rotator._proxies
            logger.info("[pool] loaded %d proxies from %s", rotator.count, config.browser.proxy_list_file)
        except Exception as exc:
            logger.warning("[pool] failed to load proxy list: %s", exc)

    # Merge and deduplicate (DB takes priority)
    seen = set()
    merged: list[str] = []
    for url in db_proxies + file_proxies:
        if url not in seen:
            seen.add(url)
            merged.append(url)
    if merged:
        proxy_list = merged
        logger.info("[pool] total unique proxies: %d (db=%d, file=%d)", len(merged), len(db_proxies), len(file_proxies))
    _pool_instance = BrowserPool(
        pool_size=config.browser.pool_size,
        max_pool_size=config.browser.max_pool_size,
        headless=config.browser.headless,
        geoip=config.browser.geoip,
        humanize=config.browser.humanize,
        locale=config.browser.locale,
        block_images=config.browser.block_images,
        proxy=config.browser.proxy,
        os_target=config.browser.os_target,
        fonts=config.browser.fonts,
        block_webgl=config.browser.block_webgl,
        addons=config.browser.addons,
        proxy_list=proxy_list,
    )
    # Wire up Redis stats push callback
    try:
        from src.admin.repository import save_pool_stats
        _pool_instance.set_stats_callback(save_pool_stats)
    except Exception:
        pass
    await _pool_instance.start()
    _pool_started = True
    logger.info("[pool] BrowserPool ready (size=%d, max=%d) in %.1fms",
                config.browser.pool_size, config.browser.max_pool_size,
                (_t.monotonic() - t0) * 1000)
    return _pool_instance


async def _shutdown_pool() -> None:
    """Stop the global BrowserPool."""
    global _pool_instance, _pool_started
    if _pool_instance and _pool_started:
        await _pool_instance.stop()
        _pool_started = False
        logger.info("BrowserPool stopped")


# ---------------------------------------------------------------------------
# Lifespan — per-session, but pool is singleton so startup is instant
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Each MCP session gets a ref to the shared pool."""
    logger.info("[lifespan] session starting — acquiring pool")
    pool = await _ensure_pool()
    logger.info("[lifespan] session ready — pool._started=%s", pool._started)
    yield {"pool": pool}
    logger.info("[lifespan] session ending")


mcp = FastMCP(
    "web-search-fast",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
    instructions=(
        "Real-time web search and page reading service using a stealth browser. "
        "Use this when you need CURRENT information that may be beyond your training data, including: "
        "latest documentation, recent news/events, up-to-date API references, "
        "package versions, changelogs, bug reports, security advisories, "
        "pricing, availability, or any fact that changes over time. "
        "Also use this to verify uncertain claims or find authoritative sources. "
        "Prefer engine='duckduckgo' for speed and reliability. "
        "Use depth=1 for quick lookups, depth=2 when you need full page content."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="web_search",
    description=(
        "Search the web for current information. Use this when you need to find "
        "up-to-date facts, documentation, tutorials, news, package info, error solutions, "
        "or anything that may have changed after your training cutoff. "
        "Returns titles, URLs, and snippets in markdown. "
        "Set depth=2 to also fetch full page content for each result (slower but more detailed). "
        "Prefer engine='duckduckgo' for speed; use 'google' for broader coverage."
    ),
)
async def web_search(
    query: str,
    engine: str = "duckduckgo",
    depth: int = 1,
    max_results: int = 5,
    ctx: Context = None,
) -> str:
    """Search the web and return results as markdown."""
    import json as _json
    import time as _t
    t0 = _t.monotonic()
    logger.info("[web_search] called: query=%r engine=%s depth=%d max_results=%d", query, engine, depth, max_results)

    from src.api.schemas import SearchRequest
    from src.config import SearchEngine
    from src.core.search import SearchError, do_search
    from src.formatter.json_fmt import format_json
    from src.formatter.markdown_fmt import format_markdown

    pool: BrowserPool = ctx.request_context.lifespan_context["pool"]
    logger.debug("[web_search] pool acquired, _started=%s", pool._started)

    try:
        search_engine = SearchEngine(engine.lower())
    except ValueError:
        return f"Error: Unknown engine '{engine}'. Available: google, bing, duckduckgo"

    depth = max(1, min(3, depth))
    max_results = max(1, min(20, max_results))

    try:
        req = SearchRequest(
            query=query,
            engine=search_engine,
            depth=depth,
            max_results=max_results,
            timeout=25,
        )
        logger.info("[web_search] starting search ...")
        response = await do_search(pool, req)
        result = format_markdown(response)
        logger.info("[web_search] done in %.0fms, %d results", (_t.monotonic() - t0) * 1000, response.total)
        return result
    except SearchError as e:
        logger.error("[web_search] SearchError: %s", e)
        return f"Search error: {e}"
    except Exception as e:
        logger.exception("[web_search] unexpected error")
        return f"Error: {e}"


@mcp.tool(
    name="get_page_content",
    description=(
        "Fetch and read a single web page, extracting its main content as clean markdown. "
        "Use this to read full articles, documentation pages, blog posts, or any URL "
        "you already know. Ideal after web_search when you need the complete content "
        "of a specific result, or when the user provides a URL to read."
    ),
)
async def get_page_content(
    url: str,
    ctx: Context = None,
) -> str:
    """Fetch and extract content from a specific URL."""
    import time as _t
    t0 = _t.monotonic()
    logger.info("[get_page_content] called: url=%r", url)

    from src.core.search import fetch_url_content

    pool: BrowserPool = ctx.request_context.lifespan_context["pool"]

    try:
        content = await fetch_url_content(pool, url, timeout=20)
        elapsed = (_t.monotonic() - t0) * 1000
        if not content:
            logger.warning("[get_page_content] empty content from %s (%.0fms)", url, elapsed)
            return f"Could not extract content from {url}"
        logger.info("[get_page_content] done in %.0fms, %d chars", elapsed, len(content))
        return f"# Content from {url}\n\n{content}"
    except Exception as e:
        logger.exception("[get_page_content] error fetching %s", url)
        return f"Error fetching {url}: {e}"


@mcp.tool(
    name="list_search_engines",
    description="List available search engines and check browser pool health status.",
)
async def list_search_engines(
    ctx: Context = None,
) -> str:
    """List available search engines."""
    from src.core.search import ENGINES

    pool: BrowserPool = ctx.request_context.lifespan_context["pool"]
    stats = pool.stats
    lines = [
        "# Available Search Engines",
        "",
        "## Browser Pool Status",
        "",
        f"- **Active:** {stats['started']}",
        f"- **Pool size:** {stats['pool_size']}",
        f"- **Total requests:** {stats['total_requests']}",
        f"- **Total failures:** {stats['total_failures']}",
        f"- **Consecutive failures:** {stats['consecutive_failures']}",
        f"- **Restarts:** {stats['restart_count']}",
        "",
        "## Engines",
        "",
    ]
    for engine_enum, engine_impl in ENGINES.items():
        lines.append(f"- **{engine_enum.value}** ({engine_impl.__class__.__name__})")
    lines.extend([
        "",
        "## Notes",
        "",
        "- **DuckDuckGo**: Most reliable, recommended as default",
        "- **Google**: May trigger captcha on some IPs, auto-falls back to DuckDuckGo",
        "- **Bing**: Uses global.bing.com to avoid geo-redirect",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for the MCP server."""
    parser = argparse.ArgumentParser(description="Web Search MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "http"],
        default="http",
        help="Transport protocol (default: http)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8897, help="Bind port (default: 8897)")
    args = parser.parse_args()

    if args.transport == "stdio":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            filename="/tmp/web-search-mcp.log",
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )

    if args.transport in ("sse", "http"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port

        import os

        import uvicorn
        from starlette.routing import Mount
        from starlette.staticfiles import StaticFiles

        from src.admin.database import close_db, init_db
        from src.admin.routes import admin_routes
        from src.config import get_admin_config
        from src.middleware.api_key_auth import APIKeyAuthMiddleware
        from src.middleware.ip_ban import IPBanMiddleware
        from src.middleware.search_log import SearchLogMiddleware

        admin_cfg = get_admin_config()

        if args.transport == "http":
            app = mcp.streamable_http_app()
        else:
            app = mcp.sse_app()

        # Wrap the MCP app's lifespan to also init admin DB + browser pool
        _original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def _combined_lifespan(a):
            await init_db(admin_cfg.db_path)
            from src.admin.repository import init_redis
            await init_redis(admin_cfg.redis_url or None)
            await _ensure_pool()
            logger.info(
                "%s server starting on %s:%d (admin: %s)",
                args.transport.upper(), args.host, args.port,
                "enabled" if admin_cfg.admin_token else "open",
            )
            try:
                async with _original_lifespan(a) as state:
                    yield state
            finally:
                await _shutdown_pool()
                await close_db()

        app.router.lifespan_context = _combined_lifespan

        # Health check endpoint (no auth required)
        from starlette.responses import JSONResponse as _JSONResponse
        from starlette.routing import Route as _Route

        async def _health(request):
            pool = _pool_instance
            healthy = pool._started if pool else False
            return _JSONResponse({"status": "ok" if healthy else "degraded", "browser": healthy})

        async def _pool_stats(request):
            pool = _pool_instance
            if not pool:
                return _JSONResponse({"error": "Pool not initialized"}, status_code=503)
            return _JSONResponse(pool.stats)

        app.routes.insert(0, _Route("/health", _health))
        app.routes.insert(0, _Route("/pool/stats", _pool_stats))

        # ---- Search REST API (GET/POST /search) ----
        from starlette.responses import PlainTextResponse as _PlainTextResponse

        async def _search_get(request):
            """GET /search?q=...&engine=...&depth=...&format=...&max_results=...&timeout=..."""
            from src.api.schemas import SearchRequest
            from src.config import OutputFormat, SearchEngine
            from src.core.search import SearchError, do_search
            from src.formatter.json_fmt import format_json
            from src.formatter.markdown_fmt import format_markdown

            q = request.query_params.get("q", "").strip()
            if not q:
                return _JSONResponse({"error": "Missing required parameter: q"}, status_code=400)

            try:
                req = SearchRequest(
                    query=q,
                    engine=SearchEngine(request.query_params.get("engine", "duckduckgo").lower()),
                    depth=int(request.query_params.get("depth", "1")),
                    format=OutputFormat(request.query_params.get("format", "json").lower()),
                    max_results=int(request.query_params.get("max_results", "10")),
                    timeout=int(request.query_params.get("timeout", "30")),
                )
            except (ValueError, Exception) as e:
                return _JSONResponse({"error": f"Invalid parameter: {e}"}, status_code=400)

            pool = _pool_instance
            if not pool or not _pool_started:
                return _JSONResponse({"error": "Browser pool not ready"}, status_code=503)

            try:
                response = await do_search(pool, req)
                if req.format == OutputFormat.MARKDOWN:
                    return _PlainTextResponse(format_markdown(response), media_type="text/markdown")
                return _JSONResponse(format_json(response))
            except SearchError as e:
                return _JSONResponse({"error": str(e)}, status_code=503)

        async def _search_post(request):
            """POST /search {query, engine, depth, format, max_results, timeout}"""
            from src.api.schemas import SearchRequest
            from src.config import OutputFormat
            from src.core.search import SearchError, do_search
            from src.formatter.json_fmt import format_json
            from src.formatter.markdown_fmt import format_markdown

            try:
                body = await request.json()
            except Exception:
                return _JSONResponse({"error": "Invalid JSON body"}, status_code=400)

            try:
                req = SearchRequest(**body)
            except Exception as e:
                return _JSONResponse({"error": f"Invalid request: {e}"}, status_code=400)

            pool = _pool_instance
            if not pool or not _pool_started:
                return _JSONResponse({"error": "Browser pool not ready"}, status_code=503)

            try:
                response = await do_search(pool, req)
                if req.format == OutputFormat.MARKDOWN:
                    return _PlainTextResponse(format_markdown(response), media_type="text/markdown")
                return _JSONResponse(format_json(response))
            except SearchError as e:
                return _JSONResponse({"error": str(e)}, status_code=503)

        app.routes.insert(0, _Route("/search", _search_get, methods=["GET"]))
        app.routes.insert(0, _Route("/search", _search_post, methods=["POST"]))

        # Add admin routes before MCP routes
        for route in reversed(admin_routes):
            app.routes.insert(0, route)

        # Serve admin SPA static files if built (after API routes, before MCP catch-all)
        # Try multiple paths: local dev (__file__ = src/mcp_server.py) and Nuitka binary
        _base = os.path.dirname(os.path.abspath(__file__))
        static_dir = os.path.join(_base, "admin", "static")
        if not os.path.isdir(static_dir):
            static_dir = os.path.join(_base, "src", "admin", "static")
        if os.path.isdir(static_dir):
            from starlette.responses import FileResponse
            from starlette.routing import Route

            index_html = os.path.join(static_dir, "index.html")

            async def spa_fallback(request):
                """Serve index.html for all /admin/* paths (SPA client-side routing).
                Skip /admin/api/* — those are handled by API routes.
                """
                if request.url.path.startswith("/admin/api/"):
                    from starlette.responses import JSONResponse
                    return JSONResponse({"error": "Not found"}, status_code=404)
                return FileResponse(index_html)

            # Static assets first (JS, CSS, images)
            app.routes.insert(-1, Mount("/admin/assets", app=StaticFiles(directory=os.path.join(static_dir, "assets")), name="admin-assets"))
            # SPA catch-all — must be after /admin/api/* routes
            app.routes.insert(-1, Route("/admin/{path:path}", spa_fallback))
            app.routes.insert(-1, Route("/admin", spa_fallback))

        # Middleware stack (last added = outermost = runs first)
        app.add_middleware(IPBanMiddleware)
        app.add_middleware(APIKeyAuthMiddleware)
        app.add_middleware(SearchLogMiddleware)

        async def serve() -> None:
            config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
            server = uvicorn.Server(config)
            await server.serve()

        asyncio.run(serve())
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
