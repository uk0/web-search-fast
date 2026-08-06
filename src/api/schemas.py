from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from src.config import OutputFormat, SearchEngine


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="Search query string")
    engine: SearchEngine = Field(default=SearchEngine.GOOGLE, description="Search engine to use")
    depth: int = Field(default=1, ge=1, le=3, description="Search depth: 1=SERP, 2=+content, 3=+sub-links")
    format: OutputFormat = Field(default=OutputFormat.JSON, description="Output format")
    max_results: int = Field(default=10, ge=1, le=50, description="Maximum number of results")
    timeout: int = Field(default=30, ge=5, le=120, description="Timeout in seconds")


class SubLink(BaseModel):
    url: str
    title: str = ""
    content: str = ""


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""
    content: str = ""
    sub_links: list[SubLink] = Field(default_factory=list)


class SearchMetadata(BaseModel):
    elapsed_ms: int = 0
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    engine: SearchEngine = SearchEngine.GOOGLE
    depth: int = 1
    cached: bool = False


class SearchResponse(BaseModel):
    query: str
    engine: SearchEngine
    depth: int
    total: int
    results: list[SearchResult]
    metadata: SearchMetadata


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""
