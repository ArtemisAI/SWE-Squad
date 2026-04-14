"""Unit tests for src/swe_team/webui/agents_api.py."""
from __future__ import annotations

import json
import os
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.webui.agents_api import (
    Agent,
    AgentKey,
    AgentRun,
    AgentStats,
    Model,
    _add_run_for_agent,
    _agent_stats,
    _agent_runs,
    _AGENT_ENV_TEST_RE,
    _AGENT_KEYS_RE,
    _AGENT_RUNS_RE,
    _AGENT_STATS_RE,
    _AGENTS_RE,
    _error_response,
    _get_agent_config,
    _get_runs_for_agent,
    _handle_create_agent,
    _handle_delete_agent,
    _handle_environment_test,
    _handle_get_agent,
    _handle_get_keys,
    _handle_get_runs,
    _handle_get_stats,
    _handle_list_agents,
    _handle_list_models,
    _handle_update_agent,
    _json_response,
    _update_agent_stats,
    handle_delete,
    handle_get,
    handle_post,
    handle_put,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

    _headers = {"Content-Length": str(len(body))}
    if headers:
        _headers.update(headers)
    handler.headers = MagicMock()
    handler.headers.get = lambda k, d=None: _headers.get(k, d)

    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    return handler


def _json_body(data: dict | list) -> bytes:
    return json.dumps(data).encode("utf-8")


def _read_wfile(handler: MagicMock) -> dict | list:
    """Read and decode JSON written to handler.wfile."""
    handler.wfile.seek(0)
    return json.loads(handler.wfile.read().decode("utf-8"))


def _clear_agent_state() -> None:
    """Clear global agent state between tests."""
    _agent_runs.clear()
    _agent_stats.clear()


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------


class TestRegexPatterns:
    def test_agents_re_matches_agent_path(self) -> None:
        match = _AGENTS_RE.match("/api/agents/swe_monitor")
        assert match is not None
        assert match.group(1) == "swe_monitor"

    def test_agents_re_does_not_match_list_path(self) -> None:
        match = _AGENTS_RE.match("/api/agents")
        assert match is None

    def test_agent_runs_re_matches_runs_path(self) -> None:
        match = _AGENT_RUNS_RE.match("/api/agents/swe_monitor/runs")
        assert match is not None
        assert match.group(1) == "swe_monitor"

    def test_agent_stats_re_matches_stats_path(self) -> None:
        match = _AGENT_STATS_RE.match("/api/agents/swe_monitor/stats")
        assert match is not None
        assert match.group(1) == "swe_monitor"

    def test_agent_keys_re_matches_keys_path(self) -> None:
        match = _AGENT_KEYS_RE.match("/api/agents/swe_monitor/keys")
        assert match is not None
        assert match.group(1) == "swe_monitor"

    def test_agent_env_test_re_matches_env_test_path(self) -> None:
        match = _AGENT_ENV_TEST_RE.match("/api/agents/swe_monitor/environment-test")
        assert match is not None
        assert match.group(1) == "swe_monitor"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


class TestAgent:
    def test_agent_to_dict(self) -> None:
        agent = Agent(
            name="test_agent",
            role="investigator",
            description="Test agent",
            model="sonnet",
            tools=["log_scanner"],
            max_concurrent_tasks=2,
            enabled=True,
            node="primary",
        )
        data = agent.to_dict()
        assert data["name"] == "test_agent"
        assert data["role"] == "investigator"
        assert data["description"] == "Test agent"
        assert data["model"] == "sonnet"
        assert data["tools"] == ["log_scanner"]
        assert data["max_concurrent_tasks"] == 2
        assert data["enabled"] is True
        assert data["node"] == "primary"


class TestAgentRun:
    def test_agent_run_to_dict(self) -> None:
        run = AgentRun(
            run_id="run-123",
            agent_name="swe_monitor",
            ticket_id="ticket-456",
            status="completed",
            started_at="2024-01-01T00:00:00Z",
            ended_at="2024-01-01T01:00:00Z",
            duration_seconds=3600.0,
            result="Success",
        )
        data = run.to_dict()
        assert data["run_id"] == "run-123"
        assert data["agent_name"] == "swe_monitor"
        assert data["ticket_id"] == "ticket-456"
        assert data["status"] == "completed"
        assert data["started_at"] == "2024-01-01T00:00:00Z"
        assert data["ended_at"] == "2024-01-01T01:00:00Z"
        assert data["duration_seconds"] == 3600.0
        assert data["result"] == "Success"


class TestAgentStats:
    def test_agent_stats_to_dict(self) -> None:
        stats = AgentStats(
            agent_name="swe_monitor",
            total_runs=100,
            successful_runs=90,
            failed_runs=10,
            avg_duration_seconds=1800.0,
            success_rate=90.0,
            last_run="2024-01-01T00:00:00Z",
            last_24h_runs=5,
        )
        data = stats.to_dict()
        assert data["agent_name"] == "swe_monitor"
        assert data["total_runs"] == 100
        assert data["successful_runs"] == 90
        assert data["failed_runs"] == 10
        assert data["avg_duration_seconds"] == 1800.0
        assert data["success_rate"] == 90.0
        assert data["last_run"] == "2024-01-01T00:00:00Z"
        assert data["last_24h_runs"] == 5


class TestAgentKey:
    def test_agent_key_to_dict(self) -> None:
        key = AgentKey(
            name="API_KEY",
            value_preview="sk-***1234",
            masked=False,
            last_updated="2024-01-01T00:00:00Z",
        )
        data = key.to_dict()
        assert data["name"] == "API_KEY"
        assert data["value_preview"] == "sk-***1234"
        assert data["masked"] is False
        assert data["last_updated"] == "2024-01-01T00:00:00Z"


class TestModel:
    def test_model_to_dict(self) -> None:
        model = Model(
            name="sonnet",
            tier="t2",
            description="Standard tier model",
            provider="anthropic",
        )
        data = model.to_dict()
        assert data["name"] == "sonnet"
        assert data["tier"] == "t2"
        assert data["description"] == "Standard tier model"
        assert data["provider"] == "anthropic"


# ---------------------------------------------------------------------------
# Response helpers
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

    def test_error_response_sends_custom_status(self) -> None:
        handler = _make_handler()
        handler.wfile = BytesIO()
        _error_response(handler, "not found", 404)
        handler.send_response.assert_called_with(404)

    def test_error_response_wraps_in_error_key(self) -> None:
        handler = _make_handler()
        handler.wfile = BytesIO()
        _error_response(handler, "something went wrong", 500)
        data = _read_wfile(handler)
        assert data["error"] == "something went wrong"


# ---------------------------------------------------------------------------
# handle_get
# ---------------------------------------------------------------------------


class TestHandleGet:
    def test_list_agents(self) -> None:
        handler = _make_handler(path="/api/agents")
        handler.wfile = BytesIO()
        result = handle_get(handler)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert isinstance(data, list)

    def test_list_models(self) -> None:
        handler = _make_handler(path="/api/agents/models")
        handler.wfile = BytesIO()
        result = handle_get(handler)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert isinstance(data, list)
        assert len(data) >= 3  # opus, sonnet, haiku
        model_names = {m["name"] for m in data}
        assert "opus" in model_names
        assert "sonnet" in model_names
        assert "haiku" in model_names

    def test_get_agent_by_name(self) -> None:
        # swe_monitor is defined in the default config
        handler = _make_handler(path="/api/agents/swe_monitor")
        handler.wfile = BytesIO()
        result = handle_get(handler)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert isinstance(data, dict)
        assert data["name"] == "swe_monitor"
        assert "role" in data

    def test_get_agent_not_found(self) -> None:
        handler = _make_handler(path="/api/agents/nonexistent_agent")
        handler.wfile = BytesIO()
        result = handle_get(handler)
        assert result is True
        handler.send_response.assert_called_with(404)

        data = _read_wfile(handler)
        assert "error" in data

    def test_get_agent_runs(self) -> None:
        _clear_agent_state()
        handler = _make_handler(path="/api/agents/swe_monitor/runs")
        handler.wfile = BytesIO()
        result = handle_get(handler)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert isinstance(data, list)

    def test_get_agent_stats(self) -> None:
        _clear_agent_state()
        handler = _make_handler(path="/api/agents/swe_monitor/stats")
        handler.wfile = BytesIO()
        result = handle_get(handler)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert isinstance(data, dict)
        assert data["agent_name"] == "swe_monitor"
        assert "total_runs" in data
        assert "success_rate" in data

    def test_get_agent_keys(self) -> None:
        handler = _make_handler(path="/api/agents/swe_monitor/keys")
        handler.wfile = BytesIO()
        result = handle_get(handler)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert isinstance(data, list)

    def test_unrecognised_path_returns_false(self) -> None:
        handler = _make_handler(path="/api/unknown/path")
        result = handle_get(handler)
        assert result is False


# ---------------------------------------------------------------------------
# handle_post
# ---------------------------------------------------------------------------


class TestHandlePost:
    def test_create_agent_success(self) -> None:
        payload = {
            "name": "new_agent",
            "role": "investigator",
            "description": "A test agent",
            "model": "sonnet",
            "enabled": True,
        }
        handler = _make_handler(
            path="/api/agents",
            body=_json_body(payload),
            method="POST",
        )
        handler.wfile = BytesIO()
        result = handle_post(handler)
        assert result is True
        handler.send_response.assert_called_with(201)

        data = _read_wfile(handler)
        assert data["name"] == "new_agent"
        assert data["role"] == "investigator"
        assert data["enabled"] is True

    def test_create_agent_missing_name(self) -> None:
        payload = {"role": "investigator"}
        handler = _make_handler(
            path="/api/agents",
            body=_json_body(payload),
            method="POST",
        )
        handler.wfile = BytesIO()
        result = handle_post(handler)
        assert result is True
        handler.send_response.assert_called_with(400)

        data = _read_wfile(handler)
        assert "Missing required field: name" in data["error"]

    def test_create_agent_missing_role(self) -> None:
        payload = {"name": "test_agent"}
        handler = _make_handler(
            path="/api/agents",
            body=_json_body(payload),
            method="POST",
        )
        handler.wfile = BytesIO()
        result = handle_post(handler)
        assert result is True
        handler.send_response.assert_called_with(400)

        data = _read_wfile(handler)
        assert "Missing required field: role" in data["error"]

    def test_create_agent_invalid_role(self) -> None:
        payload = {"name": "test", "role": "invalid_role"}
        handler = _make_handler(
            path="/api/agents",
            body=_json_body(payload),
            method="POST",
        )
        handler.wfile = BytesIO()
        result = handle_post(handler)
        assert result is True
        handler.send_response.assert_called_with(400)

        data = _read_wfile(handler)
        assert "Invalid role" in data["error"]

    def test_create_agent_invalid_json(self) -> None:
        handler = _make_handler(
            path="/api/agents",
            body=b"not json",
            method="POST",
        )
        handler.wfile = BytesIO()
        result = handle_post(handler)
        assert result is True
        handler.send_response.assert_called_with(400)

    def test_environment_test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SWE_TEAM_ID", "test-team")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://test")
        payload = {"model": "sonnet"}
        handler = _make_handler(
            path="/api/agents/swe_monitor/environment-test",
            body=_json_body(payload),
            method="POST",
        )
        handler.wfile = BytesIO()
        result = handle_post(handler)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert data["ok"] is True
        assert "message" in data

    def test_environment_test_agent_not_found(self) -> None:
        payload = {}
        handler = _make_handler(
            path="/api/agents/nonexistent/environment-test",
            body=_json_body(payload),
            method="POST",
        )
        handler.wfile = BytesIO()
        result = handle_post(handler)
        assert result is True
        handler.send_response.assert_called_with(404)

    def test_environment_test_missing_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SWE_TEAM_ID", raising=False)
        payload = {}
        handler = _make_handler(
            path="/api/agents/swe_monitor/environment-test",
            body=_json_body(payload),
            method="POST",
        )
        handler.wfile = BytesIO()
        result = handle_post(handler)
        assert result is True
        handler.send_response.assert_called_with(503)

        data = _read_wfile(handler)
        assert "Missing environment variables" in data["error"]

    def test_unrecognised_path_returns_false(self) -> None:
        handler = _make_handler(path="/api/agents/unknown/endpoint", method="POST")
        result = handle_post(handler)
        assert result is False


# ---------------------------------------------------------------------------
# handle_put
# ---------------------------------------------------------------------------


class TestHandlePut:
    def test_update_agent_success(self) -> None:
        payload = {"description": "Updated description", "enabled": False}
        handler = _make_handler(
            path="/api/agents/swe_monitor",
            body=_json_body(payload),
            method="PUT",
        )
        handler.wfile = BytesIO()
        result = handle_put(handler)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert data["name"] == "swe_monitor"
        assert data["description"] == "Updated description"
        assert data["enabled"] is False

    def test_update_agent_not_found(self) -> None:
        payload = {"description": "Test"}
        handler = _make_handler(
            path="/api/agents/nonexistent",
            body=_json_body(payload),
            method="PUT",
        )
        handler.wfile = BytesIO()
        result = handle_put(handler)
        assert result is True
        handler.send_response.assert_called_with(404)

    def test_update_agent_invalid_json(self) -> None:
        handler = _make_handler(
            path="/api/agents/swe_monitor",
            body=b"bad json",
            method="PUT",
        )
        handler.wfile = BytesIO()
        result = handle_put(handler)
        assert result is True
        handler.send_response.assert_called_with(400)

    def test_unrecognised_path_returns_false(self) -> None:
        handler = _make_handler(path="/api/unknown", method="PUT")
        result = handle_put(handler)
        assert result is False


# ---------------------------------------------------------------------------
# handle_delete
# ---------------------------------------------------------------------------


class TestHandleDelete:
    def test_delete_agent_success(self) -> None:
        handler = _make_handler(
            path="/api/agents/swe_monitor",
            method="DELETE",
        )
        handler.wfile = BytesIO()
        result = handle_delete(handler)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert data["ok"] is True

    def test_delete_agent_not_found(self) -> None:
        handler = _make_handler(
            path="/api/agents/nonexistent",
            method="DELETE",
        )
        handler.wfile = BytesIO()
        result = handle_delete(handler)
        assert result is True
        handler.send_response.assert_called_with(404)

    def test_unrecognised_path_returns_false(self) -> None:
        handler = _make_handler(path="/api/agents", method="DELETE")
        result = handle_delete(handler)
        assert result is False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestInternalHelpers:
    def test_get_runs_for_agent_empty(self) -> None:
        _clear_agent_state()
        runs = _get_runs_for_agent("test_agent")
        assert runs == []

    def test_add_and_get_runs(self) -> None:
        _clear_agent_state()
        run = AgentRun(
            run_id="run-1",
            agent_name="test_agent",
            status="completed",
            started_at="2024-01-01T00:00:00Z",
        )
        _add_run_for_agent(run)

        runs = _get_runs_for_agent("test_agent")
        assert len(runs) == 1
        assert runs[0].run_id == "run-1"

    def test_update_agent_stats_empty(self) -> None:
        _clear_agent_state()
        _update_agent_stats("test_agent")
        stats = _agent_stats.get("test_agent")
        assert stats is not None
        assert stats.total_runs == 0
        assert stats.success_rate == 0.0

    def test_update_agent_stats_with_runs(self) -> None:
        _clear_agent_state()
        now = "2024-01-01T12:00:00Z"

        # Add some runs
        _add_run_for_agent(
            AgentRun(
                run_id="run-1",
                agent_name="test_agent",
                status="completed",
                started_at=now,
                duration_seconds=100.0,
            )
        )
        _add_run_for_agent(
            AgentRun(
                run_id="run-2",
                agent_name="test_agent",
                status="failed",
                started_at=now,
                duration_seconds=200.0,
            )
        )
        _add_run_for_agent(
            AgentRun(
                run_id="run-3",
                agent_name="test_agent",
                status="completed",
                started_at=now,
                duration_seconds=300.0,
            )
        )

        stats = _agent_stats.get("test_agent")
        assert stats is not None
        assert stats.total_runs == 3
        assert stats.successful_runs == 2
        assert stats.failed_runs == 1
        assert stats.avg_duration_seconds == 200.0
        assert stats.success_rate == pytest.approx(66.67, rel=0.01)


# ---------------------------------------------------------------------------
# Direct handler tests
# ---------------------------------------------------------------------------


class TestDirectHandlers:
    def test_handle_list_agents_returns_list(self) -> None:
        handler = _make_handler(path="/api/agents")
        handler.wfile = BytesIO()
        _handle_list_agents(handler)
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert isinstance(data, list)

    def test_handle_list_models_returns_models(self) -> None:
        handler = _make_handler(path="/api/agents/models")
        handler.wfile = BytesIO()
        _handle_list_models(handler)
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert isinstance(data, list)
        model_names = [m["name"] for m in data]
        assert "opus" in model_names
        assert "sonnet" in model_names
        assert "haiku" in model_names

    def test_handle_get_agent_found(self) -> None:
        handler = _make_handler(path="/api/agents/swe_monitor")
        handler.wfile = BytesIO()
        _handle_get_agent(handler, "swe_monitor")
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert data["name"] == "swe_monitor"

    def test_handle_get_agent_not_found(self) -> None:
        handler = _make_handler(path="/api/agents/unknown")
        handler.wfile = BytesIO()
        _handle_get_agent(handler, "unknown")
        handler.send_response.assert_called_with(404)

    def test_handle_get_runs_empty(self) -> None:
        _clear_agent_state()
        with patch("src.swe_team.webui.agents_api._get_agent_config") as mock_get:
            # Mock a valid agent config
            mock_get.return_value = MagicMock(
                name="test",
                role=MagicMock(value="test_role"),
            )
            handler = _make_handler(path="/api/agents/test/runs")
            handler.wfile = BytesIO()
            _handle_get_runs(handler, "test")
            handler.send_response.assert_called_with(200)

            data = _read_wfile(handler)
            assert data == []

    def test_handle_get_runs_with_data(self) -> None:
        _clear_agent_state()
        run = AgentRun(
            run_id="run-123",
            agent_name="test",
            status="completed",
            started_at="2024-01-01T00:00:00Z",
        )
        _add_run_for_agent(run)

        with patch("src.swe_team.webui.agents_api._get_agent_config") as mock_get:
            # Mock a valid agent config
            mock_get.return_value = MagicMock(
                name="test",
                role=MagicMock(value="test_role"),
            )
            handler = _make_handler(path="/api/agents/test/runs")
            handler.wfile = BytesIO()
            _handle_get_runs(handler, "test")
            handler.send_response.assert_called_with(200)

            data = _read_wfile(handler)
            assert len(data) == 1
            assert data[0]["run_id"] == "run-123"

    def test_handle_get_stats_empty(self) -> None:
        _clear_agent_state()
        with patch("src.swe_team.webui.agents_api._get_agent_config") as mock_get:
            # Mock a valid agent config
            mock_get.return_value = MagicMock(
                name="test",
                role=MagicMock(value="test_role"),
            )
            handler = _make_handler(path="/api/agents/test/stats")
            handler.wfile = BytesIO()
            _handle_get_stats(handler, "test")
            handler.send_response.assert_called_with(200)

            data = _read_wfile(handler)
            assert data["total_runs"] == 0
            assert data["success_rate"] == 0.0

    def test_handle_get_stats_with_runs(self) -> None:
        _clear_agent_state()
        run = AgentRun(
            run_id="run-123",
            agent_name="test",
            status="completed",
            started_at="2024-01-01T12:00:00Z",
        )
        _add_run_for_agent(run)

        with patch("src.swe_team.webui.agents_api._get_agent_config") as mock_get:
            # Mock a valid agent config
            mock_get.return_value = MagicMock(
                name="test",
                role=MagicMock(value="test_role"),
            )
            handler = _make_handler(path="/api/agents/test/stats")
            handler.wfile = BytesIO()
            _handle_get_stats(handler, "test")
            handler.send_response.assert_called_with(200)

            data = _read_wfile(handler)
            assert data["total_runs"] == 1
            assert data["successful_runs"] == 1
            assert data["success_rate"] == 100.0

    def test_handle_create_agent_missing_name(self) -> None:
        payload = {"role": "investigator"}
        handler = _make_handler(
            path="/api/agents",
            body=_json_body(payload),
            method="POST",
        )
        handler.wfile = BytesIO()
        _handle_create_agent(handler)
        handler.send_response.assert_called_with(400)

    def test_handle_create_agent_missing_role(self) -> None:
        payload = {"name": "test"}
        handler = _make_handler(
            path="/api/agents",
            body=_json_body(payload),
            method="POST",
        )
        handler.wfile = BytesIO()
        _handle_create_agent(handler)
        handler.send_response.assert_called_with(400)

    def test_handle_update_agent_success(self) -> None:
        payload = {"description": "Updated"}
        handler = _make_handler(
            path="/api/agents/swe_monitor",
            body=_json_body(payload),
            method="PUT",
        )
        handler.wfile = BytesIO()
        _handle_update_agent(handler, "swe_monitor")
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert data["description"] == "Updated"

    def test_handle_update_agent_not_found(self) -> None:
        payload = {"description": "Test"}
        handler = _make_handler(
            path="/api/agents/unknown",
            body=_json_body(payload),
            method="PUT",
        )
        handler.wfile = BytesIO()
        _handle_update_agent(handler, "unknown")
        handler.send_response.assert_called_with(404)

    def test_handle_delete_agent_success(self) -> None:
        handler = _make_handler(path="/api/agents/swe_monitor", method="DELETE")
        handler.wfile = BytesIO()
        _handle_delete_agent(handler, "swe_monitor")
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert data["ok"] is True

    def test_handle_delete_agent_not_found(self) -> None:
        handler = _make_handler(path="/api/agents/unknown", method="DELETE")
        handler.wfile = BytesIO()
        _handle_delete_agent(handler, "unknown")
        handler.send_response.assert_called_with(404)

    def test_handle_get_keys_success(self) -> None:
        handler = _make_handler(path="/api/agents/swe_monitor/keys")
        handler.wfile = BytesIO()
        _handle_get_keys(handler, "swe_monitor")
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert isinstance(data, list)

    def test_handle_get_keys_agent_not_found(self) -> None:
        handler = _make_handler(path="/api/agents/unknown/keys")
        handler.wfile = BytesIO()
        _handle_get_keys(handler, "unknown")
        handler.send_response.assert_called_with(404)

    def test_handle_environment_test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SWE_TEAM_ID", "test-team")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://test")

        payload = {}
        handler = _make_handler(
            path="/api/agents/swe_monitor/environment-test",
            body=_json_body(payload),
            method="POST",
        )
        handler.wfile = BytesIO()
        _handle_environment_test(handler, "swe_monitor")
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert data["ok"] is True

    def test_handle_environment_test_missing_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SWE_TEAM_ID", raising=False)

        payload = {}
        handler = _make_handler(
            path="/api/agents/swe_monitor/environment-test",
            body=_json_body(payload),
            method="POST",
        )
        handler.wfile = BytesIO()
        _handle_environment_test(handler, "swe_monitor")
        handler.send_response.assert_called_with(503)

        data = _read_wfile(handler)
        assert "Missing environment variables" in data["error"]
