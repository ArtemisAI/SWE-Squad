"""Tests for Instance Settings feature.

Covers:
- Dashboard API endpoints (GET/POST /api/instance/settings)
- Instance heartbeat endpoint (GET /api/instance/heartbeat)
- Helper functions (_read_instance_settings, _write_instance_settings, _get_instance_heartbeat)
"""

from __future__ import annotations

import json
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from unittest import mock
import time

import pytest

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_instance_settings(tmp_path):
    """Create a temporary instance_settings.json with sample data."""
    settings = {
        "name": "Test Instance",
        "description": "Test description",
        "isolated_workspaces": False,
        "auto_restart": False,
        "heartbeat_interval_seconds": 60,
        "experimental_features": {
            "parallel_execution": False,
            "adaptive_throttling": True,
            "semantic_memory": True,
            "regression_detection": True,
        },
    }
    settings_path = tmp_path / "instance_settings.json"
    settings_path.write_text(json.dumps(settings, indent=2))
    return settings_path


@pytest.fixture
def tmp_status_json(tmp_path):
    """Create a temporary status.json for heartbeat testing."""
    status = {
        "last_cycle_time": "2024-01-01T12:00:00Z",
        "time": "2024-01-01T12:00:00Z",
        "ticket_summary": {"total": 10, "by_severity": {"critical": 1, "high": 2, "medium": 3, "low": 4}},
    }
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(status, indent=2))
    return status_path


@pytest.fixture
def tmp_jobs_json(tmp_path):
    """Create a temporary jobs.json for heartbeat testing."""
    jobs = [
        {"job_id": "monitor", "name": "Monitor", "enabled": True},
        {"job_id": "investigator", "name": "Investigator", "enabled": False},
    ]
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(json.dumps(jobs, indent=2))
    return jobs_path


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestInstanceSettingsHelpers:
    """Test instance settings helper functions from dashboard_server."""

    def test_read_instance_settings_default(self, tmp_path):
        """Test reading instance settings when file doesn't exist returns defaults."""
        from scripts.ops.dashboard_server import (
            _read_instance_settings,
            _DEFAULT_INSTANCE_SETTINGS,
        )
        settings_path = tmp_path / "nonexistent_instance_settings.json"

        with mock.patch("scripts.ops.dashboard_server._INSTANCE_SETTINGS_PATH", settings_path):
            settings = _read_instance_settings()

        assert settings == _DEFAULT_INSTANCE_SETTINGS

    def test_read_instance_settings_existing(self, tmp_instance_settings):
        """Test reading existing instance settings file."""
        from scripts.ops.dashboard_server import _read_instance_settings

        with mock.patch("scripts.ops.dashboard_server._INSTANCE_SETTINGS_PATH", tmp_instance_settings):
            settings = _read_instance_settings()

        assert settings["name"] == "Test Instance"
        assert settings["description"] == "Test description"
        assert settings["isolated_workspaces"] is False
        assert settings["auto_restart"] is False
        assert settings["heartbeat_interval_seconds"] == 60
        assert settings["experimental_features"]["parallel_execution"] is False

    def test_write_instance_settings(self, tmp_path):
        """Test writing instance settings to file."""
        from scripts.ops.dashboard_server import (
            _write_instance_settings,
            _read_instance_settings,
        )
        settings_path = tmp_path / "instance_settings.json"

        new_settings = {
            "name": "Updated Instance",
            "description": "Updated description",
            "isolated_workspaces": True,
        }

        with mock.patch("scripts.ops.dashboard_server._INSTANCE_SETTINGS_PATH", settings_path):
            result = _write_instance_settings(new_settings)

        assert result is True
        assert settings_path.exists()

        # Verify written content
        with mock.patch("scripts.ops.dashboard_server._INSTANCE_SETTINGS_PATH", settings_path):
            read_back = _read_instance_settings()

        assert read_back["name"] == "Updated Instance"
        assert read_back["description"] == "Updated description"
        assert read_back["isolated_workspaces"] is True
        # Other defaults should be preserved
        assert read_back["auto_restart"] is False
        assert read_back["heartbeat_interval_seconds"] == 60
        assert read_back["connection_methods"] == []

    def test_write_instance_settings_merge_experimental_features(self, tmp_path):
        """Test that experimental_features dict is merged, not replaced."""
        from scripts.ops.dashboard_server import (
            _write_instance_settings,
            _read_instance_settings,
        )
        settings_path = tmp_path / "instance_settings.json"

        # Write initial settings
        initial = {
            "name": "Test",
            "experimental_features": {
                "parallel_execution": False,
                "adaptive_throttling": True,
                "semantic_memory": True,
                "regression_detection": True,
            },
        }

        with mock.patch("scripts.ops.dashboard_server._INSTANCE_SETTINGS_PATH", settings_path):
            _write_instance_settings(initial)

        # Update only one experimental feature
        update = {
            "experimental_features": {
                "parallel_execution": True,
            },
        }

        with mock.patch("scripts.ops.dashboard_server._INSTANCE_SETTINGS_PATH", settings_path):
            _write_instance_settings(update)
            read_back = _read_instance_settings()

        # Check that all experimental features are present
        assert read_back["experimental_features"]["parallel_execution"] is True
        assert read_back["experimental_features"]["adaptive_throttling"] is True
        assert read_back["experimental_features"]["semantic_memory"] is True
        assert read_back["experimental_features"]["regression_detection"] is True

    def test_write_instance_settings_connection_methods(self, tmp_path):
        """Test connection methods are persisted in instance settings."""
        from scripts.ops.dashboard_server import _write_instance_settings, _read_instance_settings

        settings_path = tmp_path / "instance_settings.json"
        methods = [
            {
                "name": "worker-1",
                "host": "test-worker-1",
                "username": "ubuntu",
                "port": 22,
                "secret_name": "SSH_KEY_WORKER_1",
            }
        ]

        with mock.patch("scripts.ops.dashboard_server._INSTANCE_SETTINGS_PATH", settings_path):
            assert _write_instance_settings({"connection_methods": methods}) is True
            read_back = _read_instance_settings()

        assert read_back["connection_methods"] == methods

    def test_get_instance_heartbeat(self, tmp_status_json, tmp_jobs_json):
        """Test instance heartbeat status generation."""
        from scripts.ops.dashboard_server import _get_instance_heartbeat

        with mock.patch("scripts.ops.dashboard_server._STATUS_PATH", tmp_status_json), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH", tmp_jobs_json), \
             mock.patch("scripts.ops.dashboard_server._read_instance_settings") as mock_settings:
            mock_settings.return_value = {"name": "Test Instance"}
            heartbeat = _get_instance_heartbeat()

        assert heartbeat["instance_name"] == "Test Instance"
        assert heartbeat["status"] == "healthy"
        assert heartbeat["last_cycle_time"] == "2024-01-01T12:00:00Z"
        assert heartbeat["agents_active"] == 1  # One enabled job
        assert heartbeat["total_agents"] == 2
        assert "timestamp" in heartbeat

    def test_get_instance_heartbeat_no_status(self, tmp_jobs_json):
        """Test heartbeat when status.json doesn't exist."""
        from scripts.ops.dashboard_server import _get_instance_heartbeat

        with mock.patch("scripts.ops.dashboard_server._STATUS_PATH", tmp_jobs_json.parent / "nonexistent"), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH", tmp_jobs_json), \
             mock.patch("scripts.ops.dashboard_server._read_instance_settings") as mock_settings:
            mock_settings.return_value = {"name": "Test Instance"}
            heartbeat = _get_instance_heartbeat()

        assert heartbeat["instance_name"] == "Test Instance"
        assert heartbeat["status"] == "unknown"
        assert heartbeat["last_cycle_time"] is None

    def test_calculate_uptime(self, tmp_path):
        """Test uptime calculation based on file mtime."""
        from scripts.ops.dashboard_server import _calculate_uptime

        test_file = tmp_path / "test_file.txt"
        test_file.write_text("test")
        time.sleep(0.1)  # Small delay to ensure measurable uptime

        with mock.patch("scripts.ops.dashboard_server._STATUS_PATH", test_file):
            uptime = _calculate_uptime()

        assert uptime > 0
        assert uptime < 1  # Should be less than 1 second for our small delay

    def test_calculate_uptime_no_file(self, tmp_path):
        """Test uptime calculation when file doesn't exist."""
        from scripts.ops.dashboard_server import _calculate_uptime

        nonexistent = tmp_path / "nonexistent.txt"

        with mock.patch("scripts.ops.dashboard_server._STATUS_PATH", nonexistent):
            uptime = _calculate_uptime()

        assert uptime == 0.0


# ---------------------------------------------------------------------------
# Dashboard API endpoint tests (mock HTTP handler)
# ---------------------------------------------------------------------------

class TestInstanceSettingsAPI:
    """Test the /api/instance/settings endpoints via DashboardHandler."""

    def _make_handler(self, method, path, body=None, instance_settings_path=None):
        """Create a mock DashboardHandler for testing."""
        from scripts.ops.dashboard_server import DashboardHandler

        # Build a mock request
        request_body = json.dumps(body).encode() if body else b""

        handler = mock.MagicMock(spec=DashboardHandler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(request_body))}
        handler.rfile = BytesIO(request_body)
        handler.wfile = BytesIO()
        handler.store = None
        handler.scheduler = None
        handler.control_plane = None

        # Wire up the real methods
        handler._read_post_body = lambda **kw: DashboardHandler._read_post_body(handler, **kw)

        # Capture responses by tracking wfile writes
        def _mock_json_response(data, status=200, **kw):
            handler.wfile.write(json.dumps(data).encode())
        handler._json_response = _mock_json_response

        # Create a simple handler for instance settings
        def _handle_instance_settings_get():
            from scripts.ops.dashboard_server import _read_instance_settings
            data = _read_instance_settings()
            handler._json_response(data)

        def _handle_instance_settings_post():
            req_body = handler._read_post_body()
            from scripts.ops.dashboard_server import (
                _write_instance_settings,
                _read_instance_settings,
            )
            if _write_instance_settings(req_body):
                handler._json_response({"ok": True, "settings": _read_instance_settings()})
            else:
                handler._json_response({"error": "Failed to save"}, status=500)

        handler._handle_instance_settings_get = _handle_instance_settings_get
        handler._handle_instance_settings_post = _handle_instance_settings_post

        return handler

    def test_get_instance_settings(self, tmp_instance_settings):
        """Test GET /api/instance/settings endpoint."""
        handler = self._make_handler("GET", "/api/instance/settings", instance_settings_path=tmp_instance_settings)

        with mock.patch("scripts.ops.dashboard_server._INSTANCE_SETTINGS_PATH", tmp_instance_settings):
            handler._handle_instance_settings_get()

        # Check response was written to wfile
        response_bytes = handler.wfile.getvalue()
        response = json.loads(response_bytes)
        assert response["name"] == "Test Instance"
        assert response["description"] == "Test description"
        assert response["isolated_workspaces"] is False
        assert response["auto_restart"] is False
        assert response["heartbeat_interval_seconds"] == 60
        assert response["experimental_features"]["parallel_execution"] is False

    def test_post_instance_settings(self, tmp_path):
        """Test POST /api/instance/settings endpoint."""
        body = {
            "name": "New Instance",
            "description": "New description",
            "isolated_workspaces": True,
            "auto_restart": True,
            "heartbeat_interval_seconds": 120,
        }
        settings_path = tmp_path / "instance_settings.json"

        handler = self._make_handler("POST", "/api/instance/settings", body=body, instance_settings_path=settings_path)

        with mock.patch("scripts.ops.dashboard_server._INSTANCE_SETTINGS_PATH", settings_path):
            handler._handle_instance_settings_post()

        # Check response was written to wfile
        response_bytes = handler.wfile.getvalue()
        response = json.loads(response_bytes)
        assert response["ok"] is True
        assert response["settings"]["name"] == "New Instance"
        assert response["settings"]["isolated_workspaces"] is True

        # Verify file was written
        assert settings_path.exists()
        written = json.loads(settings_path.read_text())
        assert written["name"] == "New Instance"
        assert written["isolated_workspaces"] is True


class TestCreationMethods:
    """Test instance creation methods endpoints."""

    def test_get_creation_methods_returns_four_methods(self):
        """GET /api/instance/creation-methods returns all 4 methods."""
        from scripts.ops.dashboard_server import _get_creation_methods

        result = _get_creation_methods()
        assert "methods" in result
        methods = result["methods"]
        assert len(methods) == 4

        ids = [m["id"] for m in methods]
        assert "docker" in ids
        assert "local" in ids
        assert "ssh" in ids
        assert "cloud" in ids

    def test_creation_methods_schema(self):
        """Each creation method has required fields."""
        from scripts.ops.dashboard_server import _get_creation_methods

        result = _get_creation_methods()
        for method in result["methods"]:
            assert "id" in method
            assert "name" in method
            assert "description" in method
            assert "icon" in method
            assert "available" in method
            assert "config_schema" in method
            assert isinstance(method["config_schema"], list)

    def test_cloud_method_not_available(self):
        """Cloud method should be marked as not available."""
        from scripts.ops.dashboard_server import _get_creation_methods

        result = _get_creation_methods()
        cloud = next(m for m in result["methods"] if m["id"] == "cloud")
        assert cloud["available"] is False

    def test_docker_local_ssh_available(self):
        """Docker, local, and SSH methods should be available."""
        from scripts.ops.dashboard_server import _get_creation_methods

        result = _get_creation_methods()
        for method_id in ("docker", "local", "ssh"):
            method = next(m for m in result["methods"] if m["id"] == method_id)
            assert method["available"] is True, f"{method_id} should be available"

    def test_provision_instance_docker(self, tmp_path):
        """Provision a Docker instance stores it correctly."""
        from scripts.ops.dashboard_server import _provision_instance

        instances_path = tmp_path / "provisioned_instances.json"
        with mock.patch("scripts.ops.dashboard_server._PROVISIONED_INSTANCES_PATH", instances_path):
            result = _provision_instance("docker", "test-docker", {"image": "swe-squad:latest"})

        assert result["ok"] is True
        assert result["instance"]["name"] == "test-docker"
        assert result["instance"]["method"] == "docker"
        assert result["instance"]["status"] == "pending"
        assert result["instance"]["config"]["image"] == "swe-squad:latest"

        # Verify persisted
        stored = json.loads(instances_path.read_text())
        assert len(stored) == 1
        assert stored[0]["name"] == "test-docker"

    def test_provision_instance_unknown_method(self, tmp_path):
        """Provisioning with unknown method returns error."""
        from scripts.ops.dashboard_server import _provision_instance

        instances_path = tmp_path / "provisioned_instances.json"
        with mock.patch("scripts.ops.dashboard_server._PROVISIONED_INSTANCES_PATH", instances_path):
            result = _provision_instance("kubernetes", "test-k8s", {})

        assert result["ok"] is False
        assert "Unknown" in result["error"]

    def test_provision_instance_unavailable_method(self, tmp_path):
        """Provisioning with unavailable method (cloud) returns error."""
        from scripts.ops.dashboard_server import _provision_instance

        instances_path = tmp_path / "provisioned_instances.json"
        with mock.patch("scripts.ops.dashboard_server._PROVISIONED_INSTANCES_PATH", instances_path):
            result = _provision_instance("cloud", "test-cloud", {"provider": "aws"})

        assert result["ok"] is False
        assert "not yet available" in result["error"]

    def test_provision_instance_empty_name(self, tmp_path):
        """Provisioning with empty name returns error."""
        from scripts.ops.dashboard_server import _provision_instance

        instances_path = tmp_path / "provisioned_instances.json"
        with mock.patch("scripts.ops.dashboard_server._PROVISIONED_INSTANCES_PATH", instances_path):
            result = _provision_instance("docker", "", {})

        assert result["ok"] is False
        assert "name" in result["error"].lower()

    def test_provision_multiple_instances(self, tmp_path):
        """Provisioning multiple instances appends to the list."""
        from scripts.ops.dashboard_server import _provision_instance

        instances_path = tmp_path / "provisioned_instances.json"
        with mock.patch("scripts.ops.dashboard_server._PROVISIONED_INSTANCES_PATH", instances_path):
            _provision_instance("docker", "instance-1", {"image": "v1"})
            _provision_instance("local", "instance-2", {"working_directory": "/tmp"})

        stored = json.loads(instances_path.read_text())
        assert len(stored) == 2
        assert stored[0]["method"] == "docker"
        assert stored[1]["method"] == "local"

    def test_read_provisioned_instances_empty(self, tmp_path):
        """Reading provisioned instances from missing file returns empty list."""
        from scripts.ops.dashboard_server import _read_provisioned_instances

        with mock.patch("scripts.ops.dashboard_server._PROVISIONED_INSTANCES_PATH", tmp_path / "nope.json"):
            result = _read_provisioned_instances()

        assert result == []


class TestSSHConnectionsAPI:
    """Tests for SSH connection helper endpoints on DashboardHandler."""

    class _FakeUserStore:
        def __init__(self):
            self._users = set()
            self._secrets = {}

        def get_user(self, login):
            return {"github_login": login} if login in self._users else None

        def get_or_create_user(self, login):
            self._users.add(login)
            return {"github_login": login}

        def set_secret(self, login, name, value):
            self._secrets[(login, name)] = value

        def get_secret(self, login, name):
            key = (login, name)
            if key not in self._secrets:
                raise ValueError("missing secret")
            return self._secrets[key]

    def _make_handler(self, body):
        from scripts.ops.dashboard_server import DashboardHandler

        handler = mock.MagicMock(spec=DashboardHandler)
        handler.headers = {}
        handler.wfile = BytesIO()
        handler._read_post_body = lambda: body
        handler._json_response = lambda data, status=200, **kw: handler.wfile.write(json.dumps(data).encode())
        return handler

    def test_generate_ssh_key_stores_secret_and_returns_public_key(self):
        """POST ssh/generate stores private key in secrets and returns public key."""
        from scripts.ops.dashboard_server import DashboardHandler
        store = self._FakeUserStore()
        handler = self._make_handler({"secret_name": "SSH_KEY_TEST", "comment": "unit@test"})

        def _subprocess_side_effect(args, **kwargs):
            if args[:2] == ["ssh-keygen", "-q"]:
                key_path = Path(args[-1])
                key_path.write_text("PRIVATE")
                Path(f"{key_path}.pub").write_text("ssh-ed25519 AAAATEST unit@test\n")
                return mock.Mock(returncode=0, stdout="", stderr="")
            if args[:2] == ["ssh-keygen", "-lf"]:
                return mock.Mock(returncode=0, stdout="256 SHA256:abcde unit@test (ED25519)\n", stderr="")
            raise AssertionError(f"Unexpected subprocess args: {args}")

        with mock.patch("scripts.ops.dashboard_server._get_user_store", return_value=store), \
             mock.patch("subprocess.run", side_effect=_subprocess_side_effect):
            DashboardHandler._handle_generate_ssh_key(handler, {"login": "alice"})

        payload = json.loads(handler.wfile.getvalue())
        assert payload["ok"] is True
        assert payload["secret_name"] == "SSH_KEY_TEST"
        assert payload["public_key"].startswith("ssh-ed25519")
        assert store.get_secret("alice", "SSH_KEY_TEST") == "PRIVATE"

    def test_import_ssh_key_rejects_invalid_key(self):
        """POST ssh/import returns 400 for invalid private key."""
        from scripts.ops.dashboard_server import DashboardHandler
        store = self._FakeUserStore()
        handler = self._make_handler({"secret_name": "SSH_KEY_TEST", "private_key": "bad-key"})

        with mock.patch("scripts.ops.dashboard_server._get_user_store", return_value=store), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout="", stderr="invalid key")):
            DashboardHandler._handle_import_ssh_key(handler, {"login": "alice"})

        payload = json.loads(handler.wfile.getvalue())
        assert payload["error"] == "Invalid SSH private key"

    def test_test_ssh_connection_uses_stored_secret(self):
        """POST connection test uses secret key and returns success."""
        from scripts.ops.dashboard_server import DashboardHandler
        store = self._FakeUserStore()
        store.get_or_create_user("alice")
        store.set_secret("alice", "SSH_KEY_TEST", "PRIVATE")
        handler = self._make_handler({
            "host": "test-worker-1",
            "username": "ubuntu",
            "port": 22,
            "secret_name": "SSH_KEY_TEST",
        })

        with mock.patch("scripts.ops.dashboard_server._get_user_store", return_value=store), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="swe-ssh-ok\n", stderr="")):
            DashboardHandler._handle_test_ssh_connection(handler, {"login": "alice"})

        payload = json.loads(handler.wfile.getvalue())
        assert payload["ok"] is True
        assert payload["exit_code"] == 0


# ---------------------------------------------------------------------------
# Heartbeat API endpoint tests
# ---------------------------------------------------------------------------

class TestHeartbeatAPI:
    """Test the /api/instance/heartbeat endpoint."""

    def test_heartbeat_response_structure(self, tmp_status_json, tmp_jobs_json):
        """Test heartbeat endpoint returns correct structure."""
        from scripts.ops.dashboard_server import _get_instance_heartbeat

        with mock.patch("scripts.ops.dashboard_server._STATUS_PATH", tmp_status_json), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH", tmp_jobs_json), \
             mock.patch("scripts.ops.dashboard_server._read_instance_settings") as mock_settings:
            mock_settings.return_value = {"name": "Test Instance"}
            heartbeat = _get_instance_heartbeat()

        required_keys = [
            "instance_name",
            "timestamp",
            "status",
            "last_cycle_time",
            "agents_active",
            "total_agents",
            "uptime_seconds",
        ]
        for key in required_keys:
            assert key in heartbeat, f"Missing required key: {key}"

    def test_heartbeat_status_healthy(self, tmp_status_json, tmp_jobs_json):
        """Test heartbeat status is 'healthy' when last_cycle exists."""
        from scripts.ops.dashboard_server import _get_instance_heartbeat

        with mock.patch("scripts.ops.dashboard_server._STATUS_PATH", tmp_status_json), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH", tmp_jobs_json), \
             mock.patch("scripts.ops.dashboard_server._read_instance_settings"):
            heartbeat = _get_instance_heartbeat()

        assert heartbeat["status"] == "healthy"

    def test_heartbeat_status_unknown(self, tmp_jobs_json):
        """Test heartbeat status is 'unknown' when no status.json."""
        from scripts.ops.dashboard_server import _get_instance_heartbeat

        with mock.patch("scripts.ops.dashboard_server._STATUS_PATH", tmp_jobs_json.parent / "nonexistent"), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH", tmp_jobs_json), \
             mock.patch("scripts.ops.dashboard_server._read_instance_settings"):
            heartbeat = _get_instance_heartbeat()

        assert heartbeat["status"] == "unknown"

    def test_heartbeat_agents_count(self, tmp_status_json, tmp_jobs_json):
        """Test heartbeat correctly counts active agents."""
        from scripts.ops.dashboard_server import _get_instance_heartbeat

        # Create jobs with mixed enabled status
        jobs = [
            {"job_id": "monitor", "name": "Monitor", "enabled": True},
            {"job_id": "investigator", "name": "Investigator", "enabled": True},
            {"job_id": "developer", "name": "Developer", "enabled": False},
        ]
        jobs_path = tmp_jobs_json.parent / "test_jobs.json"
        jobs_path.write_text(json.dumps(jobs, indent=2))

        with mock.patch("scripts.ops.dashboard_server._STATUS_PATH", tmp_status_json), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH", jobs_path), \
             mock.patch("scripts.ops.dashboard_server._read_instance_settings"):
            heartbeat = _get_instance_heartbeat()

        assert heartbeat["agents_active"] == 2
        assert heartbeat["total_agents"] == 3

    def test_heartbeat_degraded_on_timeout(self, tmp_jobs_json):
        """Test heartbeat returns 'degraded' status when file reads timeout."""
        from scripts.ops.dashboard_server import _get_instance_heartbeat

        # Mock _read_json_file_with_timeout to return None (timeout)
        with mock.patch("scripts.ops.dashboard_server._STATUS_PATH", tmp_jobs_json.parent / "nonexistent"), \
             mock.patch("scripts.ops.dashboard_server._JOBS_PATH", tmp_jobs_json), \
             mock.patch("scripts.ops.dashboard_server._read_json_file_with_timeout", return_value=None), \
             mock.patch("scripts.ops.dashboard_server._read_instance_settings") as mock_settings:
            mock_settings.return_value = {"name": "Test Instance"}
            heartbeat = _get_instance_heartbeat()

        # On timeout, should return degraded status (fail-secure)
        assert heartbeat["status"] == "degraded"
        assert heartbeat["instance_name"] == "Test Instance"
        assert heartbeat["agents_active"] == 0
        assert heartbeat["total_agents"] == 0
        assert heartbeat["uptime_seconds"] == 0.0
        assert heartbeat["last_cycle_time"] is None

    def test_read_instance_settings_timeout_returns_defaults(self, tmp_path):
        """Test reading instance settings with timeout returns defaults (fail-secure)."""
        from scripts.ops.dashboard_server import (
            _read_instance_settings,
            _DEFAULT_INSTANCE_SETTINGS,
        )
        settings_path = tmp_path / "timeout_instance_settings.json"

        # Mock file read to timeout (return None)
        with mock.patch("scripts.ops.dashboard_server._INSTANCE_SETTINGS_PATH", settings_path), \
             mock.patch("scripts.ops.dashboard_server._read_json_file_with_timeout", return_value=None):
            settings = _read_instance_settings()

        # On timeout, should return defaults (fail-secure)
        assert settings == _DEFAULT_INSTANCE_SETTINGS

    def test_write_instance_settings_timeout_returns_false(self, tmp_path):
        """Test writing instance settings with timeout returns False (fail-secure)."""
        from scripts.ops.dashboard_server import _write_instance_settings
        settings_path = tmp_path / "timeout_instance_settings.json"

        # Mock file write to timeout (return False)
        with mock.patch("scripts.ops.dashboard_server._INSTANCE_SETTINGS_PATH", settings_path), \
             mock.patch("scripts.ops.dashboard_server._write_file_with_timeout", return_value=False):
            result = _write_instance_settings({"name": "Test"})

        # On timeout, should return False (fail-secure)
        assert result is False
        # File should not exist since write failed
        assert not settings_path.exists()
