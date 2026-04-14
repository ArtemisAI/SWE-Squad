"""
Tests for the Goose CLI coding engine connector.

Covers all mandatory EngineTestSuite tests (protocol compliance, identity,
run success/failure, timeout, binary not found, health check, availability,
defaults, command building, registry integration) plus Goose-specific
tests for the recipe parameter.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.goose import GooseCLIEngine

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGINE_MODULE = "src.swe_team.providers.coding_engine.generic_cli"

ENGINE_NAME = "goose"
DEFAULT_MODEL = ""
DEFAULT_BINARY = "/usr/local/bin/goose"


def _make_engine(**overrides) -> GooseCLIEngine:
    """Factory to create a GooseCLIEngine with sensible defaults."""
    defaults = {
        "binary": DEFAULT_BINARY,
        "default_model": DEFAULT_MODEL,
        "default_timeout": 60,
    }
    defaults.update(overrides)
    return GooseCLIEngine(**defaults)


# ===========================================================================
# EngineTestSuite: mandatory tests for every engine connector
# ===========================================================================


class TestEngineProtocolCompliance:
    """6.1: The engine must satisfy the CodingEngine protocol at runtime."""

    def test_isinstance_check(self):
        engine = _make_engine()
        assert isinstance(engine, CodingEngine), (
            f"{type(engine).__name__} does not satisfy CodingEngine protocol. "
            "Ensure it has name (property), run(), and health_check() methods."
        )


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

    def test_name_is_goose(self):
        engine = _make_engine()
        assert engine.name == "goose"


class TestEngineRunSuccess:
    """6.3: Successful run with mocked subprocess."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_success_returns_engine_result(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="task completed successfully",
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("fix the bug", model=DEFAULT_MODEL, timeout=60)

        assert isinstance(result, EngineResult)
        assert result.success is True
        assert result.returncode == 0

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_success_captures_stdout(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="output text here",
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert len(result.stdout) > 0

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_uses_model_override(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model="custom-model", timeout=60)

        assert result.model == "custom-model"

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
            stdout="",
            stderr="error: model not found",
            returncode=1,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.returncode == 1

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_failure_captures_stderr(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="fatal error occurred",
            returncode=1,
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

    @patch("src.swe_team.providers.coding_engine.goose.shutil.which")
    @patch("src.swe_team.providers.coding_engine.goose.subprocess.run")
    def test_health_check_with_real_binary(self, mock_run, mock_which):
        mock_which.return_value = "/usr/local/bin/goose"
        mock_run.return_value = MagicMock(returncode=0)
        engine = _make_engine(binary="goose")
        assert engine.health_check() is True

    def test_health_check_with_missing_binary(self):
        engine = _make_engine(binary="/nonexistent/path/to/engine")
        assert engine.health_check() is False


class TestEngineAvailability:
    """6.8: Availability check."""

    def test_is_available_returns_bool(self):
        engine = _make_engine()
        assert isinstance(engine.is_available(), bool)

    @patch("src.swe_team.providers.coding_engine.goose.shutil.which")
    @patch("src.swe_team.providers.coding_engine.goose.subprocess.run")
    def test_is_available_true_when_version_succeeds(self, mock_run, mock_which):
        mock_which.return_value = "/usr/local/bin/goose"
        mock_run.return_value = MagicMock(returncode=0)
        engine = _make_engine(binary="goose")

        assert engine.is_available() is True

    @patch("src.swe_team.providers.coding_engine.goose.shutil.which")
    @patch("src.swe_team.providers.coding_engine.goose.subprocess.run")
    def test_is_available_false_when_version_fails(self, mock_run, mock_which):
        mock_which.return_value = "/usr/local/bin/goose"
        mock_run.return_value = MagicMock(returncode=1)
        engine = _make_engine(binary="goose")

        assert engine.is_available() is False

    @patch("src.swe_team.providers.coding_engine.goose.shutil.which")
    @patch("src.swe_team.providers.coding_engine.goose.subprocess.run")
    def test_is_available_false_for_missing_binary(self, mock_run, mock_which):
        mock_which.return_value = None
        mock_run.side_effect = FileNotFoundError("not found")
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
        engine = _make_engine(default_model="custom")
        assert engine.model() == "custom"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_default_model_used_when_no_override(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(default_model="my-default")
        result = engine.run("prompt", model=None, timeout=60)

        assert result.model == "my-default"

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
# Command building -- Goose uses "run --text" subcommand
# ===========================================================================


class TestCommandBuilding:
    """Verify the subprocess command is built correctly."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_binary_is_first_arg(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(binary="/usr/local/bin/goose")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/goose"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_subcommand_in_args(self, mock_run):
        """Goose uses 'run --text' subcommand pattern."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("fix the bug", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "run" in cmd
        assert "--text" in cmd
        assert "fix the bug" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_prompt_in_args(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("hello world", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "hello world" in cmd


# ===========================================================================
# Goose-specific: recipe parameter
# ===========================================================================


class TestGooseRecipe:
    """Test recipe parameter support."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_recipe_by_default(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--recipe" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_recipe_added_to_command(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(recipe="code-review")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        recipe_idx = cmd.index("--recipe")
        assert cmd[recipe_idx + 1] == "code-review"

    def test_recipe_stored(self):
        engine = _make_engine(recipe="code-review")
        assert engine._recipe == "code-review"

    def test_recipe_none_by_default(self):
        engine = _make_engine()
        assert engine._recipe is None


# ===========================================================================
# Registry integration
# ===========================================================================


class TestRegistryIntegration:
    """Verify the engine integrates with the provider registry."""

    def test_goose_in_registry(self):
        from src.swe_team.providers.coding_engine import list_engines
        engines = list_engines()
        assert "goose" in engines

    def test_goose_resolve(self):
        from src.swe_team.providers.coding_engine import resolve_engine
        engine = resolve_engine("goose", {"timeout_seconds": 60})
        assert engine.name == "goose"
