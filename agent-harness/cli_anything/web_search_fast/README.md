# cli-anything-web-search-fast

AI-friendly CLI harness for [web-search-fast](https://github.com/uk0/web-search-fast) — direct web search from the command line using a stealth browser (Camoufox).

## Installation

```bash
# 1. Clone web-search-fast
git clone https://github.com/uk0/web-search-fast
cd web-search-fast

# 2. Install web-search-fast dependencies
pip install -e .

# 3. Fetch Camoufox browser
python -m camoufox fetch

# 4. Install the CLI harness
cd agent-harness
pip install -e .
```

## Quick Start

```bash
# Simple search
cli-anything-web-search-fast search "Python 3.13 new features"

# With options
cli-anything-web-search-fast search "latest CVE" -e google -d 2 -n 10

# JSON output for agents
cli-anything-web-search-fast --json search "query" --format json

# Fetch a single URL
cli-anything-web-search-fast fetch "https://example.com/article"

# Interactive REPL
cli-anything-web-search-fast repl
```

## Commands

| Command | Description |
|---------|-------------|
| `search QUERY` | Search the web |
| `batch FILE` | Batch search from file (one query per line) |
| `fetch URL` | Fetch single URL content as markdown |
| `engine list` | List available engines |
| `engine probe` | Probe engines with test query |
| `pool-status` | Check browser pool health |
| `session show` | Show session history |
| `session save NAME` | Save session |
| `session load NAME` | Load session |
| `repl` | Interactive search REPL |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BROWSER_POOL_SIZE` | 30 | Concurrency slots |
| `BROWSER_PROXY` | — | Proxy URL |
| `BROWSER_OS` | — | OS fingerprint |
| `BROWSER_BLOCK_WEBGL` | false | Block WebGL |
| `BROWSER_FONTS` | — | Custom fonts |

## For AI Agents

Use `--json` for machine-readable output. The harness manages browser lifecycle automatically.
