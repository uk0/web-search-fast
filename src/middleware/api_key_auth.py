"""API Key authentication middleware — validates Bearer tokens against database."""
from __future__ import annotations

import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Paths that skip API key auth: health/pool plus the ops probe + metrics
# surface, which orchestrators (K8s, Prometheus) must scrape without a token.
_SKIP_PREFIXES = ("/health", "/pool/", "/livez", "/readyz", "/metrics")

# Admin static assets don't need auth (JS/CSS/images)
_ADMIN_STATIC_PREFIXES = ("/admin/assets/",)


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Validate API key from Bearer token.

    Auth model:
    - ADMIN_TOKEN env var: grants access to admin panel API (/admin/api/*)
    - Database API keys (wsm_ prefix): grants access to MCP/search endpoints
    - ADMIN_TOKEN also grants access to MCP/search endpoints (superuser)

    If no ADMIN_TOKEN is set and no DB keys exist, all endpoints are open.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path

        # Skip auth for health checks and pool stats
        for prefix in _SKIP_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # Skip auth for admin static assets (JS, CSS, images)
        for prefix in _ADMIN_STATIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # Admin SPA HTML pages (not /admin/api/*) — skip auth so the SPA can load,
        # the SPA itself will call /admin/api/* with the token
        if path.startswith("/admin") and not path.startswith("/admin/api/"):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        admin_token = os.environ.get("ADMIN_TOKEN", "")

        # If no auth configured at all (no ADMIN_TOKEN and no DB keys), pass through
        if not admin_token:
            try:
                from src.admin.repository import list_api_keys
                keys = await list_api_keys()
                if not keys:
                    return await call_next(request)
            except Exception:
                return await call_next(request)

        if not auth_header.startswith("Bearer "):
            return JSONResponse({"error": "Missing Bearer token"}, status_code=401)

        token = auth_header[7:]

        # 1. Check ADMIN_TOKEN (superuser — access to everything)
        if admin_token and token == admin_token:
            request.state.api_key_id = None
            return await call_next(request)

        # 2. Admin API routes require ADMIN_TOKEN — wsm_ keys cannot access
        if path.startswith("/admin/api/"):
            return JSONResponse({"error": "Admin API requires ADMIN_TOKEN"}, status_code=403)

        # 3. Check database API keys (wsm_ prefix) — MCP/search only
        try:
            from src.admin.repository import increment_call_count, verify_api_key
            key = await verify_api_key(token)
            if key:
                # Check call limit
                if key.call_limit > 0 and key.call_count >= key.call_limit:
                    return JSONResponse({"error": "API key call limit exceeded"}, status_code=429)
                await increment_call_count(key.id)
                request.state.api_key_id = key.id
                return await call_next(request)
        except Exception:
            pass

        return JSONResponse({"error": "Invalid token"}, status_code=403)
