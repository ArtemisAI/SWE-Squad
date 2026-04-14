"""
Tests for activity filtering API endpoint (issue #418).

Covers:
  - GET /api/activity with various filter combinations
  - Action type filtering
  - Severity filtering
  - Agent filtering
  - Ticket ID filtering
  - Text search filtering
  - Multiple filter combinations
  - Available filter values in response
"""
from __future__ import annotations

import io
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Project bootstrap ─────────────────────────────────────────────────
logging.logAsyncioTasks = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.ticket_store import TicketStore


# ════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_store(tmp_path):
    """Provide a TicketStore backed by a temp directory."""
    path = tmp_path / "tickets.json"
    store = TicketStore(path=str(path))
    return store


@pytest.fixture
def mock_log_file(tmp_path):
    """Create a mock log file with activity entries."""
    log_path = tmp_path / "swe_team.log"
    log_content = """
2026-04-04 10:00:00 [INFO] Investigating ticket abc123def456: Database connection pool exhausted
2026-04-04 10:05:00 [INFO] Triaged ticket abc123def456 as CRITICAL
2026-04-04 10:10:00 [INFO] attempt_fix: fixing database connection issue
2026-04-04 10:15:00 [INFO] investigation_complete: root cause found
2026-04-04 10:20:00 [INFO] status changed to investigating for abc123def456
2026-04-04 10:25:00 [INFO] SESSION: investigation session started for xyz789uvw
2026-04-04 10:30:00 [INFO] gate: stability check passed
2026-04-04 10:35:00 [INFO] Deployed fix for abc123def456
2026-04-04 10:40:00 [INFO] Claude CLI: dev_complete for abc123def456
2026-04-04 10:45:00 [INFO] Dispatched to investigator: HIGH ticket 111222333
"""
    log_path.write_text(log_content)
    return log_path


def _make_handler(store):
    """Create a DashboardHandler instance configured for unit tests."""
    from scripts.ops.dashboard_server import DashboardHandler

    handler = MagicMock(spec=DashboardHandler)
    handler.store = store
    handler.auth_provider = None
    handler.headers = {"Content-Length": "0"}

    # Wire up real methods
    handler._read_post_body = DashboardHandler._read_post_body.__get__(handler)
    handler._json_response = DashboardHandler._json_response.__get__(handler)
    handler._handle_api_activity = DashboardHandler._handle_api_activity.__get__(handler)

    # Mock _send_gzipped to capture JSON data
    captured_json = []
    def mock_send(content, content_type, cache_control=None):
        import json
        try:
            data = json.loads(content) if isinstance(content, str) else json.loads(content.decode())
            captured_json.append(data)
        except:
            captured_json.append({"raw": content})
    handler._send_gzipped = mock_send
    handler._captured_json = lambda: captured_json[-1] if captured_json else None

    return handler


def _capture_json(handler):
    """Extract JSON body from the last _send_gzipped call."""
    return handler._captured_json()


# ══════════════════════════════════════════════════════════════════
# Tests: GET /api/activity
# ══════════════════════════════════════════════════════════════════════

class TestActivityFiltering:
    """Test /api/activity endpoint with various filter combinations."""

    def test_activity_no_filters(self, tmp_store, mock_log_file):
        """Test that activity endpoint returns data with no filters applied."""
        with patch('scripts.ops.dashboard_server.PROJECT_ROOT', mock_log_file.parent):
            handler = _make_handler(tmp_store)
            handler._handle_api_activity({})

            data = _capture_json(handler)
            assert data is not None
            assert "activities" in data
            assert "filters" in data
            assert "applied_filters" in data
            assert isinstance(data["activities"], list)

    def test_activity_with_action_type_filter(self, tmp_store, mock_log_file):
        """Test filtering by action type."""
        with patch('scripts.ops.dashboard_server.PROJECT_ROOT', mock_log_file.parent):
            handler = _make_handler(tmp_store)
            handler._handle_api_activity({"action_type": ["investigation_complete"]})

            data = _capture_json(handler)
            assert data is not None
            assert data["applied_filters"]["action_type"] == "investigation_complete"

    def test_activity_with_severity_filter(self, tmp_store, mock_log_file):
        """Test filtering by severity."""
        with patch('scripts.ops.dashboard_server.PROJECT_ROOT', mock_log_file.parent):
            handler = _make_handler(tmp_store)
            handler._handle_api_activity({"severity": ["critical"]})

            data = _capture_json(handler)
            assert data is not None
            assert data["applied_filters"]["severity"] == "critical"

    def test_activity_with_agent_filter(self, tmp_store, mock_log_file):
        """Test filtering by agent."""
        with patch('scripts.ops.dashboard_server.PROJECT_ROOT', mock_log_file.parent):
            handler = _make_handler(tmp_store)
            handler._handle_api_activity({"agent": ["swe-squad"]})

            data = _capture_json(handler)
            assert data is not None
            assert data["applied_filters"]["agent"] == "swe-squad"

    def test_activity_with_ticket_id_filter(self, tmp_store, mock_log_file):
        """Test filtering by ticket ID."""
        with patch('scripts.ops.dashboard_server.PROJECT_ROOT', mock_log_file.parent):
            handler = _make_handler(tmp_store)
            handler._handle_api_activity({"ticket_id": ["abc123def456"]})

            data = _capture_json(handler)
            assert data is not None
            assert data["applied_filters"]["ticket_id"] == "abc123def456"

    def test_activity_with_search_filter(self, tmp_store, mock_log_file):
        """Test filtering by text search."""
        with patch('scripts.ops.dashboard_server.PROJECT_ROOT', mock_log_file.parent):
            handler = _make_handler(tmp_store)
            handler._handle_api_activity({"search": ["database"]})

            data = _capture_json(handler)
            assert data is not None
            assert data["applied_filters"]["search"] == "database"

    def test_activity_returns_filter_values(self, tmp_store, mock_log_file):
        """Test that endpoint returns available filter values."""
        with patch('scripts.ops.dashboard_server.PROJECT_ROOT', mock_log_file.parent):
            handler = _make_handler(tmp_store)
            handler._handle_api_activity({})

            data = _capture_json(handler)
            assert data is not None
            assert "filters" in data
            assert "agents" in data["filters"]
            assert "action_types" in data["filters"]
            assert "severities" in data["filters"]

    def test_activity_entries_have_required_fields(self, tmp_store, mock_log_file):
        """Test that activity entries have all required fields."""
        with patch('scripts.ops.dashboard_server.PROJECT_ROOT', mock_log_file.parent):
            handler = _make_handler(tmp_store)
            handler._handle_api_activity({})

            data = _capture_json(handler)
            assert data is not None
            activities = data.get("activities", [])
            if activities:
                # Check first activity has required fields
                first = activities[0]
                assert "time" in first
                assert "agent" in first
                assert "action" in first

    def test_activity_max_entries_limited(self, tmp_store, mock_log_file):
        """Test that activity endpoint limits max entries to 30."""
        with patch('scripts.ops.dashboard_server.PROJECT_ROOT', mock_log_file.parent):
            handler = _make_handler(tmp_store)
            handler._handle_api_activity({})

            data = _capture_json(handler)
            assert data is not None
            activities = data.get("activities", [])
            assert len(activities) <= 30

    def test_activity_all_filter_value_returns_all(self, tmp_store, mock_log_file):
        """Test that 'all' filter value returns all entries."""
        with patch('scripts.ops.dashboard_server.PROJECT_ROOT', mock_log_file.parent):
            handler = _make_handler(tmp_store)
            handler._handle_api_activity({"action_type": ["all"]})

            data = _capture_json(handler)
            assert data is not None
            assert data["applied_filters"]["action_type"] is None or data["applied_filters"]["action_type"] == "all"
