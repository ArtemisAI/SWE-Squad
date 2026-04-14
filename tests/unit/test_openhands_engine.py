"""
Tests for the OpenHands coding engine connector.

Covers all mandatory EngineTestSuite tests (protocol compliance, identity,
run success/failure, timeout, health check, availability, defaults) plus
OpenHands-specific tests for API request building and error handling.
"""
from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.openhands import OpenHandsEngine

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGINE_MODULE = "src.swe_team.providers.coding_engine.openhands"

ENGINE_NAME = "openhands"
DEFAULT_API_URL = "http://localhost:3000"
DEFAULT_API_KEY = "test-key-123"


def _make_engine(**overrides) -> OpenHandsEngine:
    """Factory to create an OpenHandsEngine with sensible defaults."""
    defaults = {
        "api_url": DEFAULT_API_URL,
        "api_key": DEFAULT_API_KEY,
        "default_model": "",
        "default_timeout": 60,
    }
    defaults.update(overrides)
    return OpenHandsEngine(**defaults)


def _mock_response(body: dict, status: int = 200):
    """Create a mock HTTP response."""
    data = json.dumps(body).encode("utf-8")
    mock = MagicMock()
    mock.read.return_value = data
    mock.status = status
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


# ===========================================================================
# EngineTestSuite: mandatory tests for every engine connector
# ===========================================================================


class TestEngineProtocolCompliance:
    """6.1: The engine must satisfy the CodingEngine protocol at runtime."""

    def test_isinstance_check(self):
        engine = _make_engine()
        assert isinstance(engine, CodingEngine)


class TestEngineIdentity:
    """6.2: The name property must return a non-empty string."""

    def test_name_returns_string(self):
        engine = _make_engine()
        assert isinstance(engine.name, str)

    def test_name_not_empty(self):
        engine = _make_engine()
        assert len(engine.name) > 0

    def test_name_is_lowercase(self):
        engine = _make_engine()
        assert engine.name == engine.name.lower()

    def test_name_is_openhands(self):
        engine = _make_engine()
        assert engine.name == "openhands"


class TestEngineRunSuccess:
    """6.3: Successful run with mocked HTTP."""

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_run_success_returns_engine_result(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"result": "task completed"})
        engine = _make_engine()
        result = engine.run("fix the bug", model="", timeout=60)

        assert isinstance(result, EngineResult)
        assert result.success is True
        assert result.returncode == 0

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_run_success_captures_result(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"result": "output text here"})
        engine = _make_engine()
        result = engine.run("prompt", model="", timeout=60)

        assert result.stdout == "output text here"

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_run_uses_output_fallback(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"output": "fallback output"})
        engine = _make_engine()
        result = engine.run("prompt", model="", timeout=60)

        assert result.stdout == "fallback output"

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_run_captures_session_id(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({
            "result": "ok", "session_id": "sess-123",
        })
        engine = _make_engine()
        result = engine.run("prompt", model="", timeout=60)

        assert result.session_id == "sess-123"

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_run_captures_cost(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({
            "result": "ok", "cost": 0.05,
        })
        engine = _make_engine()
        result = engine.run("prompt", model="", timeout=60)

        assert result.cost_usd == 0.05

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_run_passes_cwd_in_payload(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"result": "ok"})
        engine = _make_engine()
        engine.run("prompt", model="", timeout=60, cwd="/tmp/workspace")

        # Verify the request was made (URL contains /api/submit)
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert body["cwd"] == "/tmp/workspace"


class TestEngineRunFailure:
    """6.4: Failed run with HTTP errors."""

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_run_http_error(self, mock_urlopen):
        error = urllib.error.HTTPError(
            url="http://localhost:3000/api/submit",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=BytesIO(b"server error"),
        )
        mock_urlopen.side_effect = error
        engine = _make_engine()
        result = engine.run("prompt", model="", timeout=60)

        assert result.success is False
        assert result.returncode == -1
        assert "500" in result.stderr

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_run_connection_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        engine = _make_engine()
        result = engine.run("prompt", model="", timeout=60)

        assert result.success is False
        assert result.returncode == -1
        assert "connection" in result.stderr.lower()


class TestEngineTimeout:
    """6.5: Timeout handling."""

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_timeout_returns_failure(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("timed out")
        engine = _make_engine()
        result = engine.run("prompt", model="", timeout=1)

        assert result.success is False
        assert result.returncode == -1


class TestEngineHealthCheck:
    """6.7: Health check."""

    def test_health_check_returns_bool(self):
        engine = _make_engine()
        assert isinstance(engine.health_check(), bool)

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_health_check_success(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({}, status=200)
        engine = _make_engine()
        assert engine.health_check() is True

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_health_check_failure(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        engine = _make_engine()
        assert engine.health_check() is False


class TestEngineAvailability:
    """6.8: Availability check."""

    def test_is_available_returns_bool(self):
        engine = _make_engine()
        assert isinstance(engine.is_available(), bool)

    def test_is_available_true_with_api_url(self):
        engine = _make_engine(api_url="http://localhost:3000")
        assert engine.is_available() is True

    def test_is_available_false_with_empty_url(self):
        engine = _make_engine(api_url="")
        assert engine.is_available() is False


# ===========================================================================
# Model and defaults
# ===========================================================================


class TestEngineDefaults:
    """Verify constructor defaults are applied correctly."""

    def test_default_model_is_empty(self):
        engine = _make_engine()
        assert engine.model() == ""

    def test_custom_default_model(self):
        engine = _make_engine(default_model="gpt-4o")
        assert engine.model() == "gpt-4o"

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_default_model_used_when_no_override(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"result": "ok"})
        engine = _make_engine(default_model="custom-model")
        result = engine.run("prompt", model=None, timeout=60)

        assert result.model == "custom-model"

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_default_timeout_used(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"result": "ok"})
        engine = _make_engine(default_timeout=120)
        engine.run("prompt", model="", timeout=None)

        call_kwargs = mock_urlopen.call_args[1]
        assert call_kwargs["timeout"] == 120


# ===========================================================================
# API request building -- OpenHands-specific
# ===========================================================================


class TestOpenHandsRequestBuilding:
    """Verify the HTTP request is built correctly."""

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_request_url(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"result": "ok"})
        engine = _make_engine(api_url="http://myhost:3000")
        engine.run("prompt", model="", timeout=60)

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://myhost:3000/api/submit"

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_request_method_is_post(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"result": "ok"})
        engine = _make_engine()
        engine.run("prompt", model="", timeout=60)

        req = mock_urlopen.call_args[0][0]
        assert req.method == "POST"

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_request_includes_auth_header(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"result": "ok"})
        engine = _make_engine(api_key="my-secret-key")
        engine.run("prompt", model="", timeout=60)

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer my-secret-key"

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_request_body_contains_prompt(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"result": "ok"})
        engine = _make_engine()
        engine.run("fix the bug", model="", timeout=60)

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert body["prompt"] == "fix the bug"

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_request_body_includes_model_when_set(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"result": "ok"})
        engine = _make_engine()
        engine.run("prompt", model="gpt-4o", timeout=60)

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert body["model"] == "gpt-4o"

    @patch(f"{ENGINE_MODULE}.urllib.request.urlopen")
    def test_trailing_slash_stripped_from_api_url(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response({"result": "ok"})
        engine = _make_engine(api_url="http://localhost:3000/")
        engine.run("prompt", model="", timeout=60)

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://localhost:3000/api/submit"


# ===========================================================================
# Registry integration
# ===========================================================================


class TestRegistryIntegration:
    """Verify the engine integrates with the provider registry."""

    def test_openhands_in_registry(self):
        from src.swe_team.providers.coding_engine import list_engines
        engines = list_engines()
        assert "openhands" in engines

    def test_openhands_resolve(self):
        from src.swe_team.providers.coding_engine import resolve_engine
        engine = resolve_engine("openhands", {"timeout_seconds": 60})
        assert engine.name == "openhands"
