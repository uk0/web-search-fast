"""SQLite database connection management via aiosqlite."""
from __future__ import annotations

import logging
import os

import aiosqlite

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None

DB_PATH = os.environ.get("WSM_DB_PATH", "data/wsm.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    call_limit INTEGER DEFAULT 0,
    call_count INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS search_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id TEXT,
    query TEXT NOT NULL,
    engine TEXT,
    ip_address TEXT NOT NULL,
    user_agent TEXT,
    status_code INTEGER,
    elapsed_ms INTEGER,
    request_body TEXT,
    response_body TEXT,
    tool_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ip_bans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address TEXT NOT NULL UNIQUE,
    reason TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_search_logs_created ON search_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_search_logs_ip ON search_logs(ip_address);
CREATE INDEX IF NOT EXISTS idx_search_logs_key ON search_logs(api_key_id);
CREATE INDEX IF NOT EXISTS idx_search_logs_engine ON search_logs(engine);
CREATE INDEX IF NOT EXISTS idx_search_logs_elapsed ON search_logs(elapsed_ms);

CREATE TABLE IF NOT EXISTS proxies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    scheme TEXT NOT NULL DEFAULT 'socks5h',
    is_active INTEGER NOT NULL DEFAULT 1,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def get_db() -> aiosqlite.Connection:
    """Return the singleton database connection."""
    global _db
    if _db is not None:
        return _db
    raise RuntimeError("Database not initialized. Call init_db() first.")


async def init_db(db_path: str | None = None) -> None:
    """Initialize database connection and create tables."""
    global _db
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    logger.info("Initializing SQLite database at %s", path)
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(_SCHEMA)
    await _db.commit()
    # Migrate: add new columns if missing (for existing databases)
    try:
        await _db.execute("SELECT request_body FROM search_logs LIMIT 0")
    except Exception:
        await _db.execute("ALTER TABLE search_logs ADD COLUMN request_body TEXT")
        await _db.execute("ALTER TABLE search_logs ADD COLUMN response_body TEXT")
        await _db.execute("ALTER TABLE search_logs ADD COLUMN tool_name TEXT")
        await _db.commit()
        logger.info("Migrated search_logs: added request_body, response_body, tool_name")
    logger.info("Database initialized successfully")


async def close_db() -> None:
    """Close the database connection."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
        logger.info("Database connection closed")
