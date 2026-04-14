"""Tests for the CodeGPT CLI coding engine."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import CodingEngine
from src.swe_team.providers.coding_engine.codegpt import CodeGPTCLIEngine


class TestCodeGPTEngineBasics:
    def test_name_is_codegpt(self):
        engine = CodeGPTCLIEngine()
        assert engine.name == "codegpt"

    def test_model_returns_default(self):
        engine = CodeGPTCLIEngine(default_model="")
        assert engine.model() == ""

    def test_model_returns_custom_default(self):
        engine = CodeGPTCLIEngine(default_model="gpt-4")
        assert engine.model() == "gpt-4"

    def test_is_available_returns_bool(self):
        engine = CodeGPTCLIEngine()
        assert isinstance(engine.is_available(), bool)

    def test_health_check_returns_bool(self):
        engine = CodeGPTCLIEngine()
        assert isinstance(engine.health_check(), bool)

    def test_is_available_true_when_binary_exists(self):
        engine = CodeGPTCLIEngine(binary="/bin/sh")
        assert engine.is_available() is True

    def test_provider_sets_env_var(self):
        engine = CodeGPTCLIEngine(provider="openai")
        assert engine._env_vars.get("CODEGPT_PROVIDER") == "openai"

    def test_protocol_compliance(self):
        """CodeGPTCLIEngine satisfies the CodingEngine Protocol."""
        engine = CodeGPTCLIEngine()
        assert isinstance(engine, CodingEngine)


class TestCodeGPTEngineRun:
    @patch("src.swe_team.providers.coding_engine.codegpt.subprocess.run")
    def test_run_success(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="generated code",
            stderr="",
            returncode=0,
        )
        engine = CodeGPTCLIEngine(default_model="gpt-4", default_timeout=60)
        result = engine.run("explain this function")

        assert result.success is True
        assert result.stdout == "generated code"
        assert result.returncode == 0

    @patch("src.swe_team.providers.coding_engine.codegpt.subprocess.run")
    def test_run_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="codegpt error",
            returncode=1,
        )
        engine = CodeGPTCLIEngine()
        result = engine.run("prompt")

        assert result.success is False
        assert result.returncode == 1
        assert "codegpt error" in result.stderr

    @patch("src.swe_team.providers.coding_engine.codegpt.subprocess.run")
    def test_run_timeout_raises(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="codegpt", timeout=60)
        engine = CodeGPTCLIEngine(default_timeout=60)
        with pytest.raises(subprocess.TimeoutExpired):
            engine.run("prompt")

    @patch("src.swe_team.providers.coding_engine.codegpt.subprocess.run")
    def test_run_binary_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")
        engine = CodeGPTCLIEngine(binary="/nonexistent/codegpt")
        result = engine.run("prompt")

        assert result.success is False
        assert result.returncode == -1
        assert "not found" in result.stderr.lower()

    @patch("src.swe_team.providers.coding_engine.codegpt.subprocess.run")
    def test_run_passes_timeout_override(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = CodeGPTCLIEngine(default_timeout=300)
        engine.run("prompt", timeout=60)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 60

    @patch("src.swe_team.providers.coding_engine.codegpt.subprocess.run")
    def test_run_passes_cwd_and_env(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = CodeGPTCLIEngine()
        engine.run("prompt", cwd="/tmp/worktree", env={"PATH": "/custom/bin"})

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == "/tmp/worktree"
        assert call_kwargs["env"]["PATH"] == "/custom/bin"

    @patch("src.swe_team.providers.coding_engine.codegpt.subprocess.run")
    def test_run_sends_prompt_via_stdin(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = CodeGPTCLIEngine()
        engine.run("explain this code")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["input"] == "explain this code"

    @patch("src.swe_team.providers.coding_engine.codegpt.subprocess.run")
    def test_run_with_env_vars_merged(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = CodeGPTCLIEngine(env_vars={"CODEGPT_CONFIG": "/path/to/config"})
        engine.run("prompt", env={"EXTRA_VAR": "value"})

        call_kwargs = mock_run.call_args[1]
        env = call_kwargs["env"]
        assert env["CODEGPT_CONFIG"] == "/path/to/config"
        assert env["EXTRA_VAR"] == "value"

    @patch("src.swe_team.providers.coding_engine.codegpt.subprocess.run")
    def test_run_with_provider_env_var(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = CodeGPTCLIEngine(provider="anthropic")
        engine.run("prompt")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["env"]["CODEGPT_PROVIDER"] == "anthropic"


class TestBuildCmd:
    def test_build_cmd_base(self):
        engine = CodeGPTCLIEngine()
        cmd = engine._build_cmd("prompt")
        assert "codegpt" in cmd[0]

    def test_build_cmd_with_model_flag(self):
        engine = CodeGPTCLIEngine(model_flag="--model")
        cmd = engine._build_cmd("prompt", model="gpt-4-turbo")
        assert "--model" in cmd
        assert "gpt-4-turbo" in cmd

    def test_build_cmd_with_args_template(self):
        engine = CodeGPTCLIEngine(args_template=["--verbose", "--yes"])
        cmd = engine._build_cmd("prompt")
        assert "--verbose" in cmd
        assert "--yes" in cmd

    def test_build_cmd_with_custom_model_flag(self):
        engine = CodeGPTCLIEngine(model_flag="-m")
        cmd = engine._build_cmd("prompt", model="claude-3")
        assert "-m" in cmd
        assert "claude-3" in cmd


class TestRegistry:
    def test_resolve_codegpt(self):
        from src.swe_team.providers.coding_engine import resolve_engine

        engine = resolve_engine("codegpt", {"timeout_seconds": 60})
        assert engine.name == "codegpt"

    def test_resolve_codegpt_with_provider(self):
        from src.swe_team.providers.coding_engine import resolve_engine

        engine = resolve_engine("codegpt", {"provider": "openai"})
        assert engine.name == "codegpt"
        assert engine._env_vars.get("CODEGPT_PROVIDER") == "openai"

    def test_list_engines_includes_codegpt(self):
        from src.swe_team.providers.coding_engine import list_engines

        engines = list_engines()
        assert "codegpt" in engines
