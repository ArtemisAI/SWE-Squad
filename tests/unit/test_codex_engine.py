"""
Tests for the OpenAI Codex CLI coding engine connector.

Covers all mandatory EngineTestSuite tests (protocol compliance, identity,
run success/failure, timeout, binary not found, health check, availability,
defaults) plus Codex-specific tests for approval_mode, sandbox, and quiet flags.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.codex import CodexCLIEngine

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGINE_MODULE = "src.swe_team.providers.coding_engine.generic_cli"

ENGINE_NAME = "codex"
DEFAULT_MODEL = "o4-mini"
DEFAULT_BINARY = "/usr/local/bin/codex"


def _make_engine(**overrides) -> CodexCLIEngine:
    """Factory to create a CodexCLIEngine with sensible defaults."""
    defaults = {
        "binary": DEFAULT_BINARY,
        "default_model": DEFAULT_MODEL,
        "default_timeout": 60,
    }
    defaults.update(overrides)
    return CodexCLIEngine(**defaults)


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
        assert engine.name == engine.name.lower(), (
            f"Engine name '{engine.name}' contains uppercase characters. "
            "Use lowercase identifiers (e.g., 'codex', not 'Codex')."
        )

    def test_name_is_codex(self):
        engine = _make_engine()
        assert engine.name == "codex"


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
        result = engine.run("prompt", model="gpt-4o", timeout=60)

        assert result.model == "gpt-4o"

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

    def test_default_model_is_o4_mini(self):
        engine = _make_engine()
        assert engine.model() == "o4-mini"

    def test_custom_default_model(self):
        engine = _make_engine(default_model="o3")
        assert engine.model() == "o3"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_default_model_used_when_no_override(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(default_model="o4-mini")
        result = engine.run("prompt", model=None, timeout=60)

        assert result.model == "o4-mini"

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
# Command building -- Codex-specific flags
# ===========================================================================


class TestCodexCommandBuilding:
    """Verify the subprocess command includes Codex-specific flags."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_binary_is_first_arg(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(binary="/usr/local/bin/codex")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/codex"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_model_flag_in_command(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model="gpt-4o", timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        assert "gpt-4o" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_prompt_in_args(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("fix the authentication bug", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "fix the authentication bug" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_full_auto_flag_default(self, mock_run):
        """Default approval mode includes --full-auto flag."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--full-auto" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_quiet_flag_default(self, mock_run):
        """Default quiet mode includes --quiet flag."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--quiet" in cmd


# ===========================================================================
# Codex-specific: approval_mode variations
# ===========================================================================


class TestCodexApprovalMode:
    """Test all valid approval modes and invalid input."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_suggest_mode(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(approval_mode="suggest")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--suggest" in cmd
        assert "--full-auto" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_auto_edit_mode(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(approval_mode="auto-edit")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--auto-edit" in cmd
        assert "--full-auto" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_full_auto_mode_explicit(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(approval_mode="full-auto")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--full-auto" in cmd

    def test_invalid_approval_mode_raises(self):
        with pytest.raises(ValueError, match="approval_mode"):
            _make_engine(approval_mode="yolo")

    def test_approval_mode_stored(self):
        engine = _make_engine(approval_mode="suggest")
        assert engine._approval_mode == "suggest"


# ===========================================================================
# Codex-specific: sandbox flag
# ===========================================================================


class TestCodexSandbox:
    """Test sandbox isolation options."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_sandbox_by_default(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--sandbox" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_docker_sandbox(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(sandbox="docker")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        sandbox_idx = cmd.index("--sandbox")
        assert cmd[sandbox_idx + 1] == "docker"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_ssh_sandbox(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(sandbox="ssh")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        sandbox_idx = cmd.index("--sandbox")
        assert cmd[sandbox_idx + 1] == "ssh"

    def test_invalid_sandbox_raises(self):
        with pytest.raises(ValueError, match="sandbox"):
            _make_engine(sandbox="qemu")

    def test_sandbox_stored(self):
        engine = _make_engine(sandbox="docker")
        assert engine._sandbox == "docker"


# ===========================================================================
# Codex-specific: quiet mode
# ===========================================================================


class TestCodexQuietMode:
    """Test quiet flag toggling."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_quiet_enabled_by_default(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--quiet" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_quiet_disabled(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(quiet=False)
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--quiet" not in cmd

    def test_quiet_stored(self):
        engine = _make_engine(quiet=False)
        assert engine._quiet is False


# ===========================================================================
# Output parsing
# ===========================================================================


class TestOutputParsing:
    """Verify engine output is parsed correctly (text mode)."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_text_output_stripped(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="  output with whitespace  \n",
            stderr="",
            returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.stdout == "output with whitespace"

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
# Registry integration (verify engine can be resolved by name)
# ===========================================================================


class TestRegistryIntegration:
    """Verify the engine integrates with the provider registry.

    Uncomment once codex is registered in __init__.py.
    """

    # def test_codex_in_registry(self):
    #     """Codex is registered in the engine registry."""
    #     from src.swe_team.providers.coding_engine import list_engines
    #     engines = list_engines()
    #     assert "codex" in engines

    # def test_codex_resolve(self):
    #     """Codex can be resolved from the registry."""
    #     from src.swe_team.providers.coding_engine import resolve_engine
    #     engine = resolve_engine("codex", {"timeout_seconds": 60})
    #     assert engine.name == "codex"
    pass
