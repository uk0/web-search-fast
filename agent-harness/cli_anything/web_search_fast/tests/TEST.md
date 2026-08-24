# TEST.md — cli-anything-web-search-fast

## Test Plan

### Unit Tests (test_core.py)

No external dependencies (browser/network). Uses synthetic data.

| Test | What it verifies |
|------|------------------|
| `test_session_create_default` | load_session returns valid default |
| `test_session_save_load` | Round-trip persistence |
| `test_session_add_search` | add_search_result appends correctly |
| `test_session_max_history` | History capped at 100 entries |
| `test_export_json` | JSON export to file |
| `test_export_markdown` | Markdown export to file |
| `test_export_no_path` | Export returns None when no path given |
| `test_cli_help` | CLI --help exits 0 |
| `test_cli_search_help` | search --help exits 0 |
| `test_cli_engine_list` | engine list exits 0 (needs source import) |
| `test_cli_dry_run` | --dry-run shows params without searching |
| `test_cli_batch_missing_file` | batch with nonexistent file exits error |

### E2E Tests (test_full_e2e.py)

Requires Camoufox browser + network. Marked with `@pytest.mark.integration`.

| Test | What it verifies |
|------|------------------|
| `test_search_duckduckgo` | Search returns results with DuckDuckGo |
| `test_search_json_output` | JSON format produces valid JSON |
| `test_search_markdown_output` | Markdown format produces headers |
| `test_search_with_depth2` | Depth=2 fetches page content |
| `test_fetch_url` | Fetch extracts page content |
| `test_engine_probe` | Probe all engines |
| `test_pool_status` | Pool health check passes |
| `test_batch_search` | Batch processes multiple queries |

### Subprocess Tests (TestCLISubprocess)

Tests the installed CLI command via subprocess.

| Test | What it verifies |
|------|------------------|
| `test_cli_installed` | `which cli-anything-web-search-fast` succeeds |
| `test_cli_help_subprocess` | `cli-anything-web-search-fast --help` exits 0 |
| `test_cli_version` | `--version` shows version |

## Test Results

**Date:** 2026-06-05
**Platform:** Windows 11 Pro, Python 3.14.3, pytest 9.0.3
**Pass rate:** 19/19 (100%)

```
cli_anything/web_search_fast/tests/test_core.py::TestSession::test_load_default_session PASSED
cli_anything/web_search_fast/tests/test_core.py::TestSession::test_save_and_load_roundtrip PASSED
cli_anything/web_search_fast/tests/test_core.py::TestSession::test_add_search_result PASSED
cli_anything/web_search_fast/tests/test_core.py::TestSession::test_max_history_capped PASSED
cli_anything/web_search_fast/tests/test_core.py::TestSession::test_add_fetch_result PASSED
cli_anything/web_search_fast/tests/test_core.py::TestExport::test_export_json_to_file PASSED
cli_anything/web_search_fast/tests/test_core.py::TestExport::test_export_json_no_path PASSED
cli_anything/web_search_fast/tests/test_core.py::TestExport::test_export_markdown_to_file PASSED
cli_anything/web_search_fast/tests/test_core.py::TestExport::test_export_markdown_no_path PASSED
cli_anything/web_search_fast/tests/test_core.py::TestCLISmoke::test_help PASSED
cli_anything/web_search_fast/tests/test_core.py::TestCLISmoke::test_search_help PASSED
cli_anything/web_search_fast/tests/test_core.py::TestCLISmoke::test_fetch_help PASSED
cli_anything/web_search_fast/tests/test_core.py::TestCLISmoke::test_engine_list_help PASSED
cli_anything/web_search_fast/tests/test_core.py::TestCLISmoke::test_dry_run PASSED
cli_anything/web_search_fast/tests/test_core.py::TestCLISmoke::test_dry_run_verbose PASSED
cli_anything/web_search_fast/tests/test_core.py::TestCLISmoke::test_batch_missing_file PASSED
cli_anything/web_search_fast/tests/test_core.py::TestCLISmoke::test_session_show_empty PASSED
cli_anything/web_search_fast/tests/test_core.py::TestCLISmoke::test_session_save_and_load PASSED
cli_anything/web_search_fast/tests/test_core.py::TestCLISubprocess::test_cli_help_subprocess PASSED

19 passed in 0.54s
```

### Notes
- E2E tests skipped (require Camoufox browser + network)
- CLI accessible via `python -m cli_anything.web_search_fast.web_search_fast_cli`
- Windows: Scripts directory not in PATH by default, use `python -m` form
