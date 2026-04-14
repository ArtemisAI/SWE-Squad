"""Tests for the OpenClaw engine connector.

Based on the engine test template (tests/unit/test_engine_template.py).
Covers protocol compliance, run success/failure, timeout, binary-not-found,
JSON output parsing, safety filter, and registry integration.
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.openclaw import OpenClawEngine

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGINE_MODULE = "src.swe_team.providers.coding_engine.openclaw"
# GenericCLIEngine (parent) uses subprocess from generic_cli module
PARENT_MODULE = "src.swe_team.providers.coding_engine.generic_cli"
ENGINE_NAME = "openclaw"
DEFAULT_MODEL = "kimi-k2.5"
DEFAULT_BINARY = "/usr/local/bin/openclaw"


def _make_engine(**overrides) -> OpenClawEngine:
    """Factory to create a test OpenClawEngine with sensible defaults."""
    defaults = {
        "binary": DEFAULT_BINARY,
        "default_model": DEFAULT_MODEL,
        "default_timeout": 60,
    }
    defaults.update(overrides)
    return OpenClawEngine(**defaults)


# ===========================================================================
# Protocol compliance
# ===========================================================================


class TestProtocolCompliance:
    """The engine must satisfy the CodingEngine protocol at runtime."""

    def test_isinstance_check(self):
        engine = _make_engine()
        assert isinstance(engine, CodingEngine)

    def test_name_returns_string(self):
        engine = _make_engine()
        assert isinstance(engine.name, str)

    def test_name_is_openclaw(self):
        engine = _make_engine()
        assert engine.name == "openclaw"

    def test_name_is_lowercase(self):
        engine = _make_engine()
        assert engine.name == engine.name.lower()


# ===========================================================================
# Run success
# ===========================================================================


class TestRunSuccess:
    """Successful run with mocked subprocess."""

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_run_success_returns_engine_result(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "task completed"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("fix the bug", model=DEFAULT_MODEL, timeout=60)

        assert isinstance(result, EngineResult)
        assert result.success is True
        assert result.returncode == 0

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_run_success_captures_stdout(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "output text here"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.stdout == "output text here"

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_run_uses_model_override(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "ok"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model="custom-model", timeout=60)

        assert result.model == "custom-model"

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_run_uses_timeout_override(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "ok"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine(default_timeout=300)
        engine.run("prompt", model=DEFAULT_MODEL, timeout=30)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 30

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_run_passes_cwd(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "ok"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60, cwd="/tmp/workspace")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == "/tmp/workspace"


# ===========================================================================
# Run failure
# ===========================================================================


class TestRunFailure:
    """Failed run with mocked subprocess."""

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_run_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="error: model not found",
            returncode=1,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.returncode == 1

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_run_failure_captures_stderr(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="fatal error occurred",
            returncode=1,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert "fatal error" in result.stderr.lower()


# ===========================================================================
# Timeout
# ===========================================================================


class TestTimeout:
    """Timeout handling."""

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_timeout_raises_timeout_expired(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=DEFAULT_BINARY, timeout=60,
        )
        engine = _make_engine()

        with pytest.raises(subprocess.TimeoutExpired):
            engine.run("prompt", model=DEFAULT_MODEL, timeout=60)


# ===========================================================================
# Binary not found
# ===========================================================================


class TestBinaryNotFound:
    """Binary not found handling."""

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_binary_not_found_returns_failure(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file or directory")
        engine = _make_engine(binary="/nonexistent/openclaw")
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.returncode == -1
        assert "not found" in result.stderr.lower()


# ===========================================================================
# Health check
# ===========================================================================


class TestHealthCheck:
    """Health check."""

    def test_health_check_returns_bool(self):
        engine = _make_engine()
        assert isinstance(engine.health_check(), bool)

    def test_health_check_with_real_binary(self):
        engine = _make_engine(binary="/bin/sh")
        assert engine.health_check() is True

    def test_health_check_with_missing_binary(self):
        engine = _make_engine(binary="/nonexistent/path/to/openclaw")
        assert engine.health_check() is False


# ===========================================================================
# OpenClaw-specific: JSON output parsing
# ===========================================================================


class TestOpenClawJsonParsing:
    """OpenClaw's JSON output format parsing."""

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_full_json_output(self, mock_run):
        """Full OpenClaw JSON response with usage and session info."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({
                "result": "Fixed the null pointer bug in parser.py",
                "model": "kimi-k2.5",
                "usage": {
                    "input_tokens": 1234,
                    "output_tokens": 567,
                    "cost_usd": 0.01,
                },
                "session_id": "abc-123",
                "status": "success",
            }),
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("fix the bug", model=DEFAULT_MODEL, timeout=60)

        assert result.stdout == "Fixed the null pointer bug in parser.py"
        assert result.model == "kimi-k2.5"
        assert result.cost_usd == 0.01
        assert result.input_tokens == 1234
        assert result.output_tokens == 567
        assert result.session_id == "abc-123"

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_minimal_json_output(self, mock_run):
        """JSON with only the result field."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "done"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.stdout == "done"
        assert result.cost_usd is None
        assert result.session_id is None

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_invalid_json_fallback(self, mock_run):
        """Non-JSON output falls back to raw text."""
        mock_run.return_value = MagicMock(
            stdout="not valid json at all",
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert "not valid json" in result.stdout

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_empty_output(self, mock_run):
        """Empty stdout returns empty result."""
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.stdout == ""
        assert result.success is True

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_json_with_non_string_result(self, mock_run):
        """Non-string result field gets stringified."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": 42, "status": "success"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.stdout == "42"

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_json_with_non_dict_usage(self, mock_run):
        """Non-dict usage field is ignored gracefully."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({
                "result": "ok",
                "usage": "invalid",
            }),
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.stdout == "ok"
        assert result.cost_usd is None


# ===========================================================================
# OpenClaw-specific: Safety filter
# ===========================================================================


class TestSafetyFilter:
    """Safety filter blocks sensitive keywords before sending to OpenClaw."""

    def test_blocks_password(self):
        engine = _make_engine()
        result = engine.run("my password is hunter2", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.returncode == -1
        assert "blocked" in result.stderr.lower()
        assert result.metadata.get("error_type") == "safety_block"
        assert result.metadata.get("blocked_keyword") == "password"

    def test_blocks_api_key(self):
        engine = _make_engine()
        result = engine.run("set the api_key to abc123", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.metadata.get("blocked_keyword") == "api_key"

    def test_blocks_secret(self):
        engine = _make_engine()
        result = engine.run("the secret value is XYZ", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.metadata.get("blocked_keyword") == "secret"

    def test_blocks_private_key(self):
        engine = _make_engine()
        result = engine.run("load the private_key from disk", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.metadata.get("blocked_keyword") == "private_key"

    def test_blocks_credential(self):
        engine = _make_engine()
        result = engine.run("fetch the credential from vault", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.metadata.get("blocked_keyword") == "credential"

    def test_blocks_token(self):
        engine = _make_engine()
        result = engine.run("use this token to authenticate", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.metadata.get("blocked_keyword") == "token"

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_allows_clean_prompt(self, mock_run):
        """Prompts without sensitive keywords pass through."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "ok"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("fix the null pointer bug", model=DEFAULT_MODEL, timeout=60)

        assert result.success is True
        mock_run.assert_called_once()

    def test_case_insensitive_blocking(self):
        engine = _make_engine()
        result = engine.run("my PASSWORD is hunter2", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.metadata.get("blocked_keyword") == "password"


# ===========================================================================
# Gateway mode
# ===========================================================================


class TestGatewayMode:
    """Gateway WebSocket mode (via subprocess to openclaw gateway)."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_gateway_mode_uses_gateway_command(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "gateway response"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine(gateway_url="ws://localhost:8765")
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.success is True
        assert result.stdout == "gateway response"
        cmd = mock_run.call_args[0][0]
        assert "gateway" in cmd
        assert "ws://localhost:8765" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_gateway_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="openclaw", timeout=60,
        )
        engine = _make_engine(gateway_url="ws://localhost:8765")

        with pytest.raises(subprocess.TimeoutExpired):
            engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_gateway_binary_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")
        engine = _make_engine(
            binary="/nonexistent/openclaw",
            gateway_url="ws://localhost:8765",
        )
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.returncode == -1
        assert "not found" in result.stderr.lower()


# ===========================================================================
# Defaults
# ===========================================================================


class TestDefaults:
    """Constructor defaults."""

    def test_default_model(self):
        engine = _make_engine()
        assert engine.model() == "kimi-k2.5"

    def test_custom_model(self):
        engine = _make_engine(default_model="gpt-4o")
        assert engine.model() == "gpt-4o"

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_default_model_used_when_no_override(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "ok"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine(default_model="kimi-k2.5")
        result = engine.run("prompt", model=None, timeout=60)

        assert result.model == "kimi-k2.5"


# ===========================================================================
# Command building
# ===========================================================================


class TestCommandBuilding:
    """Verify the subprocess command is built correctly for OpenClaw."""

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_binary_is_first_arg(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "ok"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine(binary="/usr/local/bin/openclaw")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/openclaw"

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_run_subcommand_in_args(self, mock_run):
        """OpenClaw uses 'run' subcommand."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "ok"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "run" in cmd

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_output_json_flag(self, mock_run):
        """OpenClaw includes --output json flag."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "ok"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--output" in cmd
        json_idx = cmd.index("--output")
        assert cmd[json_idx + 1] == "json"

    @patch(f"{PARENT_MODULE}.subprocess.run")
    def test_model_flag_in_command(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "ok"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model="kimi-k2.5", timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        assert "kimi-k2.5" in cmd


# ===========================================================================
# Registry integration
# ===========================================================================


class TestRegistryIntegration:
    """Verify OpenClaw is registered in the provider registry."""

    def test_openclaw_in_registry(self):
        from src.swe_team.providers.coding_engine import list_engines
        engines = list_engines()
        assert "openclaw" in engines

    def test_openclaw_resolves(self):
        from src.swe_team.providers.coding_engine import resolve_engine
        engine = resolve_engine("openclaw", {"timeout_seconds": 60})
        assert engine.name == "openclaw"

    def test_openclaw_resolves_with_gateway(self):
        from src.swe_team.providers.coding_engine import resolve_engine
        engine = resolve_engine("openclaw", {
            "timeout_seconds": 60,
            "gateway_url": "ws://localhost:8765",
        })
        assert engine.name == "openclaw"
