"""Unit tests for src/swe_team/control_plane_api.py — no real network calls."""
from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import src.swe_team.control_plane_api as api_mod
from src.swe_team.control_plane import ControlPlane, PipelineState, ProjectConfig
from src.swe_team.control_plane_api import (
    _error_response,
    _json_response,
    _read_json_body,
    handle_delete,
    handle_get,
    handle_post,
    handle_put,
    set_executor_ref,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_control_plane(tmp_path: Path) -> ControlPlane:
    config_path = tmp_path / "control_plane.yaml"
    queue_path = tmp_path / "queue.json"
    return ControlPlane(
        config_path=str(config_path),
        queue_path=str(queue_path),
    )


def _make_handler(
    method: str = "GET",
    path: str = "/",
    body: bytes = b"",
    headers: dict | None = None,
) -> MagicMock:
    """Build a minimal fake BaseHTTPRequestHandler."""
    handler = MagicMock()
    handler.path = path
    handler.command = method

    # Headers mock: .get(key, default)
    _headers = {"Content-Length": str(len(body))}
    if headers:
        _headers.update(headers)
    handler.headers = MagicMock()
    handler.headers.get = lambda k, d=None: _headers.get(k, d)

    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    # Capture send_response / send_header / end_headers calls
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    return handler


def _json_body(data: Any) -> bytes:
    return json.dumps(data).encode("utf-8")


def _read_wfile(handler: MagicMock) -> Any:
    """Read and decode the JSON written to handler.wfile."""
    handler.wfile.seek(0)
    return json.loads(handler.wfile.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 1. _read_json_body
# ---------------------------------------------------------------------------

class TestReadJsonBody:
    def test_reads_json_body(self) -> None:
        payload = {"title": "urgent issue", "severity": "critical"}
        handler = _make_handler(body=_json_body(payload))
        result = _read_json_body(handler)
        assert result == payload

    def test_empty_body_returns_empty_dict(self) -> None:
        handler = _make_handler(body=b"", headers={"Content-Length": "0"})
        result = _read_json_body(handler)
        assert result == {}


# ---------------------------------------------------------------------------
# 2. _json_response / _error_response
# ---------------------------------------------------------------------------

class TestResponseHelpers:
    def test_json_response_sends_200_by_default(self) -> None:
        handler = _make_handler()
        handler.wfile = BytesIO()
        _json_response(handler, {"ok": True})
        handler.send_response.assert_called_with(200)

    def test_json_response_sends_custom_status(self) -> None:
        handler = _make_handler()
        handler.wfile = BytesIO()
        _json_response(handler, {"created": True}, status=201)
        handler.send_response.assert_called_with(201)

    def test_error_response_sends_400_by_default(self) -> None:
        handler = _make_handler()
        handler.wfile = BytesIO()
        _error_response(handler, "bad request")
        handler.send_response.assert_called_with(400)

    def test_error_response_wraps_in_error_key(self) -> None:
        handler = _make_handler()
        handler.wfile = BytesIO()
        body_written = []
        handler.wfile.write = lambda b: body_written.append(b)
        _error_response(handler, "not found", 404)
        combined = b"".join(body_written)
        data = json.loads(combined.decode())
        assert data["error"] == "not found"


# ---------------------------------------------------------------------------
# 3. handle_get — routing
# ---------------------------------------------------------------------------

class TestHandleGet:
    def test_get_control_status(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(path="/api/control/status")
        handler.wfile = BytesIO()
        result = handle_get(handler, cp)
        assert result is True
        handler.send_response.assert_called_with(200)

    def test_get_config_projects(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(path="/api/config/projects")
        handler.wfile = BytesIO()
        result = handle_get(handler, cp)
        assert result is True

    def test_get_single_project_not_found(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(path="/api/config/projects/nonexistent")
        handler.wfile = BytesIO()
        body_written = []
        handler.wfile.write = lambda b: body_written.append(b)
        result = handle_get(handler, cp)
        assert result is True
        handler.send_response.assert_called_with(404)

    def test_get_queue(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(path="/api/queue")
        handler.wfile = BytesIO()
        result = handle_get(handler, cp)
        assert result is True
        handler.send_response.assert_called_with(200)

    def test_get_execution_status_no_executor(self, tmp_path: Path) -> None:
        set_executor_ref(None)
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(path="/api/execution/status")
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_get(handler, cp)
        assert result is True
        data = json.loads(b"".join(body_parts).decode())
        assert data["mode"] == "sequential"

    def test_get_unrecognised_path_returns_false(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(path="/api/unknown/route")
        result = handle_get(handler, cp)
        assert result is False

    def test_get_single_project_found(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        # Inject a project
        cp.update_project("my-project", {"max_concurrent_agents": 3})
        handler = _make_handler(path="/api/config/projects/my-project")
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_get(handler, cp)
        assert result is True
        data = json.loads(b"".join(body_parts).decode())
        assert "my-project" in data

    def test_get_execution_status_with_executor(self, tmp_path: Path) -> None:
        mock_executor = MagicMock()
        mock_executor.status.return_value = {"mode": "parallel", "workers": 4}
        set_executor_ref(mock_executor)
        try:
            cp = _make_control_plane(tmp_path)
            handler = _make_handler(path="/api/execution/status")
            handler.wfile = BytesIO()
            body_parts = []
            handler.wfile.write = lambda b: body_parts.append(b)
            result = handle_get(handler, cp)
            assert result is True
            data = json.loads(b"".join(body_parts).decode())
            assert data["mode"] == "parallel"
        finally:
            set_executor_ref(None)


# ---------------------------------------------------------------------------
# 4. handle_post — routing
# ---------------------------------------------------------------------------

class TestHandlePost:
    def test_submit_urgent_ticket(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        payload = {"title": "DB down", "severity": "critical"}
        handler = _make_handler(
            path="/api/tickets/urgent",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_post(handler, cp)
        assert result is True
        handler.send_response.assert_called_with(201)

    def test_submit_urgent_missing_title(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        payload = {"severity": "critical"}
        handler = _make_handler(
            path="/api/tickets/urgent",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_post(handler, cp)
        assert result is True
        handler.send_response.assert_called_with(400)

    def test_submit_urgent_invalid_json(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(
            path="/api/tickets/urgent",
            body=b"not json at all!!!",
        )
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_post(handler, cp)
        assert result is True
        handler.send_response.assert_called_with(400)

    def test_pause_pipeline(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(path="/api/control/pause")
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_post(handler, cp)
        assert result is True
        data = json.loads(b"".join(body_parts).decode())
        assert data["status"] == "paused"
        assert cp.is_paused is True

    def test_resume_pipeline(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        cp.pause_pipeline()
        handler = _make_handler(path="/api/control/resume")
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_post(handler, cp)
        assert result is True
        data = json.loads(b"".join(body_parts).decode())
        assert data["status"] == "resumed"
        assert cp.is_paused is False

    def test_promote_ticket_not_found(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(path="/api/queue/nonexistent-id/promote")
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_post(handler, cp)
        assert result is True
        handler.send_response.assert_called_with(404)

    def test_unrecognised_post_path_returns_false(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(path="/api/unknown/action")
        result = handle_post(handler, cp)
        assert result is False


# ---------------------------------------------------------------------------
# 5. handle_put — routing
# ---------------------------------------------------------------------------

class TestHandlePut:
    def test_set_cycle_interval(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        payload = {"cycle_interval_minutes": 30}
        handler = _make_handler(
            path="/api/control/cycle-interval",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_put(handler, cp)
        assert result is True
        handler.send_response.assert_called_with(200)
        state = cp.pipeline_state
        assert state.cycle_interval_minutes == 30

    def test_set_cycle_interval_missing_field(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        payload = {}
        handler = _make_handler(
            path="/api/control/cycle-interval",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_put(handler, cp)
        assert result is True
        handler.send_response.assert_called_with(400)

    def test_set_model_routing(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        payload = {"t2_standard": "haiku", "t3_fast": "haiku"}
        handler = _make_handler(
            path="/api/control/model-routing",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_put(handler, cp)
        assert result is True

    def test_update_project_config_bulk(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        payload = {"my-repo": {"max_concurrent_agents": 5}}
        handler = _make_handler(
            path="/api/config/projects",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_put(handler, cp)
        assert result is True
        data = json.loads(b"".join(body_parts).decode())
        assert "my-repo" in data

    def test_update_single_project_by_name(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        payload = {"priority_weight": 0.9}
        handler = _make_handler(
            path="/api/config/projects/example-org/my-app",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_put(handler, cp)
        assert result is True

    def test_update_ticket_priority_not_found(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        payload = {"priority": 10}
        handler = _make_handler(
            path="/api/queue/nonexistent-id/priority",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_put(handler, cp)
        assert result is True
        handler.send_response.assert_called_with(404)

    def test_unrecognised_put_path_returns_false(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(path="/api/unknown/something")
        result = handle_put(handler, cp)
        assert result is False


# ---------------------------------------------------------------------------
# 6. handle_delete — routing
# ---------------------------------------------------------------------------

class TestHandleDelete:
    def test_delete_ticket_not_found(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(path="/api/queue/nonexistent")
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_delete(handler, cp)
        assert result is True
        handler.send_response.assert_called_with(404)

    def test_delete_ticket_success(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        # First create a ticket
        ticket = cp.submit_urgent_ticket({"title": "to delete"})
        ticket_id = ticket.ticket_id

        handler = _make_handler(path=f"/api/queue/{ticket_id}")
        handler.wfile = BytesIO()
        body_parts = []
        handler.wfile.write = lambda b: body_parts.append(b)
        result = handle_delete(handler, cp)
        assert result is True
        handler.send_response.assert_called_with(200)

    def test_delete_unrecognised_path_returns_false(self, tmp_path: Path) -> None:
        cp = _make_control_plane(tmp_path)
        handler = _make_handler(path="/api/config/something")
        result = handle_delete(handler, cp)
        assert result is False


# ---------------------------------------------------------------------------
# 7. set_executor_ref
# ---------------------------------------------------------------------------

class TestSetExecutorRef:
    def test_set_and_retrieve_executor_ref(self) -> None:
        mock_exec = MagicMock()
        set_executor_ref(mock_exec)
        assert api_mod._executor_ref is mock_exec

    def test_clear_executor_ref(self) -> None:
        set_executor_ref(MagicMock())
        set_executor_ref(None)
        assert api_mod._executor_ref is None
