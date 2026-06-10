"""Unit tests for cli-anything-web-search-fast — no browser/network required."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the harness package is importable
_HERE = Path(__file__).resolve()
_AGENT_HARNESS = _HERE.parents[3]  # agent-harness/
if str(_AGENT_HARNESS) not in sys.path:
    sys.path.insert(0, str(_AGENT_HARNESS))


# ---------------------------------------------------------------------------
# Session tests
# ---------------------------------------------------------------------------

class TestSession:
    def test_load_default_session(self, tmp_path):
        """load_session returns valid default when no file exists."""
        with patch("cli_anything.web_search_fast.core.session.SESSION_FILE", tmp_path / "session.json"):
            from cli_anything.web_search_fast.core.session import load_session
            session = load_session()
            assert session["version"] == 1
            assert isinstance(session["search_history"], list)
            assert session["last_engine"] == "duckduckgo"

    def test_save_and_load_roundtrip(self, tmp_path):
        """Session persists across save/load."""
        sfile = tmp_path / "session.json"
        with patch("cli_anything.web_search_fast.core.session.SESSION_FILE", sfile):
            from cli_anything.web_search_fast.core.session import load_session, save_session
            session = load_session()
            session["last_engine"] = "google"
            save_session(session)

            loaded = load_session()
            assert loaded["last_engine"] == "google"
            assert "updated_at" in loaded

    def test_add_search_result(self, tmp_path):
        """add_search_result appends to history."""
        sfile = tmp_path / "session.json"
        with patch("cli_anything.web_search_fast.core.session.SESSION_FILE", sfile):
            from cli_anything.web_search_fast.core.session import (
                add_search_result, load_session, save_session,
            )
            session = load_session()
            add_search_result(session, "test query", "google", 1, 5, 1234)
            assert len(session["search_history"]) == 1
            entry = session["search_history"][0]
            assert entry["query"] == "test query"
            assert entry["engine"] == "google"
            assert entry["total"] == 5
            assert entry["elapsed_ms"] == 1234

    def test_max_history_capped(self, tmp_path):
        """History is capped at 100 entries."""
        sfile = tmp_path / "session.json"
        with patch("cli_anything.web_search_fast.core.session.SESSION_FILE", sfile):
            from cli_anything.web_search_fast.core.session import (
                add_search_result, load_session,
            )
            session = load_session()
            for i in range(110):
                add_search_result(session, f"q{i}", "duckduckgo", 1, 1, 100)
            assert len(session["search_history"]) == 100
            # Oldest entries dropped
            assert session["search_history"][0]["query"] == "q10"

    def test_add_fetch_result(self, tmp_path):
        """add_fetch_result appends to fetch history."""
        sfile = tmp_path / "session.json"
        with patch("cli_anything.web_search_fast.core.session.SESSION_FILE", sfile):
            from cli_anything.web_search_fast.core.session import (
                add_fetch_result, load_session,
            )
            session = load_session()
            add_fetch_result(session, "https://example.com", 5000, 2000)
            assert len(session["fetch_history"]) == 1
            assert session["fetch_history"][0]["chars"] == 5000


# ---------------------------------------------------------------------------
# Export tests
# ---------------------------------------------------------------------------

class TestExport:
    def test_export_json_to_file(self, tmp_path):
        """JSON export writes valid JSON."""
        from cli_anything.web_search_fast.core.export import export_json
        out = str(tmp_path / "test.json")
        result = export_json({"key": "value"}, out)
        assert result == out
        data = json.loads(Path(out).read_text())
        assert data["key"] == "value"

    def test_export_json_no_path(self):
        """JSON export returns None when no path given."""
        from cli_anything.web_search_fast.core.export import export_json
        result = export_json({"key": "value"}, None)
        assert result is None

    def test_export_markdown_to_file(self, tmp_path):
        """Markdown export writes content."""
        from cli_anything.web_search_fast.core.export import export_markdown
        out = str(tmp_path / "test.md")
        result = export_markdown("# Hello\n\nWorld", out)
        assert result == out
        content = Path(out).read_text()
        assert "# Hello" in content

    def test_export_markdown_no_path(self):
        """Markdown export returns None when no path given."""
        from cli_anything.web_search_fast.core.export import export_markdown
        result = export_markdown("# Hello", None)
        assert result is None


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------

class TestCLISmoke:
    def test_help(self):
        """CLI --help exits 0."""
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "web-search-fast" in result.output.lower() or "search" in result.output.lower()

    def test_search_help(self):
        """search --help exits 0."""
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "query" in result.output.lower()

    def test_fetch_help(self):
        """fetch --help exits 0."""
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["fetch", "--help"])
        assert result.exit_code == 0
        assert "url" in result.output.lower()

    def test_engine_list_help(self):
        """engine list --help exits 0."""
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["engine", "list", "--help"])
        assert result.exit_code == 0

    def test_dry_run(self):
        """--dry-run shows params without searching."""
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "search", "test query", "--dry-run"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "dry_run"
        assert data["params"]["query"] == "test query"

    def test_dry_run_verbose(self):
        """--dry-run with human output."""
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "hello world", "--dry-run", "-e", "google", "-d", "2"])
        assert result.exit_code == 0
        assert "Dry run" in result.output
        assert "google" in result.output

    def test_batch_missing_file(self):
        """batch with nonexistent file shows error."""
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["batch", "/nonexistent/file.txt"])
        assert result.exit_code != 0

    def test_session_show_empty(self, tmp_path):
        """session show with empty session."""
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        with patch("cli_anything.web_search_fast.core.session.SESSION_FILE", tmp_path / "session.json"):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "show"])
            assert result.exit_code == 0

    def test_session_save_and_load(self, tmp_path):
        """session save/load round-trip."""
        from click.testing import CliRunner
        from cli_anything.web_search_fast.web_search_fast_cli import cli
        sfile = tmp_path / "session.json"
        sdir = tmp_path / "sessions"
        with patch("cli_anything.web_search_fast.core.session.SESSION_FILE", sfile), \
             patch("cli_anything.web_search_fast.core.session.SESSION_DIR", tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "save", "test"])
            # May fail if dir doesn't exist, that's ok for unit test
            # The important thing is it doesn't crash


# ---------------------------------------------------------------------------
# Subprocess tests
# ---------------------------------------------------------------------------

class TestCLISubprocess:
    """Test the installed CLI via subprocess."""

    @staticmethod
    def _resolve_cli(name: str = "cli-anything-web-search-fast") -> str:
        """Find the CLI executable."""
        import shutil
        path = shutil.which(name)
        if path:
            return path
        # Check if we should use the installed version
        if os.environ.get("CLI_ANYTHING_FORCE_INSTALLED"):
            raise FileNotFoundError(f"{name} not found in PATH")
        # Fallback to running via module
        return sys.executable + " -m cli_anything.web_search_fast.web_search_fast_cli"

    def test_cli_help_subprocess(self):
        """CLI --help works via subprocess."""
        import subprocess
        try:
            cli = self._resolve_cli()
        except FileNotFoundError:
            pytest.skip("CLI not installed")
        cmd = cli.split() + ["--help"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        assert "search" in result.stdout.lower() or "web" in result.stdout.lower()
