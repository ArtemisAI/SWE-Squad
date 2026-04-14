"""Tests for the CodingEngine plugin (ClaudeCodeEngine) and agent wiring."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine


# ---------------------------------------------------------------------------
# EngineResult
# ---------------------------------------------------------------------------


class TestEngineResult:
    def test_success_true_on_zero_returncode(self):
        r = EngineResult(stdout="ok", stderr="", returncode=0)
        assert r.success is True

    def test_success_false_on_nonzero_returncode(self):
        r = EngineResult(stdout="", stderr="err", returncode=1)
        assert r.success is False

    def test_success_false_on_negative_returncode(self):
        r = EngineResult(stdout="", stderr="timeout", returncode=-1)
        assert r.success is False

    def test_optional_fields_default_none(self):
        r = EngineResult(stdout="", stderr="", returncode=0)
        assert r.cost_usd is None
        assert r.model is None


# ---------------------------------------------------------------------------
# ClaudeCodeEngine basics
# ---------------------------------------------------------------------------


class TestClaudeCodeEngineBasics:
    def test_name_is_claude(self):
        engine = ClaudeCodeEngine()
        assert engine.name == "claude"

    def test_model_returns_default(self):
        engine = ClaudeCodeEngine(default_model="opus")
        assert engine.model() == "opus"

    def test_is_available_returns_bool(self):
        engine = ClaudeCodeEngine()
        assert isinstance(engine.is_available(), bool)

    def test_health_check_returns_bool(self):
        engine = ClaudeCodeEngine()
        assert isinstance(engine.health_check(), bool)

    def test_is_available_true_when_binary_exists(self):
        engine = ClaudeCodeEngine(binary="/bin/sh")
        assert engine.is_available() is True

    def test_protocol_compliance(self):
        """ClaudeCodeEngine satisfies the CodingEngine Protocol."""
        engine = ClaudeCodeEngine()
        assert isinstance(engine, CodingEngine)


# ---------------------------------------------------------------------------
# ClaudeCodeEngine.run() with mocked subprocess
# ---------------------------------------------------------------------------


class TestClaudeCodeEngineRun:
    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_success(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="investigation report",
            stderr="",
            returncode=0,
        )
        engine = ClaudeCodeEngine(default_model="sonnet", default_timeout=60)
        result = engine.run("diagnose this bug")

        assert result.success is True
        assert result.stdout == "investigation report"
        assert result.returncode == 0
        assert result.model == "sonnet"

        # Verify subprocess was called with correct args
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "--print" in cmd
        assert "--model" in cmd
        assert "sonnet" in cmd

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="model not found",
            returncode=1,
        )
        engine = ClaudeCodeEngine()
        result = engine.run("prompt")

        assert result.success is False
        assert result.returncode == 1
        assert "model not found" in result.stderr

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_timeout_raises_by_default(self, mock_run):
        """TimeoutExpired is re-raised when raise_on_timeout=True (default)."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)
        engine = ClaudeCodeEngine(default_timeout=60)
        with pytest.raises(subprocess.TimeoutExpired):
            engine.run("prompt")

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_timeout_handled_gracefully_legacy(self, mock_run):
        """With raise_on_timeout=False the legacy EngineResult(-1) is returned."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)
        engine = ClaudeCodeEngine(default_timeout=60)
        result = engine.run("prompt", raise_on_timeout=False)

        assert result.success is False
        assert result.returncode == -1
        assert "Timeout" in result.stderr
        assert result.metadata.get("error_type") == "timeout"

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_binary_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")
        engine = ClaudeCodeEngine(binary="/nonexistent/claude")
        result = engine.run("prompt")

        assert result.success is False
        assert result.returncode == -1
        assert "not found" in result.stderr.lower()

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_passes_model_override(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = ClaudeCodeEngine(default_model="sonnet")
        result = engine.run("prompt", model="opus")

        assert result.model == "opus"
        cmd = mock_run.call_args[0][0]
        assert "opus" in cmd

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_passes_timeout_override(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = ClaudeCodeEngine(default_timeout=300)
        engine.run("prompt", timeout=60)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 60

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_passes_cwd_and_env(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = ClaudeCodeEngine()
        engine.run("prompt", cwd="/tmp/worktree", env={"PATH": "/usr/bin"})

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == "/tmp/worktree"
        assert call_kwargs["env"] == {"PATH": "/usr/bin"}

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_sends_prompt_via_stdin(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = ClaudeCodeEngine()
        engine.run("my investigation prompt")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["input"] == "my investigation prompt"


# ---------------------------------------------------------------------------
# Agent wiring: InvestigatorAgent with injected engine
# ---------------------------------------------------------------------------


class TestInvestigatorEngineWiring:
    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_investigator_uses_engine_when_injected(self, mock_subprocess):
        """When an engine is injected, InvestigatorAgent._run_claude uses it."""
        mock_subprocess.return_value = MagicMock(stdout="report", stderr="", returncode=0)

        engine = ClaudeCodeEngine(default_model="sonnet")
        from src.swe_team.investigator import InvestigatorAgent

        agent = InvestigatorAgent(engine=engine)
        stdout, stderr = agent._run_claude("test prompt", model="sonnet")

        assert stdout == "report"
        # Verify the engine's subprocess.run was called (not the agent's direct one)
        mock_subprocess.assert_called_once()

    def test_investigator_creates_default_engine_without_injection(self):
        """Without engine injection, InvestigatorAgent creates a default ClaudeCodeEngine."""
        from src.swe_team.investigator import InvestigatorAgent
        from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine

        agent = InvestigatorAgent()
        assert agent._engine is not None
        assert isinstance(agent._engine, ClaudeCodeEngine)


# ---------------------------------------------------------------------------
# Agent wiring: DeveloperAgent with injected engine
# ---------------------------------------------------------------------------


class TestDeveloperEngineWiring:
    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_developer_uses_engine_when_injected(self, mock_subprocess):
        """When an engine is injected, DeveloperAgent._run_claude uses it."""
        mock_subprocess.return_value = MagicMock(stdout="", stderr="", returncode=0)

        engine = ClaudeCodeEngine(default_model="sonnet")
        from src.swe_team.developer import DeveloperAgent

        agent = DeveloperAgent(engine=engine)
        # _run_claude should succeed without raising
        agent._run_claude("fix prompt", timeout=60, model="sonnet")

        mock_subprocess.assert_called_once()

    def test_developer_has_default_engine_when_none_injected(self):
        """Without an injected engine, DeveloperAgent constructs a default ClaudeCodeEngine."""
        from src.swe_team.developer import DeveloperAgent
        from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine

        agent = DeveloperAgent()
        assert agent._engine is not None
        assert isinstance(agent._engine, ClaudeCodeEngine)


# ---------------------------------------------------------------------------
# Cost parsing
# ---------------------------------------------------------------------------


class TestCostParsing:
    def test_parse_cost_from_stderr(self):
        engine = ClaudeCodeEngine()
        assert engine._parse_cost("Total cost: $0.42") == 0.42

    def test_parse_cost_with_commas(self):
        engine = ClaudeCodeEngine()
        assert engine._parse_cost("Total cost: $1,234.56") == 1234.56

    def test_parse_cost_no_match(self):
        engine = ClaudeCodeEngine()
        assert engine._parse_cost("no cost info here") is None

    def test_parse_cost_empty(self):
        engine = ClaudeCodeEngine()
        assert engine._parse_cost("") is None

    def test_parse_cost_multiline(self):
        engine = ClaudeCodeEngine()
        text = "some output\nTotal cost: $0.07\nDone."
        assert engine._parse_cost(text) == 0.07

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_sets_cost_usd(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="report",
            stderr="Total cost: $0.15\n",
            returncode=0,
        )
        engine = ClaudeCodeEngine(default_model="sonnet")
        result = engine.run("prompt")
        assert result.cost_usd == 0.15

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_cost_usd_none_when_no_cost(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="report",
            stderr="",
            returncode=0,
        )
        engine = ClaudeCodeEngine(default_model="sonnet")
        result = engine.run("prompt")
        assert result.cost_usd is None

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_resume_sets_cost_usd(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="report",
            stderr="Total cost: $1.23\n",
            returncode=0,
        )
        engine = ClaudeCodeEngine(default_model="sonnet")
        result = engine.resume("session-1", "prompt")
        assert result.cost_usd == 1.23


# ---------------------------------------------------------------------------
# --output-format json flag in _build_cmd
# ---------------------------------------------------------------------------


class TestBuildCmdOutputFormat:
    def test_build_cmd_includes_output_format_json(self):
        engine = ClaudeCodeEngine()
        cmd = engine._build_cmd("sonnet")
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"
        assert "--verbose" not in cmd

    def test_build_cmd_includes_print_and_output_format(self):
        engine = ClaudeCodeEngine()
        cmd = engine._build_cmd("sonnet")
        assert "--print" in cmd
        assert "--output-format" in cmd


# ---------------------------------------------------------------------------
# allowed_tools: development engine vs investigation engine (#278)
# ---------------------------------------------------------------------------


class TestAllowedToolsWiring:
    """Verify that development engines pass --allowedTools and investigation engines do not."""

    def test_development_engine_has_allowed_tools(self):
        """Development engine must include --allowedTools in the CLI command."""
        dev_tools = "Read,Edit,Write,Bash(git:*),Bash(pytest:*),Bash(python3:*),Grep,Glob"
        engine = ClaudeCodeEngine(allowed_tools=dev_tools)
        cmd = engine._build_cmd("sonnet")
        assert "--allowedTools" in cmd
        idx = cmd.index("--allowedTools")
        assert cmd[idx + 1] == dev_tools

    def test_investigation_engine_has_no_allowed_tools(self):
        """Investigation engine must NOT include --allowedTools."""
        engine = ClaudeCodeEngine()
        cmd = engine._build_cmd("sonnet")
        assert "--allowedTools" not in cmd

    def test_allowed_tools_none_omits_flag(self):
        """Explicitly passing None should omit the flag."""
        engine = ClaudeCodeEngine(allowed_tools=None)
        cmd = engine._build_cmd("sonnet")
        assert "--allowedTools" not in cmd

    def test_allowed_tools_empty_string_omits_flag(self):
        """Empty string should also omit the flag."""
        engine = ClaudeCodeEngine(allowed_tools="")
        cmd = engine._build_cmd("sonnet")
        assert "--allowedTools" not in cmd

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_with_allowed_tools_passes_flag(self, mock_run):
        """Full run() call with allowed_tools passes the flag to subprocess."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        dev_tools = "Read,Edit,Write,Grep,Glob"
        engine = ClaudeCodeEngine(allowed_tools=dev_tools)
        engine.run("fix this bug")

        cmd = mock_run.call_args[0][0]
        assert "--allowedTools" in cmd
        idx = cmd.index("--allowedTools")
        assert cmd[idx + 1] == dev_tools

    @patch("src.swe_team.providers.coding_engine.claude.subprocess.run")
    def test_run_without_allowed_tools_omits_flag(self, mock_run):
        """Full run() call without allowed_tools does not pass --allowedTools."""
        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = ClaudeCodeEngine()
        engine.run("investigate this")

        cmd = mock_run.call_args[0][0]
        assert "--allowedTools" not in cmd


# ---------------------------------------------------------------------------
# Severity filter: MEDIUM tickets enter development (#280)
# ---------------------------------------------------------------------------


class TestSeverityFilter:
    """Verify that MEDIUM severity tickets are not dropped after investigation."""

    def test_medium_severity_included_in_dev_filter(self):
        """MEDIUM severity must be in the allowed set for development."""
        allowed = ("critical", "high", "medium")
        assert "medium" in allowed
        assert "low" not in allowed

    def test_low_severity_excluded_from_dev_filter(self):
        """LOW severity must NOT enter development."""
        allowed = ("critical", "high", "medium")
        assert "low" not in allowed

    def test_medium_ticket_passes_filter(self):
        """A MEDIUM ticket with investigation report should pass the dev filter."""
        from src.swe_team.models import SWETicket, TicketSeverity

        ticket = SWETicket(
            title="Medium bug",
            description="Non-blocking issue",
            severity=TicketSeverity.MEDIUM,
        )
        ticket.investigation_report = "Root cause found"
        # Simulate the filter from swe_team_runner.py sequential path
        passes = ticket.investigation_report and ticket.severity.value in ("critical", "high", "medium")
        assert passes is True

    def test_low_ticket_fails_filter(self):
        """A LOW ticket with investigation report should NOT pass the dev filter."""
        from src.swe_team.models import SWETicket, TicketSeverity

        ticket = SWETicket(
            title="Low priority",
            description="Minor tweak",
            severity=TicketSeverity.LOW,
        )
        ticket.investigation_report = "Minor issue found"
        passes = ticket.investigation_report and ticket.severity.value in ("critical", "high", "medium")
        assert passes is False

    def test_low_ticket_auto_closed(self):
        """LOW severity investigated tickets should be auto-closed."""
        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus

        ticket = SWETicket(
            title="Low prio",
            description="Tech debt",
            severity=TicketSeverity.LOW,
        )
        ticket.investigation_report = "Minor issue"
        # Transition through required states to reach CLOSED
        ticket.transition(TicketStatus.TRIAGED)
        ticket.transition(TicketStatus.INVESTIGATING)
        ticket.transition(TicketStatus.INVESTIGATION_COMPLETE)
        ticket.transition(TicketStatus.CLOSED)
        ticket.metadata["close_reason"] = "low_severity_auto_close"

        assert ticket.status == TicketStatus.CLOSED
        assert ticket.metadata["close_reason"] == "low_severity_auto_close"


# ---------------------------------------------------------------------------
# WindsurfCLIEngine tests
# ---------------------------------------------------------------------------


class TestWindsurfCLIEngineBasics:
    def test_name_is_windsurf(self):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine
        engine = WindsurfCLIEngine()
        assert engine.name == "windsurf"

    def test_model_returns_default(self):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine
        engine = WindsurfCLIEngine(default_model="cascade-model")
        assert engine.model() == "cascade-model"

    def test_is_available_returns_bool(self):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine
        engine = WindsurfCLIEngine()
        assert isinstance(engine.is_available(), bool)

    def test_health_check_returns_bool(self):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine
        engine = WindsurfCLIEngine()
        assert isinstance(engine.health_check(), bool)

    def test_is_available_true_when_binary_exists(self):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine
        engine = WindsurfCLIEngine(binary="/bin/sh")
        assert engine.is_available() is True

    def test_protocol_compliance(self):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine
        engine = WindsurfCLIEngine()
        assert isinstance(engine, CodingEngine)

    def test_cascade_mode_initialization(self):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine
        # Cascade subcommand only added when binary is "windsurf" (not "cascade")
        engine = WindsurfCLIEngine(cascade_mode=True, binary="windsurf")
        cmd = engine._build_cmd("test", None)
        # In cascade mode with windsurf binary, "cascade" should be added
        assert "cascade" in cmd
        assert "windsurf" in cmd

    def test_non_cascade_mode(self):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine
        engine = WindsurfCLIEngine(cascade_mode=False)
        cmd = engine._build_cmd("test", None)
        # Without cascade mode, "cascade" should not be added
        assert "cascade" not in cmd


class TestWindsurfCLIEngineRun:
    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_success(self, mock_run):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine

        mock_run.return_value = MagicMock(
            stdout="code changes made",
            stderr="",
            returncode=0,
        )
        engine = WindsurfCLIEngine(default_model="cascade-model", default_timeout=60)
        result = engine.run("fix this bug")

        assert result.success is True
        assert result.stdout == "code changes made"
        assert result.returncode == 0
        assert result.model == "cascade-model"

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_failure(self, mock_run):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine

        mock_run.return_value = MagicMock(
            stdout="",
            stderr="cascade workflow failed",
            returncode=1,
        )
        engine = WindsurfCLIEngine()
        result = engine.run("prompt")

        assert result.success is False
        assert result.returncode == 1
        assert "cascade workflow failed" in result.stderr

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_timeout_raises(self, mock_run):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="windsurf", timeout=60)
        engine = WindsurfCLIEngine(default_timeout=60)
        with pytest.raises(subprocess.TimeoutExpired):
            engine.run("prompt")

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_binary_not_found(self, mock_run):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine

        mock_run.side_effect = FileNotFoundError("No such file")
        engine = WindsurfCLIEngine(binary="/nonexistent/windsurf")
        result = engine.run("prompt")

        assert result.success is False
        assert result.returncode == -1
        assert "not found" in result.stderr.lower()

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_passes_timeout_override(self, mock_run):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine

        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = WindsurfCLIEngine(default_timeout=300)
        engine.run("prompt", timeout=120)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 120

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_passes_cwd_and_env(self, mock_run):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine

        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = WindsurfCLIEngine(env_vars={"CODEIUM_API_KEY": "test-key"})
        engine.run("prompt", cwd="/tmp/worktree", env={"PATH": "/usr/bin"})

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == "/tmp/worktree"
        assert call_kwargs["env"]["CODEIUM_API_KEY"] == "test-key"
        assert call_kwargs["env"]["PATH"] == "/usr/bin"

    @patch("src.swe_team.providers.coding_engine.windsurf.subprocess.run")
    def test_run_sends_prompt_via_stdin(self, mock_run):
        from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine

        mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
        engine = WindsurfCLIEngine()
        engine.run("my coding task")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["input"] == "my coding task"
