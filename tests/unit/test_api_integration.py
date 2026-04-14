"""
Integration tests for dashboard API endpoint groups.

Tests the full handler flow for each major endpoint group by simulating
request/response cycles through DashboardHandler methods.

Covers:
  - Suggestions API: list, accept, dismiss
  - Execution Mode API: get mode, set mode, checkpoints, approve/reject
  - Label Triggers API: list, create, delete, duplicate
  - Scheduler Templates API: list, apply
  - Rate Limits API: get status
  - Project Env API: list, set, delete
  - MCP Servers API: full CRUD (list, add, patch, delete)
  - Ticket Feed API: get feed, add comment
  - Budget Policies API: get policies, save policies
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Project bootstrap ─────────────────────────────────────────────────────────
logging.logAsyncioTasks = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.ticket_store import TicketStore


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_store(tmp_path):
    """Provide a TicketStore backed by a temp directory."""
    path = tmp_path / "tickets.json"
    return TicketStore(path=str(path))


@pytest.fixture
def sample_ticket():
    """Provide a sample SWETicket."""
    return SWETicket(
        ticket_id="feed-ticket-001",
        title="Test ticket for feed",
        description="A ticket used for feed testing.",
        severity=TicketSeverity.HIGH,
        status=TicketStatus.TRIAGED,
        source_module="test_module",
        assigned_to=None,
        metadata={"fingerprint": "fp-feed-001"},
    )


@pytest.fixture
def store_with_ticket(tmp_store, sample_ticket):
    """Store with a pre-loaded ticket."""
    tmp_store.add(sample_ticket)
    return tmp_store


# ══════════════════════════════════════════════════════════════════════════════
# Helper: Mock handler
# ══════════════════════════════════════════════════════════════════════════════

def _make_handler(store, tmp_path=None):
    """Create a DashboardHandler instance configured for unit tests."""
    from scripts.ops.dashboard_server import DashboardHandler

    handler = MagicMock(spec=DashboardHandler)
    handler.store = store
    handler.auth_provider = None
    handler.headers = {"Content-Length": "0"}
    handler.server = MagicMock()

    # Wire up real methods we need
    handler._read_post_body = DashboardHandler._read_post_body.__get__(handler)
    handler._json_response = DashboardHandler._json_response.__get__(handler)

    # Suggestions
    handler._handle_list_suggestions = DashboardHandler._handle_list_suggestions.__get__(handler)
    handler._handle_suggestion_accept = DashboardHandler._handle_suggestion_accept.__get__(handler)
    handler._handle_suggestion_dismiss = DashboardHandler._handle_suggestion_dismiss.__get__(handler)

    # Execution mode
    handler._handle_patch_execution_mode = DashboardHandler._handle_patch_execution_mode.__get__(handler)
    handler._handle_get_execution_checkpoints = DashboardHandler._handle_get_execution_checkpoints.__get__(handler)
    handler._handle_checkpoint_approve = DashboardHandler._handle_checkpoint_approve.__get__(handler)
    handler._handle_checkpoint_reject = DashboardHandler._handle_checkpoint_reject.__get__(handler)

    # Label triggers
    handler._handle_list_label_triggers = DashboardHandler._handle_list_label_triggers.__get__(handler)
    handler._handle_create_label_trigger = DashboardHandler._handle_create_label_trigger.__get__(handler)
    handler._handle_delete_label_trigger = DashboardHandler._handle_delete_label_trigger.__get__(handler)
    handler._load_label_triggers = DashboardHandler._load_label_triggers.__get__(handler)
    handler._save_label_triggers = DashboardHandler._save_label_triggers.__get__(handler)

    # Scheduler templates
    handler._handle_apply_template = DashboardHandler._handle_apply_template.__get__(handler)

    # Rate limits
    handler._handle_get_rate_limits = DashboardHandler._handle_get_rate_limits.__get__(handler)

    # Project env
    handler._handle_list_project_env = DashboardHandler._handle_list_project_env.__get__(handler)
    handler._handle_set_project_env = DashboardHandler._handle_set_project_env.__get__(handler)
    handler._handle_delete_project_env = DashboardHandler._handle_delete_project_env.__get__(handler)

    # MCP servers
    handler._handle_mcp_servers_list = DashboardHandler._handle_mcp_servers_list.__get__(handler)
    handler._handle_mcp_server_add = DashboardHandler._handle_mcp_server_add.__get__(handler)
    handler._handle_mcp_server_delete = DashboardHandler._handle_mcp_server_delete.__get__(handler)
    handler._handle_mcp_server_patch = DashboardHandler._handle_mcp_server_patch.__get__(handler)
    handler._read_mcp_servers = DashboardHandler._read_mcp_servers.__get__(handler)
    handler._write_mcp_servers = DashboardHandler._write_mcp_servers.__get__(handler)

    # Feed
    handler._handle_get_ticket_feed = DashboardHandler._handle_get_ticket_feed.__get__(handler)
    handler._handle_add_feed_comment = DashboardHandler._handle_add_feed_comment.__get__(handler)
    handler._read_feed = DashboardHandler._read_feed.__get__(handler)
    handler._write_feed = DashboardHandler._write_feed.__get__(handler)
    handler._get_feed_path = DashboardHandler._get_feed_path.__get__(handler)
    handler._generate_feed_from_ticket = DashboardHandler._generate_feed_from_ticket.__get__(handler)

    # Budget policies
    handler._handle_budget_policies_get = DashboardHandler._handle_budget_policies_get.__get__(handler)
    handler._handle_budget_policies_post = DashboardHandler._handle_budget_policies_post.__get__(handler)

    # Set MCP servers path to temp dir if provided
    if tmp_path:
        handler._MCP_SERVERS_PATH = tmp_path / "mcp_servers.json"

    return handler


def _set_body(handler, body_dict):
    """Set the POST body for a mock handler."""
    raw = json.dumps(body_dict).encode()
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)


def _capture(handler):
    """Return a capture function and a list to collect (data, status) tuples."""
    responses = []
    def capture(data, status=200, **kwargs):
        responses.append((data, status))
    handler._json_response = capture
    return responses


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Suggestions API
# ══════════════════════════════════════════════════════════════════════════════

class TestSuggestionsAPI:
    """Tests for GET /api/suggestions, POST /api/suggestions/<id>/accept|dismiss."""

    @patch("scripts.ops.dashboard_server._read_suggestions")
    def test_list_suggestions_empty(self, mock_read, tmp_store):
        mock_read.return_value = []
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_list_suggestions()

        assert len(responses) == 1
        data, status = responses[0]
        assert status == 200
        assert data["suggestions"] == []
        assert data["count"] == 0

    @patch("scripts.ops.dashboard_server._read_suggestions")
    def test_list_suggestions_with_items(self, mock_read, tmp_store):
        mock_read.return_value = [
            {"id": "s1", "title": "Add linting", "status": "pending", "category": "quality"},
            {"id": "s2", "title": "Add CI", "status": "accepted", "category": "devops"},
        ]
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_list_suggestions()

        data, status = responses[0]
        assert status == 200
        assert data["count"] == 2
        assert data["suggestions"][0]["id"] == "s1"

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    @patch("scripts.ops.dashboard_server._write_suggestions")
    @patch("scripts.ops.dashboard_server._read_suggestions")
    def test_accept_suggestion_creates_ticket(self, mock_read, mock_write, mock_sse, store_with_ticket):
        mock_read.return_value = [
            {"id": "s1", "title": "Improve error handling", "status": "pending",
             "category": "quality", "description": "Better errors", "impact": "medium"},
        ]
        handler = _make_handler(store_with_ticket)
        responses = _capture(handler)

        handler._handle_suggestion_accept("s1")

        data, status = responses[0]
        assert status == 200
        assert data["status"] == "ok"
        assert data["suggestion_id"] == "s1"
        assert "ticket_id" in data

        # Verify ticket was created in the store
        ticket = store_with_ticket.get(data["ticket_id"])
        assert ticket is not None
        assert ticket.title == "Improve error handling"
        assert "suggestion" in ticket.labels
        mock_write.assert_called_once()
        mock_sse.assert_called_once()

    @patch("scripts.ops.dashboard_server._read_suggestions")
    def test_accept_nonexistent_suggestion(self, mock_read, tmp_store):
        mock_read.return_value = []
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_suggestion_accept("nonexistent")

        data, status = responses[0]
        assert status == 404
        assert "error" in data

    @patch("scripts.ops.dashboard_server._read_suggestions")
    def test_accept_already_accepted(self, mock_read, tmp_store):
        mock_read.return_value = [
            {"id": "s1", "title": "Done", "status": "accepted"},
        ]
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_suggestion_accept("s1")

        data, status = responses[0]
        assert status == 400
        assert "not pending" in data["error"]

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    @patch("scripts.ops.dashboard_server._write_suggestions")
    @patch("scripts.ops.dashboard_server._read_suggestions")
    def test_dismiss_suggestion(self, mock_read, mock_write, mock_sse, tmp_store):
        mock_read.return_value = [
            {"id": "s1", "title": "Not useful", "status": "pending"},
        ]
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {"reason": "Not relevant"})

        handler._handle_suggestion_dismiss("s1")

        data, status = responses[0]
        assert status == 200
        assert data["status"] == "ok"
        mock_write.assert_called_once()
        # Verify the suggestion was updated
        written = mock_write.call_args[0][0]
        assert written[0]["status"] == "dismissed"
        assert written[0]["dismiss_reason"] == "Not relevant"

    @patch("scripts.ops.dashboard_server._read_suggestions")
    def test_dismiss_nonexistent(self, mock_read, tmp_store):
        mock_read.return_value = []
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {})

        handler._handle_suggestion_dismiss("ghost")

        data, status = responses[0]
        assert status == 404


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Execution Mode API
# ══════════════════════════════════════════════════════════════════════════════

class TestExecutionModeAPI:
    """Tests for GET/PATCH /api/execution/mode, GET /api/execution/checkpoints,
    POST /api/execution/checkpoints/<id>/approve|reject."""

    def test_get_execution_mode_default(self):
        """_read_execution_mode returns 'start' by default when file missing."""
        from scripts.ops.dashboard_server import _read_execution_mode
        with patch("scripts.ops.dashboard_server._read_json_file_with_timeout", return_value=None):
            result = _read_execution_mode()
        assert result["mode"] == "start"
        assert "plan" in result["available_modes"]
        assert "review" in result["available_modes"]
        assert "start" in result["available_modes"]

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    @patch("scripts.ops.dashboard_server._write_execution_mode", return_value=True)
    def test_set_execution_mode_valid(self, mock_write, mock_sse, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {"mode": "review"})

        handler._handle_patch_execution_mode()

        data, status = responses[0]
        assert status == 200
        assert data["mode"] == "review"
        assert data["status"] == "ok"
        mock_write.assert_called_once_with("review")
        mock_sse.assert_called_once()

    def test_set_execution_mode_invalid(self, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {"mode": "yolo"})

        handler._handle_patch_execution_mode()

        data, status = responses[0]
        assert status == 400
        assert "Invalid mode" in data["error"]

    @patch("scripts.ops.dashboard_server._write_execution_mode", return_value=False)
    def test_set_execution_mode_write_failure(self, mock_write, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {"mode": "plan"})

        handler._handle_patch_execution_mode()

        data, status = responses[0]
        assert status == 500

    @patch("scripts.ops.dashboard_server._read_checkpoints")
    def test_get_checkpoints_filters_pending(self, mock_read, tmp_store):
        mock_read.return_value = [
            {"id": "cp1", "status": "pending", "ticket_id": "t1"},
            {"id": "cp2", "status": "approved", "ticket_id": "t2"},
            {"id": "cp3", "status": "pending", "ticket_id": "t3"},
        ]
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_get_execution_checkpoints()

        data, status = responses[0]
        assert status == 200
        assert data["count"] == 2
        ids = [cp["id"] for cp in data["checkpoints"]]
        assert "cp1" in ids
        assert "cp3" in ids
        assert "cp2" not in ids

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    @patch("scripts.ops.dashboard_server._write_checkpoints")
    @patch("scripts.ops.dashboard_server._read_checkpoints")
    def test_approve_checkpoint(self, mock_read, mock_write, mock_sse, tmp_store):
        mock_read.return_value = [
            {"id": "cp1", "status": "pending", "ticket_id": "t1"},
        ]
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_checkpoint_approve("cp1")

        data, status = responses[0]
        assert status == 200
        assert data["new_status"] == "approved"
        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert written[0]["status"] == "approved"
        assert "resolved_at" in written[0]

    @patch("scripts.ops.dashboard_server._read_checkpoints")
    def test_approve_nonexistent_checkpoint(self, mock_read, tmp_store):
        mock_read.return_value = []
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_checkpoint_approve("missing")

        data, status = responses[0]
        assert status == 404

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    @patch("scripts.ops.dashboard_server._write_checkpoints")
    @patch("scripts.ops.dashboard_server._read_checkpoints")
    def test_reject_checkpoint_with_feedback(self, mock_read, mock_write, mock_sse, tmp_store):
        mock_read.return_value = [
            {"id": "cp1", "status": "pending", "ticket_id": "t1"},
        ]
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {"feedback": "Needs more tests"})

        handler._handle_checkpoint_reject("cp1")

        data, status = responses[0]
        assert status == 200
        assert data["new_status"] == "rejected"
        written = mock_write.call_args[0][0]
        assert written[0]["feedback"] == "Needs more tests"


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Label Triggers API
# ══════════════════════════════════════════════════════════════════════════════

class TestLabelTriggersAPI:
    """Tests for /api/github/label-triggers CRUD."""

    def test_list_empty(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", tmp_path / "triggers.json"):
            handler._handle_list_label_triggers()

        data, status = responses[0]
        assert status == 200
        assert data["triggers"] == []

    def test_create_trigger(self, tmp_store, tmp_path):
        triggers_path = tmp_path / "triggers.json"
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {"label": "swe-squad", "severity": "high", "auto_assign": True})

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_path):
            handler._handle_create_label_trigger()

        data, status = responses[0]
        assert status == 200
        assert data["ok"] is True
        assert data["trigger"]["label"] == "swe-squad"
        assert data["trigger"]["severity"] == "high"

        # Verify persisted
        saved = json.loads(triggers_path.read_text())
        assert len(saved) == 1
        assert saved[0]["label"] == "swe-squad"

    def test_create_trigger_missing_label(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {"severity": "high"})

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", tmp_path / "triggers.json"):
            handler._handle_create_label_trigger()

        data, status = responses[0]
        assert status == 400
        assert "label is required" in data["error"]

    def test_create_trigger_invalid_severity(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {"label": "test", "severity": "extreme"})

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", tmp_path / "triggers.json"):
            handler._handle_create_label_trigger()

        data, status = responses[0]
        assert status == 400
        assert "Invalid severity" in data["error"]

    def test_create_duplicate_updates_existing(self, tmp_store, tmp_path):
        triggers_path = tmp_path / "triggers.json"
        triggers_path.write_text(json.dumps([
            {"label": "bug", "severity": "low", "auto_assign": False, "enabled": True},
        ]))
        handler = _make_handler(tmp_store)

        # Create with same label but different severity
        responses = _capture(handler)
        _set_body(handler, {"label": "bug", "severity": "critical", "auto_assign": True})

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_path):
            handler._handle_create_label_trigger()

        data, status = responses[0]
        assert status == 200
        assert data["trigger"]["severity"] == "critical"

        saved = json.loads(triggers_path.read_text())
        assert len(saved) == 1  # Still one, not two
        assert saved[0]["severity"] == "critical"

    def test_delete_trigger(self, tmp_store, tmp_path):
        triggers_path = tmp_path / "triggers.json"
        triggers_path.write_text(json.dumps([
            {"label": "bug", "severity": "high", "auto_assign": True, "enabled": True},
            {"label": "feature", "severity": "low", "auto_assign": False, "enabled": True},
        ]))
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_path):
            handler._handle_delete_label_trigger("bug")

        data, status = responses[0]
        assert status == 200
        assert data["ok"] is True
        assert data["deleted"] == "bug"

        saved = json.loads(triggers_path.read_text())
        assert len(saved) == 1
        assert saved[0]["label"] == "feature"

    def test_delete_nonexistent_trigger(self, tmp_store, tmp_path):
        triggers_path = tmp_path / "triggers.json"
        triggers_path.write_text(json.dumps([]))
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_path):
            handler._handle_delete_label_trigger("ghost")

        data, status = responses[0]
        assert status == 404


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Scheduler Templates API
# ══════════════════════════════════════════════════════════════════════════════

class TestSchedulerTemplatesAPI:
    """Tests for GET /api/scheduler/templates, POST /api/scheduler/templates/<id>/apply."""

    def test_list_templates(self):
        """SCHEDULER_TEMPLATES is a non-empty list with expected fields."""
        from scripts.ops.dashboard_server import SCHEDULER_TEMPLATES
        assert isinstance(SCHEDULER_TEMPLATES, list)
        assert len(SCHEDULER_TEMPLATES) > 0
        for tpl in SCHEDULER_TEMPLATES:
            assert "id" in tpl
            assert "name" in tpl
            assert "cron" in tpl
            assert "action" in tpl

    def test_get_scheduler_template_found(self):
        from scripts.ops.dashboard_server import _get_scheduler_template
        tpl = _get_scheduler_template("daily-triage")
        assert tpl is not None
        assert tpl["id"] == "daily-triage"

    def test_get_scheduler_template_not_found(self):
        from scripts.ops.dashboard_server import _get_scheduler_template
        assert _get_scheduler_template("nonexistent-template") is None

    @patch("scripts.ops.dashboard_server._get_scheduler_and_store")
    def test_apply_template(self, mock_get_sched, tmp_store):
        from src.swe_team.scheduler import ScheduledJob
        mock_store = MagicMock()
        mock_scheduler = MagicMock()
        # Make add_job return a ScheduledJob
        mock_scheduler.add_job.side_effect = lambda job: job
        mock_get_sched.return_value = (mock_store, mock_scheduler)

        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {})

        handler._handle_apply_template("daily-triage")

        data, status = responses[0]
        assert status == 200
        assert data["ok"] is True
        assert "job" in data
        assert "template" in data
        assert data["template"]["id"] == "daily-triage"
        mock_scheduler.add_job.assert_called_once()

    def test_apply_nonexistent_template(self, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {})

        handler._handle_apply_template("no-such-template")

        data, status = responses[0]
        assert status == 404
        assert "not found" in data["error"]


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Rate Limits API
# ══════════════════════════════════════════════════════════════════════════════

class TestRateLimitsAPI:
    """Tests for GET /api/rate-limits."""

    @patch("scripts.ops.dashboard_server.logger")
    def test_get_rate_limits_success(self, mock_logger, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        with patch("src.swe_team.rate_limiter.get_all_lifecycle_statuses", return_value=[
            {"provider": "anthropic", "state": "healthy", "requests_remaining": 100},
        ]):
            handler._handle_get_rate_limits()

        data, status = responses[0]
        assert status == 200
        assert "providers" in data
        assert len(data["providers"]) == 1
        assert data["providers"][0]["provider"] == "anthropic"

    def test_get_rate_limits_empty(self, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        with patch("src.swe_team.rate_limiter.get_all_lifecycle_statuses", return_value=[]):
            handler._handle_get_rate_limits()

        data, status = responses[0]
        assert status == 200
        assert data["providers"] == []

    def test_get_rate_limits_error(self, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        with patch(
            "src.swe_team.rate_limiter.get_all_lifecycle_statuses",
            side_effect=RuntimeError("Registry crashed"),
        ):
            handler._handle_get_rate_limits()

        data, status = responses[0]
        assert status == 500
        assert "error" in data


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Project Env API
# ══════════════════════════════════════════════════════════════════════════════

class TestProjectEnvAPI:
    """Tests for /api/projects/<name>/env CRUD."""

    @patch("scripts.ops.dashboard_server._load_project_env", return_value=[])
    def test_list_env_empty(self, mock_load, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_list_project_env("my-project")

        data, status = responses[0]
        assert status == 200
        assert data["env_vars"] == []

    @patch("scripts.ops.dashboard_server._load_project_env")
    def test_list_env_masks_secrets(self, mock_load, tmp_store):
        mock_load.return_value = [
            {"key": "PUBLIC_KEY", "value": "abc123", "secret": False},
            {"key": "API_TOKEN", "value": "super-secret", "secret": True},
        ]
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_list_project_env("my-project")

        data, status = responses[0]
        assert status == 200
        assert len(data["env_vars"]) == 2
        assert data["env_vars"][0]["value"] == "abc123"
        assert data["env_vars"][1]["value"] == "********"

    @patch("scripts.ops.dashboard_server._save_project_env", return_value=True)
    @patch("scripts.ops.dashboard_server._load_project_env", return_value=[])
    def test_set_env_var(self, mock_load, mock_save, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {"key": "NODE_ENV", "value": "production", "secret": False})

        handler._handle_set_project_env("my-project")

        data, status = responses[0]
        assert status == 201
        assert data["ok"] is True
        assert data["key"] == "NODE_ENV"
        mock_save.assert_called_once()
        saved_vars = mock_save.call_args[0][1]
        assert len(saved_vars) == 1
        assert saved_vars[0]["key"] == "NODE_ENV"

    def test_set_env_var_missing_key(self, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {"value": "something"})

        handler._handle_set_project_env("my-project")

        data, status = responses[0]
        assert status == 400
        assert "key" in data["error"].lower()

    @patch("scripts.ops.dashboard_server._save_project_env", return_value=True)
    @patch("scripts.ops.dashboard_server._load_project_env")
    def test_delete_env_var(self, mock_load, mock_save, tmp_store):
        mock_load.return_value = [
            {"key": "A", "value": "1", "secret": False},
            {"key": "B", "value": "2", "secret": False},
        ]
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_delete_project_env("my-project", "A")

        data, status = responses[0]
        assert status == 200
        assert data["ok"] is True
        assert data["deleted"] == "A"
        saved_vars = mock_save.call_args[0][1]
        assert len(saved_vars) == 1
        assert saved_vars[0]["key"] == "B"

    @patch("scripts.ops.dashboard_server._load_project_env", return_value=[])
    def test_delete_env_var_not_found(self, mock_load, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_delete_project_env("my-project", "MISSING")

        data, status = responses[0]
        assert status == 404


# ══════════════════════════════════════════════════════════════════════════════
# Tests: MCP Servers API
# ══════════════════════════════════════════════════════════════════════════════

class TestMCPServersAPI:
    """Tests for /api/mcp/servers full CRUD cycle."""

    def test_list_empty(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store, tmp_path=tmp_path)
        responses = _capture(handler)

        handler._handle_mcp_servers_list()

        data, status = responses[0]
        assert status == 200
        assert data["servers"] == []

    def test_add_server(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store, tmp_path=tmp_path)
        responses = _capture(handler)
        _set_body(handler, {
            "name": "my-mcp",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-everything"],
            "env": {"DEBUG": "true"},
        })

        handler._handle_mcp_server_add()

        data, status = responses[0]
        assert status == 201
        assert data["ok"] is True
        assert data["server"]["name"] == "my-mcp"
        assert data["server"]["enabled"] is True

    def test_add_duplicate_server(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store, tmp_path=tmp_path)
        # Seed one server
        handler._write_mcp_servers([{"name": "existing", "command": "node", "args": [], "env": {}, "enabled": True}])

        responses = _capture(handler)
        _set_body(handler, {"name": "existing", "command": "python"})

        handler._handle_mcp_server_add()

        data, status = responses[0]
        assert status == 409
        assert "already exists" in data["error"]

    def test_add_server_missing_name(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store, tmp_path=tmp_path)
        responses = _capture(handler)
        _set_body(handler, {"command": "npx"})

        handler._handle_mcp_server_add()

        data, status = responses[0]
        assert status == 400
        assert "name" in data["error"]

    def test_add_server_missing_command(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store, tmp_path=tmp_path)
        responses = _capture(handler)
        _set_body(handler, {"name": "test"})

        handler._handle_mcp_server_add()

        data, status = responses[0]
        assert status == 400
        assert "command" in data["error"]

    def test_patch_server(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store, tmp_path=tmp_path)
        handler._write_mcp_servers([
            {"name": "srv1", "command": "node", "args": [], "env": {}, "enabled": True},
        ])
        responses = _capture(handler)
        _set_body(handler, {"enabled": False, "command": "python3"})

        handler._handle_mcp_server_patch("srv1")

        data, status = responses[0]
        assert status == 200
        assert data["ok"] is True
        assert data["server"]["enabled"] is False
        assert data["server"]["command"] == "python3"

    def test_patch_nonexistent_server(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store, tmp_path=tmp_path)
        responses = _capture(handler)
        _set_body(handler, {"enabled": False})

        handler._handle_mcp_server_patch("ghost")

        data, status = responses[0]
        assert status == 404

    def test_delete_server(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store, tmp_path=tmp_path)
        handler._write_mcp_servers([
            {"name": "srv1", "command": "node", "args": [], "env": {}, "enabled": True},
            {"name": "srv2", "command": "python", "args": [], "env": {}, "enabled": True},
        ])
        responses = _capture(handler)

        handler._handle_mcp_server_delete("srv1")

        data, status = responses[0]
        assert status == 200
        assert data["ok"] is True
        assert data["deleted"] == "srv1"

        # Verify only srv2 remains
        remaining = handler._read_mcp_servers()
        assert len(remaining) == 1
        assert remaining[0]["name"] == "srv2"

    def test_delete_nonexistent_server(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store, tmp_path=tmp_path)
        responses = _capture(handler)

        handler._handle_mcp_server_delete("ghost")

        data, status = responses[0]
        assert status == 404

    def test_full_crud_cycle(self, tmp_store, tmp_path):
        """End-to-end: list (empty) -> add -> list (1) -> patch -> delete -> list (empty)."""
        handler = _make_handler(tmp_store, tmp_path=tmp_path)

        # 1. List empty
        responses = _capture(handler)
        handler._handle_mcp_servers_list()
        assert responses[0][0]["servers"] == []

        # 2. Add
        responses.clear()
        _set_body(handler, {"name": "test-srv", "command": "node", "args": ["server.js"]})
        handler._handle_mcp_server_add()
        assert responses[0][1] == 201

        # 3. List (1 server)
        responses.clear()
        handler._handle_mcp_servers_list()
        assert len(responses[0][0]["servers"]) == 1

        # 4. Patch
        responses.clear()
        _set_body(handler, {"enabled": False})
        handler._handle_mcp_server_patch("test-srv")
        assert responses[0][0]["server"]["enabled"] is False

        # 5. Delete
        responses.clear()
        handler._handle_mcp_server_delete("test-srv")
        assert responses[0][0]["ok"] is True

        # 6. List (empty again)
        responses.clear()
        handler._handle_mcp_servers_list()
        assert responses[0][0]["servers"] == []


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Ticket Feed API
# ══════════════════════════════════════════════════════════════════════════════

class TestTicketFeedAPI:
    """Tests for GET /api/tickets/<id>/feed, POST /api/tickets/<id>/feed/comment."""

    def test_get_feed_for_existing_ticket(self, store_with_ticket, tmp_path):
        handler = _make_handler(store_with_ticket)
        responses = _capture(handler)

        with patch("scripts.ops.dashboard_server._FEEDS_DIR", tmp_path / "feeds"):
            handler._handle_get_ticket_feed("feed-ticket-001")

        data, status = responses[0]
        assert status == 200
        assert "feed" in data
        assert isinstance(data["feed"], list)

    def test_get_feed_for_missing_ticket(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_get_ticket_feed("nonexistent")

        data, status = responses[0]
        assert status == 404

    def test_add_feed_comment(self, store_with_ticket, tmp_path):
        handler = _make_handler(store_with_ticket)
        responses = _capture(handler)
        _set_body(handler, {"content": "Looking into this", "author": "alice"})

        with patch("scripts.ops.dashboard_server._FEEDS_DIR", tmp_path / "feeds"):
            handler._handle_add_feed_comment("feed-ticket-001")

        data, status = responses[0]
        assert status == 200
        assert data["ok"] is True
        assert "entry" in data
        assert data["entry"]["type"] == "comment"

        # Verify comment was stored in ticket metadata
        ticket = store_with_ticket.get("feed-ticket-001")
        comments = ticket.metadata.get("comments", [])
        assert len(comments) == 1
        assert comments[0]["text"] == "Looking into this"
        assert comments[0]["source"] == "alice"

    def test_add_feed_comment_empty_content(self, store_with_ticket, tmp_path):
        handler = _make_handler(store_with_ticket)
        responses = _capture(handler)
        _set_body(handler, {"content": "", "author": "bob"})

        with patch("scripts.ops.dashboard_server._FEEDS_DIR", tmp_path / "feeds"):
            handler._handle_add_feed_comment("feed-ticket-001")

        data, status = responses[0]
        assert status == 400
        assert "content" in data["error"].lower()

    def test_add_feed_comment_to_missing_ticket(self, tmp_store, tmp_path):
        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        _set_body(handler, {"content": "Hello"})

        handler._handle_add_feed_comment("nonexistent")

        data, status = responses[0]
        assert status == 404


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Budget Policies API
# ══════════════════════════════════════════════════════════════════════════════

class TestBudgetPoliciesAPI:
    """Tests for GET/POST /api/budget/policies."""

    @patch("scripts.ops.dashboard_server.get_budget_api")
    def test_get_policies(self, mock_get_api, tmp_store):
        from src.swe_team.budget_api import BudgetPolicy
        mock_api = MagicMock()
        mock_api.get_policies.return_value = [
            BudgetPolicy(id="default", team_id="alpha"),
        ]
        mock_get_api.return_value = mock_api

        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_budget_policies_get({})

        data, status = responses[0]
        assert status == 200
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["team_id"] == "alpha"
        assert data[0]["daily_limit_cents"] == 5000

    @patch("scripts.ops.dashboard_server.get_budget_api")
    def test_get_policies_error(self, mock_get_api, tmp_store):
        mock_get_api.side_effect = RuntimeError("DB connection failed")

        handler = _make_handler(tmp_store)
        responses = _capture(handler)

        handler._handle_budget_policies_get({})

        data, status = responses[0]
        assert status == 500
        assert "error" in data

    @patch("scripts.ops.dashboard_server.get_budget_api")
    def test_save_policy(self, mock_get_api, tmp_store):
        from src.swe_team.budget_api import BudgetPolicy
        mock_api = MagicMock()
        result_policy = BudgetPolicy(id="new-policy", team_id="beta", daily_limit_cents=10000)
        mock_api.set_policy.return_value = result_policy
        mock_get_api.return_value = mock_api

        handler = _make_handler(tmp_store)
        responses = _capture(handler)
        body = json.dumps({
            "id": "new-policy",
            "team_id": "beta",
            "daily_limit_cents": 10000,
        }).encode()

        handler._handle_budget_policies_post({}, body)

        data, status = responses[0]
        assert status == 200
        assert data["team_id"] == "beta"
        assert data["daily_limit_cents"] == 10000
        mock_api.set_policy.assert_called_once()
