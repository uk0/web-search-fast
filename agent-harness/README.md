# cli-anything-web-search-fast

> CLI wrapper for [web-search-fast](https://github.com/uk0/web-search-fast) — no server required.

## What's this?

Directly imports web-search-fast's Python modules, bypassing the HTTP/MCP server layer. Perfect for:

- Script integration
- AI agent tool calls
- One-shot search tasks

## Install

```bash
cd web-search-fast/agent-harness
pip install -e .
python -m camoufox fetch
```

## Usage

```bash
# Single search
cli-anything search run "python asyncio" --engine duckduckgo --json

# Batch search
cli-anything search batch "query1" "query2" --max-results 3

# Fetch URL content
cli-anything fetch url "https://example.com"

# Engine status
cli-anything engine list
```

## Commands

| Group | Commands | Purpose |
|-------|----------|---------|
| `search` | `run`, `batch` | Execute searches |
| `fetch` | `url` | Fetch single URL content |
| `engine` | `list`, `probe` | Engine management |
| `session` | `save`, `load`, `show` | Session persistence |
| `pool` | `status` | Browser pool health |

## Key Design Decisions

1. **Direct import** — no HTTP server needed; imports `src.*` modules directly
2. **Async lifecycle** — each command does `pool.start()` → work → `pool.stop()`
3. **Session state** — JSON file at `~/.cli-anything-web-search-fast/session.json`
4. **Auto-save** — search/fetch results auto-save to session after one-shot mutations
5. **--dry-run** — shows search parameters without executing
6. **--json** — machine-readable output for AI agents

## Environment Variables

All `BROWSER_*` env vars from web-search-fast are supported:

| Variable | Default | Description |
|----------|---------|-------------|
| `BROWSER_POOL_SIZE` | 30 | Concurrency slots |
| `BROWSER_MAX_POOL_SIZE` | 90 | Max auto-scaled slots |
| `BROWSER_PROXY` | — | Proxy URL |
| `BROWSER_OS` | — | OS fingerprint |
| `BROWSER_BLOCK_WEBGL` | false | Block WebGL |
| `BROWSER_FONTS` | — | Custom fonts (comma-separated) |
| `BROWSER_ADDONS` | — | Firefox addons |
| `BROWSER_PROXY_LIST` | — | Proxy list file |

## License

MIT — same as [web-search-fast](https://github.com/uk0/web-search-fast)
