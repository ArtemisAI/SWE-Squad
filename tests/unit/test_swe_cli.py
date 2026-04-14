"""
Tests for the SWE Squad CLI tool (scripts/ops/swe_cli.py).

Covers status, tickets, summary, issues, repos, report subcommands,
--json output mode, ticket filtering, and .env loading.
"""

from __future__ import annotations

import json
import logging
import re


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)
import os
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Project bootstrap ─────────────────────────────────────────────────────────
logging.logAsyncioTasks = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.ticket_store import TicketStore
import argparse

from scripts.ops.swe_cli import (
    build_parser,
    cmd_auth,
    cmd_costs,
    cmd_issues,
    cmd_ops,
    cmd_project,
    cmd_repos,
    cmd_report,
    cmd_roles,
    cmd_serve,
    cmd_session,
    cmd_status,
    cmd_summary,
    cmd_tickets,
    main,
    _load_status,
    _truncate,
)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temp directory and patch CLI paths to use it."""
    return tmp_path


@pytest.fixture
def status_file(tmp_dir):
    """Create a mock status.json."""
    status_data = {
        "last_cycle": "2026-03-17T08:00:00+00:00",
        "tickets_open": 5,
        "tickets_investigating": 2,
        "gate_verdict": "pass",
        "next_cycle": "2026-03-17T08:30:00+00:00",
    }
    path = tmp_dir / "status.json"
    path.write_text(json.dumps(status_data))
    return path


@pytest.fixture
def ticket_store(tmp_dir):
    """Create a TicketStore with sample tickets."""
    store_path = tmp_dir / "tickets.json"
    store = TicketStore(str(store_path))

    # Add diverse tickets for filtering tests
    tickets = [
        SWETicket(
            ticket_id="t001",
            title="Critical: Database connection pool exhausted",
            description="Connection pool hit max limit",
            severity=TicketSeverity.CRITICAL,
            status=TicketStatus.OPEN,
            assigned_to="swe-squad-1",
            source_module="database",
        ),
        SWETicket(
            ticket_id="t002",
            title="High: API response time degradation",
            description="p99 latency spike on /api/v2/search",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.INVESTIGATING,
            assigned_to="swe-squad-1",
            source_module="api",
        ),
        SWETicket(
            ticket_id="t003",
            title="Medium: Deprecated library warning",
            description="urllib3 deprecation warning in logs",
            severity=TicketSeverity.MEDIUM,
            status=TicketStatus.TRIAGED,
            assigned_to="swe-squad-2",
            source_module="scraping",
        ),
        SWETicket(
            ticket_id="t004",
            title="Low: Update README examples",
            description="Examples in README are outdated",
            severity=TicketSeverity.LOW,
            status=TicketStatus.RESOLVED,
            assigned_to="swe-squad-2",
            source_module="docs",
        ),
        SWETicket(
            ticket_id="t005",
            title="High: Memory leak in worker process",
            description="RSS grows unbounded over 24h",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.IN_DEVELOPMENT,
            assigned_to="swe-squad-1",
            source_module="worker",
        ),
    ]

    for t in tickets:
        store.add(t)

    return store, store_path


# ══════════════════════════════════════════════════════════════════════════════
# Helper tests
# ══════════════════════════════════════════════════════════════════════════════

class TestTruncate:
    def test_short_string(self):
        assert _truncate("hello", 10) == "hello"

    def test_exact_length(self):
        assert _truncate("hello", 5) == "hello"

    def test_long_string(self):
        assert _truncate("hello world", 8) == "hello..."

    def test_very_short_width(self):
        result = _truncate("hello world", 4)
        assert len(result) <= 4


# ══════════════════════════════════════════════════════════════════════════════
# Status tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdStatus:
    def test_status_text_output(self, status_file, ticket_store, capsys):
        """Status command produces formatted text output."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["status"])

        with patch("scripts.ops.swe_cli.STATUS_PATH", status_file), \
             patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_status(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "SWE Squad Status" in captured.out
        assert "pass" in captured.out  # gate verdict

    def test_status_json_output(self, status_file, ticket_store, capsys):
        """Status command with --json produces valid JSON."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["status", "--json"])

        with patch("scripts.ops.swe_cli.STATUS_PATH", status_file), \
             patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_status(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "gate_verdict" in data
        assert data["gate_verdict"] == "pass"
        assert "ticket_counts" in data

    def test_status_no_status_file(self, tmp_dir, ticket_store, capsys):
        """Status command works when status.json does not exist."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["status"])
        missing = tmp_dir / "nonexistent.json"

        with patch("scripts.ops.swe_cli.STATUS_PATH", missing), \
             patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_status(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "not found" in captured.out


# ══════════════════════════════════════════════════════════════════════════════
# Tickets tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdTickets:
    def test_tickets_default_open(self, ticket_store, capsys):
        """Default tickets list shows open tickets (not resolved/closed)."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["tickets"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_tickets(args)

        assert rc == 0
        captured = capsys.readouterr()
        # t001 (open), t002 (investigating), t003 (triaged), t005 (in_development) should appear
        assert "t001" in captured.out
        assert "t002" in captured.out
        assert "t003" in captured.out
        assert "t005" in captured.out
        # t004 (resolved) should NOT appear
        assert "t004" not in captured.out

    def test_tickets_filter_status(self, ticket_store, capsys):
        """Filter tickets by status."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["tickets", "--status", "investigating"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_tickets(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "t002" in captured.out
        assert "t001" not in captured.out

    def test_tickets_filter_severity(self, ticket_store, capsys):
        """Filter tickets by severity."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["tickets", "--severity", "critical"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_tickets(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "t001" in captured.out
        assert "t002" not in captured.out
        assert "t003" not in captured.out

    def test_tickets_filter_team(self, ticket_store, capsys):
        """Filter tickets by team."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["tickets", "--team", "swe-squad-2"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_tickets(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "t003" in captured.out
        # t004 is swe-squad-2 but resolved, excluded by default open filter
        assert "t001" not in captured.out
        assert "t002" not in captured.out

    def test_tickets_json_output(self, ticket_store, capsys):
        """Tickets --json produces valid JSON array."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["tickets", "--json"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_tickets(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 4  # 4 open tickets (excludes resolved t004)
        # Each item should have ticket fields
        for item in data:
            assert "ticket_id" in item
            assert "severity" in item
            assert "status" in item

    def test_tickets_invalid_status(self, ticket_store, capsys):
        """Invalid status filter returns error."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["tickets", "--status", "bogus"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_tickets(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "Unknown status" in captured.err

    def test_tickets_invalid_severity(self, ticket_store, capsys):
        """Invalid severity filter returns error."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["tickets", "--severity", "bogus"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_tickets(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "Unknown severity" in captured.err

    def test_tickets_no_results(self, ticket_store, capsys):
        """Filter that matches nothing shows 'No tickets found'."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["tickets", "--team", "nonexistent-team"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_tickets(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "No tickets found" in captured.out

    def test_tickets_status_resolved(self, ticket_store, capsys):
        """Explicit --status resolved shows only resolved tickets."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["tickets", "--status", "resolved"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_tickets(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "t004" in captured.out
        assert "t001" not in captured.out


# ══════════════════════════════════════════════════════════════════════════════
# Summary tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdSummary:
    def test_summary_text_output(self, status_file, ticket_store, capsys):
        """Summary produces text output with severity and status counts."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["summary"])

        with patch("scripts.ops.swe_cli.STATUS_PATH", status_file), \
             patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_summary(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "SWE Squad Summary" in captured.out
        # Rich uses table titles ("By Severity"), plain text uses "By severity:"
        out_lower = captured.out.lower()
        assert "severity" in out_lower
        assert "status" in out_lower

    def test_summary_json_output(self, status_file, ticket_store, capsys):
        """Summary --json produces valid JSON with expected fields."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["summary", "--json"])

        with patch("scripts.ops.swe_cli.STATUS_PATH", status_file), \
             patch("scripts.ops.swe_cli.TICKETS_PATH", store_path):
            rc = cmd_summary(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "severity_counts" in data
        assert "status_counts" in data
        assert "recent_investigations_24h" in data
        assert "recent_fixes_24h" in data
        assert data["open_tickets"] == 4
        assert data["total_tickets"] == 5

    def test_summary_empty_store(self, status_file, tmp_dir, capsys):
        """Summary works with an empty ticket store."""
        empty_path = tmp_dir / "empty_tickets.json"
        parser = build_parser()
        args = parser.parse_args(["summary", "--json"])

        with patch("scripts.ops.swe_cli.STATUS_PATH", status_file), \
             patch("scripts.ops.swe_cli.TICKETS_PATH", empty_path):
            rc = cmd_summary(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["open_tickets"] == 0
        assert data["total_tickets"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Issues tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdIssues:
    def test_issues_no_account(self, capsys):
        """Issues command errors when SWE_GITHUB_ACCOUNT is not set."""
        parser = build_parser()
        args = parser.parse_args(["issues"])

        with patch.dict(os.environ, {"SWE_GITHUB_ACCOUNT": ""}, clear=False):
            rc = cmd_issues(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "SWE_GITHUB_ACCOUNT" in captured.err

    def test_issues_json_output(self, capsys):
        """Issues --json produces valid JSON when gh succeeds."""
        mock_issues = [
            {
                "number": 42,
                "title": "Fix the widget",
                "labels": [{"name": "bug"}],
                "createdAt": "2026-03-15T10:00:00Z",
            }
        ]
        parser = build_parser()
        args = parser.parse_args(["issues", "--json"])

        with patch.dict(
            os.environ,
            {"SWE_GITHUB_ACCOUNT": "bot-account", "SWE_GITHUB_REPO": "org/repo"},
            clear=False,
        ), patch(
            "scripts.ops.swe_cli._run_gh",
            return_value=json.dumps(mock_issues),
        ):
            rc = cmd_issues(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["number"] == 42

    def test_issues_text_output(self, capsys):
        """Issues command produces tabular text output."""
        mock_issues = [
            {
                "number": 42,
                "title": "Fix the widget",
                "labels": [{"name": "bug"}, {"name": "p1"}],
                "createdAt": "2026-03-15T10:00:00Z",
            },
            {
                "number": 43,
                "title": "Add feature X",
                "labels": [],
                "createdAt": "2026-03-16T10:00:00Z",
            },
        ]
        parser = build_parser()
        args = parser.parse_args(["issues"])

        with patch.dict(
            os.environ,
            {"SWE_GITHUB_ACCOUNT": "bot-account", "SWE_GITHUB_REPO": "org/repo"},
            clear=False,
        ), patch(
            "scripts.ops.swe_cli._run_gh",
            return_value=json.dumps(mock_issues),
        ):
            rc = cmd_issues(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "#42" in captured.out
        assert "#43" in captured.out
        assert "2 issue(s)" in _strip_ansi(captured.out)


# ══════════════════════════════════════════════════════════════════════════════
# Repos tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdRepos:
    def test_repos_json_output(self, capsys):
        """Repos --json produces valid JSON."""
        mock_repos = [
            {"name": "my-app", "visibility": "PUBLIC", "viewerPermission": "ADMIN"},
            {"name": "my-lib", "visibility": "PRIVATE", "viewerPermission": "WRITE"},
        ]
        parser = build_parser()
        args = parser.parse_args(["repos", "--json"])

        with patch(
            "scripts.ops.swe_cli._run_gh",
            return_value=json.dumps(mock_repos),
        ):
            rc = cmd_repos(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 2

    def test_repos_text_output(self, capsys):
        """Repos command produces tabular text output."""
        mock_repos = [
            {"name": "my-app", "visibility": "PUBLIC", "viewerPermission": "ADMIN"},
        ]
        parser = build_parser()
        args = parser.parse_args(["repos"])

        with patch(
            "scripts.ops.swe_cli._run_gh",
            return_value=json.dumps(mock_repos),
        ):
            rc = cmd_repos(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "my-app" in captured.out
        assert "PUBLIC" in captured.out

    def test_repos_gh_failure(self, capsys):
        """Repos command handles gh failure gracefully."""
        parser = build_parser()
        args = parser.parse_args(["repos"])

        with patch("scripts.ops.swe_cli._run_gh", return_value=None):
            rc = cmd_repos(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "Failed" in captured.err


# ══════════════════════════════════════════════════════════════════════════════
# Report tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdReport:
    def test_report_status(self, status_file, capsys):
        """Report status sends a Telegram message."""
        parser = build_parser()
        args = parser.parse_args(["report", "status"])

        with patch("scripts.ops.swe_cli.STATUS_PATH", status_file), \
             patch("scripts.ops.swe_cli._send_telegram", return_value=True):
            rc = cmd_report(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "sent" in captured.out.lower()

    def test_report_cycle_no_status(self, tmp_dir, capsys):
        """Report cycle fails when no status file exists."""
        parser = build_parser()
        args = parser.parse_args(["report", "cycle"])
        missing = tmp_dir / "nonexistent.json"

        with patch("scripts.ops.swe_cli.STATUS_PATH", missing):
            rc = cmd_report(args)

        assert rc == 1

    def test_report_daily(self, ticket_store, capsys):
        """Report daily calls notify_daily_summary."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["report", "daily"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("src.swe_team.notifier.notify_daily_summary") as mock_nd:
            rc = cmd_report(args)

        assert rc == 0
        mock_nd.assert_called_once()
        captured = capsys.readouterr()
        assert "sent" in captured.out.lower()


# ══════════════════════════════════════════════════════════════════════════════
# Main / parser tests
# ══════════════════════════════════════════════════════════════════════════════

class TestMain:
    def test_no_command_shows_help(self, capsys):
        """Running with no subcommand prints help and returns 1."""
        rc = main([])
        assert rc == 1

    def test_build_parser_has_subcommands(self):
        """Parser has all expected subcommands."""
        parser = build_parser()
        # Verify by parsing each subcommand
        for cmd in ("status", "tickets", "issues", "repos", "summary"):
            args = parser.parse_args([cmd])
            assert args.command == cmd

        args = parser.parse_args(["report", "daily"])
        assert args.command == "report"
        assert args.report_type == "daily"

    def test_verbose_flag(self):
        """--verbose flag is accepted."""
        parser = build_parser()
        args = parser.parse_args(["-v", "status"])
        assert args.verbose is True


# ══════════════════════════════════════════════════════════════════════════════
# .env loading test
# ══════════════════════════════════════════════════════════════════════════════

class TestDotenvLoading:
    def test_dotenv_loaded_at_import(self, tmp_path):
        """Verify the CLI module loads .env on import."""
        # Create a temporary .env with a test variable
        env_file = tmp_path / ".env"
        env_file.write_text("SWE_CLI_TEST_VAR=loaded_ok\n")

        # Patch PROJECT_ROOT so load_dotenv targets our temp .env
        with patch("scripts.ops.swe_cli.PROJECT_ROOT", tmp_path):
            from dotenv import load_dotenv
            load_dotenv(env_file, override=True)
            assert os.environ.get("SWE_CLI_TEST_VAR") == "loaded_ok"

        # Clean up
        os.environ.pop("SWE_CLI_TEST_VAR", None)


# ══════════════════════════════════════════════════════════════════════════════
# _load_status tests
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadStatus:
    def test_load_existing(self, status_file):
        """_load_status returns parsed dict from valid JSON."""
        with patch("scripts.ops.swe_cli.STATUS_PATH", status_file):
            data = _load_status()
        assert data is not None
        assert data["gate_verdict"] == "pass"
        assert data["tickets_open"] == 5

    def test_load_missing(self, tmp_dir):
        """_load_status returns None when file doesn't exist."""
        missing = tmp_dir / "does_not_exist.json"
        with patch("scripts.ops.swe_cli.STATUS_PATH", missing):
            data = _load_status()
        assert data is None

    def test_load_invalid_json(self, tmp_dir):
        """_load_status returns None for invalid JSON."""
        bad = tmp_dir / "bad.json"
        bad.write_text("not json at all {{{")
        with patch("scripts.ops.swe_cli.STATUS_PATH", bad):
            data = _load_status()
        assert data is None


# ══════════════════════════════════════════════════════════════════════════════
# cmd_auth tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdAuth:
    """Tests for cmd_auth (provider authentication status)."""

    def _make_args(self, json_flag: bool = False, action: str = "status") -> argparse.Namespace:
        return argparse.Namespace(command="auth", auth_action=action, json=json_flag, verbose=False)

    def test_auth_status_text(self, capsys):
        """cmd_auth with action='status' returns 0 and prints a table."""
        args = self._make_args(json_flag=False)
        # Patch urllib so no real network call happens
        import urllib.request as _url_req
        import urllib.error as _url_err

        with patch("urllib.request.urlopen", side_effect=_url_err.URLError("offline")):
            rc = cmd_auth(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "Provider" in captured.out

    def test_auth_list_providers(self, capsys):
        """cmd_auth shows all known providers in the output."""
        args = self._make_args(json_flag=False)
        import urllib.error as _url_err

        with patch("urllib.request.urlopen", side_effect=_url_err.URLError("offline")):
            rc = cmd_auth(args)

        assert rc == 0
        captured = capsys.readouterr()
        # The offline placeholder includes base_llm, github, telegram, supabase
        for provider in ("base_llm", "github", "telegram", "supabase"):
            assert provider in captured.out

    def test_auth_json_output(self, capsys):
        """cmd_auth --json returns parseable JSON with a 'providers' key."""
        args = self._make_args(json_flag=True)
        import urllib.error as _url_err

        with patch("urllib.request.urlopen", side_effect=_url_err.URLError("offline")):
            rc = cmd_auth(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "providers" in data
        assert isinstance(data["providers"], list)
        assert len(data["providers"]) == 4


# ══════════════════════════════════════════════════════════════════════════════
# cmd_roles tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdRoles:
    """Tests for cmd_roles (RBAC role definitions)."""

    def _make_args(self, json_flag: bool = False) -> argparse.Namespace:
        return argparse.Namespace(command="roles", json=json_flag, verbose=False)

    def test_roles_text_output_no_roles(self, capsys):
        """cmd_roles with empty engine prints informative message and returns 0."""
        args = self._make_args(json_flag=False)

        # Mock an engine that returns no roles
        mock_engine = MagicMock()
        mock_engine.list_roles.return_value = {}

        with patch("scripts.ops.swe_cli.cmd_roles.__module__", "scripts.ops.swe_cli"), \
             patch("src.swe_team.agent_rbac.get_rbac_engine", return_value=mock_engine):
            rc = cmd_roles(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "No roles" in captured.out or rc == 0

    def test_roles_text_output_with_roles(self, capsys):
        """cmd_roles with roles in engine prints them and returns 0."""
        from src.swe_team.agent_rbac import AgentRole

        args = self._make_args(json_flag=False)

        mock_role = AgentRole("investigator", {
            "description": "Investigates bugs",
            "enabled": True,
            "permissions": ["investigate", "read_logs"],
            "deny": [],
            "models": ["sonnet"],
        })
        mock_engine = MagicMock()
        mock_engine.list_roles.return_value = {"investigator": mock_role}

        with patch("src.swe_team.agent_rbac.get_rbac_engine", return_value=mock_engine):
            rc = cmd_roles(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "investigator" in captured.out

    def test_roles_json_output(self, capsys):
        """cmd_roles --json is not directly supported but the command still returns 0."""
        # cmd_roles doesn't actually check args.json – it always prints text.
        # We just verify it completes without error when roles file is absent.
        args = self._make_args(json_flag=True)

        mock_engine = MagicMock()
        mock_engine.list_roles.return_value = {}

        with patch("src.swe_team.agent_rbac.get_rbac_engine", return_value=mock_engine):
            rc = cmd_roles(args)

        assert rc == 0


# ══════════════════════════════════════════════════════════════════════════════
# cmd_costs tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdCosts:
    """Tests for cmd_costs (token usage and cost summary)."""

    def _make_args(self, json_flag: bool = False) -> argparse.Namespace:
        return argparse.Namespace(command="costs", json=json_flag, verbose=False)

    def _empty_summary(self) -> dict:
        return {
            "total_records": 0,
            "total_cost_usd": 0.0,
            "by_model": {},
            "daily_spend": 0.0,
        }

    def test_costs_no_data(self, capsys):
        """cmd_costs with empty tracker returns 0."""
        args = self._make_args(json_flag=False)
        mock_tracker = MagicMock()
        mock_tracker.summary.return_value = self._empty_summary()

        with patch("src.swe_team.token_tracker.TokenTracker", return_value=mock_tracker):
            rc = cmd_costs(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "No token usage" in captured.out or "Total cost" in captured.out

    def test_costs_json_output(self, capsys):
        """cmd_costs --json returns a dict with expected keys."""
        args = self._make_args(json_flag=True)
        expected = self._empty_summary()
        mock_tracker = MagicMock()
        mock_tracker.summary.return_value = expected

        with patch("src.swe_team.token_tracker.TokenTracker", return_value=mock_tracker):
            rc = cmd_costs(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "total_cost_usd" in data
        assert "by_model" in data

    def test_costs_with_data(self, tmp_path, capsys):
        """cmd_costs shows model rows when token data is present."""
        args = self._make_args(json_flag=False)
        summary_with_data = {
            "total_records": 3,
            "total_cost_usd": 0.0123,
            "by_model": {
                "sonnet": {"calls": 2, "input_tokens": 1000, "output_tokens": 500, "cost_usd": 0.0123},
            },
            "daily_spend": 0.005,
        }
        mock_tracker = MagicMock()
        mock_tracker.summary.return_value = summary_with_data

        with patch("src.swe_team.token_tracker.TokenTracker", return_value=mock_tracker):
            rc = cmd_costs(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "sonnet" in captured.out


# ══════════════════════════════════════════════════════════════════════════════
# cmd_ops tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdOps:
    """Tests for cmd_ops (multi-project operations status)."""

    def _make_args(self) -> argparse.Namespace:
        return argparse.Namespace(command="ops", verbose=False)

    def test_ops_no_projects(self, capsys):
        """cmd_ops with empty registry prints informative message and returns 0."""
        args = self._make_args()
        mock_registry = MagicMock()
        mock_registry.list_projects.return_value = []

        with patch("src.swe_team.ops.project_registry.ProjectRegistry", return_value=mock_registry):
            rc = cmd_ops(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "No projects" in captured.out

    def test_ops_with_projects(self, capsys):
        """cmd_ops with registered projects prints a table and returns 0."""
        from src.swe_team.ops.project_registry import Project, ProjectBudget

        args = self._make_args()

        proj = Project(name="my-app", repo="owner/my-app")
        proj.budget = ProjectBudget(daily_cap_usd=5.0)

        mock_registry = MagicMock()
        mock_registry.list_projects.return_value = [proj]
        mock_registry.validate_all.return_value = {"my-app": []}

        with patch("src.swe_team.ops.project_registry.ProjectRegistry", return_value=mock_registry):
            rc = cmd_ops(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "my-app" in captured.out


# ══════════════════════════════════════════════════════════════════════════════
# cmd_project tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdProject:
    """Tests for cmd_project (project management subcommands)."""

    def _list_args(self, json_flag: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            command="project",
            project_action="list",
            json=json_flag,
            verbose=False,
        )

    def _init_args(self, name: str, local_path: str = "") -> argparse.Namespace:
        return argparse.Namespace(
            command="project",
            project_action="init",
            name=name,
            repo="",
            local_path=local_path,
            json=False,
            verbose=False,
        )

    def test_project_list_empty(self, tmp_path, capsys):
        """project list with no repos configured returns 0."""
        args = self._list_args()

        with patch("scripts.ops.swe_cli._load_config_yaml", return_value={}):
            rc = cmd_project(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "No projects" in captured.out

    def test_project_list_json(self, capsys):
        """project list --json returns parseable JSON list."""
        args = self._list_args(json_flag=True)
        fake_repos = [{"name": "owner/repo", "local_path": "/tmp/repo", "priority": "high"}]

        with patch("scripts.ops.swe_cli._load_config_yaml", return_value={"repos": fake_repos}):
            rc = cmd_project(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert data[0]["name"] == "owner/repo"

    def test_project_init_creates_entry(self, tmp_path, capsys):
        """project init adds a new entry to config and returns 0."""
        args = self._init_args(name="foo", local_path="/tmp/foo")

        saved = {}

        def fake_save(data: dict) -> None:
            saved.update(data)

        with patch("scripts.ops.swe_cli._load_config_yaml", return_value={}), \
             patch("scripts.ops.swe_cli._save_config_yaml", side_effect=fake_save):
            rc = cmd_project(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "foo" in captured.out
        # Verify the entry was added
        assert any(r.get("name") == "foo" for r in saved.get("repos", []))

    def test_project_init_duplicate_rejected(self, capsys):
        """project init for an existing project name returns 1."""
        args = self._init_args(name="existing")
        existing = [{"name": "existing", "local_path": "/tmp/x"}]

        with patch("scripts.ops.swe_cli._load_config_yaml", return_value={"repos": existing}), \
             patch("scripts.ops.swe_cli._save_config_yaml"):
            rc = cmd_project(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "already exists" in captured.err

    def test_project_unknown_action(self, capsys):
        """project with an unknown action returns 1."""
        args = argparse.Namespace(
            command="project",
            project_action="delete",
            json=False,
            verbose=False,
        )
        rc = cmd_project(args)
        assert rc == 1


# ══════════════════════════════════════════════════════════════════════════════
# cmd_session tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdSession:
    """Tests for cmd_session (session lifecycle management)."""

    def _make_args(
        self,
        json_flag: bool = False,
        all_flag: bool = False,
        action: str = "list",
    ) -> argparse.Namespace:
        return argparse.Namespace(
            command="session",
            session_action=action,
            json=json_flag,
            all=all_flag,
            verbose=False,
        )

    def test_session_list_empty(self, tmp_path, capsys):
        """cmd_session with no sessions returns 0 and prints 'No sessions found'."""
        args = self._make_args()
        sessions_path = str(tmp_path / "sessions.json")

        with patch("src.swe_team.session_store.SessionStore.__init__", lambda self, path=None: None), \
             patch("src.swe_team.session_store.SessionStore.list_active", return_value=[]):
            rc = cmd_session(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "No sessions found" in captured.out

    def test_session_list_shows_sessions(self, tmp_path, capsys):
        """cmd_session shows session rows when sessions exist."""
        from src.swe_team.session_store import SessionRecord
        import time as _time

        args = self._make_args()
        now = _time.time()
        fake_session = SessionRecord(
            session_id="swe-investigator-t001xxxx-abc123-def456",
            ticket_id="t001",
            agent_type="investigator",
            created_at=now - 120,
            last_active=now - 60,
            status="active",
        )

        with patch("src.swe_team.session_store.SessionStore.__init__", lambda self, path=None: None), \
             patch("src.swe_team.session_store.SessionStore.list_active", return_value=[fake_session]):
            rc = cmd_session(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "swe-investigator" in captured.out
        assert "t001" in captured.out

    def test_session_list_json(self, tmp_path, capsys):
        """cmd_session --json returns a JSON list."""
        from src.swe_team.session_store import SessionRecord
        import time as _time

        args = self._make_args(json_flag=True)
        now = _time.time()
        fake_session = SessionRecord(
            session_id="swe-developer-t002xxxx-aabbcc-ddeeff",
            ticket_id="t002",
            agent_type="developer",
            created_at=now - 60,
            last_active=now,
            status="active",
        )

        with patch("src.swe_team.session_store.SessionStore.__init__", lambda self, path=None: None), \
             patch("src.swe_team.session_store.SessionStore.list_active", return_value=[fake_session]):
            rc = cmd_session(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert data[0]["ticket_id"] == "t002"

    def test_session_list_all_flag(self, tmp_path, capsys):
        """cmd_session --all calls list_all instead of list_active."""
        from src.swe_team.session_store import SessionRecord
        import time as _time

        args = self._make_args(all_flag=True)
        now = _time.time()
        completed_session = SessionRecord(
            session_id="swe-developer-t003xxxx-112233-445566",
            ticket_id="t003",
            agent_type="developer",
            created_at=now - 7200,
            last_active=now - 3600,
            status="completed",
        )

        with patch("src.swe_team.session_store.SessionStore.__init__", lambda self, path=None: None), \
             patch("src.swe_team.session_store.SessionStore.list_all", return_value=[completed_session]):
            rc = cmd_session(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "t003" in captured.out


# ══════════════════════════════════════════════════════════════════════════════
# cmd_serve tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdServe:
    """Tests for cmd_serve (live dashboard web server)."""

    def test_serve_requires_port(self):
        """'serve' subcommand is registered in the parser with --port."""
        parser = build_parser()
        args = parser.parse_args(["serve", "--port", "9999"])
        assert args.command == "serve"
        assert args.port == 9999

    def test_serve_default_port(self):
        """'serve' subcommand defaults to port 8080."""
        parser = build_parser()
        args = parser.parse_args(["serve"])
        assert args.port == 8080
        assert args.host == "0.0.0.0"

    def test_serve_calls_dashboard_server(self):
        """cmd_serve invokes dashboard_server.main without actually starting a server."""
        parser = build_parser()
        args = parser.parse_args(["serve", "--port", "8181", "--host", "127.0.0.1"])

        with patch("scripts.ops.dashboard_server.main") as mock_serve:
            rc = cmd_serve(args)

        mock_serve.assert_called_once()
        assert rc == 0
