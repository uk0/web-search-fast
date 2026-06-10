"""CLI harness for web-search-fast — direct web search from the command line.

Wraps the core search engine (Camoufox + multi-engine fallback) as a Click CLI
with --json output, session management, and batch processing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import click

# ---------------------------------------------------------------------------
# Ensure src/ from web-search-fast is importable
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
# Walk up to find agent-harness, then go one level up to repo root
for _p in _HERE.parents:
    if (_p / "src" / "core" / "search.py").exists():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break
else:
    # Fallback: agent-harness's parent = repo root
    _repo = _HERE.parents[3] if len(_HERE.parents) > 3 else _HERE.parents[-1]
    if str(_repo) not in sys.path:
        sys.path.insert(0, str(_repo))

from cli_anything.web_search_fast.core.session import (
    add_fetch_result,
    add_search_result,
    load_session,
    save_session,
)
from cli_anything.web_search_fast.core.export import export_json, export_markdown

logger = logging.getLogger("wsf-cli")

# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run async coroutine from sync Click context."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # Already in an event loop (e.g. Jupyter)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as ex:
            return ex.submit(asyncio.run, coro).result()


@asynccontextmanager
async def _managed_pool(config_overrides: dict | None = None):
    """Start BrowserPool, yield it, stop on exit."""
    from src.config import get_config
    from src.scraper.browser import BrowserPool

    config = get_config()
    bc = config.browser

    # Apply CLI overrides
    if config_overrides:
        for k, v in config_overrides.items():
            if v is not None and hasattr(bc, k):
                setattr(bc, k, v)

    pool = BrowserPool(
        pool_size=bc.pool_size,
        max_pool_size=bc.max_pool_size,
        headless=bc.headless,
        geoip=bc.geoip,
        humanize=bc.humanize,
        locale=bc.locale,
        block_images=bc.block_images,
        proxy=bc.proxy,
        os_target=bc.os_target,
        fonts=bc.fonts,
        block_webgl=bc.block_webgl,
        addons=bc.addons,
    )
    await pool.start()
    try:
        yield pool
    finally:
        await pool.stop()


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, ensure_ascii=False))


def _print_error(msg: str, as_json: bool = False) -> None:
    if as_json:
        _print_json({"status": "error", "error": msg})
    else:
        click.echo(f"Error: {msg}", err=True)


# ---------------------------------------------------------------------------
# CLI root
# ---------------------------------------------------------------------------


@click.group()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx: click.Context, as_json: bool, verbose: bool) -> None:
    """cli-anything-web-search-fast — AI-friendly web search CLI.

    Wraps web-search-fast's stealth browser search engine for direct
    command-line usage. Supports Google, Bing, and DuckDuckGo with
    automatic fallback and multi-depth page crawling.
    """
    ctx.ensure_object(dict)
    ctx.obj["json"] = as_json
    ctx.obj["verbose"] = verbose
    if verbose:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)


# ---------------------------------------------------------------------------
# search run
# ---------------------------------------------------------------------------

@cli.command("search")
@click.argument("query")
@click.option("--engine", "-e", default="duckduckgo", type=click.Choice(["google", "bing", "duckduckgo"]), help="Search engine")
@click.option("--depth", "-d", default=1, type=click.IntRange(1, 3), help="1=SERP, 2=+content, 3=+sub-links")
@click.option("--max-results", "-n", default=5, type=click.IntRange(1, 20), help="Max results")
@click.option("--format", "fmt", default="markdown", type=click.Choice(["json", "markdown"]), help="Output format")
@click.option("--timeout", "-t", default=30, type=click.IntRange(5, 120), help="Timeout in seconds")
@click.option("--output", "-o", default=None, help="Save output to file")
@click.option("--dry-run", is_flag=True, help="Show params without executing")
@click.pass_context
def search_run(
    ctx: click.Context,
    query: str,
    engine: str,
    depth: int,
    max_results: int,
    fmt: str,
    timeout: int,
    output: str | None,
    dry_run: bool,
) -> None:
    """Search the web using a stealth browser.

    Examples:

      cli-anything-web-search-fast search "Python async patterns"

      cli-anything-web-search-fast search "latest CVE" -e google -d 2 -n 10

      cli-anything-web-search-fast search "news" --json --format json
    """
    as_json = ctx.obj.get("json", False)

    if dry_run:
        params = {
            "command": "search",
            "query": query,
            "engine": engine,
            "depth": depth,
            "max_results": max_results,
            "format": fmt,
            "timeout": timeout,
        }
        if as_json:
            _print_json({"status": "dry_run", "params": params})
        else:
            click.echo("Dry run — parameters:")
            for k, v in params.items():
                click.echo(f"  {k}: {v}")
        return

    result = _run(_search_impl(query, engine, depth, max_results, fmt, timeout))

    if result is None:
        _print_error("Search failed", as_json)
        sys.exit(1)

    # Write to file if requested
    saved_to = None
    if output:
        if fmt == "json":
            saved_to = export_json(result["data"], output)
        else:
            saved_to = export_markdown(result["markdown"], output)

    # Auto-save to session
    session = load_session()
    add_search_result(
        session, query, engine, depth,
        result.get("total", 0), result.get("elapsed_ms", 0),
        output_path=saved_to,
    )

    # Print output
    if as_json:
        out = {
            "status": "ok",
            "query": query,
            "engine": result.get("engine", engine),
            "depth": depth,
            "total": result.get("total", 0),
            "elapsed_ms": result.get("elapsed_ms", 0),
        }
        if fmt == "json":
            out["results"] = result["data"]
        else:
            out["markdown"] = result["markdown"]
        if saved_to:
            out["saved_to"] = saved_to
        _print_json(out)
    else:
        if fmt == "json":
            _print_json(result["data"])
        else:
            click.echo(result["markdown"])


async def _search_impl(
    query: str, engine: str, depth: int, max_results: int, fmt: str, timeout: int,
) -> dict[str, Any] | None:
    """Core search implementation."""
    from src.api.schemas import SearchRequest
    from src.config import OutputFormat, SearchEngine
    from src.core.search import SearchError, do_search
    from src.formatter.json_fmt import format_json
    from src.formatter.markdown_fmt import format_markdown

    try:
        search_engine = SearchEngine(engine)
    except ValueError:
        return None

    req = SearchRequest(
        query=query,
        engine=search_engine,
        depth=depth,
        max_results=max_results,
        format=OutputFormat(fmt),
        timeout=timeout,
    )

    t0 = time.monotonic()
    try:
        async with _managed_pool() as pool:
            response = await do_search(pool, req)
            elapsed = int((time.monotonic() - t0) * 1000)
            return {
                "data": format_json(response),
                "markdown": format_markdown(response),
                "total": response.total,
                "engine": response.engine.value,
                "elapsed_ms": elapsed,
            }
    except SearchError as e:
        logger.error("SearchError: %s", e)
        return None
    except Exception as e:
        logger.exception("Unexpected error")
        return None


# ---------------------------------------------------------------------------
# search batch
# ---------------------------------------------------------------------------

@cli.command("batch")
@click.argument("queries_file", type=click.Path(exists=True))
@click.option("--engine", "-e", default="duckduckgo", type=click.Choice(["google", "bing", "duckduckgo"]))
@click.option("--depth", "-d", default=1, type=click.IntRange(1, 3))
@click.option("--max-results", "-n", default=5, type=click.IntRange(1, 20))
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "markdown"]))
@click.option("--timeout", "-t", default=30, type=click.IntRange(5, 120))
@click.option("--output-dir", "-o", default=None, help="Directory for output files")
@click.pass_context
def search_batch(
    ctx: click.Context,
    queries_file: str,
    engine: str,
    depth: int,
    max_results: int,
    fmt: str,
    timeout: int,
    output_dir: str | None,
) -> None:
    """Batch search from a file (one query per line).

    Examples:

      cli-anything-web-search-fast batch queries.txt -o results/
    """
    as_json = ctx.obj.get("json", False)
    queries = [q.strip() for q in Path(queries_file).read_text(encoding="utf-8").splitlines() if q.strip()]

    if not queries:
        _print_error("No queries found in file", as_json)
        return

    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    results = _run(_batch_impl(queries, engine, depth, max_results, fmt, timeout, output_dir))

    if as_json:
        _print_json({"status": "ok", "total_queries": len(queries), "results": results})
    else:
        for r in results:
            status = "OK" if r["success"] else "FAIL"
            click.echo(f"[{status}] {r['query']} — {r.get('total', 0)} results in {r.get('elapsed_ms', 0)}ms")


async def _batch_impl(
    queries: list[str], engine: str, depth: int, max_results: int,
    fmt: str, timeout: int, output_dir: str | None,
) -> list[dict]:
    """Run batch searches sharing a single browser pool."""
    from src.api.schemas import SearchRequest
    from src.config import OutputFormat, SearchEngine
    from src.core.search import SearchError, do_search
    from src.formatter.json_fmt import format_json
    from src.formatter.markdown_fmt import format_markdown

    search_engine = SearchEngine(engine)
    session = load_session()
    results = []

    async with _managed_pool() as pool:
        for i, query in enumerate(queries):
            req = SearchRequest(
                query=query, engine=search_engine, depth=depth,
                max_results=max_results, format=OutputFormat(fmt), timeout=timeout,
            )
            t0 = time.monotonic()
            try:
                response = await do_search(pool, req)
                elapsed = int((time.monotonic() - t0) * 1000)
                saved_to = None
                if output_dir:
                    ext = "json" if fmt == "json" else "md"
                    out_path = str(Path(output_dir) / f"result_{i:03d}.{ext}")
                    if fmt == "json":
                        saved_to = export_json(format_json(response), out_path)
                    else:
                        saved_to = export_markdown(format_markdown(response), out_path)
                add_search_result(session, query, engine, depth, response.total, elapsed, saved_to)
                results.append({
                    "query": query, "success": True, "total": response.total,
                    "engine": response.engine.value, "elapsed_ms": elapsed, "saved_to": saved_to,
                })
            except (SearchError, Exception) as e:
                elapsed = int((time.monotonic() - t0) * 1000)
                results.append({
                    "query": query, "success": False, "error": str(e), "elapsed_ms": elapsed,
                })

    return results


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

@cli.command("fetch")
@click.argument("url")
@click.option("--timeout", "-t", default=20, type=click.IntRange(5, 120), help="Timeout in seconds")
@click.option("--output", "-o", default=None, help="Save content to file")
@click.pass_context
def fetch_url(ctx: click.Context, url: str, timeout: int, output: str | None) -> None:
    """Fetch a single URL and extract main content as markdown.

    Examples:

      cli-anything-web-search-fast fetch "https://example.com/article"

      cli-anything-web-search-fast fetch "https://docs.python.org/3/" -o page.md
    """
    as_json = ctx.obj.get("json", False)
    result = _run(_fetch_impl(url, timeout))

    if result is None:
        _print_error(f"Failed to fetch {url}", as_json)
        sys.exit(1)

    saved_to = None
    if output:
        saved_to = export_markdown(result["content"], output)

    session = load_session()
    add_fetch_result(session, url, result["chars"], result["elapsed_ms"], saved_to)

    if as_json:
        out = {
            "status": "ok", "url": url, "chars": result["chars"],
            "elapsed_ms": result["elapsed_ms"],
        }
        if saved_to:
            out["saved_to"] = saved_to
        else:
            out["content"] = result["content"]
        _print_json(out)
    else:
        if saved_to:
            click.echo(f"Saved to {saved_to} ({result['chars']} chars)")
        else:
            click.echo(result["content"])


async def _fetch_impl(url: str, timeout: int) -> dict[str, Any] | None:
    """Fetch single URL content."""
    from src.core.search import fetch_url_content

    t0 = time.monotonic()
    try:
        async with _managed_pool() as pool:
            content = await fetch_url_content(pool, url, timeout=timeout)
            elapsed = int((time.monotonic() - t0) * 1000)
            if not content:
                return None
            return {"content": content, "chars": len(content), "elapsed_ms": elapsed}
    except Exception as e:
        logger.exception("Fetch error")
        return None


# ---------------------------------------------------------------------------
# engine list / probe
# ---------------------------------------------------------------------------

@cli.group("engine")
def engine_group() -> None:
    """Search engine management."""
    pass


@engine_group.command("list")
@click.pass_context
def engine_list(ctx: click.Context) -> None:
    """List available search engines."""
    from src.core.search import ENGINES

    as_json = ctx.obj.get("json", False)
    engines = []
    for eng in ENGINES:
        engines.append({
            "name": eng.value,
            "class": ENGINES[eng].__class__.__name__,
        })

    if as_json:
        _print_json({"status": "ok", "engines": engines})
    else:
        click.echo("Available search engines:")
        for e in engines:
            click.echo(f"  - {e['name']} ({e['class']})")


@engine_group.command("probe")
@click.option("--engine", "-e", default=None, type=click.Choice(["google", "bing", "duckduckgo"]))
@click.option("--query", "-q", default="test", help="Probe query")
@click.pass_context
def engine_probe(ctx: click.Context, engine: str | None, query: str) -> None:
    """Probe search engines with a test query.

    Examples:

      cli-anything-web-search-fast engine probe
      cli-anything-web-search-fast engine probe -e google -q "hello world"
    """
    as_json = ctx.obj.get("json", False)
    result = _run(_engine_probe_impl(engine, query))
    if as_json:
        _print_json(result)
    else:
        for r in result.get("results", []):
            status = "OK" if r["success"] else "FAIL"
            click.echo(f"[{status}] {r['engine']} — {r.get('total', 0)} results in {r.get('elapsed_ms', 0)}ms")
            if r.get("error"):
                click.echo(f"       Error: {r['error']}")


async def _engine_probe_impl(engine: str | None, query: str) -> dict:
    """Probe one or all engines."""
    from src.api.schemas import SearchRequest
    from src.config import SearchEngine
    from src.core.search import ENGINES, SearchError, do_search

    engines_to_test = [SearchEngine(engine)] if engine else list(ENGINES.keys())
    results = []

    async with _managed_pool() as pool:
        for eng in engines_to_test:
            req = SearchRequest(query=query, engine=eng, depth=1, max_results=3, timeout=20)
            t0 = time.monotonic()
            try:
                resp = await do_search(pool, req)
                elapsed = int((time.monotonic() - t0) * 1000)
                results.append({
                    "engine": eng.value, "success": True,
                    "total": resp.total, "elapsed_ms": elapsed,
                })
            except Exception as e:
                elapsed = int((time.monotonic() - t0) * 1000)
                results.append({
                    "engine": eng.value, "success": False,
                    "error": str(e), "elapsed_ms": elapsed,
                })

    return {"status": "ok", "query": query, "results": results}


# ---------------------------------------------------------------------------
# pool status
# ---------------------------------------------------------------------------

@cli.command("pool-status")
@click.pass_context
def pool_status(ctx: click.Context) -> None:
    """Check browser pool health status.

    Examples:

      cli-anything-web-search-fast pool-status
    """
    as_json = ctx.obj.get("json", False)
    result = _run(_pool_status_impl())
    if as_json:
        _print_json(result)
    else:
        click.echo("Browser Pool Status:")
        for k, v in result.get("pool", {}).items():
            click.echo(f"  {k}: {v}")


async def _pool_status_impl() -> dict:
    """Get pool status."""
    from src.scraper.browser import BrowserPool
    from src.config import get_config

    config = get_config()
    bc = config.browser
    pool = BrowserPool(
        pool_size=bc.pool_size, max_pool_size=bc.max_pool_size,
        headless=bc.headless, geoip=bc.geoip,
    )
    await pool.start()
    healthy = await pool.is_healthy()
    stats = pool.stats
    stats["healthy"] = healthy
    await pool.stop()
    return {"status": "ok", "pool": stats}


# ---------------------------------------------------------------------------
# session management
# ---------------------------------------------------------------------------

@cli.group("session")
def session_group() -> None:
    """Session management — search history and configuration."""
    pass


@session_group.command("show")
@click.pass_context
def session_show(ctx: click.Context) -> None:
    """Show current session state."""
    as_json = ctx.obj.get("json", False)
    session = load_session()
    if as_json:
        _print_json(session)
    else:
        click.echo(f"Session created: {session.get('created_at', 'unknown')}")
        click.echo(f"Last engine: {session.get('last_engine', 'n/a')}")
        click.echo(f"Last depth: {session.get('last_depth', 'n/a')}")
        history = session.get("search_history", [])
        click.echo(f"Searches: {len(history)}")
        if history:
            click.echo("\nRecent searches:")
            for entry in history[-5:]:
                click.echo(f"  [{entry.get('timestamp', '?')}] {entry.get('query', '?')} "
                           f"({entry.get('engine', '?')}, {entry.get('total', 0)} results)")


@session_group.command("save")
@click.argument("name", default="default")
@click.pass_context
def session_save(ctx: click.Context, name: str) -> None:
    """Save current session with a name."""
    import shutil
    as_json = ctx.obj.get("json", False)
    session = load_session()
    save_dir = session_dir = Path.home() / ".cli-anything-web-search-fast" / "sessions"
    save_dir.mkdir(parents=True, exist_ok=True)
    dest = save_dir / f"{name}.json"
    dest.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")
    if as_json:
        _print_json({"status": "ok", "saved": str(dest)})
    else:
        click.echo(f"Session saved to {dest}")


@session_group.command("load")
@click.argument("name", default="default")
@click.pass_context
def session_load(ctx: click.Context, name: str) -> None:
    """Load a named session."""
    as_json = ctx.obj.get("json", False)
    src = Path.home() / ".cli-anything-web-search-fast" / "sessions" / f"{name}.json"
    if not src.exists():
        _print_error(f"Session '{name}' not found", as_json)
        return
    session = json.loads(src.read_text(encoding="utf-8"))
    save_session(session)
    if as_json:
        _print_json({"status": "ok", "loaded": name})
    else:
        click.echo(f"Session '{name}' loaded")


# ---------------------------------------------------------------------------
# REPL mode
# ---------------------------------------------------------------------------

@cli.command("repl")
@click.pass_context
def repl(ctx: click.Context) -> None:
    """Interactive REPL for searching.

    Type queries directly, or use commands:
      /engine <name>  — switch engine
      /depth <1-3>    — set depth
      /quit           — exit

    Examples:

      cli-anything-web-search-fast repl
    """
    as_json = ctx.obj.get("json", False)
    session = load_session()
    engine = session.get("last_engine", "duckduckgo")
    depth = session.get("last_depth", 1)

    click.echo(f"web-search-fast REPL (engine={engine}, depth={depth})")
    click.echo("Type queries or /command. /quit to exit.\n")

    # Pre-start pool for reuse
    pool_result = _run(_start_pool())

    if pool_result is None:
        _print_error("Failed to start browser pool", as_json)
        return

    pool = pool_result
    try:
        while True:
            try:
                line = input(f"[{engine}] > ").strip()
            except (EOFError, KeyboardInterrupt):
                click.echo("\nBye!")
                break

            if not line:
                continue

            if line.startswith("/"):
                parts = line.split(maxsplit=1)
                cmd = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else ""

                if cmd in ("/quit", "/exit", "/q"):
                    click.echo("Bye!")
                    break
                elif cmd == "/engine":
                    if arg in ("google", "bing", "duckduckgo"):
                        engine = arg
                        click.echo(f"Engine: {engine}")
                    else:
                        click.echo("Valid engines: google, bing, duckduckgo")
                elif cmd == "/depth":
                    try:
                        d = int(arg)
                        if 1 <= d <= 3:
                            depth = d
                            click.echo(f"Depth: {depth}")
                        else:
                            click.echo("Depth must be 1, 2, or 3")
                    except ValueError:
                        click.echo("Usage: /depth <1-3>")
                elif cmd == "/history":
                    history = session.get("search_history", [])
                    for entry in history[-10:]:
                        click.echo(f"  [{entry.get('timestamp', '?')}] {entry.get('query', '?')}")
                elif cmd == "/help":
                    click.echo("Commands: /engine, /depth, /history, /help, /quit")
                else:
                    click.echo(f"Unknown command: {cmd}. Type /help for help.")
                continue

            # Execute search
            result = _run(_repl_search(pool, line, engine, depth))
            if result:
                click.echo(result["markdown"])
                add_search_result(session, line, engine, depth, result["total"], result["elapsed_ms"])
            else:
                click.echo("Search failed.")

    finally:
        _run(_stop_pool(pool))


async def _start_pool():
    """Start and return a BrowserPool."""
    from src.config import get_config
    from src.scraper.browser import BrowserPool

    config = get_config()
    bc = config.browser
    pool = BrowserPool(
        pool_size=bc.pool_size, max_pool_size=bc.max_pool_size,
        headless=bc.headless, geoip=bc.geoip, humanize=bc.humanize,
        locale=bc.locale, block_images=bc.block_images,
    )
    await pool.start()
    return pool


async def _stop_pool(pool) -> None:
    await pool.stop()


async def _repl_search(pool, query: str, engine: str, depth: int) -> dict | None:
    from src.api.schemas import SearchRequest
    from src.config import OutputFormat, SearchEngine
    from src.core.search import SearchError, do_search
    from src.formatter.markdown_fmt import format_markdown

    req = SearchRequest(
        query=query, engine=SearchEngine(engine),
        depth=depth, max_results=5, format=OutputFormat.MARKDOWN, timeout=25,
    )
    t0 = time.monotonic()
    try:
        response = await do_search(pool, req)
        elapsed = int((time.monotonic() - t0) * 1000)
        return {
            "markdown": format_markdown(response),
            "total": response.total,
            "elapsed_ms": elapsed,
        }
    except SearchError:
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
