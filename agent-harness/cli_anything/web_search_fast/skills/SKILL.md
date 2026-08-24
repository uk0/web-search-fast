---
name: cli-anything-web-search-fast
description: AI-friendly CLI harness for web-search-fast — direct web search from the command line with stealth browser (Camoufox), multi-engine fallback, and JSON output. Use when users ask to search the web, fetch web pages, or do real-time web queries from the CLI.
type: skill
---

# cli-anything-web-search-fast

AI-friendly CLI harness wrapping [web-search-fast](https://github.com/uk0/web-search-fast) — provides direct web search from the command line using a stealth browser (Camoufox) with automatic engine fallback (Google → DuckDuckGo → Bing), multi-depth page crawling, and machine-readable JSON output.

## Prerequisites

- Python 3.10+
- Camoufox browser fetched: `python -m camoufox fetch`
- web-search-fast installed: `pip install -e .` (in the repo root)
- CLI harness installed: `pip install -e .` (in `agent-harness/`)

## Commands

### `search` — Search the web

```bash
cli-anything-web-search-fast search "QUERY" [OPTIONS]
```

**Options:**
- `--engine, -e` — Search engine: google, bing, duckduckgo (default: duckduckgo)
- `--depth, -d` — Search depth: 1=SERP, 2=+content, 3=+sub-links (default: 1)
- `--max-results, -n` — Max results 1-20 (default: 5)
- `--format` — Output format: json, markdown (default: markdown)
- `--timeout, -t` — Timeout 5-120s (default: 30)
- `--output, -o` — Save output to file
- `--dry-run` — Show parameters without executing

**Examples:**
```bash
# Simple search
cli-anything-web-search-fast search "Python async patterns"

# Google with depth=2 (fetches full page content)
cli-anything-web-search-fast search "latest CVE" -e google -d 2 -n 10

# JSON output for agent consumption
cli-anything-web-search-fast --json search "query" --format json

# Save to file
cli-anything-web-search-fast search "news" -o results.md

# Dry run
cli-anything-web-search-fast search "test" --dry-run
```

### `batch` — Batch search from file

```bash
cli-anything-web-search-fast batch QUERIES_FILE [OPTIONS]
```

One query per line. Options same as `search`.

**Example:**
```bash
cli-anything-web-search-fast batch queries.txt -o results/ -e duckduckgo
```

### `fetch` — Fetch single URL

```bash
cli-anything-web-search-fast fetch URL [OPTIONS]
```

**Options:**
- `--timeout, -t` — Timeout in seconds (default: 20)
- `--output, -o` — Save content to file

**Examples:**
```bash
cli-anything-web-search-fast fetch "https://example.com/article"
cli-anything-web-search-fast fetch "https://docs.python.org/3/" -o page.md
```

### `engine list` — List available engines

```bash
cli-anything-web-search-fast engine list
```

### `engine probe` — Probe engines

```bash
cli-anything-web-search-fast engine probe [-e ENGINE] [-q QUERY]
```

### `pool-status` — Browser pool health

```bash
cli-anything-web-search-fast pool-status
```

### `session show/save/load` — Session management

```bash
cli-anything-web-search-fast session show
cli-anything-web-search-fast session save my-session
cli-anything-web-search-fast session load my-session
```

### `repl` — Interactive REPL

```bash
cli-anything-web-search-fast repl
```

Type queries directly. Commands: `/engine`, `/depth`, `/history`, `/help`, `/quit`

## JSON Output

Add `--json` to any command for machine-readable output:

```bash
cli-anything-web-search-fast --json search "Python" --format json
```

```json
{
  "status": "ok",
  "query": "Python",
  "engine": "duckduckgo",
  "depth": 1,
  "total": 5,
  "elapsed_ms": 3200,
  "results": { ... }
}
```

## Search Engines

| Engine | Speed | Reliability | Notes |
|--------|-------|-------------|-------|
| duckduckgo | Fast | High | Recommended default |
| google | Medium | Medium | May trigger CAPTCHA |
| bing | Medium | High | Uses global.bing.com |

## Search Depth

| Depth | Behavior | Use case |
|-------|----------|----------|
| 1 | SERP only (title, URL, snippet) | Quick lookups |
| 2 | + full page content | Detailed research |
| 3 | + outbound links content | Deep exploration |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BROWSER_POOL_SIZE` | 30 | Concurrency slots |
| `BROWSER_MAX_POOL_SIZE` | 90 | Max auto-scaled slots |
| `BROWSER_PROXY` | — | Proxy URL (socks5/http) |
| `BROWSER_OS` | — | OS fingerprint |
| `BROWSER_BLOCK_WEBGL` | false | Block WebGL fingerprint |
| `BROWSER_FONTS` | — | Custom fonts (comma-separated) |
| `BROWSER_ADDONS` | — | Firefox addon paths |
| `BROWSER_PROXY_LIST` | — | Proxy list file |

## Important Behavioral Notes

1. **First run is slow** — Camoufox browser startup takes 1-2s
2. **Browser pool auto-scales** — starts at pool_size, scales up to max_pool_size
3. **Engine fallback** — if primary engine returns 0 results, falls back to alternatives
4. **Session auto-save** — search results are saved to `~/.cli-anything-web-search-fast/session.json`
5. **Depth=2/3 is slower** — fetches actual page content, budget-aware timeout

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Search failed / error |

## For AI Agents

Use `--json` flag for machine-readable output. The harness manages the browser pool lifecycle automatically (start → search → stop). Session state persists across invocations. Use `--dry-run` to verify parameters before executing.
