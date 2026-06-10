"""Session management — persists search history and pool config."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

SESSION_DIR = Path.home() / ".cli-anything-web-search-fast"
SESSION_FILE = SESSION_DIR / "session.json"


def _ensure_dir() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def load_session() -> dict[str, Any]:
    """Load session from disk, or return default."""
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "search_history": [],
        "fetch_history": [],
        "pool_config": {},
        "last_engine": "duckduckgo",
        "last_depth": 1,
        "last_max_results": 5,
    }


def save_session(session: dict[str, Any]) -> None:
    """Persist session to disk."""
    _ensure_dir()
    session["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    SESSION_FILE.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")


def add_search_result(
    session: dict[str, Any],
    query: str,
    engine: str,
    depth: int,
    total: int,
    elapsed_ms: int,
    output_path: str | None = None,
) -> None:
    """Append a search entry to history and auto-save."""
    entry = {
        "query": query,
        "engine": engine,
        "depth": depth,
        "total": total,
        "elapsed_ms": elapsed_ms,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if output_path:
        entry["output_path"] = output_path
    session.setdefault("search_history", []).append(entry)
    session["last_engine"] = engine
    session["last_depth"] = depth
    # Keep last 100 entries
    session["search_history"] = session["search_history"][-100:]
    save_session(session)


def add_fetch_result(
    session: dict[str, Any],
    url: str,
    chars: int,
    elapsed_ms: int,
    output_path: str | None = None,
) -> None:
    """Append a fetch entry to history and auto-save."""
    entry = {
        "url": url,
        "chars": chars,
        "elapsed_ms": elapsed_ms,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if output_path:
        entry["output_path"] = output_path
    session.setdefault("fetch_history", []).append(entry)
    session["fetch_history"] = session["fetch_history"][-100:]
    save_session(session)
