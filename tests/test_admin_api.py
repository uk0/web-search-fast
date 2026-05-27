"""Tests for admin REST API endpoints."""
from __future__ import annotations

import os
import tempfile

import pytest
from starlette.testclient import TestClient

from src.admin.database import close_db, init_db
from src.admin.routes import admin_routes

ADMIN_TOKEN = "test-admin-tkn"  # noqa: S105 — test-only value


@pytest.fixture(autouse=True)
async def _setup_db():
    """Create a fresh in-memory-like temp DB for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    await init_db(db_path)
    yield
    await close_db()
    os.unlink(db_path)


@pytest.fixture()
def client():
    from starlette.applications import Starlette
    from src.middleware.api_key_auth import APIKeyAuthMiddleware
    app = Starlette(routes=admin_routes)
    app.add_middleware(APIKeyAuthMiddleware)
    return TestClient(app)


@pytest.fixture()
def auth_headers():
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


class TestStatsEndpoint:
    def test_get_stats(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.get("/admin/api/stats", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "total_searches" in data
        assert "active_keys" in data
        assert "banned_ips" in data

    def test_stats_no_auth_when_no_token(self, client):
        with _patch_admin_token(""):
            resp = client.get("/admin/api/stats")
        assert resp.status_code == 200


class TestAPIKeysEndpoints:
    def test_create_and_list_keys(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.post(
                "/admin/api/keys",
                json={"name": "test-key", "call_limit": 100},
                headers=auth_headers,
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["name"] == "test-key"
            assert data["call_limit"] == 100
            assert "key" in data  # plaintext key returned on creation
            assert data["key"].startswith("wsm_")

            # List keys
            resp = client.get("/admin/api/keys", headers=auth_headers)
            assert resp.status_code == 200
            keys = resp.json()
            assert len(keys) == 1
            assert keys[0]["name"] == "test-key"
            assert "key" not in keys[0]  # plaintext NOT in list

    def test_revoke_key(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.post(
                "/admin/api/keys",
                json={"name": "to-revoke"},
                headers=auth_headers,
            )
            key_id = resp.json()["id"]

            resp = client.delete(f"/admin/api/keys/{key_id}", headers=auth_headers)
            assert resp.status_code == 200

            # Verify it's revoked
            resp = client.get("/admin/api/keys", headers=auth_headers)
            keys = resp.json()
            assert keys[0]["is_active"] is False

    def test_revoke_nonexistent_key(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.delete(
                "/admin/api/keys/nonexistent-id", headers=auth_headers
            )
            assert resp.status_code == 404

    def test_create_key_invalid_body(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.post(
                "/admin/api/keys", json={}, headers=auth_headers
            )
            assert resp.status_code == 400

    def test_reactivate_revoked_key(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.post(
                "/admin/api/keys",
                json={"name": "to-reactivate"},
                headers=auth_headers,
            )
            key_id = resp.json()["id"]

            # Revoke first
            client.delete(f"/admin/api/keys/{key_id}", headers=auth_headers)
            keys = client.get("/admin/api/keys", headers=auth_headers).json()
            assert keys[0]["is_active"] is False

            # Reactivate via PATCH
            resp = client.patch(
                f"/admin/api/keys/{key_id}",
                json={"is_active": True},
                headers=auth_headers,
            )
            assert resp.status_code == 200
            assert resp.json()["is_active"] is True

            keys = client.get("/admin/api/keys", headers=auth_headers).json()
            assert keys[0]["is_active"] is True

    def test_patch_key_to_inactive(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.post(
                "/admin/api/keys",
                json={"name": "toggle-via-patch"},
                headers=auth_headers,
            )
            key_id = resp.json()["id"]
            resp = client.patch(
                f"/admin/api/keys/{key_id}",
                json={"is_active": False},
                headers=auth_headers,
            )
            assert resp.status_code == 200
            keys = client.get("/admin/api/keys", headers=auth_headers).json()
            assert keys[0]["is_active"] is False

    def test_patch_nonexistent_key(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.patch(
                "/admin/api/keys/missing-id",
                json={"is_active": True},
                headers=auth_headers,
            )
            assert resp.status_code == 404


class TestProxiesEndpoints:
    def test_import_and_list_proxies(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.post(
                "/admin/api/proxies",
                json={"urls": ["socks5h://user:pass@1.2.3.4:1080", "http://5.6.7.8:8080"]},
                headers=auth_headers,
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["added"] == 2

            resp = client.get("/admin/api/proxies", headers=auth_headers)
            assert resp.status_code == 200
            proxies = resp.json()
            assert len(proxies) == 2

    def test_import_duplicates_ignored(self, client, auth_headers):
        with _patch_admin_token():
            client.post(
                "/admin/api/proxies",
                json={"urls": ["socks5h://dup@host:1080"]},
                headers=auth_headers,
            )
            # Import same URL again
            client.post(
                "/admin/api/proxies",
                json={"urls": ["socks5h://dup@host:1080"]},
                headers=auth_headers,
            )
            resp = client.get("/admin/api/proxies", headers=auth_headers)
            proxies = resp.json()
            assert len(proxies) == 1

    def test_proxy_stats(self, client, auth_headers):
        with _patch_admin_token():
            client.post(
                "/admin/api/proxies",
                json={"urls": ["socks5h://a@h:1080", "http://b:8080"]},
                headers=auth_headers,
            )
            resp = client.get("/admin/api/proxies/stats", headers=auth_headers)
            assert resp.status_code == 200
            stats = resp.json()
            assert stats["total"] == 2
            assert stats["active"] == 2
            assert stats["inactive"] == 0

    def test_toggle_proxy(self, client, auth_headers):
        with _patch_admin_token():
            client.post(
                "/admin/api/proxies",
                json={"urls": ["socks5h://toggle@h:1080"]},
                headers=auth_headers,
            )
            proxies = client.get("/admin/api/proxies", headers=auth_headers).json()
            proxy_id = proxies[0]["id"]

            # Disable
            resp = client.patch(
                f"/admin/api/proxies/{proxy_id}",
                json={"is_active": False},
                headers=auth_headers,
            )
            assert resp.status_code == 200

            proxies = client.get("/admin/api/proxies", headers=auth_headers).json()
            assert proxies[0]["is_active"] is False

    def test_delete_proxy(self, client, auth_headers):
        with _patch_admin_token():
            client.post(
                "/admin/api/proxies",
                json={"urls": ["socks5h://del@h:1080"]},
                headers=auth_headers,
            )
            proxies = client.get("/admin/api/proxies", headers=auth_headers).json()
            proxy_id = proxies[0]["id"]

            resp = client.delete(f"/admin/api/proxies/{proxy_id}", headers=auth_headers)
            assert resp.status_code == 200

            proxies = client.get("/admin/api/proxies", headers=auth_headers).json()
            assert len(proxies) == 0

    def test_import_mixed_schemes(self, client, auth_headers):
        with _patch_admin_token():
            client.post(
                "/admin/api/proxies",
                json={"urls": [
                    "socks5h://u:p@h:1080",
                    "socks5://u:p@h:1081",
                    "http://h:8080",
                    "https://h:8443",
                ]},
                headers=auth_headers,
            )
            proxies = client.get("/admin/api/proxies", headers=auth_headers).json()
            schemes = {p["scheme"] for p in proxies}
            assert schemes == {"socks5h", "socks5", "http", "https"}

    def test_import_empty_list(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.post(
                "/admin/api/proxies",
                json={"urls": []},
                headers=auth_headers,
            )
            assert resp.status_code == 400

    def test_delete_nonexistent_proxy(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.delete("/admin/api/proxies/99999", headers=auth_headers)
            assert resp.status_code == 404

    def test_test_proxy_nonexistent(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.post("/admin/api/proxies/99999/test", headers=auth_headers)
            assert resp.status_code == 404

    def test_test_proxy_unreachable(self, client, auth_headers):
        # Point at a non-routable address so the test fails fast without network.
        with _patch_admin_token():
            client.post(
                "/admin/api/proxies",
                json={"urls": ["socks5h://127.0.0.1:1"]},
                headers=auth_headers,
            )
            proxies = client.get("/admin/api/proxies", headers=auth_headers).json()
            proxy_id = proxies[0]["id"]
            resp = client.post(
                f"/admin/api/proxies/{proxy_id}/test?timeout=2",
                headers=auth_headers,
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is False
            assert "error" in body
            assert "latency_ms" in body


class TestSystemEndpoint:
    def test_get_system(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.get("/admin/api/system", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "cpu_percent" in data
        assert "memory" in data
        assert "total_gb" in data["memory"]
        assert "percent" in data["memory"]
        assert "process" in data
        assert "rss_mb" in data["process"]
        assert "pool" in data
        assert "started" in data["pool"]
        assert "active_tabs" in data["pool"]


class TestAnalyticsEndpoint:
    def test_get_analytics_empty(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.get("/admin/api/analytics", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "timeline" in data
        assert "engines" in data
        assert "success_rate" in data
        assert isinstance(data["timeline"], list)
        assert isinstance(data["engines"], list)

    @pytest.mark.asyncio
    async def test_analytics_with_data(self, client, auth_headers):
        from src.admin.repository import log_search
        await log_search(query="test1", ip_address="1.1.1.1", engine="duckduckgo", status_code=200, elapsed_ms=150)
        await log_search(query="test2", ip_address="1.1.1.1", engine="google", status_code=200, elapsed_ms=300)
        await log_search(query="test3", ip_address="1.1.1.1", engine="duckduckgo", status_code=500, elapsed_ms=50)

        with _patch_admin_token():
            resp = client.get("/admin/api/analytics?hours=24", headers=auth_headers)
        data = resp.json()
        assert len(data["engines"]) >= 1
        assert data["success_rate"] < 100  # one 500 status

    def test_analytics_7d(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.get("/admin/api/analytics?hours=168", headers=auth_headers)
        assert resp.status_code == 200


class TestIPBansEndpoints:
    def test_ban_and_list(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.post(
                "/admin/api/ip-bans",
                json={"ip": "10.0.0.1", "reason": "spam"},
                headers=auth_headers,
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["ip_address"] == "10.0.0.1"
            assert data["reason"] == "spam"

            resp = client.get("/admin/api/ip-bans", headers=auth_headers)
            assert resp.status_code == 200
            bans = resp.json()
            assert len(bans) == 1

    def test_unban(self, client, auth_headers):
        with _patch_admin_token():
            client.post(
                "/admin/api/ip-bans",
                json={"ip": "10.0.0.2"},
                headers=auth_headers,
            )
            resp = client.delete("/admin/api/ip-bans/10.0.0.2", headers=auth_headers)
            assert resp.status_code == 200

            resp = client.get("/admin/api/ip-bans", headers=auth_headers)
            assert len(resp.json()) == 0

    def test_unban_nonexistent(self, client, auth_headers):
        with _patch_admin_token():
            resp = client.delete("/admin/api/ip-bans/99.99.99.99", headers=auth_headers)
            assert resp.status_code == 404


class TestSearchLogsEndpoint:
    @pytest.mark.asyncio
    async def test_list_logs(self, client, auth_headers):
        from src.admin.repository import log_search
        await log_search(query="test query", ip_address="1.2.3.4", engine="google")

        with _patch_admin_token():
            resp = client.get("/admin/api/search-logs", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["query"] == "test query"

    @pytest.mark.asyncio
    async def test_filter_by_ip(self, client, auth_headers):
        from src.admin.repository import log_search
        await log_search(query="q1", ip_address="1.1.1.1")
        await log_search(query="q2", ip_address="2.2.2.2")

        with _patch_admin_token():
            resp = client.get("/admin/api/search-logs?ip=1.1.1.1", headers=auth_headers)
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["ip_address"] == "1.1.1.1"


class TestAdminAuth:
    def test_wrong_admin_token_rejected(self, client):
        with _patch_admin_token():
            resp = client.get(
                "/admin/api/stats",
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert resp.status_code == 403

    def test_missing_admin_token_rejected(self, client):
        with _patch_admin_token():
            resp = client.get("/admin/api/stats")
            assert resp.status_code == 401


# --- Helpers ---

from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def _patch_admin_token(token: str = ADMIN_TOKEN):
    with patch.dict(os.environ, {"ADMIN_TOKEN": token}):
        yield
