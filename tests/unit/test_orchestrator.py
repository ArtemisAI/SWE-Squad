"""Tests for OrchestratorAgent — plan parsing, checklist rendering, progress tracking."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.orchestrator import (
    OrchestratorAgent,
    OrchestrationPlan,
    SubTask,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(**overrides) -> SWETicket:
    defaults = dict(
        ticket_id="test-001",
        title="Fix broken scraper",
        description="The scraper throws IndexError on empty pages",
        severity=TicketSeverity.CRITICAL,
        status=TicketStatus.INVESTIGATING,
        source_module="scraper",
        error_log="IndexError: list index out of range",
        metadata={"github_issue": 42},
    )
    defaults.update(overrides)
    return SWETicket(**defaults)


# ---------------------------------------------------------------------------
# SubTask tests
# ---------------------------------------------------------------------------

class TestSubTask:
    def test_default_values(self):
        t = SubTask()
        assert t.id == ""
        assert t.model == "sonnet"
        assert t.status == "pending"
        assert t.files_to_read == []
        assert t.files_to_modify == []

    def test_status_transitions(self):
        t = SubTask(id="1", description="Write tests", status="pending")
        t.status = "running"
        assert t.status == "running"
        t.status = "completed"
        assert t.status == "completed"

    def test_status_failed(self):
        t = SubTask(id="2", description="Deploy", status="running")
        t.status = "failed"
        assert t.status == "failed"


# ---------------------------------------------------------------------------
# OrchestrationPlan tests
# ---------------------------------------------------------------------------

class TestOrchestrationPlan:
    def test_checklist_with_pending_tasks(self):
        plan = OrchestrationPlan(
            ticket_id="t-1",
            session_tag="SWE-SQUAD-ISSUE#42",
            sub_tasks=[
                SubTask(id="1", description="Read error logs", model="haiku", status="pending"),
                SubTask(id="2", description="Fix parser", model="sonnet", status="pending"),
            ],
        )
        checklist = plan.to_checklist()
        assert "## Orchestration Plan" in checklist
        assert "`SWE-SQUAD-ISSUE#42`" in checklist
        assert "- [ ] [haiku] Read error logs" in checklist
        assert "- [ ] [sonnet] Fix parser" in checklist

    def test_checklist_with_completed_task(self):
        plan = OrchestrationPlan(
            ticket_id="t-2",
            session_tag="SWE-SQUAD-TICKET-abc123",
            sub_tasks=[
                SubTask(id="1", description="Investigate root cause", model="sonnet", status="completed"),
                SubTask(id="2", description="Write fix", model="sonnet", status="running"),
            ],
        )
        checklist = plan.to_checklist()
        assert "- [x] [sonnet] Investigate root cause" in checklist
        assert "- [ ] [sonnet] Write fix" in checklist
        # Running status should be shown
        assert "running" in checklist

    def test_checklist_with_analysis(self):
        plan = OrchestrationPlan(
            ticket_id="t-3",
            session_tag="SWE-SQUAD-ISSUE#10",
            analysis="Root cause is a missing null check in parser.py",
            sub_tasks=[SubTask(id="1", description="Fix it", model="sonnet")],
        )
        checklist = plan.to_checklist()
        assert "### Analysis" in checklist
        assert "missing null check" in checklist

    def test_checklist_analysis_truncated_at_500(self):
        long_analysis = "A" * 600
        plan = OrchestrationPlan(
            ticket_id="t-4",
            session_tag="SWE-SQUAD-ISSUE#99",
            analysis=long_analysis,
            sub_tasks=[SubTask(id="1", description="Fix")],
        )
        checklist = plan.to_checklist()
        # The analysis in the checklist should be truncated to 500 chars
        analysis_section = checklist.split("### Analysis\n")[1].split("\n\n")[0]
        assert len(analysis_section) == 500

    def test_checklist_no_analysis(self):
        plan = OrchestrationPlan(
            ticket_id="t-5",
            session_tag="tag",
            analysis="",
            sub_tasks=[SubTask(id="1", description="Do work")],
        )
        checklist = plan.to_checklist()
        assert "### Analysis" not in checklist


# ---------------------------------------------------------------------------
# _parse_plan tests
# ---------------------------------------------------------------------------

class TestParsePlan:
    def setup_method(self):
        self.agent = OrchestratorAgent(claude_path="/usr/bin/claude", repo_root=Path("/tmp"))

    def test_parse_empty_input_fallback(self):
        plan = self.agent._parse_plan("", "ticket-1", "SWE-SQUAD-ISSUE#1")
        assert len(plan.sub_tasks) == 1
        assert plan.sub_tasks[0].model == "sonnet"
        assert "falling back" in plan.analysis.lower()

    def test_parse_single_task(self):
        raw = "TASK: Fix the parser | MODEL: sonnet | READ: parser.py | MODIFY: parser.py"
        plan = self.agent._parse_plan(raw, "t-1", "tag-1")
        assert len(plan.sub_tasks) == 1
        t = plan.sub_tasks[0]
        assert t.description == "Fix the parser"
        assert t.model == "sonnet"
        assert "parser.py" in t.files_to_read
        assert "parser.py" in t.files_to_modify

    def test_parse_multiple_tasks(self):
        raw = (
            "Some analysis text\n"
            "TASK: Read logs | MODEL: haiku | READ: logs.py\n"
            "TASK: Write fix | MODEL: sonnet | READ: main.py | MODIFY: main.py\n"
            "TASK: Run tests | MODEL: haiku | READ: test_main.py\n"
        )
        plan = self.agent._parse_plan(raw, "t-2", "tag-2")
        assert len(plan.sub_tasks) == 3
        assert plan.sub_tasks[0].model == "haiku"
        assert plan.sub_tasks[1].model == "sonnet"
        assert plan.sub_tasks[2].model == "haiku"
        assert "analysis text" in plan.analysis

    def test_parse_task_ids_sequential(self):
        raw = "TASK: A | MODEL: sonnet\nTASK: B | MODEL: haiku\nTASK: C | MODEL: sonnet\n"
        plan = self.agent._parse_plan(raw, "t-3", "tag-3")
        assert [t.id for t in plan.sub_tasks] == ["1", "2", "3"]

    def test_parse_no_task_lines_fallback(self):
        raw = "Here is my analysis but I forgot to use the TASK format."
        plan = self.agent._parse_plan(raw, "t-4", "tag-4")
        assert len(plan.sub_tasks) == 1
        assert plan.sub_tasks[0].description == "Investigate and fix"

    def test_parse_multiple_read_files(self):
        raw = "TASK: Review code | MODEL: haiku | READ: a.py, b.py, c.py | MODIFY: a.py"
        plan = self.agent._parse_plan(raw, "t-5", "tag-5")
        assert plan.sub_tasks[0].files_to_read == ["a.py", "b.py", "c.py"]
        assert plan.sub_tasks[0].files_to_modify == ["a.py"]

    def test_parse_task_default_model(self):
        raw = "TASK: Do something without model spec"
        plan = self.agent._parse_plan(raw, "t-6", "tag-6")
        assert plan.sub_tasks[0].model == "sonnet"


# ---------------------------------------------------------------------------
# _build_plan_prompt tests
# ---------------------------------------------------------------------------

class TestBuildPlanPrompt:
    def setup_method(self):
        self.agent = OrchestratorAgent(claude_path="/usr/bin/claude", repo_root=Path("/tmp"))

    def test_prompt_includes_ticket_title(self):
        ticket = _make_ticket(title="Fix broken login flow")
        prompt = self.agent._build_plan_prompt(ticket)
        assert "Fix broken login flow" in prompt

    def test_prompt_includes_severity(self):
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        prompt = self.agent._build_plan_prompt(ticket)
        assert "critical" in prompt

    def test_prompt_includes_module(self):
        ticket = _make_ticket(source_module="auth_service")
        prompt = self.agent._build_plan_prompt(ticket)
        assert "auth_service" in prompt

    def test_prompt_includes_error_log(self):
        ticket = _make_ticket(error_log="KeyError: 'user_id'")
        prompt = self.agent._build_plan_prompt(ticket)
        assert "KeyError" in prompt

    def test_prompt_truncates_long_description(self):
        ticket = _make_ticket(description="X" * 1000)
        prompt = self.agent._build_plan_prompt(ticket)
        # Description is truncated to 500 chars
        assert "X" * 500 in prompt
        assert "X" * 501 not in prompt


# ---------------------------------------------------------------------------
# Model boundary enforcement
# ---------------------------------------------------------------------------

class TestModelBoundary:
    def setup_method(self):
        self.agent = OrchestratorAgent(claude_path="/usr/bin/echo", repo_root=Path("/tmp"))

    @patch("src.swe_team.orchestrator.subprocess.run")
    def test_code_task_with_claude_model_allowed(self, mock_run):
        mock_run.return_value = MagicMock(stdout="result", returncode=0)
        ticket = _make_ticket()
        task = SubTask(id="1", description="Implement the fix", model="sonnet")
        result = self.agent.execute_subtask(task, ticket)
        # Should not raise — sonnet is a Claude model
        assert mock_run.called

    def test_code_task_with_non_claude_model_blocked(self):
        ticket = _make_ticket()
        task = SubTask(id="1", description="Implement the parser fix", model="gemini")
        with pytest.raises(ValueError, match="MODEL BOUNDARY VIOLATION"):
            self.agent.execute_subtask(task, ticket)

    @patch("src.swe_team.orchestrator.subprocess.run")
    def test_non_code_task_skips_boundary_check(self, mock_run):
        """Tasks without code-generation keywords skip boundary enforcement."""
        mock_run.return_value = MagicMock(stdout="analysis", returncode=0)
        ticket = _make_ticket()
        task = SubTask(id="1", description="Analyze the logs", model="gemini")
        # Should not raise — "analyze" is not a code-gen keyword
        result = self.agent.execute_subtask(task, ticket)
        assert mock_run.called


# ---------------------------------------------------------------------------
# update_progress tests
# ---------------------------------------------------------------------------

class TestUpdateProgress:
    def setup_method(self):
        self.agent = OrchestratorAgent(claude_path="/usr/bin/echo", repo_root=Path("/tmp"))

    @patch("src.swe_team.orchestrator.update_github_comment")
    def test_update_calls_github(self, mock_update):
        mock_update.return_value = True
        plan = OrchestrationPlan(
            ticket_id="t-1",
            session_tag="tag",
            sub_tasks=[SubTask(id="1", description="Do work", status="completed")],
        )
        self.agent.update_progress(plan, comment_id=123, repo="owner/repo")
        mock_update.assert_called_once()
        body = mock_update.call_args[0][1]
        assert "[x]" in body

    def test_update_skips_without_comment_id(self):
        plan = OrchestrationPlan(ticket_id="t-1", session_tag="tag")
        # Should not raise even without comment_id
        self.agent.update_progress(plan, comment_id=None, repo="owner/repo")

    @patch("src.swe_team.orchestrator.update_github_comment", side_effect=Exception("API error"))
    def test_update_handles_github_failure_gracefully(self, mock_update):
        plan = OrchestrationPlan(
            ticket_id="t-1",
            session_tag="tag",
            sub_tasks=[SubTask(id="1", description="Work")],
        )
        # Should not raise
        self.agent.update_progress(plan, comment_id=456, repo="owner/repo")
