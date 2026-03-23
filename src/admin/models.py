"""Pydantic models for admin API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class APIKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    call_limit: int = Field(default=0, ge=0, description="0 = unlimited")
    expires_at: str | None = None


class APIKeyOut(BaseModel):
    id: str
    name: str
    key_prefix: str
    call_limit: int
    call_count: int
    is_active: bool
    created_at: str
    expires_at: str | None = None


class APIKeyCreated(APIKeyOut):
    """Returned only on creation — includes the plaintext key."""
    key: str


class SearchLogOut(BaseModel):
    id: int
    api_key_id: str | None = None
    query: str
    engine: str | None = None
    ip_address: str
    user_agent: str | None = None
    status_code: int | None = None
    elapsed_ms: int | None = None
    request_body: str | None = None
    response_body: str | None = None
    tool_name: str | None = None
    created_at: str


class IPBanCreate(BaseModel):
    ip: str = Field(..., min_length=1)
    reason: str = ""


class IPBanOut(BaseModel):
    id: int
    ip_address: str
    reason: str
    created_at: str


class DashboardStats(BaseModel):
    total_searches: int = 0
    searches_today: int = 0
    active_keys: int = 0
    banned_ips: int = 0


class PaginatedResponse(BaseModel):
    items: list = []
    total: int = 0
    page: int = 1
    page_size: int = 20


class ProxyCreate(BaseModel):
    urls: list[str] = Field(..., min_length=1)
    scheme: str = Field(default="", description="Default scheme for URLs without prefix (socks5h, socks5, http, https)")

class ProxyOut(BaseModel):
    id: int
    url: str
    scheme: str
    is_active: bool
    fail_count: int
    last_used_at: str | None = None
    created_at: str

class ProxyStats(BaseModel):
    total: int = 0
    active: int = 0
    inactive: int = 0
    total_failures: int = 0
