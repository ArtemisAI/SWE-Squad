"""
Tests for recently added API endpoints:
  - GET/POST/DELETE /api/accounts/<id>/secrets — account secrets management
  - GET/POST/DELETE /api/projects/<name>/secrets — project secrets management
  - POST /api/teams/<name>/start|stop|restart — team lifecycle controls
  - GET /api/teams/<name>/health — team health check
  - UserStore secrets CRUD
"""

from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

import pytest

# ── Project bootstrap ───────────���─────────────────────────────────────────────
logging.logAsyncioTasks = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ══════════��═══════════════════════════════��═══════════════════════════════════
# Helpers
# ═══════��═════════════���════════════════════════════��═══════════════════════════

def _make_handler(store=None):
    """Create a DashboardHandler instance configured for unit tests."""
    from scripts.ops.dashboard_server import DashboardHandler

    handler = MagicMock(spec=DashboardHandler)
    handler.store = store or MagicMock()
    handler.auth_provider = None
    handler.headers = {"Content-Length": "0"}
    handler._read_post_body = DashboardHandler._read_post_body.__get__(handler)
    handler._json_response = DashboardHandler._json_response.__get__(handler)

    return handler


def _set_body(handler, body_dict):
    raw = json.dumps(body_dict).encode()
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)


def _capture_responses(handler):
    responses = []
    def capture(data, status=200):
        responses.append((data, status))
    handler._json_response = capture
    return responses


# ═════���════════════════════���═══════════════════════════════════════════════════
# Tests: Team Lifecycle Controls
# ═════════════════════════════════════════════════════════════════════════���════

class TestTeamRunState:
    """Tests for _get_team_run_state / _set_team_run_state helpers."""

    def test_default_state_is_running(self):
        from scripts.ops.dashboard_server import _get_team_run_state
        state = _get_team_run_state("unit-test-default-team")
        assert isinstance(state, dict)
        assert state["status"] == "running"
        assert "last_check" in state

    def test_set_and_get_state(self):
        from scripts.ops.dashboard_server import _get_team_run_state, _set_team_run_state
        _set_team_run_state("unit-test-setter", "stopped")
        state = _get_team_run_state("unit-test-setter")
        assert state["status"] == "stopped"
        # Cleanup
        _set_team_run_state("unit-test-setter", "running")

    def test_set_returns_updated_state(self):
        from scripts.ops.dashboard_server import _set_team_run_state
        result = _set_team_run_state("unit-test-return", "stopping")
        assert isinstance(result, dict)
        assert result["status"] == "stopping"
        # Cleanup
        _set_team_run_state("unit-test-return", "running")


def _mock_config_path(teams_dict):
    """Create a context manager that patches _CONFIG_PATH with a temp yaml file."""
    import tempfile
    import yaml
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.safe_dump({"teams": teams_dict}, f)
    f.close()
    return patch("scripts.ops.dashboard_server._CONFIG_PATH", Path(f.name))


class TestTeamActions:
    """Tests for _handle_team_action (start/stop/restart)."""

    def test_start_stopped_team(self):
        from scripts.ops.dashboard_server import (
            DashboardHandler, _set_team_run_state, _get_team_run_state,
        )
        _set_team_run_state("action-start", "stopped")

        handler = _make_handler()
        responses = _capture_responses(handler)

        with _mock_config_path({"action-start": {"vm": "10.0.0.1"}}):
            DashboardHandler._handle_team_action.__get__(handler)("action-start", "start")

        assert len(responses) == 1
        data, status = responses[0]
        assert status == 200
        assert _get_team_run_state("action-start")["status"] in ("running", "starting")
        _set_team_run_state("action-start", "running")

    def test_stop_running_team(self):
        from scripts.ops.dashboard_server import (
            DashboardHandler, _set_team_run_state, _get_team_run_state,
        )
        _set_team_run_state("action-stop", "running")

        handler = _make_handler()
        responses = _capture_responses(handler)

        with _mock_config_path({"action-stop": {"vm": "10.0.0.2"}}):
            DashboardHandler._handle_team_action.__get__(handler)("action-stop", "stop")

        assert len(responses) == 1
        data, status = responses[0]
        assert status == 200
        assert _get_team_run_state("action-stop")["status"] in ("stopped", "stopping")
        _set_team_run_state("action-stop", "running")

    def test_start_already_running_returns_409(self):
        from scripts.ops.dashboard_server import DashboardHandler, _set_team_run_state
        _set_team_run_state("action-dup", "running")

        handler = _make_handler()
        responses = _capture_responses(handler)

        with _mock_config_path({"action-dup": {"vm": "10.0.0.3"}}):
            DashboardHandler._handle_team_action.__get__(handler)("action-dup", "start")

        assert len(responses) == 1
        _, status = responses[0]
        assert status == 409

    def test_restart_team(self):
        from scripts.ops.dashboard_server import (
            DashboardHandler, _set_team_run_state, _get_team_run_state,
        )
        _set_team_run_state("action-restart", "running")

        handler = _make_handler()
        responses = _capture_responses(handler)

        with _mock_config_path({"action-restart": {"vm": "10.0.0.4"}}):
            DashboardHandler._handle_team_action.__get__(handler)("action-restart", "restart")

        assert len(responses) == 1
        data, status = responses[0]
        assert status == 200
        _set_team_run_state("action-restart", "running")

    def test_action_unknown_team_returns_404(self):
        from scripts.ops.dashboard_server import DashboardHandler
        handler = _make_handler()
        responses = _capture_responses(handler)

        with _mock_config_path({}):
            DashboardHandler._handle_team_action.__get__(handler)("ghost-team", "start")

        assert len(responses) == 1
        _, status = responses[0]
        assert status == 404


class TestTeamHealth:
    """Tests for _handle_team_health."""

    def test_health_returns_structured_data(self):
        from scripts.ops.dashboard_server import DashboardHandler, _set_team_run_state
        _set_team_run_state("health-test", "running")

        handler = _make_handler()
        responses = _capture_responses(handler)

        with _mock_config_path({"health-test": {"vm": "10.0.0.5"}}):
            DashboardHandler._handle_team_health.__get__(handler)("health-test")

        assert len(responses) == 1
        data, status = responses[0]
        assert status == 200
        assert data["name"] == "health-test"
        assert "status" in data
        assert "run_state" in data

    def test_health_unknown_team_returns_404(self):
        from scripts.ops.dashboard_server import DashboardHandler
        handler = _make_handler()
        responses = _capture_responses(handler)

        with _mock_config_path({}):
            DashboardHandler._handle_team_health.__get__(handler)("nonexistent-team")

        assert len(responses) == 1
        _, status = responses[0]
        assert status == 404


# ═══���═══════════════════════��══════════════════════════════════���═══════════════
# Tests: Account & Project Secrets via Handler
# ══════════════════���════════════════════════════════��══════════════════════════

class TestSecretHandlers:
    """Tests for secrets endpoints using _get_user_store() mock."""

    def test_list_account_secrets_no_user_store(self):
        """When user_store unavailable, returns 503."""
        from scripts.ops.dashboard_server import DashboardHandler
        handler = _make_handler()
        responses = _capture_responses(handler)

        with patch("scripts.ops.dashboard_server._get_user_store", return_value=None):
            DashboardHandler._handle_list_account_secrets.__get__(handler)("acc-1")

        assert len(responses) == 1
        _, status = responses[0]
        assert status == 503

    def test_list_account_secrets_success(self):
        from scripts.ops.dashboard_server import DashboardHandler
        mock_us = MagicMock()
        mock_us.list_account_secret_names.return_value = [
            {"name": "GH_TOKEN", "expires_at": None},
            {"name": "API_KEY", "expires_at": None},
        ]
        handler = _make_handler()
        responses = _capture_responses(handler)

        with patch("scripts.ops.dashboard_server._get_user_store", return_value=mock_us):
            DashboardHandler._handle_list_account_secrets.__get__(handler)("acc-1")

        assert len(responses) == 1
        data, status = responses[0]
        assert status == 200
        assert [e["name"] for e in data["secrets"]] == ["GH_TOKEN", "API_KEY"]

    def test_create_account_secret_success(self):
        from scripts.ops.dashboard_server import DashboardHandler
        mock_us = MagicMock()
        handler = _make_handler()
        _set_body(handler, {"name": "MY_SECRET", "value": "s3cret"})
        responses = _capture_responses(handler)

        with patch("scripts.ops.dashboard_server._get_user_store", return_value=mock_us):
            DashboardHandler._handle_create_account_secret.__get__(handler)("acc-1")

        assert len(responses) == 1
        data, status = responses[0]
        assert status == 201
        mock_us.set_account_secret.assert_called_once()
        call_args = mock_us.set_account_secret.call_args
        assert call_args[0][:3] == ("acc-1", "MY_SECRET", "s3cret")

    def test_create_account_secret_missing_value(self):
        from scripts.ops.dashboard_server import DashboardHandler
        mock_us = MagicMock()
        handler = _make_handler()
        _set_body(handler, {"name": "ONLY_NAME"})
        responses = _capture_responses(handler)

        with patch("scripts.ops.dashboard_server._get_user_store", return_value=mock_us):
            DashboardHandler._handle_create_account_secret.__get__(handler)("acc-1")

        assert len(responses) == 1
        _, status = responses[0]
        assert status == 400

    def test_create_account_secret_missing_name(self):
        from scripts.ops.dashboard_server import DashboardHandler
        mock_us = MagicMock()
        handler = _make_handler()
        _set_body(handler, {"value": "val"})
        responses = _capture_responses(handler)

        with patch("scripts.ops.dashboard_server._get_user_store", return_value=mock_us):
            DashboardHandler._handle_create_account_secret.__get__(handler)("acc-1")

        assert len(responses) == 1
        _, status = responses[0]
        assert status == 400

    def test_delete_account_secret_success(self):
        from scripts.ops.dashboard_server import DashboardHandler
        mock_us = MagicMock()
        mock_us.delete_account_secret.return_value = True
        handler = _make_handler()
        responses = _capture_responses(handler)

        with patch("scripts.ops.dashboard_server._get_user_store", return_value=mock_us):
            DashboardHandler._handle_delete_account_secret.__get__(handler)("acc-1", "MY_SECRET")

        assert len(responses) == 1
        data, status = responses[0]
        assert status == 200
        assert data["deleted"] == "MY_SECRET"

    def test_delete_account_secret_not_found(self):
        from scripts.ops.dashboard_server import DashboardHandler
        mock_us = MagicMock()
        mock_us.delete_account_secret.return_value = False
        handler = _make_handler()
        responses = _capture_responses(handler)

        with patch("scripts.ops.dashboard_server._get_user_store", return_value=mock_us):
            DashboardHandler._handle_delete_account_secret.__get__(handler)("acc-1", "MISSING")

        assert len(responses) == 1
        _, status = responses[0]
        assert status == 404

    def test_list_project_secrets_success(self):
        from scripts.ops.dashboard_server import DashboardHandler
        mock_us = MagicMock()
        mock_us.list_project_secret_names.return_value = [
            {"name": "DB_PASS", "expires_at": None},
        ]
        handler = _make_handler()
        responses = _capture_responses(handler)

        with patch("scripts.ops.dashboard_server._get_user_store", return_value=mock_us):
            DashboardHandler._handle_list_project_secrets.__get__(handler)("proj-1")

        assert len(responses) == 1
        data, status = responses[0]
        assert status == 200
        assert [e["name"] for e in data["secrets"]] == ["DB_PASS"]

    def test_create_project_secret_success(self):
        from scripts.ops.dashboard_server import DashboardHandler
        mock_us = MagicMock()
        handler = _make_handler()
        _set_body(handler, {"name": "DB_PASSWORD", "value": "p@ss"})
        responses = _capture_responses(handler)

        with patch("scripts.ops.dashboard_server._get_user_store", return_value=mock_us):
            DashboardHandler._handle_create_project_secret.__get__(handler)("proj-1")

        assert len(responses) == 1
        _, status = responses[0]
        assert status == 201
        mock_us.set_project_secret.assert_called_once()
        call_args = mock_us.set_project_secret.call_args
        assert call_args[0][:3] == ("proj-1", "DB_PASSWORD", "p@ss")

    def test_delete_project_secret_success(self):
        from scripts.ops.dashboard_server import DashboardHandler
        mock_us = MagicMock()
        mock_us.delete_project_secret.return_value = True
        handler = _make_handler()
        responses = _capture_responses(handler)

        with patch("scripts.ops.dashboard_server._get_user_store", return_value=mock_us):
            DashboardHandler._handle_delete_project_secret.__get__(handler)("proj-1", "DB_PASSWORD")

        assert len(responses) == 1
        data, status = responses[0]
        assert status == 200


# ══════���════════════════════════���═══════════════════════��══════════════════════
# Tests: UserStore Secrets Schema (integration)
# ══════════════���═══════════════════════════════════════════════════════════════

class TestUserStoreSecrets:
    """Integration tests for UserStore account_secrets and project_secrets."""

    @pytest.fixture
    def user_store(self, tmp_path):
        from src.swe_team.webui.user_store import UserStore
        db_path = tmp_path / "test_users.db"
        # Generate a deterministic 32-byte key for tests
        key = b"test-encryption-key-32-bytes!!"[:32].ljust(32, b"\x00")
        store = UserStore(db_path=str(db_path), encryption_key=key)
        return store

    def test_account_secrets_crud(self, user_store):
        # Initially empty
        entries = user_store.list_account_secret_names("acc-1")
        assert entries == []

        # Create
        user_store.set_account_secret("acc-1", "GH_TOKEN", "ghp_abc123")
        entries = user_store.list_account_secret_names("acc-1")
        names = [e["name"] for e in entries]
        assert "GH_TOKEN" in names

        # Update (upsert)
        user_store.set_account_secret("acc-1", "GH_TOKEN", "ghp_newvalue")
        entries = user_store.list_account_secret_names("acc-1")
        names = [e["name"] for e in entries]
        assert names.count("GH_TOKEN") == 1

        # Delete
        result = user_store.delete_account_secret("acc-1", "GH_TOKEN")
        assert result is True
        entries = user_store.list_account_secret_names("acc-1")
        names = [e["name"] for e in entries]
        assert "GH_TOKEN" not in names

    def test_project_secrets_crud(self, user_store):
        entries = user_store.list_project_secret_names("swe-squad")
        assert entries == []

        user_store.set_project_secret("swe-squad", "DB_PASSWORD", "p@ssw0rd")
        entries = user_store.list_project_secret_names("swe-squad")
        names = [e["name"] for e in entries]
        assert "DB_PASSWORD" in names

        result = user_store.delete_project_secret("swe-squad", "DB_PASSWORD")
        assert result is True
        entries = user_store.list_project_secret_names("swe-squad")
        names = [e["name"] for e in entries]
        assert "DB_PASSWORD" not in names

    def test_secrets_isolation_between_accounts(self, user_store):
        user_store.set_account_secret("acc-1", "SECRET_A", "value_a")
        user_store.set_account_secret("acc-2", "SECRET_B", "value_b")

        names_1 = [e["name"] for e in user_store.list_account_secret_names("acc-1")]
        names_2 = [e["name"] for e in user_store.list_account_secret_names("acc-2")]
        assert "SECRET_A" in names_1
        assert "SECRET_B" not in names_1
        assert "SECRET_B" in names_2
        assert "SECRET_A" not in names_2

    def test_secrets_isolation_between_projects(self, user_store):
        user_store.set_project_secret("proj-1", "KEY_A", "val_a")
        user_store.set_project_secret("proj-2", "KEY_B", "val_b")

        names_1 = [e["name"] for e in user_store.list_project_secret_names("proj-1")]
        assert "KEY_A" in names_1
        assert "KEY_B" not in names_1

    def test_secret_values_are_encrypted(self, user_store):
        user_store.set_account_secret("acc-enc", "PLAINTEXT_CHECK", "my-secret-value")

        conn = sqlite3.connect(str(user_store._db_path))
        row = conn.execute(
            "SELECT encrypted_value FROM account_secrets WHERE account_id=? AND name=?",
            ("acc-enc", "PLAINTEXT_CHECK")
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] != "my-secret-value"
        assert len(row[0]) > 0

    def test_multiple_secrets_per_account(self, user_store):
        user_store.set_account_secret("acc-multi", "KEY_1", "val1")
        user_store.set_account_secret("acc-multi", "KEY_2", "val2")
        user_store.set_account_secret("acc-multi", "KEY_3", "val3")

        entries = user_store.list_account_secret_names("acc-multi")
        assert len(entries) == 3
        assert set(e["name"] for e in entries) == {"KEY_1", "KEY_2", "KEY_3"}

    def test_delete_nonexistent_returns_false(self, user_store):
        result = user_store.delete_account_secret("acc-none", "MISSING")
        assert result is False

    def test_delete_nonexistent_project_secret(self, user_store):
        result = user_store.delete_project_secret("proj-none", "MISSING")
        assert result is False
