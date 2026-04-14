"""Tests for the Sweep CLI coding engine."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import CodingEngine
from src.swe_team.providers.coding_engine.sweep import SweepCLIEngine


class TestSweepCLIEngineBasics:
    def test_name_is_sweep(self):
        engine = SweepCLIEngine()
        assert engine.name == "sweep"

    def test_model_returns_default(self):
        engine = SweepCLIEngine(default_model="")
        assert engine.model() == ""

    def test_repo_returns_configured(self):
        engine = SweepCLIEngine(repo="owner/repo")
        assert engine.repo() == "owner/repo"

    def test_is_available_returns_bool(self):
        engine = SweepCLIEngine()
        assert isinstance(engine.is_available(), bool)

    def test_health_check_returns_bool(self):
        engine = SweepCLIEngine()
        assert isinstance(engine.health_check(), bool)

    def test_is_available_true_when_binary_exists(self):
        engine = SweepCLIEngine(binary="/bin/sh")
        assert engine.is_available() is True

    def test_protocol_compliance(self):
        """SweepCLIEngine satisfies the CodingEngine Protocol."""
        engine = SweepCLIEngine()
        assert isinstance(engine, CodingEngine)


class TestSweepCLIEngineRun:
    @patch("src.swe_team.providers.coding_engine.sweep.subprocess.run")
    def test_run_success(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="PR created successfully",
            stderr="",
            returncode=0,
        )
        engine = SweepCLIEngine(default_model="", default_timeout=60)
        result = engine.run("fix the login bug")

        assert result.success is True
        assert result.stdout == "PR created successfully"
        assert result.returncode == 0

    @patch("src.swe_team.providers.coding_engine.sweep.subprocess.run")
    def test_run_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="sweep error",
            returncode=1,
        )
        engine = SweepCLIEngine()
        result = engine.run("prompt")

        assert result.success is False
        assert result.returncode == 1
        assert "sweep error" in result.stderr

    @patch("src.swe_team.providers.coding_engine.sweep.subprocess.run")
    def test_run_timeout_raises(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sweep", timeout=60)
        engine = SweepCLIEngine(default_timeout=60)
        with pytest.raises(subprocess.TimeoutExpired):
            engine.run("prompt")

    @patch("src.swe_team.providers.coding_engine.sweep.subprocess.run")
    def test_run_binary_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")
        engine = SweepCLIEngine(binary="/nonexistent/sweep")
        result = engine.run("prompt")

        assert result.success is False
        assert result.returncode == -1
        assert "not found" in result.stderr.lower()

    @patch("src.swe_team.providers.coding_engine.sweep.subprocess.run")
    def test_run_passes_timeout_override(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = SweepCLIEngine(default_timeout=300)
        engine.run("prompt", timeout=60)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 60

    @patch("src.swe_team.providers.coding_engine.sweep.subprocess.run")
    def test_run_passes_cwd_and_env(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = SweepCLIEngine()
        custom_path = "/custom/bin"
        engine.run("prompt", cwd="/tmp/worktree", env={"PATH": custom_path})

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == "/tmp/worktree"
        # Engine merges system env with custom env; check our custom var is present
        assert call_kwargs["env"]["PATH"] == custom_path

    @patch("src.swe_team.providers.coding_engine.sweep.subprocess.run")
    def test_run_sends_prompt_via_stdin(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = SweepCLIEngine()
        engine.run("fix the bug")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["input"] == "fix the bug"

    @patch("src.swe_team.providers.coding_engine.sweep.subprocess.run")
    def test_run_with_env_vars_merged(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = SweepCLIEngine(env_vars={"SWEEP_ENV": "test"})
        engine.run("prompt", env={"EXTRA_VAR": "value"})

        call_kwargs = mock_run.call_args[1]
        env = call_kwargs["env"]
        assert env["SWEEP_ENV"] == "test"
        assert env["EXTRA_VAR"] == "value"


class TestBuildCmd:
    def test_build_cmd_base(self):
        engine = SweepCLIEngine()
        cmd = engine._build_cmd("prompt")
        assert cmd[0].endswith("sweep")

    def test_build_cmd_with_model_flag(self):
        engine = SweepCLIEngine(model_flag="--model")
        cmd = engine._build_cmd("prompt", model="custom-model")
        assert "--model" in cmd
        assert "custom-model" in cmd

    def test_build_cmd_with_args_template(self):
        engine = SweepCLIEngine(args_template=["--verbose", "--yes"])
        cmd = engine._build_cmd("prompt")
        assert "--verbose" in cmd
        assert "--yes" in cmd

    def test_build_cmd_with_repo(self):
        engine = SweepCLIEngine(repo="your-org/SWE-Squad")
        cmd = engine._build_cmd("prompt")
        assert "--repo" in cmd
        assert "your-org/SWE-Squad" in cmd

    def test_build_cmd_without_repo(self):
        engine = SweepCLIEngine()
        cmd = engine._build_cmd("prompt")
        assert "--repo" not in cmd


class TestRegistry:
    def test_resolve_sweep(self):
        from src.swe_team.providers.coding_engine import resolve_engine

        engine = resolve_engine("sweep", {"timeout_seconds": 60})
        assert engine.name == "sweep"

    def test_list_engines_includes_sweep(self):
        from src.swe_team.providers.coding_engine import list_engines

        engines = list_engines()
        assert "sweep" in engines
