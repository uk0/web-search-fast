"""Shared utilities for the CLI harness."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for CLI usage."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def ensure_source_on_path() -> None:
    """Add the web-search-fast repo root to sys.path so `src.*` is importable."""
    # Walk up from this file to find the repo root (where src/ lives)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "src" / "core" / "search.py").exists():
            repo_root = str(parent)
            if repo_root not in sys.path:
                sys.path.insert(0, repo_root)
            return
    # Fallback: assume repo is sibling of agent-harness
    harness_dir = here.parents[2]  # agent-harness/
    repo_root = str(harness_dir.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


def run_async(coro):
    """Run an async coroutine in a new event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in an async context (e.g. Jupyter) — use nest_asyncio or new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)
