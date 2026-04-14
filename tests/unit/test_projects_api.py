"""Tests for the Projects/Repos management feature.

Covers:
- Dashboard API endpoints (GET/POST/DELETE /api/projects)
- CLI project subcommands (project list, project init)
- Config helpers (_load_projects_from_config, _save_project_to_config, etc.)
"""

from __future__ import annotations
import re


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)

import json
import os
import sys
import tempfile
import textwrap
from io import BytesIO
from pathlib import Path
from unittest import mock

import pytest
import yaml

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_config(tmp_path):
    """Create a temporary swe_team.yaml with sample repos."""
    config = {
        "repos": [
            {
                "name": "your-org/example-app",
                "local_path": "/home/agent/Projects/example-app",
                "description": "Primary product",
                "priority": "medium",
            },
            {
                "name": "your-org/SWE-Squad",
                "local_path": "/home/agent/SWE-Squad",
                "description": "This repo",
                "priority": "medium",
            },
        ],
        "enabled": False,
        "team_id": "test",
    }
    config_path = tmp_path / "swe_team.yaml"
    config_path.write_text(yaml.dump(config, default_flow_style=False))
    return config_path


@pytest.fixture
def empty_config(tmp_path):
    """Create a temporary swe_team.yaml with no repos."""
    config = {"enabled": False, "team_id": "test"}
    config_path = tmp_path / "swe_team.yaml"
    config_path.write_text(yaml.dump(config, default_flow_style=False))
    return config_path


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestConfigHelpers:
    """Test the config load/save helpers from dashboard_server."""

    def test_load_projects_from_config(self, tmp_config):
        from scripts.ops.dashboard_server import _load_projects_from_config, _CONFIG_PATH
        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            projects = _load_projects_from_config()
        assert len(projects) == 2
        assert projects[0]["name"] == "your-org/example-app"
        assert projects[0]["local_path"] == "/home/agent/Projects/example-app"
        assert projects[0]["enabled"] is True

    def test_load_projects_from_empty_config(self, empty_config):
        from scripts.ops.dashboard_server import _load_projects_from_config
        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", empty_config):
            projects = _load_projects_from_config()
        assert projects == []

    def test_save_project_to_config(self, tmp_config):
        from scripts.ops.dashboard_server import (
            _save_project_to_config,
            _load_projects_from_config,
        )
        project = {
            "name": "your-org/NewProject",
            "local_path": "/home/agent/NewProject",
            "description": "New project",
            "enabled": True,
            "priority": "high",
        }
        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            ok = _save_project_to_config(project)
            assert ok is True
            projects = _load_projects_from_config()
        assert len(projects) == 3
        assert projects[2]["name"] == "your-org/NewProject"
        assert projects[2]["enabled"] is True

    def test_save_project_to_config_preserves_enabled_false(self, tmp_config):
        from scripts.ops.dashboard_server import (
            _save_project_to_config,
            _load_projects_from_config,
        )
        project = {
            "name": "your-org/DisabledProject",
            "local_path": "/home/agent/DisabledProject",
            "description": "Disabled project",
            "enabled": False,
            "priority": "low",
        }
        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            ok = _save_project_to_config(project)
            assert ok is True
            projects = _load_projects_from_config()
        assert len(projects) == 3
        assert projects[2]["name"] == "your-org/DisabledProject"
        assert projects[2]["enabled"] is False

    def test_save_duplicate_project_fails(self, tmp_config):
        from scripts.ops.dashboard_server import _save_project_to_config
        project = {
            "name": "your-org/example-app",
            "local_path": "/some/path",
        }
        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            ok = _save_project_to_config(project)
        assert ok is False

    def test_delete_project_from_config(self, tmp_config):
        from scripts.ops.dashboard_server import (
            _delete_project_from_config,
            _load_projects_from_config,
        )
        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            ok = _delete_project_from_config("your-org/example-app")
            assert ok is True
            projects = _load_projects_from_config()
        assert len(projects) == 1
        assert projects[0]["name"] == "your-org/SWE-Squad"

    def test_delete_nonexistent_project(self, tmp_config):
        from scripts.ops.dashboard_server import _delete_project_from_config
        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            ok = _delete_project_from_config("nonexistent/project")
        assert ok is False


# ---------------------------------------------------------------------------
# CLI project command tests
# ---------------------------------------------------------------------------

class TestCLIProjectCommands:
    """Test the swe_cli project subcommands."""

    def test_project_list_text(self, tmp_config, capsys):
        from scripts.ops.swe_cli import build_parser, cmd_project

        with mock.patch("scripts.ops.swe_cli.PROJECT_ROOT", tmp_config.parent):
            # Need config/swe_team.yaml under PROJECT_ROOT
            config_dir = tmp_config.parent / "config"
            config_dir.mkdir(exist_ok=True)
            import shutil
            shutil.copy(tmp_config, config_dir / "swe_team.yaml")

            parser = build_parser()
            args = parser.parse_args(["project", "list"])
            result = cmd_project(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "your-org/example-app" in output
        assert "your-org/SWE-Squad" in output
        assert "2 project(s)" in _strip_ansi(output)

    def test_project_list_json(self, tmp_config, capsys):
        from scripts.ops.swe_cli import build_parser, cmd_project

        with mock.patch("scripts.ops.swe_cli.PROJECT_ROOT", tmp_config.parent):
            config_dir = tmp_config.parent / "config"
            config_dir.mkdir(exist_ok=True)
            import shutil
            shutil.copy(tmp_config, config_dir / "swe_team.yaml")

            parser = build_parser()
            args = parser.parse_args(["project", "list", "--json"])
            result = cmd_project(args)

        assert result == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert len(data) == 2
        assert data[0]["name"] == "your-org/example-app"

    def test_project_init(self, empty_config, capsys):
        from scripts.ops.swe_cli import build_parser, cmd_project

        with mock.patch("scripts.ops.swe_cli.PROJECT_ROOT", empty_config.parent):
            config_dir = empty_config.parent / "config"
            config_dir.mkdir(exist_ok=True)
            import shutil
            shutil.copy(empty_config, config_dir / "swe_team.yaml")

            parser = build_parser()
            args = parser.parse_args([
                "project", "init", "your-org/NewRepo",
                "--repo", "your-org/NewRepo",
                "--local-path", "/tmp/new-repo",
            ])
            result = cmd_project(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "added" in output.lower()

        # Verify it was written
        written = yaml.safe_load((config_dir / "swe_team.yaml").read_text())
        assert len(written["repos"]) == 1
        assert written["repos"][0]["name"] == "your-org/NewRepo"

    def test_project_init_duplicate(self, tmp_config, capsys):
        from scripts.ops.swe_cli import build_parser, cmd_project

        with mock.patch("scripts.ops.swe_cli.PROJECT_ROOT", tmp_config.parent):
            config_dir = tmp_config.parent / "config"
            config_dir.mkdir(exist_ok=True)
            import shutil
            shutil.copy(tmp_config, config_dir / "swe_team.yaml")

            parser = build_parser()
            args = parser.parse_args([
                "project", "init", "your-org/example-app",
            ])
            result = cmd_project(args)

        assert result == 1
        err = capsys.readouterr().err
        assert "already exists" in err

    def test_project_list_empty(self, empty_config, capsys):
        from scripts.ops.swe_cli import build_parser, cmd_project

        with mock.patch("scripts.ops.swe_cli.PROJECT_ROOT", empty_config.parent):
            config_dir = empty_config.parent / "config"
            config_dir.mkdir(exist_ok=True)
            import shutil
            shutil.copy(empty_config, config_dir / "swe_team.yaml")

            parser = build_parser()
            args = parser.parse_args(["project", "list"])
            result = cmd_project(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "No projects configured" in output


# ---------------------------------------------------------------------------
# Dashboard API endpoint tests (mock HTTP handler)
# ---------------------------------------------------------------------------

class TestProjectsAPI:
    """Test the /api/projects endpoints via DashboardHandler."""

    def _make_handler(self, method, path, body=None, config_path=None):
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
        handler._json_response = lambda data, status=200, **kw: DashboardHandler._json_response(handler, data, status, **kw)
        handler._handle_list_projects = lambda: DashboardHandler._handle_list_projects(handler)
        handler._handle_get_project = lambda name: DashboardHandler._handle_get_project(handler, name)
        handler._handle_create_project = lambda: DashboardHandler._handle_create_project(handler)
        handler._handle_delete_project = lambda name: DashboardHandler._handle_delete_project(handler, name)
        handler._handle_github_repos_connect = lambda: DashboardHandler._handle_github_repos_connect(handler)

        return handler

    def test_get_projects_returns_list(self, tmp_config):
        from scripts.ops.dashboard_server import _load_projects_from_config

        handler = self._make_handler("GET", "/api/projects")

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_list_projects()

        # Check the response was written
        handler.send_response.assert_called_with(200)
        body = handler.wfile.getvalue()
        data = json.loads(body)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["name"] == "your-org/example-app"

    def test_get_single_project(self, tmp_config):
        handler = self._make_handler("GET", "/api/projects/your-org/example-app")

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_get_project("your-org/example-app")

        handler.send_response.assert_called_with(200)
        body = handler.wfile.getvalue()
        data = json.loads(body)
        assert data["name"] == "your-org/example-app"

    def test_get_single_project_not_found(self, tmp_config):
        handler = self._make_handler("GET", "/api/projects/nonexistent")

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_get_project("nonexistent")

        handler.send_response.assert_called_with(404)

    def test_post_project(self, tmp_config):
        body = {
            "name": "your-org/TestProject",
            "local_path": "/tmp/test",
            "description": "Test project",
        }
        handler = self._make_handler("POST", "/api/projects", body=body)

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_create_project()

        handler.send_response.assert_called_with(201)
        resp_body = handler.wfile.getvalue()
        data = json.loads(resp_body)
        assert data["ok"] is True
        assert data["project"]["name"] == "your-org/TestProject"

    def test_post_duplicate_project(self, tmp_config):
        body = {"name": "your-org/example-app"}
        handler = self._make_handler("POST", "/api/projects", body=body)

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_create_project()

        handler.send_response.assert_called_with(409)

    def test_post_project_no_name(self, tmp_config):
        body = {"local_path": "/tmp/foo"}
        handler = self._make_handler("POST", "/api/projects", body=body)

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_create_project()

        handler.send_response.assert_called_with(400)

    def test_delete_project(self, tmp_config):
        handler = self._make_handler("DELETE", "/api/projects/your-org/example-app")

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_delete_project("your-org/example-app")

        handler.send_response.assert_called_with(200)
        resp_body = handler.wfile.getvalue()
        data = json.loads(resp_body)
        assert data["ok"] is True
        assert data["deleted"] == "your-org/example-app"

    def test_delete_project_not_found(self, tmp_config):
        handler = self._make_handler("DELETE", "/api/projects/nonexistent")

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_delete_project("nonexistent")

        handler.send_response.assert_called_with(404)

    def test_post_github_connect_repo(self, tmp_config):
        body = {"repo": "your-org/NewGitHubRepo", "priority": "high"}
        handler = self._make_handler("POST", "/api/github/repos/connect", body=body)
        handler._check_auth = lambda: {"login": "test-user"}

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_github_repos_connect()

        handler.send_response.assert_called_with(201)
        resp_body = handler.wfile.getvalue()
        data = json.loads(resp_body)
        assert data["ok"] is True
        assert data["project"]["name"] == "your-org/NewGitHubRepo"
        assert data["project"]["priority"] == "high"
        assert data["project"]["enabled"] is True


# ---------------------------------------------------------------------------
# Repo configure CLI test
# ---------------------------------------------------------------------------

class TestRepoConfigure:
    def test_repo_configure(self, tmp_config, capsys):
        from scripts.ops.swe_cli import build_parser, cmd_repo_configure

        with mock.patch("scripts.ops.swe_cli.PROJECT_ROOT", tmp_config.parent):
            config_dir = tmp_config.parent / "config"
            config_dir.mkdir(exist_ok=True)
            import shutil
            shutil.copy(tmp_config, config_dir / "swe_team.yaml")

            parser = build_parser()
            args = parser.parse_args(["repo", "configure", "your-org/example-app"])
            result = cmd_repo_configure(args)

        assert result == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["name"] == "your-org/example-app"

    def test_repo_configure_not_found(self, tmp_config, capsys):
        from scripts.ops.swe_cli import build_parser, cmd_repo_configure

        with mock.patch("scripts.ops.swe_cli.PROJECT_ROOT", tmp_config.parent):
            config_dir = tmp_config.parent / "config"
            config_dir.mkdir(exist_ok=True)
            import shutil
            shutil.copy(tmp_config, config_dir / "swe_team.yaml")

            parser = build_parser()
            args = parser.parse_args(["repo", "configure", "nonexistent"])
            result = cmd_repo_configure(args)

        assert result == 1


# ---------------------------------------------------------------------------
# Tests for new PATCH endpoints (4.1 Project List Enrichment)
# ---------------------------------------------------------------------------

class TestProjectPatchEndpoints:
    """Test PATCH endpoints for project updates."""

    def _make_handler(self, method, path, body=None, config_path=None):
        """Create a mock DashboardHandler for testing."""
        from scripts.ops.dashboard_server import DashboardHandler

        request_body = json.dumps(body).encode() if body else b""

        handler = mock.MagicMock(spec=DashboardHandler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(request_body))}
        handler.rfile = BytesIO(request_body)
        handler.wfile = BytesIO()
        handler.store = None
        handler.scheduler = None
        handler.control_plane = None

        # Wire up real methods
        handler._read_post_body = lambda **kw: DashboardHandler._read_post_body(handler, **kw)
        handler._json_response = lambda data, status=200, **kw: DashboardHandler._json_response(handler, data, status, **kw)

        return handler

    def test_patch_project_name(self, tmp_config):
        """PATCH /api/projects/<name>/name - update project name."""
        from scripts.ops.dashboard_server import _update_project_field

        body = {"name": "NewProjectName"}

        handler = self._make_handler("PATCH", "/api/projects/your-org/example-app/name", body, tmp_config)

        with mock.patch("scripts.ops.dashboard_server._update_project_field", return_value=True):
            # Wire up the handler method
            from scripts.ops.dashboard_server import DashboardHandler
            handler._handle_update_project_name = lambda name: DashboardHandler._handle_update_project_name(handler, name)

            handler._handle_update_project_name("your-org/example-app")

        handler.send_response.assert_called_with(200)

    def test_patch_project_name_duplicate(self, tmp_config):
        """PATCH /api/projects/<name>/name - reject duplicate name."""
        from scripts.ops.dashboard_server import _load_projects_from_config

        handler = self._make_handler("PATCH", "/api/projects/your-org/example-app/name", body={"name": "your-org/SWE-Squad"}, config_path=tmp_config)

        with mock.patch("scripts.ops.dashboard_server._load_projects_from_config") as mock_load:
            mock_load.return_value = [
                {"name": "your-org/example-app", "local_path": "/path1"},
                {"name": "your-org/SWE-Squad", "local_path": "/path2"},
            ]

            from scripts.ops.dashboard_server import DashboardHandler
            handler._handle_update_project_name = lambda name: DashboardHandler._handle_update_project_name(handler, name)

            handler._handle_update_project_name("your-org/example-app")

        handler.send_response.assert_called_with(409)

    def test_patch_project_name_empty(self, tmp_config):
        """PATCH /api/projects/<name>/name - reject empty name."""
        handler = self._make_handler("PATCH", "/api/projects/test/name", body={"name": ""}, config_path=tmp_config)

        from scripts.ops.dashboard_server import DashboardHandler
        handler._handle_update_project_name = lambda name: DashboardHandler._handle_update_project_name(handler, name)

        handler._handle_update_project_name("test")

        handler.send_response.assert_called_with(400)

    def test_patch_project_description(self, tmp_config):
        """PATCH /api/projects/<name>/description - update project description."""
        body = {"description": "Updated description"}

        handler = self._make_handler("PATCH", "/api/projects/your-org/example-app/description", body, tmp_config)

        with mock.patch("scripts.ops.dashboard_server._update_project_field", return_value=True):
            from scripts.ops.dashboard_server import DashboardHandler
            handler._handle_update_project_description = lambda name: DashboardHandler._handle_update_project_description(handler, name)

            handler._handle_update_project_description("your-org/example-app")

        handler.send_response.assert_called_with(200)

    def test_patch_project_priority(self, tmp_config):
        """PATCH /api/projects/<name>/priority - update project priority."""
        body = {"priority": "high"}

        handler = self._make_handler("PATCH", "/api/projects/your-org/example-app/priority", body, tmp_config)

        with mock.patch("scripts.ops.dashboard_server._update_project_field", return_value=True):
            from scripts.ops.dashboard_server import DashboardHandler
            handler._handle_update_project_priority = lambda name: DashboardHandler._handle_update_project_priority(handler, name)

            handler._handle_update_project_priority("your-org/example-app")

        handler.send_response.assert_called_with(200)

    def test_patch_project_priority_invalid(self, tmp_config):
        """PATCH /api/projects/<name>/priority - reject invalid priority."""
        handler = self._make_handler("PATCH", "/api/projects/test/priority", body={"priority": "invalid"}, config_path=tmp_config)

        from scripts.ops.dashboard_server import DashboardHandler
        handler._handle_update_project_priority = lambda name: DashboardHandler._handle_update_project_priority(handler, name)

        handler._handle_update_project_priority("test")

        handler.send_response.assert_called_with(400)

    def test_patch_project_enabled(self, tmp_config):
        """PATCH /api/projects/<name>/enabled - toggle project enabled status."""
        body = {"enabled": False}

        handler = self._make_handler("PATCH", "/api/projects/your-org/example-app/enabled", body, tmp_config)

        with mock.patch("scripts.ops.dashboard_server._update_project_field", return_value=True):
            from scripts.ops.dashboard_server import DashboardHandler
            handler._handle_update_project_enabled = lambda name: DashboardHandler._handle_update_project_enabled(handler, name)

            handler._handle_update_project_enabled("your-org/example-app")

        handler.send_response.assert_called_with(200)

    def test_patch_project_enabled_invalid(self, tmp_config):
        """PATCH /api/projects/<name>/enabled - reject non-boolean enabled."""
        handler = self._make_handler("PATCH", "/api/projects/test/enabled", body={"enabled": "not-a-boolean"}, config_path=tmp_config)

        from scripts.ops.dashboard_server import DashboardHandler
        handler._handle_update_project_enabled = lambda name: DashboardHandler._handle_update_project_enabled(handler, name)

        handler._handle_update_project_enabled("test")

        handler.send_response.assert_called_with(400)

    def test_patch_project_local_path(self, tmp_config):
        """PATCH /api/projects/<name>/local_path - update project local path."""
        body = {"local_path": "/new/path/to/repo"}

        handler = self._make_handler("PATCH", "/api/projects/your-org/example-app/local_path", body, tmp_config)

        with mock.patch("scripts.ops.dashboard_server._update_project_field", return_value=True):
            from scripts.ops.dashboard_server import DashboardHandler
            handler._handle_update_project_local_path = lambda name: DashboardHandler._handle_update_project_local_path(handler, name)

            handler._handle_update_project_local_path("your-org/example-app")

        handler.send_response.assert_called_with(200)


# ---------------------------------------------------------------------------
# Tests for GET /api/projects/<name>/tickets endpoint (4.2 Project Detail Page)
# ---------------------------------------------------------------------------

class TestProjectTicketsEndpoint:
    """Test GET /api/projects/<name>/tickets endpoint."""

    def _make_handler(self, method, path, store=None):
        """Create a mock DashboardHandler for testing."""
        from scripts.ops.dashboard_server import DashboardHandler

        handler = mock.MagicMock(spec=DashboardHandler)
        handler.path = path
        handler.headers = {"Content-Length": "0"}
        handler.wfile = BytesIO()
        handler.store = store or mock.MagicMock()

        handler._json_response = lambda data, status=200: DashboardHandler._json_response(handler, data, status)

        return handler

    def test_get_project_tickets_empty(self):
        """GET /api/projects/<name>/tickets - return empty array for no tickets."""
        handler = self._make_handler("GET", "/api/projects/test/tickets")

        from scripts.ops.dashboard_server import DashboardHandler
        handler.store.list_all.return_value = []
        handler._handle_get_project_tickets = lambda name: DashboardHandler._handle_get_project_tickets(handler, name)

        handler._handle_get_project_tickets("test")

        handler.send_response.assert_called_with(200)

    def test_get_project_tickets_filtered(self):
        """GET /api/projects/<name>/tickets - filter by project_id."""
        handler = self._make_handler("GET", "/api/projects/test/tickets")

        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus, TicketType
        mock_tickets = [
            SWETicket(
                title="Ticket 1",
                description="Test",
                severity=TicketSeverity.MEDIUM,
                ticket_type=TicketType.UNKNOWN,
                project_id="test",
            ),
            SWETicket(
                title="Ticket 2",
                description="Test",
                severity=TicketSeverity.HIGH,
                ticket_type=TicketType.BUG,
                project_id="other-project",
            ),
        ]

        from scripts.ops.dashboard_server import DashboardHandler
        handler.store.list_all.return_value = mock_tickets
        handler._handle_get_project_tickets = lambda name: DashboardHandler._handle_get_project_tickets(handler, name)

        handler._handle_get_project_tickets("test")

        handler.send_response.assert_called_with(200)


# ---------------------------------------------------------------------------
# Tests for GET /api/projects/<name>/stats endpoint
# ---------------------------------------------------------------------------

class TestProjectStatsEndpoint:
    """Test GET /api/projects/<name>/stats endpoint."""

    def _make_handler(self, method, path, store=None):
        """Create a mock DashboardHandler for testing."""
        from scripts.ops.dashboard_server import DashboardHandler

        handler = mock.MagicMock(spec=DashboardHandler)
        handler.path = path
        handler.headers = {"Content-Length": "0"}
        handler.wfile = BytesIO()
        handler.store = store or mock.MagicMock()

        handler._json_response = lambda data, status=200: DashboardHandler._json_response(handler, data, status)

        return handler

    def test_get_project_stats_empty(self):
        """GET /api/projects/<name>/stats - return stats for empty project."""
        handler = self._make_handler("GET", "/api/projects/test/stats")

        from scripts.ops.dashboard_server import DashboardHandler
        handler.store.list_all.return_value = []
        handler._handle_get_project_stats = lambda name: DashboardHandler._handle_get_project_stats(handler, name)

        handler._handle_get_project_stats("test")

        handler.send_response.assert_called_with(200)

    def test_get_project_stats_with_tickets(self):
        """GET /api/projects/<name>/stats - calculate ticket counts and costs."""
        handler = self._make_handler("GET", "/api/projects/test/stats")

        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus, TicketType
        mock_tickets = [
            SWETicket(
                title="Ticket 1",
                description="Test",
                severity=TicketSeverity.MEDIUM,
                ticket_type=TicketType.BUG,
                project_id="test",
                status=TicketStatus.OPEN,
                metadata={"total_cost_usd": 10.50},
            ),
            SWETicket(
                title="Ticket 2",
                description="Test",
                severity=TicketSeverity.HIGH,
                ticket_type=TicketType.FEATURE,
                project_id="test",
                status=TicketStatus.RESOLVED,
                metadata={"total_cost_usd": 5.25},
            ),
        ]

        from scripts.ops.dashboard_server import DashboardHandler
        handler.store.list_all.return_value = mock_tickets
        handler._handle_get_project_stats = lambda name: DashboardHandler._handle_get_project_stats(handler, name)

        handler._handle_get_project_stats("test")

        handler.send_response.assert_called_with(200)
