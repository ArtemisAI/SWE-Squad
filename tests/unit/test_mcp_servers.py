"""
Tests for MCP Server CRUD endpoints, integration configure/test,
and ticket feed handlers.

Covers:
  - GET /api/mcp/servers — list (empty + populated)
  - POST /api/mcp/servers — add, duplicate name 409, missing fields 400
  - DELETE /api/mcp/servers/<name> — delete, nonexistent 404
  - PATCH /api/mcp/servers/<name> — toggle enabled, update fields, not found 404
  - POST /api/integrations/configure — save creds, missing connector_type 400
  - POST /api/integrations/test — missing type, unknown connector, import error
  - GET /api/tickets/<id>/feed — get feed, not found 404
  - POST /api/tickets/<id>/feed/comment — add comment, empty content 400, not found 404
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
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
    store = TicketStore(path=str(path))
    return store


@pytest.fixture
def sample_ticket():
    """Provide a sample SWETicket."""
    return SWETicket(
        ticket_id="feed-test-001",
        title="Feed test ticket",
        description="A ticket for feed testing.",
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
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_handler(store, tmp_path=None):
    """Create a DashboardHandler mock with real methods wired up."""
    from scripts.ops.dashboard_server import DashboardHandler

    handler = MagicMock(spec=DashboardHandler)
    handler.store = store
    handler.auth_provider = None
    handler.headers = {"Content-Length": "0"}

    # MCP server methods
    handler._read_post_body = DashboardHandler._read_post_body.__get__(handler)
    handler._json_response = DashboardHandler._json_response.__get__(handler)
    handler._handle_mcp_servers_list = DashboardHandler._handle_mcp_servers_list.__get__(handler)
    handler._handle_mcp_server_add = DashboardHandler._handle_mcp_server_add.__get__(handler)
    handler._handle_mcp_server_delete = DashboardHandler._handle_mcp_server_delete.__get__(handler)
    handler._handle_mcp_server_patch = DashboardHandler._handle_mcp_server_patch.__get__(handler)
    handler._read_mcp_servers = DashboardHandler._read_mcp_servers.__get__(handler)
    handler._write_mcp_servers = DashboardHandler._write_mcp_servers.__get__(handler)

    # Integration methods
    handler._handle_integration_configure = DashboardHandler._handle_integration_configure.__get__(handler)
    handler._handle_integration_test = DashboardHandler._handle_integration_test.__get__(handler)

    # Feed methods
    handler._handle_get_ticket_feed = DashboardHandler._handle_get_ticket_feed.__get__(handler)
    handler._handle_add_feed_comment = DashboardHandler._handle_add_feed_comment.__get__(handler)
    handler._read_feed = DashboardHandler._read_feed.__get__(handler)
    handler._write_feed = DashboardHandler._write_feed.__get__(handler)
    handler._get_feed_path = DashboardHandler._get_feed_path.__get__(handler)
    handler._generate_feed_from_ticket = DashboardHandler._generate_feed_from_ticket.__get__(handler)

    # Override MCP path if tmp_path provided
    if tmp_path is not None:
        handler._MCP_SERVERS_PATH = tmp_path / "mcp_servers.json"

    return handler


def _set_body(handler, body_dict):
    """Set the POST body for a mock handler."""
    raw = json.dumps(body_dict).encode()
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)


def _capture_responses(handler):
    """Return a list that captures (data, status) tuples from _json_response."""
    responses = []

    def capture(data, status=200):
        responses.append((data, status))

    handler._json_response = capture
    return responses


# ══════════════════════════════════════════════════════════════════════════════
# Tests: MCP Servers CRUD
# ══════════════════════════════════════════════════════════════════════════════

class TestMCPServersList:
    def test_list_empty(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)
        handler._handle_mcp_servers_list()
        assert len(responses) == 1
        data, status = responses[0]
        assert status == 200
        assert data["servers"] == []

    def test_list_after_add(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)

        # Add a server first
        _set_body(handler, {"name": "my-server", "command": "npx mcp-server"})
        handler._handle_mcp_server_add()

        # Now list
        handler._handle_mcp_servers_list()
        assert len(responses) == 2
        data, status = responses[1]
        assert status == 200
        assert len(data["servers"]) == 1
        assert data["servers"][0]["name"] == "my-server"


class TestMCPServerAdd:
    def test_add_success(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)
        _set_body(handler, {
            "name": "test-server",
            "command": "node server.js",
            "args": ["--port", "3000"],
            "env": {"API_KEY": "secret"},
        })
        handler._handle_mcp_server_add()
        assert len(responses) == 1
        data, status = responses[0]
        assert status == 201
        assert data["ok"] is True
        assert data["server"]["name"] == "test-server"
        assert data["server"]["command"] == "node server.js"
        assert data["server"]["args"] == ["--port", "3000"]
        assert data["server"]["env"] == {"API_KEY": "secret"}
        assert data["server"]["enabled"] is True

    def test_add_missing_name(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)
        _set_body(handler, {"command": "node server.js"})
        handler._handle_mcp_server_add()
        data, status = responses[0]
        assert status == 400
        assert "name" in data["error"].lower()

    def test_add_missing_command(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)
        _set_body(handler, {"name": "my-server"})
        handler._handle_mcp_server_add()
        data, status = responses[0]
        assert status == 400
        assert "command" in data["error"].lower()

    def test_add_duplicate_name_409(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)

        _set_body(handler, {"name": "dup-server", "command": "cmd1"})
        handler._handle_mcp_server_add()
        assert responses[0][1] == 201

        _set_body(handler, {"name": "dup-server", "command": "cmd2"})
        handler._handle_mcp_server_add()
        data, status = responses[1]
        assert status == 409
        assert "already exists" in data["error"]

    def test_add_defaults(self, tmp_path):
        """Args default to [] and env to {} when not provided."""
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)
        _set_body(handler, {"name": "minimal", "command": "run"})
        handler._handle_mcp_server_add()
        data, status = responses[0]
        assert status == 201
        assert data["server"]["args"] == []
        assert data["server"]["env"] == {}


class TestMCPServerDelete:
    def test_delete_existing(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)

        _set_body(handler, {"name": "doomed", "command": "cmd"})
        handler._handle_mcp_server_add()
        assert responses[0][1] == 201

        handler._handle_mcp_server_delete("doomed")
        data, status = responses[1]
        assert status == 200
        assert data["ok"] is True
        assert data["deleted"] == "doomed"

        # Verify it's gone
        handler._handle_mcp_servers_list()
        assert responses[2][0]["servers"] == []

    def test_delete_nonexistent_404(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)
        handler._handle_mcp_server_delete("ghost")
        data, status = responses[0]
        assert status == 404
        assert "not found" in data["error"].lower()


class TestMCPServerPatch:
    def test_toggle_enabled(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)

        _set_body(handler, {"name": "srv", "command": "cmd"})
        handler._handle_mcp_server_add()
        assert responses[0][0]["server"]["enabled"] is True

        _set_body(handler, {"enabled": False})
        handler._handle_mcp_server_patch("srv")
        data, status = responses[1]
        assert status == 200
        assert data["server"]["enabled"] is False

    def test_update_command(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)

        _set_body(handler, {"name": "srv", "command": "old-cmd"})
        handler._handle_mcp_server_add()

        _set_body(handler, {"command": "new-cmd"})
        handler._handle_mcp_server_patch("srv")
        data, status = responses[1]
        assert status == 200
        assert data["server"]["command"] == "new-cmd"

    def test_update_args_and_env(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)

        _set_body(handler, {"name": "srv", "command": "cmd"})
        handler._handle_mcp_server_add()

        _set_body(handler, {"args": ["--verbose"], "env": {"KEY": "val"}})
        handler._handle_mcp_server_patch("srv")
        data, status = responses[1]
        assert status == 200
        assert data["server"]["args"] == ["--verbose"]
        assert data["server"]["env"] == {"KEY": "val"}

    def test_patch_nonexistent_404(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)
        _set_body(handler, {"enabled": True})
        handler._handle_mcp_server_patch("nope")
        data, status = responses[0]
        assert status == 404
        assert "not found" in data["error"].lower()

    def test_enable_disable_roundtrip(self, tmp_path):
        handler = _make_handler(None, tmp_path)
        responses = _capture_responses(handler)

        _set_body(handler, {"name": "srv", "command": "cmd"})
        handler._handle_mcp_server_add()

        _set_body(handler, {"enabled": False})
        handler._handle_mcp_server_patch("srv")
        assert responses[1][0]["server"]["enabled"] is False

        _set_body(handler, {"enabled": True})
        handler._handle_mcp_server_patch("srv")
        assert responses[2][0]["server"]["enabled"] is True


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Integration Configure / Test
# ══════════════════════════════════════════════════════════════════════════════

class TestIntegrationConfigure:
    @patch("scripts.ops.dashboard_server._get_user_store")
    def test_save_credentials(self, mock_get_us):
        mock_us = MagicMock()
        mock_get_us.return_value = mock_us

        handler = _make_handler(None)
        responses = _capture_responses(handler)
        _set_body(handler, {
            "connector_type": "github",
            "credentials": {"token": "ghp_abc123"},
        })
        handler._handle_integration_configure()
        data, status = responses[0]
        assert status == 201
        assert data["ok"] is True
        assert data["connector_type"] == "github"
        mock_us.set_project_secret.assert_called_once_with(
            "_integrations", "integration:github:token", "ghp_abc123"
        )

    def test_missing_connector_type(self):
        handler = _make_handler(None)
        responses = _capture_responses(handler)
        _set_body(handler, {"credentials": {"token": "abc"}})
        handler._handle_integration_configure()
        data, status = responses[0]
        assert status == 400
        assert "connector_type" in data["error"]

    def test_invalid_credentials_type(self):
        handler = _make_handler(None)
        responses = _capture_responses(handler)
        _set_body(handler, {"connector_type": "github", "credentials": "not-a-dict"})
        handler._handle_integration_configure()
        data, status = responses[0]
        assert status == 400
        assert "credentials" in data["error"]

    @patch("scripts.ops.dashboard_server._get_user_store", return_value=None)
    def test_user_store_unavailable(self, _):
        handler = _make_handler(None)
        responses = _capture_responses(handler)
        _set_body(handler, {
            "connector_type": "github",
            "credentials": {"token": "abc"},
        })
        handler._handle_integration_configure()
        data, status = responses[0]
        assert status == 503
        assert "UserStore" in data["error"]

    @patch("scripts.ops.dashboard_server._get_user_store")
    def test_empty_credentials_skipped(self, mock_get_us):
        """Empty string values are not stored."""
        mock_us = MagicMock()
        mock_get_us.return_value = mock_us

        handler = _make_handler(None)
        responses = _capture_responses(handler)
        _set_body(handler, {
            "connector_type": "slack",
            "credentials": {"token": "", "webhook": "https://hooks.example"},
        })
        handler._handle_integration_configure()
        data, status = responses[0]
        assert status == 201
        # Only the non-empty credential should be stored
        assert mock_us.set_project_secret.call_count == 1
        call_args = mock_us.set_project_secret.call_args[0]
        assert call_args[1] == "integration:slack:webhook"


class TestIntegrationTest:
    def test_missing_connector_type(self):
        handler = _make_handler(None)
        responses = _capture_responses(handler)
        _set_body(handler, {"credentials": {}})
        handler._handle_integration_test()
        data, status = responses[0]
        assert status == 400
        assert "connector_type" in data["error"]

    def test_import_error_graceful(self):
        """When integrations module is not available, handle gracefully."""
        handler = _make_handler(None)
        responses = _capture_responses(handler)
        _set_body(handler, {"connector_type": "jira", "credentials": {}})

        from scripts.ops.dashboard_server import DashboardHandler
        real_method = DashboardHandler._handle_integration_test.__get__(handler)

        # Force ImportError by making the import fail
        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "src.swe_team.integrations":
                raise ImportError("mocked")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=mock_import):
            real_method()

        data, status = responses[0]
        assert status == 200
        assert data["ok"] is False
        assert "not available" in data["message"]

    def test_unknown_connector(self):
        """When connector type does not match any registered connector."""
        handler = _make_handler(None)
        responses = _capture_responses(handler)
        _set_body(handler, {"connector_type": "nonexistent_connector", "credentials": {}})

        from scripts.ops.dashboard_server import DashboardHandler
        real_method = DashboardHandler._handle_integration_test.__get__(handler)

        mock_module = MagicMock()
        mock_module.list_connectors.return_value = []  # no connectors match
        with patch.dict("sys.modules", {"src.swe_team.integrations": mock_module}):
            with patch("scripts.ops.dashboard_server.DashboardHandler._handle_integration_test", real_method):
                real_method()

        data, status = responses[0]
        assert status == 200
        assert data["ok"] is False
        assert "Unknown connector" in data["message"]

    def test_missing_required_fields(self):
        """When required credential fields are missing."""
        handler = _make_handler(None)
        responses = _capture_responses(handler)
        _set_body(handler, {"connector_type": "github", "credentials": {}})

        from scripts.ops.dashboard_server import DashboardHandler
        real_method = DashboardHandler._handle_integration_test.__get__(handler)

        # Create a mock connector with required fields
        mock_field = MagicMock()
        mock_field.label = "API Token"
        mock_field.required = True
        mock_field.key = "token"

        mock_connector = MagicMock()
        mock_connector.manifest.connector_type = "github"
        mock_connector.manifest.credential_schema = [mock_field]

        mock_module = MagicMock()
        mock_module.list_connectors.return_value = [mock_connector]

        with patch.dict("sys.modules", {"src.swe_team.integrations": mock_module}):
            with patch("scripts.ops.dashboard_server.DashboardHandler._handle_integration_test", real_method):
                real_method()

        data, status = responses[0]
        assert status == 200
        assert data["ok"] is False
        assert "Missing" in data["message"]
        assert "API Token" in data["message"]


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Ticket Feed
# ══════════════════════════════════════════════════════════════════════════════

class TestGetTicketFeed:
    def test_get_feed_not_found(self, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture_responses(handler)
        handler._handle_get_ticket_feed("nonexistent-id")
        data, status = responses[0]
        assert status == 404
        assert "not found" in data["error"].lower()

    @patch("scripts.ops.dashboard_server._FEEDS_DIR")
    def test_get_feed_empty_ticket(self, mock_feeds_dir, store_with_ticket, tmp_path):
        mock_feeds_dir.__truediv__ = lambda self, x: tmp_path / x
        mock_feeds_dir.mkdir = MagicMock()
        mock_feeds_dir.exists = MagicMock(return_value=True)

        handler = _make_handler(store_with_ticket)
        responses = _capture_responses(handler)
        handler._handle_get_ticket_feed("feed-test-001")
        data, status = responses[0]
        assert status == 200
        assert "feed" in data
        assert isinstance(data["feed"], list)


class TestAddFeedComment:
    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    @patch("scripts.ops.dashboard_server._FEEDS_DIR")
    def test_add_comment_success(self, mock_feeds_dir, mock_sse, store_with_ticket, tmp_path):
        mock_feeds_dir.__truediv__ = lambda self, x: tmp_path / x
        mock_feeds_dir.mkdir = MagicMock()
        mock_feeds_dir.exists = MagicMock(return_value=True)

        handler = _make_handler(store_with_ticket)
        responses = _capture_responses(handler)
        _set_body(handler, {"content": "This is a test comment", "author": "tester"})
        handler._handle_add_feed_comment("feed-test-001")
        data, status = responses[0]
        assert status == 200
        assert data["ok"] is True
        assert data["entry"]["type"] == "comment"
        assert data["entry"]["content"] == "This is a test comment"
        assert data["entry"]["actor"] == "tester"
        mock_sse.assert_called_once()

    def test_add_comment_empty_content(self, store_with_ticket):
        handler = _make_handler(store_with_ticket)
        responses = _capture_responses(handler)
        _set_body(handler, {"content": "", "author": "tester"})
        handler._handle_add_feed_comment("feed-test-001")
        data, status = responses[0]
        assert status == 400
        assert "content" in data["error"].lower()

    def test_add_comment_ticket_not_found(self, tmp_store):
        handler = _make_handler(tmp_store)
        responses = _capture_responses(handler)
        _set_body(handler, {"content": "hello", "author": "tester"})
        handler._handle_add_feed_comment("nonexistent-id")
        data, status = responses[0]
        assert status == 404
        assert "not found" in data["error"].lower()

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    @patch("scripts.ops.dashboard_server._FEEDS_DIR")
    def test_add_comment_default_author(self, mock_feeds_dir, mock_sse, store_with_ticket, tmp_path):
        """When no author is provided, defaults to 'user'."""
        mock_feeds_dir.__truediv__ = lambda self, x: tmp_path / x
        mock_feeds_dir.mkdir = MagicMock()
        mock_feeds_dir.exists = MagicMock(return_value=True)

        handler = _make_handler(store_with_ticket)
        responses = _capture_responses(handler)
        _set_body(handler, {"content": "no author specified"})
        handler._handle_add_feed_comment("feed-test-001")
        data, status = responses[0]
        assert status == 200
        assert data["entry"]["actor"] == "user"
