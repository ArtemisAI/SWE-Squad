from __future__ import annotations

import json
from io import BytesIO
from unittest import mock


def _make_handler(body: dict | None = None):
    from scripts.ops.dashboard_server import DashboardHandler

    request_body = json.dumps(body or {}).encode()
    handler = DashboardHandler.__new__(DashboardHandler)
    handler.headers = {"Content-Length": str(len(request_body))}
    handler.rfile = BytesIO(request_body)
    handler.wfile = BytesIO()
    handler.send_response = mock.MagicMock()
    handler.send_header = mock.MagicMock()
    handler.end_headers = mock.MagicMock()
    handler._read_post_body = lambda **kw: DashboardHandler._read_post_body(handler, **kw)
    handler._json_response = lambda data, status=200, **kw: DashboardHandler._json_response(
        handler, data, status, **kw
    )
    return handler


class TestEngineInstallCommands:
    def test_copilot_install_command_uses_standalone_cli_package(self):
        from scripts.ops.dashboard_server import DashboardHandler

        assert DashboardHandler._ENGINE_INSTALL_COMMANDS["copilot"] == "gh extension install github/gh-copilot"

    def test_expected_engines_have_install_commands(self):
        from scripts.ops.dashboard_server import DashboardHandler

        for engine in ("claude", "codex", "gemini", "aider", "opencode", "copilot"):
            assert engine in DashboardHandler._ENGINE_INSTALL_COMMANDS


class TestEngineInstallEndpoint:
    @mock.patch("subprocess.run")
    def test_install_response_includes_logs_and_execution_context(self, mock_run):
        from scripts.ops.dashboard_server import DashboardHandler, PROJECT_ROOT

        mock_run.return_value = mock.MagicMock(returncode=0, stdout="installed", stderr="")
        handler = _make_handler({"engine": "copilot"})

        DashboardHandler._handle_post_engine_install(handler)

        handler.send_response.assert_called_with(200)
        payload = json.loads(handler.wfile.getvalue())
        assert payload["ok"] is True
        assert payload["engine"] == "copilot"
        assert payload["command"] == "gh extension install github/gh-copilot"
        assert payload["working_directory"] == str(PROJECT_ROOT)
        assert payload["stdout"] == "installed"
        assert "duration_seconds" in payload

    def test_unknown_engine_returns_422(self):
        from scripts.ops.dashboard_server import DashboardHandler

        handler = _make_handler({"engine": "not-real"})
        DashboardHandler._handle_post_engine_install(handler)

        handler.send_response.assert_called_with(422)
        payload = json.loads(handler.wfile.getvalue())
        assert "No install command known for engine" in payload["error"]
