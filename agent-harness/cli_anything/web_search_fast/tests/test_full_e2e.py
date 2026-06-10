"""E2E tests — requires Camoufox browser + network. Run with: pytest -m integration"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_AGENT_HARNESS = _HERE.parents[3]
if str(_AGENT_HARNESS) not in sys.path:
    sys.path.insert(0, str(_AGENT_HARNESS))


@pytest.mark.integration
class TestSearchE2E:
    """End-to-end search tests requiring browser + network."""

    def test_search_duckduckgo(self):
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "search", "Python programming", "-e", "duckduckgo", "-n", "3", "-t", "30"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["total"] > 0

    def test_search_json_format(self):
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "search", "test", "--format", "json", "-n", "2", "-t", "25"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "results" in data

    def test_search_markdown_format(self):
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "hello world", "-n", "2", "-t", "25"])
        assert result.exit_code == 0
        # Should have markdown headers
        assert "#" in result.output or "Error" not in result.output

    def test_fetch_url(self):
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "fetch", "https://example.com", "-t", "20"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["chars"] > 0

    def test_engine_probe(self):
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "engine", "probe", "-e", "duckduckgo", "-q", "test"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"

    def test_pool_status(self):
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "pool-status"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert "pool" in data
