"""
Tests for the Amazon Q Developer CLI coding engine connector.

Follows the mandatory EngineTestSuite contract from test_engine_template.py
plus Amazon Q-specific tests for trust_all_tools, profile, and AWS
credential safety filtering.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.amazon_q import AmazonQCLIEngine
from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGINE_MODULE = "src.swe_team.providers.coding_engine.generic_cli"

ENGINE_NAME = "amazon_q"

DEFAULT_MODEL = ""

DEFAULT_BINARY = "/usr/local/bin/q"


def _make_engine(**overrides) -> AmazonQCLIEngine:
    """Factory to create a test AmazonQCLIEngine with sensible defaults."""
    defaults = {
        "binary": DEFAULT_BINARY,
        "default_model": DEFAULT_MODEL,
        "default_timeout": 60,
        "trust_all_tools": True,
        "profile": None,
    }
    defaults.update(overrides)
    return AmazonQCLIEngine(**defaults)


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
            "Use lowercase identifiers (e.g., 'amazon_q', not 'AmazonQ')."
        )

    def test_name_is_amazon_q(self):
        engine = _make_engine()
        assert engine.name == "amazon_q"


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
            stderr="error: command failed",
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


class TestEngineDefaults:
    """Verify constructor defaults are applied correctly."""

    def test_default_model_is_empty(self):
        engine = _make_engine()
        assert engine.model() == ""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_default_model_used_when_no_override(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(default_model="")
        result = engine.run("prompt", model=None, timeout=60)

        assert result.model == ""

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
# Amazon Q-specific: command building
# ===========================================================================


class TestAmazonQCommandBuilding:
    """Verify Amazon Q-specific command structure."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_chat_subcommand_in_args(self, mock_run):
        """The 'chat' subcommand and '-n' flag must appear in the command."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("hello", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "chat" in cmd
        assert "-n" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_trust_all_tools_enabled(self, mock_run):
        """--trust-all-tools flag present when trust_all_tools=True."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(trust_all_tools=True)
        engine.run("hello", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--trust-all-tools" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_trust_all_tools_disabled(self, mock_run):
        """--trust-all-tools flag absent when trust_all_tools=False."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(trust_all_tools=False)
        engine.run("hello", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--trust-all-tools" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_profile_flag_included(self, mock_run):
        """--profile flag and value present when profile is set."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(profile="my-aws-profile")
        engine.run("hello", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--profile" in cmd
        assert "my-aws-profile" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_profile_flag_absent_when_none(self, mock_run):
        """--profile flag absent when profile is None."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(profile=None)
        engine.run("hello", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--profile" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_no_model_flag_in_command(self, mock_run):
        """Amazon Q does not support model selection -- no model flag in cmd."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("hello", model="anything", timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "--model" not in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_prompt_in_args(self, mock_run):
        """The prompt text appears in the command arguments."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine()
        engine.run("fix the lambda function", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert "fix the lambda function" in cmd

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_binary_is_first_arg(self, mock_run):
        """The q binary path is the first element in the command."""
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(binary="/usr/local/bin/q")
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/q"


# ===========================================================================
# Amazon Q-specific: safety filter for AWS credentials
# ===========================================================================


class TestAmazonQSafetyFilter:
    """AWS credential patterns must be blocked before reaching the CLI."""

    def test_blocks_access_key_id(self):
        engine = _make_engine()
        result = engine.run(
            "use access_key_id AKIA1234", model=DEFAULT_MODEL, timeout=60,
        )
        assert result.returncode == -1
        assert result.success is False
        assert "blocked" in result.stderr.lower()
        assert result.metadata.get("error_type") == "safety_block"
        assert result.metadata.get("blocked_keyword") == "access_key_id"

    def test_blocks_secret_access_key(self):
        engine = _make_engine()
        result = engine.run(
            "my secret_access_key is abc123", model=DEFAULT_MODEL, timeout=60,
        )
        assert result.returncode == -1
        assert result.metadata.get("blocked_keyword") == "secret_access_key"

    def test_blocks_aws_session_token(self):
        engine = _make_engine()
        result = engine.run(
            "set aws_session_token to xyz", model=DEFAULT_MODEL, timeout=60,
        )
        assert result.returncode == -1
        assert result.metadata.get("blocked_keyword") == "aws_session_token"

    def test_blocks_password(self):
        engine = _make_engine()
        result = engine.run(
            "the password is hunter2", model=DEFAULT_MODEL, timeout=60,
        )
        assert result.returncode == -1
        assert result.metadata.get("blocked_keyword") == "password"

    def test_blocks_credential(self):
        engine = _make_engine()
        result = engine.run(
            "load credential from vault", model=DEFAULT_MODEL, timeout=60,
        )
        assert result.returncode == -1
        assert result.metadata.get("blocked_keyword") == "credential"

    def test_blocks_private_key(self):
        engine = _make_engine()
        result = engine.run(
            "here is the private_key content", model=DEFAULT_MODEL, timeout=60,
        )
        assert result.returncode == -1
        assert result.metadata.get("blocked_keyword") == "private_key"

    def test_blocks_case_insensitive(self):
        engine = _make_engine()
        result = engine.run(
            "ACCESS_KEY_ID=AKIA12345", model=DEFAULT_MODEL, timeout=60,
        )
        assert result.returncode == -1

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_allows_safe_prompt(self, mock_run):
        """Safe prompts pass through the filter to the subprocess."""
        mock_run.return_value = MagicMock(
            stdout="done", stderr="", returncode=0,
        )
        engine = _make_engine()
        result = engine.run(
            "deploy the lambda function to us-east-1",
            model=DEFAULT_MODEL,
            timeout=60,
        )
        assert result.success is True
        assert mock_run.called


# ===========================================================================
# Amazon Q-specific: environment variables
# ===========================================================================


class TestAmazonQEnvironment:
    """Verify AWS environment variables are passed through."""

    @patch(f"{ENGINE_MODULE}.subprocess.run")
    def test_env_vars_passed_to_subprocess(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0,
        )
        engine = _make_engine(env_vars={
            "AWS_PROFILE": "production",
            "AWS_REGION": "us-west-2",
        })
        engine.run("prompt", model=DEFAULT_MODEL, timeout=60)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["env"]["AWS_PROFILE"] == "production"
        assert call_kwargs["env"]["AWS_REGION"] == "us-west-2"


# ===========================================================================
# Registry integration
# ===========================================================================


class TestRegistryIntegration:
    """Verify the engine integrates with the provider registry."""

    def test_amazon_q_in_registry(self):
        """Amazon Q is registered in the engine registry."""
        from src.swe_team.providers.coding_engine import list_engines
        engines = list_engines()
        assert "amazon_q" in engines

    def test_amazon_q_resolves(self):
        """Amazon Q can be resolved from the registry."""
        from src.swe_team.providers.coding_engine import resolve_engine
        engine = resolve_engine("amazon_q", {"timeout_seconds": 60})
        assert engine.name == "amazon_q"
