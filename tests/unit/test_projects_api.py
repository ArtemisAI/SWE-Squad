"""Tests for the Projects/Repos management feature.

Covers:
- Dashboard API endpoints (GET/POST/DELETE /api/projects)
- CLI project subcommands (project list, project init)
- Config helpers (_load_projects_from_config, _save_project_to_config, etc.)
"""

from __future__ import annotations

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
                "name": "example-org/my-app",
                "local_path": "/home/agent/Projects/my-app",
                "description": "Primary product",
                "priority": "medium",
            },
            {
                "name": "ArtemisAI/SWE-Squad-DEV",
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
        assert projects[0]["name"] == "example-org/my-app"
        assert projects[0]["local_path"] == "/home/agent/Projects/my-app"
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
            "name": "ArtemisAI/NewProject",
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
        assert projects[2]["name"] == "ArtemisAI/NewProject"

    def test_save_duplicate_project_fails(self, tmp_config):
        from scripts.ops.dashboard_server import _save_project_to_config
        project = {
            "name": "example-org/my-app",
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
            ok = _delete_project_from_config("example-org/my-app")
            assert ok is True
            projects = _load_projects_from_config()
        assert len(projects) == 1
        assert projects[0]["name"] == "ArtemisAI/SWE-Squad-DEV"

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
        assert "example-org/my-app" in output
        assert "ArtemisAI/SWE-Squad-DEV" in output
        assert "2 project(s)" in output

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
        assert data[0]["name"] == "example-org/my-app"

    def test_project_init(self, empty_config, capsys):
        from scripts.ops.swe_cli import build_parser, cmd_project

        with mock.patch("scripts.ops.swe_cli.PROJECT_ROOT", empty_config.parent):
            config_dir = empty_config.parent / "config"
            config_dir.mkdir(exist_ok=True)
            import shutil
            shutil.copy(empty_config, config_dir / "swe_team.yaml")

            parser = build_parser()
            args = parser.parse_args([
                "project", "init", "ArtemisAI/NewRepo",
                "--repo", "ArtemisAI/NewRepo",
                "--local-path", "/tmp/new-repo",
            ])
            result = cmd_project(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "added" in output.lower()

        # Verify it was written
        written = yaml.safe_load((config_dir / "swe_team.yaml").read_text())
        assert len(written["repos"]) == 1
        assert written["repos"][0]["name"] == "ArtemisAI/NewRepo"

    def test_project_init_duplicate(self, tmp_config, capsys):
        from scripts.ops.swe_cli import build_parser, cmd_project

        with mock.patch("scripts.ops.swe_cli.PROJECT_ROOT", tmp_config.parent):
            config_dir = tmp_config.parent / "config"
            config_dir.mkdir(exist_ok=True)
            import shutil
            shutil.copy(tmp_config, config_dir / "swe_team.yaml")

            parser = build_parser()
            args = parser.parse_args([
                "project", "init", "example-org/my-app",
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
        handler._read_post_body = lambda: DashboardHandler._read_post_body(handler)
        handler._json_response = lambda data, status=200: DashboardHandler._json_response(handler, data, status)
        handler._handle_list_projects = lambda: DashboardHandler._handle_list_projects(handler)
        handler._handle_get_project = lambda name: DashboardHandler._handle_get_project(handler, name)
        handler._handle_create_project = lambda: DashboardHandler._handle_create_project(handler)
        handler._handle_delete_project = lambda name: DashboardHandler._handle_delete_project(handler, name)

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
        assert data[0]["name"] == "example-org/my-app"

    def test_get_single_project(self, tmp_config):
        handler = self._make_handler("GET", "/api/projects/example-org/my-app")

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_get_project("example-org/my-app")

        handler.send_response.assert_called_with(200)
        body = handler.wfile.getvalue()
        data = json.loads(body)
        assert data["name"] == "example-org/my-app"

    def test_get_single_project_not_found(self, tmp_config):
        handler = self._make_handler("GET", "/api/projects/nonexistent")

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_get_project("nonexistent")

        handler.send_response.assert_called_with(404)

    def test_post_project(self, tmp_config):
        body = {
            "name": "ArtemisAI/TestProject",
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
        assert data["project"]["name"] == "ArtemisAI/TestProject"

    def test_post_duplicate_project(self, tmp_config):
        body = {"name": "example-org/my-app"}
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
        handler = self._make_handler("DELETE", "/api/projects/example-org/my-app")

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_delete_project("example-org/my-app")

        handler.send_response.assert_called_with(200)
        resp_body = handler.wfile.getvalue()
        data = json.loads(resp_body)
        assert data["ok"] is True
        assert data["deleted"] == "example-org/my-app"

    def test_delete_project_not_found(self, tmp_config):
        handler = self._make_handler("DELETE", "/api/projects/nonexistent")

        with mock.patch("scripts.ops.dashboard_server._CONFIG_PATH", tmp_config):
            handler._handle_delete_project("nonexistent")

        handler.send_response.assert_called_with(404)


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
            args = parser.parse_args(["repo", "configure", "example-org/my-app"])
            result = cmd_repo_configure(args)

        assert result == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["name"] == "example-org/my-app"

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
