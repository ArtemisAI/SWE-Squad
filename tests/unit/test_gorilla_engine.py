"""
Tests for the Gorilla CLI coding engine connector.

Covers all mandatory EngineTestSuite tests (protocol compliance, identity,
run success/failure, timeout, binary not found, health check, availability,
defaults) plus Gorilla-specific tests for stdin prompt delivery.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.gorilla import GorillaCLIEngine

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGINE_MODULE = "src.swe_team.providers.coding_engine.generic_cli"

ENGINE_NAME = "gorilla"
DEFAULT_MODEL = ""
DEFAULT_BINARY = "/usr/local/bin/gorilla"


def _make_engine(**overrides) -> GorillaCLIEngine:
    """Factory to create a GorillaCLIEngine with sensible defaults."""
    defaults = {
        "binary": DEFAULT_BINARY,
        "default_model": DEFAULT_MODEL,
        "default_timeout": 60,
    }
    defaults.update(overrides)
    return GorillaCLIEngine(**defaults)


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

    def test_name_is_gorilla(self):
        engine = _make_engine()
        assert engine.name == "gorilla"


class TestEngineRunSuccess:
    """6.3: Successful run with mocked subprocess."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_success_returns_engine_result(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="curl -X GET https://api.example.com/data",
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("how to list EC2 instances", model=DEFAULT_MODEL, timeout=60)

        assert isinstance(result, EngineResult)
        assert result.success is True
        assert result.returncode == 0

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_success_captures_stdout(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="output text here", stderr="", returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert len(result.stdout) > 0

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_uses_timeout_override(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(default_timeout=300)
        engine.run("prompt", model=DEFAULT_MODEL, timeout=30)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 30

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_passes_cwd(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60, cwd="/tmp/workspace")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == "/tmp/workspace"


class TestEngineRunFailure:
    """6.4: Failed run with mocked subprocess."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="", stderr="error occurred", returncode=1,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.returncode == 1

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_failure_captures_stderr(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="", stderr="fatal error occurred", returncode=1,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert "fatal error" in result.stderr.lower()


class TestEngineTimeout:
    """6.5: Timeout handling."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_timeout_raises_timeout_expired(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=DEFAULT_BINARY, timeout=60,
        )
        engine = _make_engine()

        with pytest.raises(subprocess.TimeoutExpired):
            engine.run("prompt", model=DEFAULT_MODEL, timeout=60)


class TestEngineBinaryNotFound:
    """6.6: Binary not found handling."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_binary_not_found_returns_failure(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file or directory")
        engine = _make_engine(binary="/nonexistent/binary")
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.returncode == -1
        assert "not found" in result.stderr.lower()


class TestEngineHealthCheck:
    """6.7: Health check."""

    def test_health_check_returns_bool(self):
        engine = _make_engine()
        assert isinstance(engine.health_check(), bool)

    def test_health_check_with_real_binary(self):
        engine = _make_engine(binary="/bin/sh")
        assert engine.health_check() is True

    def test_health_check_with_missing_binary(self):
        engine = _make_engine(binary="/nonexistent/path/to/engine")
        assert engine.health_check() is False


class TestEngineAvailability:
    """6.8: Availability check."""

    def test_is_available_returns_bool(self):
        engine = _make_engine()
        assert isinstance(engine.is_available(), bool)

    def test_is_available_true_for_existing_binary(self):
        engine = _make_engine(binary="/bin/sh")
        assert engine.is_available() is True

    def test_is_available_false_for_missing_binary(self):
        engine = _make_engine(binary="/nonexistent/engine")
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
        engine = _make_engine(default_model="gorilla-7b")
        assert engine.model() == "gorilla-7b"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_default_timeout_used_when_no_override(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(default_timeout=120)
        engine.run("prompt", model=DEFAULT_MODEL, timeout=None)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 120


# ===========================================================================
# Command building -- Gorilla-specific (stdin mode)
# ===========================================================================


class TestGorillaCommandBuilding:
    """Verify the subprocess command includes Gorilla-specific structure."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_binary_is_first_arg(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(binary="/usr/local/bin/gorilla")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/gorilla"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_prompt_delivered_via_stdin(self, mock_run):
        """Gorilla reads prompts from stdin, not args."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("how to list S3 buckets", model=DEFAULT_MODEL, timeout=60)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["input"] == "how to list S3 buckets"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_model_flag_when_model_empty(self, mock_run):
        """Gorilla does not support model selection -- no --model flag."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--model" not in cmd


# ===========================================================================
# Registry integration
# ===========================================================================


class TestRegistryIntegration:
    """Verify the engine integrates with the provider registry."""

    def test_gorilla_in_registry(self):
        from src.swe_team.providers.coding_engine import list_engines
        engines = list_engines()
        assert "gorilla" in engines

    def test_gorilla_resolve(self):
        from src.swe_team.providers.coding_engine import resolve_engine
        engine = resolve_engine("gorilla", {"timeout_seconds": 60})
        assert engine.name == "gorilla"
