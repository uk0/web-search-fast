from __future__ import annotations

import os
from enum import Enum

from pydantic import BaseModel, Field


class SearchEngine(str, Enum):
    GOOGLE = "google"
    BING = "bing"
    DUCKDUCKGO = "duckduckgo"


class OutputFormat(str, Enum):
    JSON = "json"
    MARKDOWN = "markdown"


class BrowserConfig(BaseModel):
    pool_size: int = Field(default=30, ge=1, le=100, description="Initial browser concurrency slots")
    max_pool_size: int = Field(default=90, ge=1, le=100, description="Max auto-scaled concurrency slots")
    headless: bool = Field(default=True)
    timeout: int = Field(default=30, ge=5, le=120, description="Page load timeout in seconds")
    geoip: bool = Field(default=True, description="Enable GeoIP spoofing based on real IP")
    humanize: float = Field(default=0.5, ge=0, description="Humanized cursor movement duration (0 to disable)")
    locale: str = Field(default="en-US", description="Browser locale")
    block_images: bool = Field(default=True, description="Block image loading for faster page loads")
    # Advanced Camoufox features
    proxy: str = Field(default="", description="Proxy URL (e.g. socks5://127.0.0.1:1080)")
    os_target: str = Field(default="", description="Target OS fingerprint: windows, macos, linux")
    fonts: list[str] = Field(default_factory=list, description="Custom font list for fingerprint")
    block_webgl: bool = Field(default=False, description="Block WebGL fingerprinting")
    addons: list[str] = Field(default_factory=list, description="Firefox addon paths to load")
    proxy_list_file: str = Field(default="", description="Path to proxy list file (one per line)")


class AppConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    max_concurrent_searches: int = Field(default=10, ge=1)
    default_max_results: int = Field(default=10, ge=1, le=50)


class AdminConfig(BaseModel):
    admin_token: str = Field(default="", description="Admin panel auth token")
    db_path: str = Field(default="data/wsm.db", description="SQLite database path")
    redis_url: str = Field(default="", description="Redis URL (optional)")


def get_config() -> AppConfig:
    """Build config, applying env var overrides."""
    browser_kwargs: dict = {}
    if pool_size := os.environ.get("BROWSER_POOL_SIZE"):
        browser_kwargs["pool_size"] = int(pool_size)
    if max_pool_size := os.environ.get("BROWSER_MAX_POOL_SIZE"):
        browser_kwargs["max_pool_size"] = int(max_pool_size)
    if proxy := os.environ.get("BROWSER_PROXY"):
        browser_kwargs["proxy"] = proxy
    if os_target := os.environ.get("BROWSER_OS"):
        browser_kwargs["os_target"] = os_target
    if fonts := os.environ.get("BROWSER_FONTS"):
        browser_kwargs["fonts"] = [f.strip() for f in fonts.split(",") if f.strip()]
    if os.environ.get("BROWSER_BLOCK_WEBGL", "").lower() in ("1", "true", "yes"):
        browser_kwargs["block_webgl"] = True
    if addons := os.environ.get("BROWSER_ADDONS"):
        browser_kwargs["addons"] = [a.strip() for a in addons.split(",") if a.strip()]
    if proxy_list_file := os.environ.get("BROWSER_PROXY_LIST"):
        browser_kwargs["proxy_list_file"] = proxy_list_file
    return AppConfig(browser=BrowserConfig(**browser_kwargs))


def get_admin_config() -> AdminConfig:
    """Build admin config from environment variables."""
    return AdminConfig(
        admin_token=os.environ.get("ADMIN_TOKEN", ""),
        db_path=os.environ.get("WSM_DB_PATH", "data/wsm.db"),
        redis_url=os.environ.get("REDIS_URL", ""),
    )
