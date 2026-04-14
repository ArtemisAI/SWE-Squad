"""Tests for the Windsurf CLI coding engine."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import CodingEngine
from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine


class TestWindsurfCLIEngineBasics:
    def test_name_is_windsurf(self):
        engine = WindsurfCLIEngine()
        assert engine.name == "windsurf"

    def test_model_returns_default(self):
        engine = WindsurfCLIEngine(default_model="")
        assert engine.model() == ""

    def test_model_returns_custom_default(self):
        engine = WindsurfCLIEngine(default_model="codeium-model")
        assert engine.model() == "codeium-model"

    def test_is_available_returns_bool(self):
        engine = WindsurfCLIEngine()
        assert isinstance(engine.is_available(), bool)

    def test_health_check_returns_bool(self):
        engine = WindsurfCLIEngine()
        assert isinstance(engine.health_check(), bool)

    def test_is_available_true_when_binary_exists(self):
        engine = WindsurfCLIEngine(binary="/bin/sh")
        assert engine.is_available() is True

    def test_cascade_mode_flag(self):
        engine = WindsurfCLIEngine(cascade_mode=True)
        cmd = engine._build_cmd("prompt")
        # When cascade_mode=True, the binary is already 'cascade'
        assert "cascade" in cmd[0]

    def test_cascade_mode_false_no_cascade_flag(self):
        engine = WindsurfCLIEngine(cascade_mode=False)
        cmd = engine._build_cmd("prompt")
        assert "cascade" not in cmd

    def test_protocol_compliance(self):
        """WindsurfCLIEngine satisfies the CodingEngine Protocol."""
        engine = WindsurfCLIEngine()
        assert isinstance(engine, CodingEngine)


class TestWindsurfCLIEngineRun:
    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_success(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="generated code fix",
            stderr="",
            returncode=0,
        )
        engine = WindsurfCLIEngine(default_timeout=60)
        result = engine.run("fix this bug")

        assert result.success is True
        assert result.stdout == "generated code fix"
        assert result.returncode == 0
        assert result.metadata.get("cascade_mode") is False

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="windsurf error",
            returncode=1,
        )
        engine = WindsurfCLIEngine()
        result = engine.run("prompt")

        assert result.success is False
        assert result.returncode == 1
        assert "windsurf error" in result.stderr

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_timeout_raises(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="windsurf", timeout=60)
        engine = WindsurfCLIEngine(default_timeout=60)
        with pytest.raises(subprocess.TimeoutExpired):
            engine.run("prompt")

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_binary_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")
        engine = WindsurfCLIEngine(binary="/nonexistent/windsurf")
        result = engine.run("prompt")

        assert result.success is False
        assert result.returncode == -1
        assert "not found" in result.stderr.lower()

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_passes_timeout_override(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = WindsurfCLIEngine(default_timeout=300)
        engine.run("prompt", timeout=60)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 60

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_passes_cwd_and_env(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = WindsurfCLIEngine()
        engine.run("prompt", cwd="/tmp/worktree", env={"PATH": "/custom/bin"})

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == "/tmp/worktree"
        assert call_kwargs["env"]["PATH"] == "/custom/bin"

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_sends_prompt_via_stdin(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = WindsurfCLIEngine()
        engine.run("refactor this function")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["input"] == "refactor this function"

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_with_cascade_mode(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="cascade output",
            stderr="",
            returncode=0,
        )
        engine = WindsurfCLIEngine(cascade_mode=True)
        result = engine.run("debug this issue")

        cmd = mock_run.call_args[0][0]
        # When cascade_mode=True, the binary is already 'cascade'
        assert "cascade" in cmd[0]
        assert result.metadata.get("cascade_mode") is True

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_with_env_vars_merged(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = WindsurfCLIEngine(env_vars={"WINDSURF_CONFIG": "/path/to/config"})
        engine.run("prompt", env={"EXTRA_VAR": "value"})

        call_kwargs = mock_run.call_args[1]
        env = call_kwargs["env"]
        assert env["WINDSURF_CONFIG"] == "/path/to/config"
        assert env["EXTRA_VAR"] == "value"


class TestBuildCmd:
    def test_build_cmd_base(self):
        engine = WindsurfCLIEngine()
        cmd = engine._build_cmd("prompt")
        assert "windsurf" in cmd[0] or "cascade" in cmd[0]

    def test_build_cmd_with_model_flag(self):
        engine = WindsurfCLIEngine(model_flag="--model")
        cmd = engine._build_cmd("prompt", model="custom-model")
        assert "--model" in cmd
        assert "custom-model" in cmd

    def test_build_cmd_with_args_template(self):
        engine = WindsurfCLIEngine(args_template=["--verbose", "--yes"])
        cmd = engine._build_cmd("prompt")
        assert "--verbose" in cmd
        assert "--yes" in cmd


class TestRegistry:
    def test_resolve_windsurf(self):
        from src.swe_team.providers.coding_engine import resolve_engine

        engine = resolve_engine("windsurf", {"timeout_seconds": 60})
        assert engine.name == "windsurf"

    def test_resolve_windsurf_with_cascade_mode(self):
        from src.swe_team.providers.coding_engine import resolve_engine

        engine = resolve_engine("windsurf", {"timeout_seconds": 60, "cascade_mode": True})
        assert engine.name == "windsurf"
        assert engine._cascade_mode is True

    def test_list_engines_includes_windsurf(self):
        from src.swe_team.providers.coding_engine import list_engines

        engines = list_engines()
        assert "windsurf" in engines
