"""Tests for middleware: IP ban, API key auth, search log."""
from __future__ import annotations

import os
import tempfile

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.admin.database import close_db, init_db


@pytest.fixture(autouse=True)
async def _setup_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    await init_db(db_path)
    yield
    await close_db()
    os.unlink(db_path)


def _make_app_with_middleware(*middleware_classes):
    async def homepage(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[
        Route("/mcp", homepage, methods=["GET", "POST"]),
        Route("/admin/api/test", homepage),
    ])
    for mw in reversed(middleware_classes):
        app.add_middleware(mw)
    return TestClient(app)


class TestIPBanMiddleware:
    def test_allows_unbanned_ip(self):
        from src.middleware.ip_ban import IPBanMiddleware
        client = _make_app_with_middleware(IPBanMiddleware)
        resp = client.get("/mcp")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_blocks_banned_ip(self):
        from src.admin.repository import ban_ip
        from src.middleware.ip_ban import IPBanMiddleware

        # TestClient uses "testclient" as client host
        await ban_ip("testclient", reason="test ban")
        client = _make_app_with_middleware(IPBanMiddleware)
        resp = client.get("/mcp")
        assert resp.status_code == 403
        assert "banned" in resp.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_unban_allows_access(self):
        from src.admin.repository import ban_ip, unban_ip
        from src.middleware.ip_ban import IPBanMiddleware

        await ban_ip("testclient")
        await unban_ip("testclient")
        client = _make_app_with_middleware(IPBanMiddleware)
        resp = client.get("/mcp")
        assert resp.status_code == 200


class TestAPIKeyAuthMiddleware:
    def test_admin_routes_skip_auth(self):
        from src.middleware.api_key_auth import APIKeyAuthMiddleware
        client = _make_app_with_middleware(APIKeyAuthMiddleware)
        resp = client.get("/admin/api/test")
        assert resp.status_code == 200

    def test_no_auth_when_no_token_and_no_keys(self):
        from src.middleware.api_key_auth import APIKeyAuthMiddleware
        with _patch_env("ADMIN_TOKEN", ""):
            client = _make_app_with_middleware(APIKeyAuthMiddleware)
            resp = client.get("/mcp")
            assert resp.status_code == 200

    def test_admin_token_works(self):
        from src.middleware.api_key_auth import APIKeyAuthMiddleware
        with _patch_env("ADMIN_TOKEN", "my-secret"):
            client = _make_app_with_middleware(APIKeyAuthMiddleware)
            resp = client.get("/mcp", headers={"Authorization": "Bearer my-secret"})
            assert resp.status_code == 200

    def test_wrong_token_rejected(self):
        from src.middleware.api_key_auth import APIKeyAuthMiddleware
        with _patch_env("ADMIN_TOKEN", "my-secret"):
            client = _make_app_with_middleware(APIKeyAuthMiddleware)
            resp = client.get("/mcp", headers={"Authorization": "Bearer wrong"})
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_db_api_key_works(self):
        from src.admin.repository import create_api_key
        from src.middleware.api_key_auth import APIKeyAuthMiddleware

        key = await create_api_key("test-key", call_limit=10)
        with _patch_env("ADMIN_TOKEN", ""):
            client = _make_app_with_middleware(APIKeyAuthMiddleware)
            resp = client.get("/mcp", headers={"Authorization": f"Bearer {key.key}"})
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_wsm_key_rejected_on_admin_api(self):
        """wsm_ API keys must NOT access /admin/api/* routes."""
        from src.admin.repository import create_api_key
        from src.middleware.api_key_auth import APIKeyAuthMiddleware

        key = await create_api_key("test-key", call_limit=100)
        with _patch_env("ADMIN_TOKEN", "admin-secret"):
            client = _make_app_with_middleware(APIKeyAuthMiddleware)
            # wsm_ key on admin API → 403
            resp = client.get("/admin/api/test", headers={"Authorization": f"Bearer {key.key}"})
            assert resp.status_code == 403
            assert "ADMIN_TOKEN" in resp.json()["error"]

    def test_admin_token_accesses_admin_api(self):
        """ADMIN_TOKEN can access /admin/api/* routes."""
        from src.middleware.api_key_auth import APIKeyAuthMiddleware
        with _patch_env("ADMIN_TOKEN", "admin-secret"):
            client = _make_app_with_middleware(APIKeyAuthMiddleware)
            resp = client.get("/admin/api/test", headers={"Authorization": "Bearer admin-secret"})
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_call_limit_enforced(self):
        from src.admin.repository import create_api_key
        from src.middleware.api_key_auth import APIKeyAuthMiddleware

        key = await create_api_key("limited-key", call_limit=1)
        with _patch_env("ADMIN_TOKEN", ""):
            client = _make_app_with_middleware(APIKeyAuthMiddleware)
            # First call succeeds
            resp = client.get("/mcp", headers={"Authorization": f"Bearer {key.key}"})
            assert resp.status_code == 200
            # Second call exceeds limit
            resp = client.get("/mcp", headers={"Authorization": f"Bearer {key.key}"})
            assert resp.status_code == 429


class TestSearchLogMiddleware:
    @pytest.mark.asyncio
    async def test_logs_mcp_request(self):
        from src.admin.repository import list_search_logs
        from src.middleware.search_log import SearchLogMiddleware

        client = _make_app_with_middleware(SearchLogMiddleware)
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "web_search", "arguments": {"query": "test query", "engine": "duckduckgo"}},
        })
        assert resp.status_code == 200

        logs = await list_search_logs()
        assert logs.total >= 1
        log = logs.items[0]
        assert log.query == "test query"
        assert log.tool_name == "web_search"
        assert log.status_code == 200
        assert log.request_body is not None
        assert "test query" in log.request_body

    @pytest.mark.asyncio
    async def test_logs_non_search_request(self):
        from src.admin.repository import list_search_logs
        from src.middleware.search_log import SearchLogMiddleware

        client = _make_app_with_middleware(SearchLogMiddleware)
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {},
        })
        assert resp.status_code == 200

        logs = await list_search_logs()
        assert logs.total >= 1
        log = logs.items[0]
        assert log.query == "initialize"
        assert log.tool_name == "initialize"

    @pytest.mark.asyncio
    async def test_skips_non_mcp_paths(self):
        from src.admin.repository import list_search_logs
        from src.middleware.search_log import SearchLogMiddleware

        client = _make_app_with_middleware(SearchLogMiddleware)
        resp = client.get("/admin/api/test")
        assert resp.status_code == 200

        logs = await list_search_logs()
        assert logs.total == 0

    @pytest.mark.asyncio
    async def test_skips_auth_rejections(self):
        # 401/403/429 are auth/limit rejections — must NOT be logged as requests.
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from src.admin.repository import list_search_logs
        from src.middleware.search_log import SearchLogMiddleware

        async def rejected(request):
            return JSONResponse({"error": "Invalid token"}, status_code=403)

        app = Starlette(routes=[Route("/mcp", rejected, methods=["POST"])])
        app.add_middleware(SearchLogMiddleware)
        client = TestClient(app)
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/call", "id": 1})
        assert resp.status_code == 403

        logs = await list_search_logs()
        assert logs.total == 0  # rejection not recorded


# --- Helpers ---

from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def _patch_env(key: str, value: str):
    with patch.dict(os.environ, {key: value}):
        yield
