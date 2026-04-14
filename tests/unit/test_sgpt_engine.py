"""Tests for Shell-GPT (sgpt) CLI coding engine."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import CodingEngine
from src.swe_team.providers.coding_engine.sgpt import SgpTCLEngine


class TestSgpTEngineBasics:
    def test_name_is_sgpt(self):
        engine = SgpTCLEngine()
        assert engine.name == "sgpt"

    def test_model_returns_default(self):
        engine = SgpTCLEngine(default_model="")
        assert engine.model() == ""

    def test_model_returns_custom_default(self):
        engine = SgpTCLEngine(default_model="gpt-4o")
        assert engine.model() == "gpt-4o"

    def test_is_available_returns_bool(self):
        engine = SgpTCLEngine()
        assert isinstance(engine.is_available(), bool)

    def test_health_check_returns_bool(self):
        engine = SgpTCLEngine()
        assert isinstance(engine.health_check(), bool)

    def test_is_available_true_when_binary_exists(self):
        engine = SgpTCLEngine(binary="/bin/sh")
        assert engine.is_available() is True

    def test_protocol_compliance(self):
        """SgpTCLEngine satisfies CodingEngine Protocol."""
        engine = SgpTCLEngine()
        assert isinstance(engine, CodingEngine)


class TestSgpTEngineRun:
    @patch("src.swe_team.providers.coding_engine.sgpt.subprocess.run")
    def test_run_success(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ls -la",
            stderr="",
            returncode=0,
        )
        engine = SgpTCLEngine(default_model="gpt-4o", default_timeout=60)
        result = engine.run("list all files")

        assert result.success is True
        assert result.stdout == "ls -la"
        assert result.returncode == 0

    @patch("src.swe_team.providers.coding_engine.sgpt.subprocess.run")
    def test_run_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="sgpt error",
            returncode=1,
        )
        engine = SgpTCLEngine()
        result = engine.run("prompt")

        assert result.success is False
        assert result.returncode == 1
        assert "sgpt error" in result.stderr

    @patch("src.swe_team.providers.coding_engine.sgpt.subprocess.run")
    def test_run_timeout_raises(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sgpt", timeout=60)
        engine = SgpTCLEngine(default_timeout=60)
        with pytest.raises(subprocess.TimeoutExpired):
            engine.run("prompt")

    @patch("src.swe_team.providers.coding_engine.sgpt.subprocess.run")
    def test_run_binary_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")
        engine = SgpTCLEngine(binary="/nonexistent/sgpt")
        result = engine.run("prompt")

        assert result.success is False
        assert result.returncode == -1
        assert "not found" in result.stderr.lower()

    @patch("src.swe_team.providers.coding_engine.sgpt.subprocess.run")
    def test_run_passes_timeout_override(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = SgpTCLEngine(default_timeout=300)
        engine.run("prompt", timeout=60)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 60

    @patch("src.swe_team.providers.coding_engine.sgpt.subprocess.run")
    def test_run_passes_cwd_and_env(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = SgpTCLEngine()
        engine.run("prompt", cwd="/tmp/worktree", env={"PATH": "/custom/bin"})

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == "/tmp/worktree"
        assert call_kwargs["env"]["PATH"] == "/custom/bin"

    @patch("src.swe_team.providers.coding_engine.sgpt.subprocess.run")
    def test_run_sends_prompt_via_stdin(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = SgpTCLEngine()
        engine.run("generate a script")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["input"] == "generate a script"

    @patch("src.swe_team.providers.coding_engine.sgpt.subprocess.run")
    def test_run_with_env_vars_merged(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = SgpTCLEngine(env_vars={"OPENAI_API_KEY": "sk-test"})
        engine.run("prompt", env={"EXTRA_VAR": "value"})

        call_kwargs = mock_run.call_args[1]
        env = call_kwargs["env"]
        assert env["OPENAI_API_KEY"] == "sk-test"
        assert env["EXTRA_VAR"] == "value"


class TestBuildCmd:
    def test_build_cmd_base(self):
        engine = SgpTCLEngine()
        cmd = engine._build_cmd("prompt")
        assert "sgpt" in cmd[0]

    def test_build_cmd_with_model_flag(self):
        engine = SgpTCLEngine(model_flag="--model")
        cmd = engine._build_cmd("prompt", model="gpt-4-turbo")
        assert "--model" in cmd
        assert "gpt-4-turbo" in cmd

    def test_build_cmd_with_args_template(self):
        engine = SgpTCLEngine(args_template=["--shell", "--code"])
        cmd = engine._build_cmd("prompt")
        assert "--shell" in cmd
        assert "--code" in cmd

    def test_build_cmd_with_custom_model_flag(self):
        engine = SgpTCLEngine(model_flag="-m")
        cmd = engine._build_cmd("prompt", model="gpt-4o")
        assert "-m" in cmd
        assert "gpt-4o" in cmd


class TestRegistry:
    def test_resolve_sgpt(self):
        from src.swe_team.providers.coding_engine import resolve_engine

        engine = resolve_engine("sgpt", {"timeout_seconds": 60})
        assert engine.name == "sgpt"

    def test_resolve_sgpt_with_model(self):
        from src.swe_team.providers.coding_engine import resolve_engine

        engine = resolve_engine("sgpt", {"default_model": "gpt-4o"})
        assert engine.name == "sgpt"
        assert engine.model() == "gpt-4o"

    def test_list_engines_includes_sgpt(self):
        from src.swe_team.providers.coding_engine import list_engines

        engines = list_engines()
        assert "sgpt" in engines
