from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from src.api.schemas import SearchRequest, SearchResponse
from src.config import OutputFormat, SearchEngine
from src.core.search import SearchError, do_search
from src.formatter.json_fmt import format_json
from src.formatter.markdown_fmt import format_markdown
from src.scraper.browser import BrowserPool

logger = logging.getLogger(__name__)

router = APIRouter()

_pool: BrowserPool | None = None


def set_browser_pool(pool: BrowserPool) -> None:
    global _pool
    _pool = pool


async def _do_search(req: SearchRequest) -> SearchResponse:
    if _pool is None:
        raise HTTPException(status_code=503, detail="Browser pool not initialized")
    try:
        return await do_search(_pool, req)
    except SearchError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        # Catch everything at the route boundary: a raw exception propagating
        # into Starlette's BaseHTTPMiddleware stack triggers an anyio
        # exception-group unwind bug. Convert to a clean 502 instead.
        logger.error("[api] search failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=502, detail=f"search failed: {e}")


@router.post("/search", response_model=SearchResponse)
async def search_post(req: SearchRequest):
    response = await _do_search(req)
    if req.format == OutputFormat.MARKDOWN:
        return PlainTextResponse(format_markdown(response), media_type="text/markdown")
    return format_json(response)


@router.get("/search")
async def search_get(
    q: str = Query(..., min_length=1, max_length=500, description="Search query"),
    engine: SearchEngine = Query(default=SearchEngine.GOOGLE),
    depth: int = Query(default=1, ge=1, le=3),
    format: OutputFormat = Query(default=OutputFormat.JSON),
    max_results: int = Query(default=10, ge=1, le=50),
    timeout: int = Query(default=30, ge=5, le=120),
):
    req = SearchRequest(
        query=q,
        engine=engine,
        depth=depth,
        format=format,
        max_results=max_results,
        timeout=timeout,
    )
    response = await _do_search(req)
    if req.format == OutputFormat.MARKDOWN:
        return PlainTextResponse(format_markdown(response), media_type="text/markdown")
    return format_json(response)


@router.get("/health")
async def health():
    return {"status": "ok", "pool_ready": _pool is not None and _pool._started}
