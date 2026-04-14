"""
Tests for the GitHub Copilot CLI coding engine connector.

Tests the standalone ``copilot`` autonomous agent binary, NOT the legacy
``gh copilot suggest`` helper.  See https://github.com/features/copilot/cli

Follows the mandatory EngineTestSuite contract from test_engine_template.py.
All tests are self-contained with mocking -- no network or binary required.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.copilot import CopilotCLIEngine

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGINE_MODULE = "src.swe_team.providers.coding_engine.generic_cli"

ENGINE_NAME = "copilot"
DEFAULT_MODEL = ""
DEFAULT_BINARY = "/usr/local/bin/copilot"


def _make_engine(**overrides) -> CopilotCLIEngine:
    """Factory to create a CopilotCLIEngine with sensible test defaults."""
    defaults = {
        "binary": DEFAULT_BINARY,
        "default_model": DEFAULT_MODEL,
        "default_timeout": 60,
    }
    defaults.update(overrides)
    return CopilotCLIEngine(**defaults)


# ===========================================================================
# 6.1: Protocol compliance
# ===========================================================================


class TestEngineProtocolCompliance:
    """The engine must satisfy the CodingEngine protocol at runtime."""

    def test_isinstance_check(self):
        engine = _make_engine()
        assert isinstance(engine, CodingEngine), (
            f"{type(engine).__name__} does not satisfy CodingEngine protocol. "
            "Ensure it has name (property), run(), and health_check() methods."
        )


# ===========================================================================
# 6.2: Identity
# ===========================================================================


class TestEngineIdentity:
    """The name property must return a non-empty string."""

    def test_name_returns_string(self):
        engine = _make_engine()
        assert isinstance(engine.name, str)

    def test_name_not_empty(self):
        engine = _make_engine()
        assert len(engine.name) > 0

    def test_name_is_lowercase(self):
        engine = _make_engine()
        assert engine.name == engine.name.lower()

    def test_name_is_copilot(self):
        engine = _make_engine()
        assert engine.name == "copilot"


# ===========================================================================
# 6.3: Successful run
# ===========================================================================


class TestEngineRunSuccess:
    """Successful run with mocked subprocess."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_success_returns_engine_result(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="Fixed the bug in main.py",
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
        result = engine.run("prompt", model="claude-sonnet-4.5", timeout=60)

        assert result.model == "claude-sonnet-4.5"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_uses_timeout_override(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(default_timeout=600)
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


# ===========================================================================
# 6.4: Failed run
# ===========================================================================


class TestEngineRunFailure:
    """Failed run with mocked subprocess."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_run_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="error: authentication required",
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


# ===========================================================================
# 6.5: Timeout handling
# ===========================================================================


class TestEngineTimeout:
    """Timeout handling."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_timeout_raises_timeout_expired(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=DEFAULT_BINARY, timeout=60,
        )
        engine = _make_engine()

        with pytest.raises(subprocess.TimeoutExpired):
            engine.run("prompt", model=DEFAULT_MODEL, timeout=60)


# ===========================================================================
# 6.6: Binary not found
# ===========================================================================


class TestEngineBinaryNotFound:
    """Binary not found handling."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_binary_not_found_returns_failure(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file or directory")
        engine = _make_engine(binary="/nonexistent/binary")
        result = engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        assert result.success is False
        assert result.returncode == -1
        assert "not found" in result.stderr.lower()


# ===========================================================================
# 6.7: Health check
# ===========================================================================


class TestEngineHealthCheck:
    """Health check."""

    def test_health_check_returns_bool(self):
        engine = _make_engine()
        assert isinstance(engine.health_check(), bool)

    def test_health_check_with_real_binary(self):
        engine = _make_engine(binary="/bin/sh")
        assert engine.health_check() is True

    def test_health_check_with_missing_binary(self):
        engine = _make_engine(binary="/nonexistent/path/to/engine")
        assert engine.health_check() is False


# ===========================================================================
# 6.8: Availability
# ===========================================================================


class TestEngineAvailability:
    """Availability check."""

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
# Defaults
# ===========================================================================


class TestEngineDefaults:
    """Verify constructor defaults are applied correctly."""

    def test_default_model_is_empty(self):
        engine = _make_engine()
        assert engine.model() == ""

    def test_default_timeout_is_600(self):
        """Copilot is a full agent — default timeout should be 600s."""
        engine = CopilotCLIEngine(binary=DEFAULT_BINARY)
        assert engine._default_timeout == 600

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_default_model_used_when_no_override(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(default_model="gpt-5.2")
        result = engine.run("prompt", model=None, timeout=60)

        assert result.model == "gpt-5.2"

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
# Command building — Copilot autonomous agent flags
# ===========================================================================


class TestCommandBuilding:
    """Verify the subprocess command is built correctly for Copilot CLI."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_binary_is_first_arg(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(binary="/usr/local/bin/copilot")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/copilot"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_prompt_via_p_flag(self, mock_run):
        """Copilot uses -p flag for non-interactive prompt delivery."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("fix the bug", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "-p" in cmd
        assert "fix the bug" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_model_flag_in_command(self, mock_run):
        """Copilot supports --model for model selection."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model="claude-sonnet-4.5", timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        assert "claude-sonnet-4.5" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_model_flag_when_empty(self, mock_run):
        """When model is empty string, no --model flag should appear."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(default_model="")
        engine.run("prompt", model="", timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--model" not in cmd


# ===========================================================================
# Autonomous operation flags
# ===========================================================================


class TestAutonomousFlags:
    """Verify flags for fully autonomous (non-interactive) operation."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_allow_all_flag_default(self, mock_run):
        """--allow-all is enabled by default for autonomous operation."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--allow-all" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_allow_all_disabled(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine(allow_all=False)
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--allow-all" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_ask_user_flag_default(self, mock_run):
        """--no-ask-user is enabled by default."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--no-ask-user" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_ask_user_disabled(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine(no_ask_user=False)
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--no-ask-user" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_silent_flag_default(self, mock_run):
        """-s (silent) is enabled by default."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "-s" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_auto_update_flag_default(self, mock_run):
        """--no-auto-update is enabled by default for CI/automation."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--no-auto-update" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_autopilot_flag_default(self, mock_run):
        """--autopilot is enabled by default."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--autopilot" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_custom_instructions_flag(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine(no_custom_instructions=True)
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--no-custom-instructions" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_custom_instructions_off_by_default(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--no-custom-instructions" not in cmd


# ===========================================================================
# Effort and autopilot configuration
# ===========================================================================


class TestEffortAndAutopilot:
    """Verify effort level and autopilot continuation settings."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_effort_flag(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine(effort="high")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        effort_idx = cmd.index("--effort")
        assert cmd[effort_idx + 1] == "high"

    def test_invalid_effort_raises(self):
        with pytest.raises(ValueError, match="effort must be"):
            _make_engine(effort="maximum")

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_effort_xhigh(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine(effort="xhigh")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        effort_idx = cmd.index("--effort")
        assert cmd[effort_idx + 1] == "xhigh"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_max_autopilot_continues(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine(max_autopilot_continues=5)
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--max-autopilot-continues")
        assert cmd[idx + 1] == "5"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_max_autopilot_continues_by_default(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine()
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--max-autopilot-continues" not in cmd


# ===========================================================================
# Session management (resume)
# ===========================================================================


class TestSessionManagement:
    """Verify session resume via --resume flag."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_session_id_adds_resume_flag(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine()
        engine.run("continue", model=DEFAULT_MODEL, timeout=60,
                    session_id="abc-123")

        cmd = mock_run.call_args[0][0]
        assert "--resume" in cmd
        assert "abc-123" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_resume_method(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine()
        result = engine.resume("abc-123", "continue working",
                               model=DEFAULT_MODEL, timeout=60)

        assert result.success is True
        cmd = mock_run.call_args[0][0]
        assert "--resume" in cmd
        assert "abc-123" in cmd

    def test_has_resume_method(self):
        engine = _make_engine()
        assert hasattr(engine, "resume")


# ===========================================================================
# Full command structure
# ===========================================================================


class TestFullCommandStructure:
    """Verify the complete command for a typical autonomous invocation."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_full_autonomous_command(self, mock_run):
        """A typical autonomous invocation should have all required flags."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine(
            binary="/usr/local/bin/copilot",
            default_model="claude-sonnet-4.5",
        )
        engine.run("fix all bugs", model=None, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/copilot"
        assert "--model" in cmd
        assert "claude-sonnet-4.5" in cmd
        assert "-p" in cmd
        assert "fix all bugs" in cmd
        assert "--allow-all" in cmd
        assert "--no-ask-user" in cmd
        assert "-s" in cmd
        assert "--no-auto-update" in cmd
        assert "--autopilot" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_minimal_command(self, mock_run):
        """With all optional flags disabled, only -p and prompt remain."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = _make_engine(
            allow_all=False,
            no_ask_user=False,
            silent=False,
            no_auto_update=False,
            autopilot=False,
        )
        engine.run("hello", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == DEFAULT_BINARY
        assert "-p" in cmd
        assert "hello" in cmd
        assert "--allow-all" not in cmd
        assert "--no-ask-user" not in cmd
        assert "-s" not in cmd
        assert "--no-auto-update" not in cmd
        assert "--autopilot" not in cmd


# ===========================================================================
# Registry integration
# ===========================================================================


class TestRegistryIntegration:
    """Verify the engine integrates with the provider registry."""

    def test_copilot_in_registry(self):
        from src.swe_team.providers.coding_engine import list_engines
        engines = list_engines()
        assert "copilot" in engines

    def test_copilot_resolves(self):
        from src.swe_team.providers.coding_engine import resolve_engine
        engine = resolve_engine("copilot", {"timeout_seconds": 60})
        assert engine.name == "copilot"

    def test_copilot_resolves_with_model(self):
        from src.swe_team.providers.coding_engine import resolve_engine
        engine = resolve_engine("copilot", {
            "default_model": "gpt-5.2",
            "timeout_seconds": 60,
        })
        assert engine.name == "copilot"
        assert engine.model() == "gpt-5.2"
