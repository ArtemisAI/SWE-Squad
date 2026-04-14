"""
Tests for the Aider CodingEngine connector.

Covers all mandatory EngineTestSuite checks (protocol compliance, identity,
run success/failure, timeout, binary not found, health check, availability,
defaults) plus Aider-specific flag tests.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.aider import AiderEngine
from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGINE_MODULE = "src.swe_team.providers.coding_engine.generic_cli"
ENGINE_NAME = "aider"
DEFAULT_MODEL = "sonnet"
DEFAULT_BINARY = "/usr/local/bin/aider"


def _make_engine(**overrides) -> AiderEngine:
    """Factory to create a test AiderEngine with sensible defaults."""
    defaults = {
        "binary": DEFAULT_BINARY,
        "default_model": DEFAULT_MODEL,
        "default_timeout": 60,
    }
    defaults.update(overrides)
    return AiderEngine(**defaults)


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
            "Use lowercase identifiers (e.g., 'aider', not 'Aider')."
        )

    def test_name_is_aider(self):
        engine = _make_engine()
        assert engine.name == "aider"


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
        result = engine.run("prompt", model="opus", timeout=60)

        assert result.model == "opus"

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


class TestEngineDefaults:
    """Verify constructor defaults are applied correctly."""

    def test_default_model(self):
        engine = _make_engine()
        assert engine.model() == "sonnet"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_default_model_used_when_no_override(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        result = engine.run("prompt", model=None, timeout=60)

        assert result.model == "sonnet"

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
# Aider-specific: flag and command building tests
# ===========================================================================


class TestAiderFlags:
    """Verify Aider-specific CLI flags are built correctly."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_default_flags_no_git_yes_always(self, mock_run):
        """Default engine uses --no-git and --yes-always."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("fix bug", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--no-git" in cmd
        assert "--yes-always" in cmd
        assert "--auto-commits" not in cmd
        assert "--no-auto-commits" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_git_false_auto_commits_false(self, mock_run):
        """When no_git=False and auto_commits=False, use --no-auto-commits."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(no_git=False, auto_commits=False)
        engine.run("fix bug", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--no-git" not in cmd
        assert "--no-auto-commits" in cmd
        assert "--auto-commits" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_git_false_auto_commits_true(self, mock_run):
        """When no_git=False and auto_commits=True, use --auto-commits."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(no_git=False, auto_commits=True)
        engine.run("fix bug", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--no-git" not in cmd
        assert "--auto-commits" in cmd
        assert "--no-auto-commits" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_yes_always_disabled(self, mock_run):
        """When yes_always=False, --yes-always is not in the command."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(yes_always=False)
        engine.run("fix bug", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--yes-always" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_edit_format_flag(self, mock_run):
        """When edit_format is set, --edit-format <format> appears in cmd."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(edit_format="diff")
        engine.run("fix bug", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--edit-format")
        assert cmd[idx + 1] == "diff"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_edit_format_none_omitted(self, mock_run):
        """When edit_format is None, --edit-format is not in the command."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(edit_format=None)
        engine.run("fix bug", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--edit-format" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_message_flag_delivers_prompt(self, mock_run):
        """The --message flag is used to deliver the prompt."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("refactor the parser", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--message")
        assert cmd[idx + 1] == "refactor the parser"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_model_flag_in_command(self, mock_run):
        """The --model flag passes the model name."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("prompt", model="opus", timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "opus"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_binary_is_first_arg(self, mock_run):
        """The aider binary is the first element of the command."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(binary="/usr/local/bin/aider")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/aider"

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_full_command_structure(self, mock_run):
        """Verify the complete command structure with all default flags."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("do the thing", model="sonnet", timeout=60)

        cmd = mock_run.call_args[0][0]
        # Expected order: binary, --model, sonnet, --message, prompt, --no-git, --yes-always
        assert cmd[0] == DEFAULT_BINARY
        assert "--model" in cmd
        assert "--message" in cmd
        assert "--no-git" in cmd
        assert "--yes-always" in cmd


class TestAiderBinaryResolution:
    """Verify binary resolution uses shutil.which with correct fallback."""

    @patch("src.swe_team.providers.coding_engine.aider.shutil.which")
    def test_uses_shutil_which(self, mock_which):
        mock_which.return_value = "/home/user/.local/bin/aider"
        engine = AiderEngine()
        assert engine._binary == "/home/user/.local/bin/aider"

    @patch("src.swe_team.providers.coding_engine.aider.shutil.which")
    def test_fallback_to_usr_local_bin(self, mock_which):
        mock_which.return_value = None
        engine = AiderEngine()
        assert engine._binary == "/usr/local/bin/aider"

    def test_explicit_binary_overrides_which(self):
        engine = AiderEngine(binary="/opt/aider/bin/aider")
        assert engine._binary == "/opt/aider/bin/aider"


class TestAiderEnvVars:
    """Verify environment variable pass-through."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_env_vars_passed_to_subprocess(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(env_vars={"ANTHROPIC_API_KEY": "sk-test"})
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["env"]["ANTHROPIC_API_KEY"] == "sk-test"


# ===========================================================================
# Registry integration
# ===========================================================================


class TestAiderRegistryIntegration:
    """Verify the Aider engine integrates with the provider registry."""

    def test_aider_in_registry(self):
        from src.swe_team.providers.coding_engine import list_engines
        engines = list_engines()
        assert "aider" in engines

    def test_aider_resolve(self):
        from src.swe_team.providers.coding_engine import resolve_engine
        engine = resolve_engine("aider", {"timeout_seconds": 60})
        assert engine.name == "aider"
