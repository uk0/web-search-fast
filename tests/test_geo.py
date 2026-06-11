"""Tests for IP-derived per-proxy geo fingerprint resolution."""
from __future__ import annotations

import pytest

from src.scraper import geo


@pytest.fixture(autouse=True)
def _clear():
    geo.clear_cache()
    yield
    geo.clear_cache()


class TestHttpxProxyUrl:
    def test_bridge_url_passthrough(self):
        # SOCKS5-auth proxies arrive as a creds-free local bridge URL
        cfg = {"server": "http://127.0.0.1:19100"}
        assert geo._httpx_proxy_url(cfg) == "http://127.0.0.1:19100"

    def test_http_auth_folded_into_url(self):
        cfg = {"server": "http://host.example:8080", "username": "u", "password": "p"}
        assert geo._httpx_proxy_url(cfg) == "http://u:p@host.example:8080"


class TestGeoForIp:
    def test_known_ip_resolves(self):
        fp = geo._geo_for_ip("8.8.8.8")
        assert fp is not None
        assert fp["ip"] == "8.8.8.8"
        assert "-" in fp["locale"]  # e.g. en-US
        assert "/" in fp["timezone_id"]  # e.g. America/Chicago
        assert isinstance(fp["latitude"], float)

    def test_private_ip_returns_none(self):
        assert geo._geo_for_ip("10.0.0.0") is None

    def test_garbage_ip_returns_none(self):
        assert geo._geo_for_ip("not-an-ip") is None


class TestContextGeoKwargs:
    def test_shape(self):
        fp = {"ip": "1.2.3.4", "locale": "ja-JP", "timezone_id": "Asia/Tokyo",
              "latitude": 35.6, "longitude": 139.6}
        kw = geo.context_geo_kwargs(fp)
        assert kw["locale"] == "ja-JP"
        assert kw["timezone_id"] == "Asia/Tokyo"
        assert kw["geolocation"]["latitude"] == 35.6
        assert kw["permissions"] == ["geolocation"]


class TestCacheLifecycle:
    @pytest.mark.asyncio
    async def test_resolve_populates_cache(self, monkeypatch):
        async def fake_fetch(proxy_url, timeout):
            return "8.8.8.8"
        monkeypatch.setattr(geo, "_fetch_exit_ip", fake_fetch)

        url = "socks5h://user:pass@1.2.3.4:1080"
        fp = await geo.resolve(url, {"server": "http://127.0.0.1:19100"})
        assert fp is not None
        assert geo.peek(url) == fp
        assert geo.should_resolve(url) is False  # already resolved

    @pytest.mark.asyncio
    async def test_resolve_failure_enters_cooldown(self, monkeypatch):
        async def fake_fetch(proxy_url, timeout):
            return None  # exit IP unreachable
        monkeypatch.setattr(geo, "_fetch_exit_ip", fake_fetch)

        url = "http://dead:8080"
        fp = await geo.resolve(url, {"server": "http://dead:8080"})
        assert fp is None
        assert geo.peek(url) is None
        assert geo.should_resolve(url) is False  # in failure cooldown

    def test_should_resolve_fresh_proxy(self):
        assert geo.should_resolve("http://new:8080") is True

    def test_clear_cache_resets(self, monkeypatch):
        geo._resolved["x"] = {"ip": "1.1.1.1"}
        geo._failed["y"] = 123.0
        geo.clear_cache()
        assert geo.peek("x") is None
        assert geo.should_resolve("y") is True
