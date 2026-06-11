from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from src.api.routes import router, set_browser_pool
from src.config import get_config
from src.scraper.browser import BrowserPool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    config = get_config()
    pool = BrowserPool(
        pool_size=config.browser.pool_size,
        max_pool_size=config.browser.max_pool_size,
        headless=config.browser.headless,
        geoip=config.browser.geoip,
        humanize=config.browser.humanize,
        locale=config.browser.locale,
        block_images=config.browser.block_images,
        block_resources=config.browser.block_resources,
    )
    await pool.start()
    set_browser_pool(pool)
    yield
    await pool.stop()


app = FastAPI(
    title="Web Search MCP",
    description="High-performance web search to JSON/Markdown service",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)


if __name__ == "__main__":
    import uvicorn
    config = get_config()
    uvicorn.run("src.main:app", host=config.host, port=config.port, reload=True)
