"""
Tests for the SWE Squad observability dashboard (issue #20).

Covers:
  - Dashboard data generation with mocked TicketStore
  - Telegram message formatting
  - HTML rendering
  - CLI dashboard subcommand (JSON and HTML modes)
  - CLI report dashboard subcommand
  - Edge cases: empty store, no status file, rate limit tracker
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Project bootstrap ─────────────────────────────────────────────────────────
logging.logAsyncioTasks = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.ticket_store import TicketStore
from scripts.ops.dashboard_data import (
    generate_dashboard_data,
    format_dashboard_telegram,
    render_dashboard_html,
    _parse_timestamp,
    _ticket_github_url,
)
from scripts.ops.swe_cli import build_parser, cmd_dashboard, cmd_report, main


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temp directory."""
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
def now():
    """Return a stable 'now' for tests."""
    return datetime.now(timezone.utc)


@pytest.fixture
def ticket_store(tmp_dir, now):
    """Create a TicketStore with diverse sample tickets."""
    store_path = tmp_dir / "tickets.json"
    store = TicketStore(str(store_path))

    recent_ts = (now - timedelta(hours=2)).isoformat()
    old_ts = (now - timedelta(hours=48)).isoformat()

    tickets = [
        SWETicket(
            ticket_id="t001",
            title="Critical: Database connection pool exhausted",
            description="Connection pool hit max limit",
            severity=TicketSeverity.CRITICAL,
            status=TicketStatus.OPEN,
            assigned_to="swe-squad-1",
            source_module="database",
            created_at=recent_ts,
            updated_at=recent_ts,
        ),
        SWETicket(
            ticket_id="t002",
            title="High: API response time degradation",
            description="p99 latency spike on /api/v2/search",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.INVESTIGATING,
            assigned_to="swe-squad-1",
            source_module="api",
            created_at=recent_ts,
            updated_at=recent_ts,
        ),
        SWETicket(
            ticket_id="t003",
            title="Medium: Deprecated library warning",
            description="urllib3 deprecation warning in logs",
            severity=TicketSeverity.MEDIUM,
            status=TicketStatus.TRIAGED,
            assigned_to="swe-squad-2",
            source_module="scraping",
            created_at=recent_ts,
            updated_at=recent_ts,
        ),
        SWETicket(
            ticket_id="t004",
            title="Low: Update README examples",
            description="Examples in README are outdated",
            severity=TicketSeverity.LOW,
            status=TicketStatus.RESOLVED,
            assigned_to="swe-squad-2",
            source_module="docs",
            created_at=old_ts,
            updated_at=recent_ts,
            test_results={"status": "pass"},
        ),
        SWETicket(
            ticket_id="t005",
            title="High: Memory leak in worker process",
            description="RSS grows unbounded over 24h",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.IN_DEVELOPMENT,
            assigned_to="swe-squad-1",
            source_module="worker",
            created_at=recent_ts,
            updated_at=recent_ts,
        ),
        SWETicket(
            ticket_id="t006",
            title="Critical: Investigation complete ticket",
            description="Already investigated",
            severity=TicketSeverity.CRITICAL,
            status=TicketStatus.INVESTIGATION_COMPLETE,
            assigned_to="swe-squad-1",
            source_module="core",
            created_at=recent_ts,
            updated_at=recent_ts,
            investigation_report="Root cause: memory overflow in buffer pool",
        ),
    ]

    for t in tickets:
        store.add(t)

    return store, store_path


@pytest.fixture
def empty_store(tmp_dir):
    """Create an empty TicketStore."""
    store_path = tmp_dir / "empty_tickets.json"
    store = TicketStore(str(store_path))
    return store, store_path


# ══════════════════════════════════════════════════════════════════════════════
# Helper function tests
# ══════════════════════════════════════════════════════════════════════════════

class TestParseTimestamp:
    def test_valid_iso(self):
        dt = _parse_timestamp("2026-03-17T08:00:00+00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_naive_timestamp_gets_utc(self):
        dt = _parse_timestamp("2026-03-17T08:00:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_invalid_string(self):
        assert _parse_timestamp("not-a-date") is None

    def test_none_input(self):
        assert _parse_timestamp(None) is None

    def test_empty_string(self):
        assert _parse_timestamp("") is None


class TestTicketGithubUrl:
    def test_explicit_github_url(self):
        t = SWETicket(
            title="test", description="test",
            metadata={"github_url": "https://github.com/org/repo/issues/42"},
        )
        assert _ticket_github_url(t) == "https://github.com/org/repo/issues/42"

    def test_issue_url_fallback(self):
        t = SWETicket(
            title="test", description="test",
            metadata={"issue_url": "https://github.com/org/repo/issues/99"},
        )
        assert _ticket_github_url(t) == "https://github.com/org/repo/issues/99"

    def test_constructed_from_issue_number(self):
        t = SWETicket(
            title="test", description="test",
            metadata={"github_issue_number": 42},
        )
        with patch.dict(os.environ, {"SWE_GITHUB_REPO": "org/repo"}):
            url = _ticket_github_url(t)
        assert url == "https://github.com/org/repo/issues/42"

    def test_no_url_available(self):
        t = SWETicket(title="test", description="test")
        assert _ticket_github_url(t) is None

    def test_issue_number_no_repo(self):
        t = SWETicket(
            title="test", description="test",
            metadata={"github_issue_number": 42},
        )
        with patch.dict(os.environ, {"SWE_GITHUB_REPO": ""}, clear=False):
            url = _ticket_github_url(t)
        assert url is None


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard data generation tests
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerateDashboardData:
    def test_basic_structure(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        assert "ticket_summary" in data
        assert "recent_activity" in data
        assert "tickets_by_state" in data
        assert "agent_performance" in data
        assert "memory_stats" in data
        assert "rate_limit_events_24h" in data
        assert "last_cycle" in data
        assert "generated_at" in data

    def test_ticket_summary_counts(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        ts = data["ticket_summary"]
        assert ts["total"] == 6
        assert ts["open"] == 5  # all except resolved t004
        assert ts["resolved"] == 1
        assert ts["investigating"] == 1  # t002

    def test_severity_breakdown(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        by_sev = data["ticket_summary"]["by_severity"]
        assert by_sev.get("critical", 0) == 2  # t001 + t006
        assert by_sev.get("high", 0) == 2  # t002 + t005
        assert by_sev.get("medium", 0) == 1  # t003

    def test_status_breakdown(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        by_status = data["ticket_summary"]["by_status"]
        assert by_status.get("open", 0) == 1
        assert by_status.get("investigating", 0) == 1
        assert by_status.get("resolved", 0) == 1

    def test_recent_activity(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        recent = data["recent_activity"]
        # All tickets were updated within 24h except none in our fixture
        assert isinstance(recent, list)
        # At least some tickets should appear (the ones with recent timestamps)
        assert len(recent) >= 1

    def test_tickets_by_state_buckets(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        buckets = data["tickets_by_state"]
        assert len(buckets["open"]) == 2  # t001 open + t003 triaged
        assert len(buckets["in_progress"]) == 3  # t002 + t005 + t006
        assert len(buckets["closed"]) == 1  # t004 resolved

    def test_tickets_by_state_includes_github_actions(self, tmp_dir, status_file):
        store_path = tmp_dir / "gh_actions_tickets.json"
        store = TicketStore(str(store_path))
        now = datetime.now(timezone.utc).isoformat()
        store.add(SWETicket(
            ticket_id="gha001",
            title="GitHub linked ticket",
            description="test",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.OPEN,
            updated_at=now,
            metadata={"github_url": "https://github.com/org/repo/issues/77"},
        ))

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        row = data["tickets_by_state"]["open"][0]
        actions = row["github_actions"]
        assert actions["view"] == "https://github.com/org/repo/issues/77"
        assert actions["comment"].endswith("#new_comment_field")

    def test_recent_activity_sorted_descending(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        recent = data["recent_activity"]
        if len(recent) >= 2:
            for i in range(len(recent) - 1):
                assert recent[i]["timestamp"] >= recent[i + 1]["timestamp"]

    def test_agent_performance(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        ap = data["agent_performance"]
        assert "investigations_24h" in ap
        assert "fixes_attempted_24h" in ap
        assert "fixes_succeeded_24h" in ap
        assert "fix_success_rate" in ap
        assert isinstance(ap["fix_success_rate"], float)

    def test_memory_stats(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        ms = data["memory_stats"]
        assert "total_embeddings" in ms
        assert "memory_hits_24h" in ms
        assert "avg_confidence" in ms

    def test_last_cycle_present(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        lc = data["last_cycle"]
        assert lc is not None
        assert lc["gate_verdict"] == "pass"
        assert lc["time"] == "2026-03-17T08:00:00+00:00"

    def test_last_cycle_none_when_no_status(self, ticket_store, tmp_dir):
        store, _ = ticket_store
        missing = tmp_dir / "nonexistent.json"
        with patch("scripts.ops.dashboard_data.STATUS_PATH", missing):
            data = generate_dashboard_data(store)

        assert data["last_cycle"] is None

    def test_generated_at_is_iso(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        # Should be parseable
        dt = datetime.fromisoformat(data["generated_at"])
        assert dt.tzinfo is not None

    def test_rate_limit_tracker_integration(self, ticket_store, status_file):
        store, _ = ticket_store
        tracker = MagicMock()
        tracker.recent_events.return_value = [
            {"timestamp": "2026-03-17T07:00:00+00:00", "model": "sonnet"},
            {"timestamp": "2026-03-17T06:00:00+00:00", "model": "sonnet"},
        ]
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store, rate_limit_tracker=tracker)

        assert data["rate_limit_events_24h"] == 2
        tracker.recent_events.assert_called_once_with(hours=24.0)

    def test_rate_limit_tracker_none(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store, rate_limit_tracker=None)

        assert data["rate_limit_events_24h"] == 0

    def test_custom_hours_window(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store, hours=1)

        # With a 1-hour window, fewer activities may show
        assert isinstance(data["recent_activity"], list)

    def test_empty_store(self, empty_store, status_file):
        store, _ = empty_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        ts = data["ticket_summary"]
        assert ts["total"] == 0
        assert ts["open"] == 0
        assert ts["resolved"] == 0
        assert data["recent_activity"] == []
        assert data["agent_performance"]["investigations_24h"] == 0

    def test_store_exception_handling(self, status_file):
        """Dashboard gracefully handles store failures."""
        broken_store = MagicMock()
        broken_store.list_all.side_effect = Exception("DB down")
        broken_store.list_open.side_effect = Exception("DB down")
        broken_store.list_recently_resolved.side_effect = Exception("DB down")

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(broken_store)

        assert data["ticket_summary"]["total"] == 0
        assert data["ticket_summary"]["open"] == 0

    def test_rate_limit_tracker_exception(self, ticket_store, status_file):
        """Dashboard handles broken rate limit tracker gracefully."""
        store, _ = ticket_store
        tracker = MagicMock()
        tracker.recent_events.side_effect = RuntimeError("broken")

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store, rate_limit_tracker=tracker)

        assert data["rate_limit_events_24h"] == 0

    def test_ticket_with_github_url_in_activity(self, tmp_dir, status_file):
        """Tickets with GitHub URLs include them in recent activity."""
        store_path = tmp_dir / "gh_tickets.json"
        store = TicketStore(str(store_path))
        now = datetime.now(timezone.utc)
        store.add(SWETicket(
            ticket_id="gh001",
            title="Has GitHub URL",
            description="test",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.OPEN,
            updated_at=now.isoformat(),
            metadata={"github_url": "https://github.com/org/repo/issues/1"},
        ))

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        activity = data["recent_activity"]
        gh_entries = [a for a in activity if a.get("github_url")]
        assert len(gh_entries) >= 1
        assert gh_entries[0]["github_url"] == "https://github.com/org/repo/issues/1"

    def test_memory_stats_with_embeddings(self, tmp_dir, status_file):
        """Tickets with embedding metadata are counted."""
        store_path = tmp_dir / "emb_tickets.json"
        store = TicketStore(str(store_path))
        now = datetime.now(timezone.utc)
        store.add(SWETicket(
            ticket_id="emb001",
            title="Has embedding",
            description="test",
            metadata={"has_embedding": True},
            updated_at=now.isoformat(),
        ))
        store.add(SWETicket(
            ticket_id="emb002",
            title="Has memory hit",
            description="test",
            metadata={
                "memory_hit": True,
                "memory_hit_at": now.isoformat(),
                "fix_confidence": {"confidence": 0.85},
            },
            updated_at=now.isoformat(),
        ))

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        ms = data["memory_stats"]
        assert ms["total_embeddings"] == 1
        assert ms["memory_hits_24h"] == 1
        assert ms["avg_confidence"] == 0.85

    def test_fix_success_rate_with_resolved(self, tmp_dir, status_file):
        """Fix success rate computed correctly from resolved tickets."""
        store_path = tmp_dir / "fix_tickets.json"
        store = TicketStore(str(store_path))
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=1)).isoformat()

        # One resolved with pass, one resolved with fail
        store.add(SWETicket(
            ticket_id="fix001",
            title="Fixed",
            description="test",
            status=TicketStatus.RESOLVED,
            updated_at=recent,
            test_results={"status": "pass"},
        ))
        store.add(SWETicket(
            ticket_id="fix002",
            title="Failed fix",
            description="test",
            status=TicketStatus.RESOLVED,
            updated_at=recent,
            test_results={"status": "fail"},
        ))

        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        ap = data["agent_performance"]
        assert ap["fixes_succeeded_24h"] == 1
        assert ap["fixes_attempted_24h"] >= 2


# ══════════════════════════════════════════════════════════════════════════════
# Telegram formatting tests
# ══════════════════════════════════════════════════════════════════════════════

class TestFormatDashboardTelegram:
    def test_basic_formatting(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        msg = format_dashboard_telegram(data)
        assert "SWE Squad Dashboard" in msg
        assert "Tickets" in msg
        assert "Agent Performance" in msg

    def test_severity_emoji_present(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        msg = format_dashboard_telegram(data)
        # Should contain severity labels
        assert "CRITICAL" in msg or "HIGH" in msg

    def test_last_cycle_section(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        msg = format_dashboard_telegram(data)
        assert "Last Cycle" in msg
        assert "pass" in msg  # gate verdict (rendered as-is from status data)

    def test_empty_data(self):
        """Format empty dashboard data without crashing."""
        data = {
            "ticket_summary": {"total": 0, "open": 0, "resolved": 0},
            "recent_activity": [],
            "agent_performance": {
                "investigations_24h": 0,
                "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0,
                "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 0, "memory_hits_24h": 0, "avg_confidence": 0.0},
            "rate_limit_events_24h": 0,
            "last_cycle": None,
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        msg = format_dashboard_telegram(data)
        assert "SWE Squad Dashboard" in msg
        assert "Generated:" in msg

    def test_rate_limit_section(self):
        """Rate limit events section appears when > 0."""
        data = {
            "ticket_summary": {"total": 0, "open": 0, "resolved": 0, "by_severity": {}},
            "recent_activity": [],
            "agent_performance": {
                "investigations_24h": 0, "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0, "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 0, "memory_hits_24h": 0, "avg_confidence": 0.0},
            "rate_limit_events_24h": 5,
            "last_cycle": None,
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        msg = format_dashboard_telegram(data)
        assert "Rate limit events" in msg
        assert "5" in msg

    def test_memory_section_hidden_when_empty(self):
        """Memory section is not included when no embeddings exist."""
        data = {
            "ticket_summary": {"total": 1, "open": 1, "resolved": 0, "by_severity": {}},
            "recent_activity": [],
            "agent_performance": {
                "investigations_24h": 0, "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0, "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 0, "memory_hits_24h": 0, "avg_confidence": 0.0},
            "rate_limit_events_24h": 0,
            "last_cycle": None,
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        msg = format_dashboard_telegram(data)
        assert "Semantic Memory" not in msg

    def test_memory_section_shown_when_present(self):
        """Memory section shows when embeddings exist."""
        data = {
            "ticket_summary": {"total": 1, "open": 1, "resolved": 0, "by_severity": {}},
            "recent_activity": [],
            "agent_performance": {
                "investigations_24h": 0, "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0, "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 10, "memory_hits_24h": 3, "avg_confidence": 0.82},
            "rate_limit_events_24h": 0,
            "last_cycle": None,
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        msg = format_dashboard_telegram(data)
        assert "Semantic Memory" in msg
        assert "10" in msg
        assert "0.82" in msg

    def test_recent_activity_with_github_links(self):
        """Activity entries with GitHub URLs produce links."""
        data = {
            "ticket_summary": {"total": 1, "open": 1, "resolved": 0, "by_severity": {}},
            "recent_activity": [
                {
                    "ticket_id": "t001",
                    "title": "Test ticket",
                    "action": "open",
                    "severity": "high",
                    "timestamp": "2026-03-17T08:00:00+00:00",
                    "github_url": "https://github.com/org/repo/issues/1",
                }
            ],
            "agent_performance": {
                "investigations_24h": 0, "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0, "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 0, "memory_hits_24h": 0, "avg_confidence": 0.0},
            "rate_limit_events_24h": 0,
            "last_cycle": None,
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        msg = format_dashboard_telegram(data)
        assert "View issue" in msg
        assert "github.com" in msg

    def test_html_escape_in_telegram(self):
        """HTML entities are escaped in Telegram messages."""
        data = {
            "ticket_summary": {"total": 0, "open": 0, "resolved": 0, "by_severity": {}},
            "recent_activity": [],
            "agent_performance": {
                "investigations_24h": 0, "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0, "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 0, "memory_hits_24h": 0, "avg_confidence": 0.0},
            "rate_limit_events_24h": 0,
            "last_cycle": {"time": "N/A", "gate_verdict": "<script>alert(1)</script>"},
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        msg = format_dashboard_telegram(data)
        assert "<script>" not in msg
        assert "&lt;script&gt;" in msg


# ══════════════════════════════════════════════════════════════════════════════
# HTML rendering tests
# ══════════════════════════════════════════════════════════════════════════════

class TestRenderDashboardHtml:
    def test_html_contains_data(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        html = render_dashboard_html(data)
        assert "<!DOCTYPE html>" in html
        assert "SWE Squad Dashboard" in html
        # Data should be injected
        assert "ticket_summary" in html

    def test_html_valid_json_embedded(self, ticket_store, status_file):
        """The embedded JSON in HTML is valid."""
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        html = render_dashboard_html(data)
        # Extract the JSON from the var assignment
        marker = "var DASHBOARD_DATA = "
        start = html.index(marker) + len(marker)
        end = html.index(";", start)
        json_str = html[start:end]
        parsed = json.loads(json_str)
        assert parsed["ticket_summary"]["total"] == data["ticket_summary"]["total"]

    def test_html_fallback_no_template(self, ticket_store, tmp_dir, status_file):
        """Fallback HTML when template file is missing."""
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file), \
             patch("scripts.ops.dashboard_data.PROJECT_ROOT", tmp_dir):
            data = generate_dashboard_data(store)
            html = render_dashboard_html(data)

        assert "<!DOCTYPE html>" in html
        assert "SWE Squad Dashboard" in html

    def test_html_auto_refresh(self, ticket_store, status_file):
        """HTML includes auto-refresh mechanism."""
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        html = render_dashboard_html(data)
        assert "setInterval" in html
        assert "setRefreshInterval" in html  # configurable auto-refresh

    def test_html_contains_webui_tabs(self, ticket_store, status_file):
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        html = render_dashboard_html(data)
        assert "Overview" in html
        assert "Open Bugs" in html
        assert "WIP Bugs" in html
        assert "Closed Bugs" in html
        assert "Issue Actions" in html

    def test_html_severity_classes(self, ticket_store, status_file):
        """HTML includes severity CSS classes."""
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        html = render_dashboard_html(data)
        assert "sev-critical" in html
        assert "sev-high" in html
        assert "sev-medium" in html
        assert "sev-low" in html

    def test_empty_data_renders(self, status_file):
        """HTML renders without error on empty data."""
        data = {
            "ticket_summary": {"total": 0, "open": 0, "resolved": 0,
                              "investigating": 0, "by_severity": {}, "by_status": {}},
            "recent_activity": [],
            "agent_performance": {
                "investigations_24h": 0, "fixes_attempted_24h": 0,
                "fixes_succeeded_24h": 0, "fix_success_rate": 0.0,
            },
            "memory_stats": {"total_embeddings": 0, "memory_hits_24h": 0, "avg_confidence": 0.0},
            "rate_limit_events_24h": 0,
            "last_cycle": None,
            "generated_at": "2026-03-17T08:00:00+00:00",
        }
        html = render_dashboard_html(data)
        assert "SWE Squad Dashboard" in html


# ══════════════════════════════════════════════════════════════════════════════
# CLI dashboard subcommand tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCmdDashboard:
    def test_dashboard_json_output(self, ticket_store, status_file, capsys):
        """Dashboard command outputs valid JSON."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["dashboard"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            rc = cmd_dashboard(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "ticket_summary" in data
        assert "recent_activity" in data
        assert "agent_performance" in data

    def test_dashboard_json_flag(self, ticket_store, status_file, capsys):
        """Dashboard --json outputs valid JSON."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["dashboard", "--json"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            rc = cmd_dashboard(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "ticket_summary" in data

    def test_dashboard_html_output(self, ticket_store, status_file, capsys):
        """Dashboard --html outputs HTML."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["dashboard", "--html"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            rc = cmd_dashboard(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "<!DOCTYPE html>" in captured.out
        assert "SWE Squad Dashboard" in captured.out

    def test_dashboard_empty_store(self, empty_store, status_file, capsys):
        """Dashboard works with an empty store."""
        store, store_path = empty_store
        parser = build_parser()
        args = parser.parse_args(["dashboard"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            rc = cmd_dashboard(args)

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ticket_summary"]["total"] == 0

    def test_dashboard_parser_registered(self):
        """Dashboard subcommand is registered in the parser."""
        parser = build_parser()
        args = parser.parse_args(["dashboard"])
        assert args.command == "dashboard"

    def test_dashboard_html_flag_parsed(self):
        """--html flag is parsed correctly."""
        parser = build_parser()
        args = parser.parse_args(["dashboard", "--html"])
        assert args.html is True

    def test_dashboard_main_entry(self, ticket_store, status_file, capsys):
        """Dashboard accessible via main()."""
        store, store_path = ticket_store
        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            rc = main(["dashboard"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "ticket_summary" in data


# ══════════════════════════════════════════════════════════════════════════════
# CLI report dashboard subcommand tests
# ══════════════════════════════════════════════════════════════════════════════

class TestReportDashboard:
    def test_report_dashboard_sends_telegram(self, ticket_store, status_file, capsys):
        """Report dashboard sends a Telegram message."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["report", "dashboard"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file), \
             patch("scripts.ops.swe_cli._send_telegram", return_value=True) as mock_send:
            rc = cmd_report(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "sent" in captured.out.lower()
        mock_send.assert_called_once()
        # Verify the message content
        msg = mock_send.call_args[0][0]
        assert "SWE Squad Dashboard" in msg

    def test_report_dashboard_telegram_failure(self, ticket_store, status_file, capsys):
        """Report dashboard handles Telegram send failure."""
        store, store_path = ticket_store
        parser = build_parser()
        args = parser.parse_args(["report", "dashboard"])

        with patch("scripts.ops.swe_cli.TICKETS_PATH", store_path), \
             patch("scripts.ops.dashboard_data.STATUS_PATH", status_file), \
             patch("scripts.ops.swe_cli._send_telegram", return_value=False):
            rc = cmd_report(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "Failed" in captured.err

    def test_report_dashboard_choice_valid(self):
        """Dashboard is a valid report type choice."""
        parser = build_parser()
        args = parser.parse_args(["report", "dashboard"])
        assert args.report_type == "dashboard"

    def test_report_dashboard_message_content(self, ticket_store, status_file):
        """Verify the Telegram message has expected sections."""
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        msg = format_dashboard_telegram(data)
        # Should have ticket counts
        assert "Total:" in msg
        assert "Open:" in msg
        # Should have agent performance
        assert "Investigations:" in msg
        assert "Success rate:" in msg


# ══════════════════════════════════════════════════════════════════════════════
# JSON serialisation round-trip tests
# ══════════════════════════════════════════════════════════════════════════════

class TestJsonRoundTrip:
    def test_dashboard_data_serialisable(self, ticket_store, status_file):
        """All dashboard data is JSON-serialisable."""
        store, _ = ticket_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        json_str = json.dumps(data)
        parsed = json.loads(json_str)
        assert parsed["ticket_summary"]["total"] == data["ticket_summary"]["total"]

    def test_empty_dashboard_serialisable(self, empty_store, status_file):
        """Empty dashboard data is JSON-serialisable."""
        store, _ = empty_store
        with patch("scripts.ops.dashboard_data.STATUS_PATH", status_file):
            data = generate_dashboard_data(store)

        json_str = json.dumps(data)
        parsed = json.loads(json_str)
        assert parsed["ticket_summary"]["total"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Grafana provisioning file tests
# ══════════════════════════════════════════════════════════════════════════════

class TestGrafanaProvisioning:
    def test_datasource_yaml_exists(self):
        """Grafana datasource YAML exists and is valid."""
        path = PROJECT_ROOT / "config" / "grafana" / "datasource.yaml"
        assert path.is_file()
        content = path.read_text()
        assert "apiVersion: 1" in content
        assert "postgres" in content
        assert "SWE-Squad-Supabase" in content

    def test_dashboard_json_exists(self):
        """Grafana dashboard JSON exists and is valid."""
        path = PROJECT_ROOT / "config" / "grafana" / "dashboard.json"
        assert path.is_file()
        data = json.loads(path.read_text())
        assert data["title"] == "SWE Squad Observability"
        assert data["uid"] == "swe-squad-observability"
        assert len(data["panels"]) >= 6

    def test_dashboard_has_ticket_panels(self):
        """Grafana dashboard has expected ticket-related panels."""
        path = PROJECT_ROOT / "config" / "grafana" / "dashboard.json"
        data = json.loads(path.read_text())
        titles = [p["title"] for p in data["panels"]]
        assert "Ticket Summary" in titles
        assert "Open Tickets" in titles
        assert "Critical Tickets" in titles

    def test_dashboard_has_flow_panel(self):
        """Grafana dashboard has ticket flow timeseries."""
        path = PROJECT_ROOT / "config" / "grafana" / "dashboard.json"
        data = json.loads(path.read_text())
        titles = [p["title"] for p in data["panels"]]
        assert "Ticket Flow (7d)" in titles

    def test_dashboard_has_team_id_variable(self):
        """Grafana dashboard has team_id template variable."""
        path = PROJECT_ROOT / "config" / "grafana" / "dashboard.json"
        data = json.loads(path.read_text())
        vars = data.get("templating", {}).get("list", [])
        names = [v["name"] for v in vars]
        assert "team_id" in names

    def test_datasource_has_ssl_require(self):
        """Datasource config requires SSL for Supabase."""
        path = PROJECT_ROOT / "config" / "grafana" / "datasource.yaml"
        content = path.read_text()
        assert "sslmode: require" in content

    def test_dashboard_queries_use_team_id(self):
        """All dashboard SQL queries filter by team_id."""
        path = PROJECT_ROOT / "config" / "grafana" / "dashboard.json"
        data = json.loads(path.read_text())
        for panel in data["panels"]:
            for target in panel.get("targets", []):
                sql = target.get("rawSql", "")
                assert "$team_id" in sql, f"Panel '{panel['title']}' missing team_id filter"
