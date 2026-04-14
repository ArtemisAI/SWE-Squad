"""
Parameterized test template for CodingEngine connectors.

Copy the EngineTestSuite class and adapt it for your engine, or run the
parameterized tests directly if your engine subclasses GenericCLIEngine.

Usage for a new engine:

    1. Copy this file to tests/unit/test_{engine_name}_engine.py
    2. Replace TemplateSubclassEngine with your engine class
    3. Update ENGINE_CLASS, ENGINE_NAME, ENGINE_MODULE, DEFAULT_MODEL
    4. Add engine-specific tests at the bottom
    5. Run: python3 -m pytest tests/unit/test_{engine_name}_engine.py -v

Every engine connector must pass ALL tests in the EngineTestSuite class.
These form the mandatory testing contract (see docs/engine-connector-architecture.md
Section 6).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult


# ---------------------------------------------------------------------------
# Configuration: change these for your engine
# ---------------------------------------------------------------------------

# The engine class to test. Replace with your engine import.
# Example: from src.swe_team.providers.coding_engine.aider import AiderEngine
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

# Module path where subprocess.run is called (for patching).
# Must match where subprocess is imported in your engine module.
ENGINE_MODULE = "src.swe_team.providers.coding_engine.generic_cli"

# Engine identifier returned by .name property
ENGINE_NAME = "generic_cli_test"

# Default model for tests
DEFAULT_MODEL = "test-model"

# Default binary name
DEFAULT_BINARY = "/usr/bin/test-engine"


def _make_engine(**overrides) -> GenericCLIEngine:
    """Factory to create a test engine instance with sensible defaults.

    Override any parameter by passing it as a keyword argument.
    Replace GenericCLIEngine with your engine class.
    """
    defaults = {
        "binary": DEFAULT_BINARY,
        "default_model": DEFAULT_MODEL,
        "default_timeout": 60,
        "args_template": ["-p", "{prompt}"],
        "model_flag": "--model",
        "prompt_via": "args",
        "output_format": "text",
    }
    defaults.update(overrides)
    return GenericCLIEngine(**defaults)


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
        # Engine names should be lowercase identifiers
        assert engine.name == engine.name.lower(), (
            f"Engine name '{engine.name}' contains uppercase characters. "
            "Use lowercase identifiers (e.g., 'aider', not 'Aider')."
        )


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
        """Engine must re-raise subprocess.TimeoutExpired by default."""
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
        """Engine must return EngineResult with returncode=-1 on FileNotFoundError."""
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
        """Health check returns True when binary exists on the system."""
        engine = _make_engine(binary="/bin/sh")
        assert engine.health_check() is True

    def test_health_check_with_missing_binary(self):
        """Health check returns False when binary does not exist."""
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

    def test_default_model(self):
        engine = _make_engine(default_model="gpt-4o")
        assert engine.model() == "gpt-4o"

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
# Command building (for GenericCLIEngine subclasses)
# ===========================================================================


class TestCommandBuilding:
    """Verify the subprocess command is built correctly."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_binary_is_first_arg(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(binary="/usr/bin/my-engine")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/my-engine"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_model_flag_in_command(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(model_flag="--model")
        engine.run("prompt", model="gpt-4o", timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        assert "gpt-4o" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_prompt_in_args_mode(self, mock_run):
        """In args mode, the prompt text appears in the command arguments."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(
            args_template=["-p", "{prompt}"],
            prompt_via="args",
        )
        engine.run("hello world", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "hello world" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_prompt_via_stdin(self, mock_run):
        """In stdin mode, the prompt is piped via subprocess input."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(
            args_template=[],
            prompt_via="stdin",
        )
        engine.run("hello world", model=DEFAULT_MODEL, timeout=60)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["input"] == "hello world"


# ===========================================================================
# Output parsing
# ===========================================================================


class TestOutputParsing:
    """Verify engine output is parsed correctly."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_text_output_stripped(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="  output with whitespace  \n",
            stderr="",
            returncode=0,
        )
        engine = _make_engine(output_format="text")
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        # Text output should be stripped of leading/trailing whitespace
        assert result.stdout == "output with whitespace"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_json_output_extracts_result(self, mock_run):
        import json
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"result": "extracted value", "extra": "ignored"}),
            stderr="",
            returncode=0,
        )
        engine = _make_engine(output_format="json")
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.stdout == "extracted value"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_json_output_fallback_on_invalid_json(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="not valid json at all",
            stderr="",
            returncode=0,
        )
        engine = _make_engine(output_format="json")
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        # Should fall back to raw text
        assert "not valid json" in result.stdout

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.stdout == ""
        assert result.success is True


# ===========================================================================
# Engine-specific tests (add yours below)
# ===========================================================================
#
# class TestYourEngineSpecificBehavior:
#     """Tests for features unique to your engine connector."""
#
#     def test_safety_filter_blocks_sensitive_prompt(self):
#         """Example: test that sensitive keywords are blocked."""
#         engine = _make_engine()
#         result = engine.run("my password is hunter2", model="model", timeout=60)
#         assert result.returncode == -1
#         assert "blocked" in result.stderr.lower()
#
#     def test_custom_output_parsing(self):
#         """Example: test engine-specific output format."""
#         pass
#
#     @pytest.mark.skipif(
#         not shutil.which("your-engine"),
#         reason="your-engine binary not installed"
#     )
#     def test_integration_health_check(self):
#         """Integration test: only runs when binary is installed."""
#         engine = _make_engine(binary="your-engine")
#         assert engine.health_check() is True


# ===========================================================================
# Registry integration (verify engine can be resolved by name)
# ===========================================================================


class TestRegistryIntegration:
    """Verify the engine integrates with the provider registry.

    Uncomment and adapt once your engine is registered in __init__.py.
    """

    def test_generic_cli_in_registry(self):
        """GenericCLI is registered in the engine registry."""
        from src.swe_team.providers.coding_engine import list_engines
        engines = list_engines()
        assert "generic_cli" in engines

    # def test_your_engine_in_registry(self):
    #     """Your engine is registered in the engine registry."""
    #     from src.swe_team.providers.coding_engine import list_engines, resolve_engine
    #     engines = list_engines()
    #     assert "{engine_name}" in engines
    #     engine = resolve_engine("{engine_name}", {"timeout_seconds": 60})
    #     assert engine.name == "{engine_name}"
