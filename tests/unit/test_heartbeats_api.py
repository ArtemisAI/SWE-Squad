"""Tests for Heartbeats API (issue #474).

Covers:
  - _get_live_runs helper function
  - _get_active_run_for_issue helper function
  - _get_scheduler_agents helper function
  - GET /api/heartbeats endpoint
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

# ── Project bootstrap ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.ticket_store import TicketStore


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temp directory."""
    return tmp_path


@pytest.fixture
def now():
    """Return a stable 'now' for tests."""
    return datetime.now(timezone.utc)


@pytest.fixture
def ticket_store(tmp_dir, now):
    """Create a TicketStore with sample tickets in various states."""
    store_path = tmp_dir / "tickets.json"
    store = TicketStore(str(store_path))

    recent_ts = (now - timedelta(minutes=5)).isoformat()
    old_heartbeat = (now - timedelta(minutes=15)).isoformat()
    stale_ts = (now - timedelta(hours=2)).isoformat()

    # Tickets with recent heartbeats (active)
    store.add(SWETicket(
        ticket_id="t001",
        title="Critical: Database connection pool exhausted",
        description="Connection pool hit max limit",
        severity=TicketSeverity.CRITICAL,
        status=TicketStatus.INVESTIGATING,
        assigned_to="swe-squad-1",
        source_module="database",
        created_at=stale_ts,
        updated_at=recent_ts,
        metadata={"last_heartbeat": recent_ts, "claude_session_id": "session-001"},
    ))

    store.add(SWETicket(
        ticket_id="t002",
        title="High: API response time degradation",
        description="p99 latency spike on /api/v2/search",
        severity=TicketSeverity.HIGH,
        status=TicketStatus.IN_DEVELOPMENT,
        assigned_to="swe-squad-1",
        source_module="api",
        created_at=stale_ts,
        updated_at=recent_ts,
        metadata={"last_heartbeat": recent_ts, "dev_session_id": "session-002"},
    ))

    store.add(SWETicket(
        ticket_id="t003",
        title="Medium: Fix CSS layout issue",
        description="Button alignment broken on mobile",
        severity=TicketSeverity.MEDIUM,
        status=TicketStatus.IN_REVIEW,
        assigned_to="swe-squad-2",
        source_module="ui",
        created_at=stale_ts,
        updated_at=recent_ts,
        metadata={"last_heartbeat": recent_ts},
    ))

    # Ticket with old heartbeat (not live)
    store.add(SWETicket(
        ticket_id="t004",
        title="High: Memory leak investigation",
        description="RSS grows unbounded over 24h",
        severity=TicketSeverity.HIGH,
        status=TicketStatus.INVESTIGATING,
        assigned_to="swe-squad-1",
        source_module="worker",
        created_at=stale_ts,
        updated_at=old_heartbeat,
        metadata={"last_heartbeat": old_heartbeat},
    ))

    # Resolved ticket (not active)
    store.add(SWETicket(
        ticket_id="t005",
        title="Low: Update README examples",
        description="Examples in README are outdated",
        severity=TicketSeverity.LOW,
        status=TicketStatus.RESOLVED,
        assigned_to="swe-squad-2",
        source_module="docs",
        created_at=stale_ts,
        updated_at=recent_ts,
        metadata={"last_heartbeat": recent_ts},
    ))

    # OPEN ticket (not active)
    store.add(SWETicket(
        ticket_id="t006",
        title="High: New feature request",
        description="User wants export feature",
        severity=TicketSeverity.HIGH,
        status=TicketStatus.OPEN,
        assigned_to="swe-squad-1",
        source_module="api",
        created_at=recent_ts,
        updated_at=recent_ts,
    ))

    # Ticket without heartbeat (uses updated_at)
    store.add(SWETicket(
        ticket_id="t007",
        title="Critical: Production outage",
        description="Service is down",
        severity=TicketSeverity.CRITICAL,
        status=TicketStatus.TESTING,
        assigned_to="swe-squad-1",
        source_module="core",
        created_at=recent_ts,
        updated_at=recent_ts,
    ))

    return store, store_path


@pytest.fixture
def config_file(tmp_dir):
    """Create a mock swe_team.yaml config."""
    config = {
        "agents": [
            {
                "name": "swe_monitor",
                "role": "monitor",
                "description": "Scans logs for errors",
                "model": "sonnet",
                "enabled": True,
                "node": "primary",
                "max_concurrent_tasks": 1,
                "tools": ["log_scanner", "github_issues"],
            },
            {
                "name": "swe_triage",
                "role": "triage",
                "description": "Classifies tickets",
                "model": "sonnet",
                "enabled": True,
                "node": "primary",
                "max_concurrent_tasks": 3,
                "tools": ["ticket_manager"],
            },
            {
                "name": "swe_developer",
                "role": "developer",
                "description": "Implements fixes",
                "model": "sonnet",
                "enabled": False,
                "node": "worker",
                "max_concurrent_tasks": 1,
                "tools": ["code_editor", "git"],
            },
        ],
    }
    config_path = tmp_dir / "swe_team.yaml"
    config_path.write_text(json.dumps(config))
    return config_path


# ══════════════════════════════════════════════════════════════════════════════
# _get_live_runs tests
# ══════════════════════════════════════════════════════════════════════════════

class TestGetLiveRuns:
    """Test _get_live_runs helper function."""

    def test_returns_active_tickets_only(self, ticket_store):
        """Test only active status tickets are returned."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import _get_live_runs

        runs = _get_live_runs(store)

        # Should include t001 (INVESTIGATING), t002 (IN_DEVELOPMENT), t003 (IN_REVIEW), t007 (TESTING)
        # Should exclude t004 (INVESTIGATING but old heartbeat is included with is_live=false), t005 (RESOLVED), t006 (OPEN)
        ticket_ids = [r["ticket_id"] for r in runs]
        assert "t001" in ticket_ids
        assert "t002" in ticket_ids
        assert "t003" in ticket_ids
        assert "t007" in ticket_ids
        assert "t005" not in ticket_ids  # RESOLVED
        assert "t006" not in ticket_ids  # OPEN

    def test_is_live_flag(self, ticket_store):
        """Test is_live flag is set correctly based on heartbeat age."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import _get_live_runs

        runs = _get_live_runs(store)

        # Find the old heartbeat ticket
        t004_run = next((r for r in runs if r["ticket_id"] == "t004"), None)
        assert t004_run is not None
        assert t004_run["is_live"] is False  # > 10 minutes old

        # Recent heartbeat tickets should be live
        live_runs = [r for r in runs if r["ticket_id"] in ["t001", "t002", "t003"]]
        for run in live_runs:
            assert run["is_live"] is True

    def test_fallback_to_updated_at(self, ticket_store):
        """Test tickets without last_heartbeat use updated_at."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import _get_live_runs

        runs = _get_live_runs(store)

        # t007 has no last_heartbeat, should use updated_at
        t007_run = next((r for r in runs if r["ticket_id"] == "t007"), None)
        assert t007_run is not None
        assert t007_run["last_heartbeat"] == t007_run["updated_at"]

    def test_sorts_by_most_recent_heartbeat(self, ticket_store):
        """Test runs are sorted by most recent heartbeat first."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import _get_live_runs

        runs = _get_live_runs(store)

        # Check sorted order (most recent first)
        if len(runs) >= 2:
            for i in range(len(runs) - 1):
                assert runs[i]["last_heartbeat"] >= runs[i + 1]["last_heartbeat"]

    def test_filters_by_ticket_id(self, ticket_store):
        """Test filtering by specific ticket_id."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import _get_live_runs

        runs = _get_live_runs(store, ticket_id="t001")

        assert len(runs) == 1
        assert runs[0]["ticket_id"] == "t001"

    def test_filters_by_since_timestamp(self, ticket_store, now):
        """Test filtering by since timestamp."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import _get_live_runs

        since = (now - timedelta(minutes=6)).isoformat()
        runs = _get_live_runs(store, since=since)

        # Only tickets updated after 'since' should be returned
        for run in runs:
            updated = datetime.fromisoformat(run["updated_at"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
            assert updated >= since_dt

    def test_run_structure(self, ticket_store):
        """Test returned run has all required fields."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import _get_live_runs

        runs = _get_live_runs(store)

        if runs:
            run = runs[0]
            required_keys = [
                "ticket_id",
                "title",
                "status",
                "severity",
                "assigned_to",
                "source_module",
                "last_heartbeat",
                "seconds_ago",
                "is_live",
                "created_at",
                "updated_at",
            ]
            for key in required_keys:
                assert key in run, f"Missing required key: {key}"
            assert isinstance(run["seconds_ago"], int)
            assert isinstance(run["is_live"], bool)


# ══════════════════════════════════════════════════════════════════════════════
# _get_active_run_for_issue tests
# ══════════════════════════════════════════════════════════════════════════════

class TestGetActiveRunForIssue:
    """Test _get_active_run_for_issue helper function."""

    def test_returns_active_run(self, ticket_store):
        """Test returns active run for existing ticket."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import _get_active_run_for_issue

        run = _get_active_run_for_issue(store, "t001")

        assert run is not None
        assert run["ticket_id"] == "t001"
        assert run["status"] == "investigating"

    def test_returns_none_for_nonexistent_ticket(self, ticket_store):
        """Test returns None for non-existent ticket."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import _get_active_run_for_issue

        run = _get_active_run_for_issue(store, "nonexistent")

        assert run is None

    def test_returns_none_for_inactive_ticket(self, ticket_store):
        """Test returns None for ticket with no active runs."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import _get_active_run_for_issue

        run = _get_active_run_for_issue(store, "t005")  # RESOLVED

        assert run is None


# ══════════════════════════════════════════════════════════════════════════════
# _get_scheduler_agents tests
# ══════════════════════════════════════════════════════════════════════════════

class TestGetSchedulerAgents:
    """Test _get_scheduler_agents helper function."""

    def test_returns_agents_from_config(self, config_file):
        """Test returns agents from config file."""
        from scripts.ops.dashboard_server import _get_scheduler_agents

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", config_file):
            agents = _get_scheduler_agents()

        assert len(agents) == 3
        assert agents[0]["name"] == "swe_monitor"
        assert agents[1]["name"] == "swe_triage"
        assert agents[2]["name"] == "swe_developer"

    def test_agent_structure(self, config_file):
        """Test returned agents have all required fields."""
        from scripts.ops.dashboard_server import _get_scheduler_agents

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", config_file):
            agents = _get_scheduler_agents()

        if agents:
            agent = agents[0]
            required_keys = [
                "name",
                "role",
                "description",
                "model",
                "enabled",
                "node",
                "max_concurrent_tasks",
                "tools",
            ]
            for key in required_keys:
                assert key in agent, f"Missing required key: {key}"

    def test_handles_missing_config(self, tmp_path):
        """Test returns empty list when config is missing."""
        from scripts.ops.dashboard_server import _get_scheduler_agents

        missing_config = tmp_path / "nonexistent.yaml"
        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", missing_config):
            agents = _get_scheduler_agents()

        assert agents == []

    def test_normalizes_agent_entries(self, tmp_path):
        """Test agent entries are normalized with default values."""
        from scripts.ops.dashboard_server import _get_scheduler_agents

        # Create config with minimal agent entry
        config = {
            "agents": [
                {
                    "name": "minimal_agent",
                    "role": "tester",
                },
            ],
        }
        config_path = tmp_path / "minimal.yaml"
        config_path.write_text(json.dumps(config))

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", config_path):
            agents = _get_scheduler_agents()

        assert len(agents) == 1
        assert agents[0]["name"] == "minimal_agent"
        assert agents[0]["role"] == "tester"
        # Check defaults
        assert agents[0]["description"] == ""
        assert agents[0]["model"] == ""
        assert agents[0]["enabled"] is False
        assert agents[0]["node"] == "primary"
        assert agents[0]["max_concurrent_tasks"] == 1
        assert agents[0]["tools"] == []


# ══════════════════════════════════════════════════════════════════════════════
# API endpoint tests
# ══════════════════════════════════════════════════════════════════════════════

class TestHeartbeatsAPI:
    """Test GET /api/heartbeats endpoint."""

    def _make_handler(self, path, query=None, store=None):
        """Create a mock DashboardHandler for testing."""
        from scripts.ops.dashboard_server import DashboardHandler
        from io import BytesIO

        handler = mock.MagicMock(spec=DashboardHandler)
        handler.path = path
        handler.headers = {"Content-Length": "0"}
        handler.rfile = BytesIO(b"")
        handler.wfile = BytesIO()
        handler.store = store

        # Capture responses by tracking wfile writes
        response_data = {}

        def _mock_json_response(data, status=200, cache_control=None):
            response_data["status"] = status
            response_data["data"] = data
            handler.wfile.write(json.dumps(data).encode())

        handler._json_response = _mock_json_response

        return handler, response_data

    def test_heartbeats_response_structure(self, ticket_store):
        """Test heartbeats endpoint returns correct structure."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import DashboardHandler

        handler, response = self._make_handler("/api/heartbeats", {}, store)

        # Import and call the actual handler method
        from scripts.ops.dashboard_server import DashboardHandler as DH
        with mock.patch.object(handler, "store", store):
            DH._handle_api_heartbeats(handler, {})

        data = response["data"]
        assert "runs" in data
        assert "count" in data
        assert "summary" in data
        assert "live_count" in data["summary"]
        assert "total_count" in data["summary"]
        assert "by_status" in data["summary"]
        assert "by_severity" in data["summary"]

    def test_heartbeats_active_only_default(self, ticket_store):
        """Test active_only defaults to true (only live runs)."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import DashboardHandler

        handler, response = self._make_handler("/api/heartbeats", {}, store)

        from scripts.ops.dashboard_server import DashboardHandler as DH
        with mock.patch.object(handler, "store", store):
            DH._handle_api_heartbeats(handler, {})

        data = response["data"]
        # Should only include live runs (t001, t002, t003, t007)
        # t004 has old heartbeat (>10 min)
        assert all(r["is_live"] for r in data["runs"])
        assert data["summary"]["live_count"] == len(data["runs"])

    def test_heartbeats_active_only_false(self, ticket_store):
        """Test active_only=false includes all active tickets."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import DashboardHandler

        handler, response = self._make_handler("/api/heartbeats", {"active_only": ["false"]}, store)

        from scripts.ops.dashboard_server import DashboardHandler as DH
        with mock.patch.object(handler, "store", store):
            DH._handle_api_heartbeats(handler, {"active_only": ["false"]})

        data = response["data"]
        # Should include t004 (old heartbeat)
        ticket_ids = [r["ticket_id"] for r in data["runs"]]
        assert "t004" in ticket_ids
        assert data["summary"]["total_count"] > data["summary"]["live_count"]

    def test_heartbeats_with_ticket_id_filter(self, ticket_store):
        """Test filtering by ticket_id parameter."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import DashboardHandler

        handler, response = self._make_handler("/api/heartbeats", {"ticket_id": ["t001"]}, store)

        from scripts.ops.dashboard_server import DashboardHandler as DH
        with mock.patch.object(handler, "store", store):
            DH._handle_api_heartbeats(handler, {"ticket_id": ["t001"]})

        data = response["data"]
        assert data["count"] == 1
        assert data["runs"][0]["ticket_id"] == "t001"
        assert "active_run" in data  # Should include active_run for single ticket query

    def test_heartbeats_with_agents_include(self, config_file, ticket_store):
        """Test including scheduler agents when agents=true."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import DashboardHandler

        handler, response = self._make_handler("/api/heartbeats", {"agents": ["true"]}, store)

        from scripts.ops.dashboard_server import DashboardHandler as DH
        with mock.patch.object(handler, "store", store), \
             mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", config_file):
            DH._handle_api_heartbeats(handler, {"agents": ["true"]})

        data = response["data"]
        assert "agents" in data
        assert len(data["agents"]) == 3
        assert data["agents"][0]["name"] == "swe_monitor"

    def test_heartbeats_summary_counts(self, ticket_store):
        """Test summary contains correct counts by status and severity."""
        store, _ = ticket_store
        from scripts.ops.dashboard_server import DashboardHandler

        handler, response = self._make_handler("/api/heartbeats", {}, store)

        from scripts.ops.dashboard_server import DashboardHandler as DH
        with mock.patch.object(handler, "store", store):
            DH._handle_api_heartbeats(handler, {})

        data = response["data"]
        summary = data["summary"]

        # Check by_status counts
        assert "investigating" in summary["by_status"]
        assert "in_development" in summary["by_status"]

        # Check by_severity counts
        assert "critical" in summary["by_severity"]
        assert "high" in summary["by_severity"]
        assert "medium" in summary["by_severity"]

        # Verify counts match runs
        total_status_count = sum(summary["by_status"].values())
        total_severity_count = sum(summary["by_severity"].values())
        assert total_status_count == data["count"]
        assert total_severity_count == data["count"]

    def test_heartbeats_empty_store(self, tmp_path):
        """Test heartbeats with empty ticket store."""
        store = TicketStore(str(tmp_path / "empty.json"))
        from scripts.ops.dashboard_server import DashboardHandler

        handler, response = self._make_handler("/api/heartbeats", {}, store)

        from scripts.ops.dashboard_server import DashboardHandler as DH
        with mock.patch.object(handler, "store", store):
            DH._handle_api_heartbeats(handler, {})

        data = response["data"]
        assert data["count"] == 0
        assert data["runs"] == []
        assert data["summary"]["live_count"] == 0
        assert data["summary"]["total_count"] == 0
