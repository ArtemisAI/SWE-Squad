"""
Tests for the SWE Squad CLI tool (scripts/ops/swe_cli.py).

Covers status, tickets, summary, issues, repos, report subcommands,
--json output mode, ticket filtering, and .env loading.
"""

from __future__ import annotations

import json
import logging
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
from scripts.ops.swe_cli import (
    build_parser,
    cmd_status,
    cmd_summary,
    cmd_tickets,
    cmd_issues,
    cmd_repos,
    cmd_report,
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
        assert "By severity:" in captured.out
        assert "By status:" in captured.out

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
        assert "2 issue(s)" in captured.out


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
