"""Unit tests for src/swe_team/qa_agent.py.

Covers:
- QAAgent.run_qa() happy path (all checks pass)
- QAAgent.run_qa() partial failure (some checks fail)
- Unknown check name handling
- Disabled agent auto-approves
- CheckResult / QAResult dataclass fields
- Missing working directory handling
- Regression baseline comparison
- Timeout handling
- _is_webui_ticket helper in reviewer.py
- _run_qa_gate integration in reviewer.py
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.qa_agent import CheckResult, QAAgent, QAResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ticket(
    ticket_id: str = "t-qa-test",
    severity: TicketSeverity = TicketSeverity.MEDIUM,
    status: TicketStatus = TicketStatus.IN_REVIEW,
    **kwargs,
) -> SWETicket:
    defaults = dict(
        ticket_id=ticket_id,
        title="Test QA bug",
        description="Something broke in the UI",
        severity=severity,
        status=status,
        investigation_report="Root cause: xyz. " * 20,
        metadata={"resolution_note": "fix_succeeded"},
    )
    defaults.update(kwargs)
    return SWETicket(**defaults)


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------

class TestCheckResult:
    def test_fields(self):
        cr = CheckResult(name="pytest", passed=True, duration_s=1.5, output="ok")
        assert cr.name == "pytest"
        assert cr.passed is True
        assert cr.duration_s == 1.5
        assert cr.output == "ok"

    def test_failed_check(self):
        cr = CheckResult(name="build", passed=False, duration_s=0.0, output="error")
        assert cr.passed is False


class TestQAResult:
    def test_approved(self):
        r = QAResult(approved=True, checks=[], summary="all good")
        assert r.approved is True
        assert r.summary == "all good"

    def test_rejected(self):
        cr = CheckResult(name="build", passed=False, duration_s=0.1, output="fail")
        r = QAResult(approved=False, checks=[cr], summary="build failed")
        assert r.approved is False
        assert len(r.checks) == 1

    def test_default_fields(self):
        r = QAResult(approved=True)
        assert r.checks == []
        assert r.summary == ""


# ---------------------------------------------------------------------------
# QAAgent.run_qa() tests
# ---------------------------------------------------------------------------

class TestQAAgentDisabled:
    def test_disabled_auto_approves(self):
        qa = QAAgent(enabled=False)
        result = qa.run_qa(_ticket(), "/nonexistent/path")
        assert result.approved is True
        assert result.summary == "QA agent disabled"
        assert result.checks == []


class TestQAAgentUnknownCheck:
    def test_unknown_check_fails(self):
        qa = QAAgent(checks=["nonexistent_check"])
        result = qa.run_qa(_ticket(), "/tmp")
        assert result.approved is False
        assert len(result.checks) == 1
        assert result.checks[0].name == "nonexistent_check"
        assert result.checks[0].passed is False
        assert "Unknown check" in result.checks[0].output


class TestQAAgentRunChecks:
    @patch("src.swe_team.qa_agent.subprocess.run")
    def test_all_checks_pass(self, mock_run, tmp_path):
        """All configured checks pass → approved=True."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="827 passed", stderr=""
        )
        # Create ui/ subdir so typescript/build checks find their cwd
        (tmp_path / "ui").mkdir()

        qa = QAAgent(checks=["pytest", "typescript", "build"])
        result = qa.run_qa(_ticket(), str(tmp_path))

        assert result.approved is True
        assert len(result.checks) == 3
        assert all(c.passed for c in result.checks)

    @patch("src.swe_team.qa_agent.subprocess.run")
    def test_one_check_fails(self, mock_run, tmp_path):
        """One check fails → approved=False."""
        (tmp_path / "ui").mkdir()

        def side_effect(cmd, **kwargs):
            if "tsc" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="TS2304: Cannot find name 'foo'"
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="827 passed", stderr=""
            )

        mock_run.side_effect = side_effect

        qa = QAAgent(checks=["pytest", "typescript"])
        result = qa.run_qa(_ticket(), str(tmp_path))

        assert result.approved is False
        assert result.checks[0].passed is True   # pytest
        assert result.checks[1].passed is False   # typescript

    @patch("src.swe_team.qa_agent.subprocess.run")
    def test_timeout_fails_check(self, mock_run, tmp_path):
        """Timeout results in a failed check."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="pytest", timeout=10)

        qa = QAAgent(checks=["pytest"], timeout=10)
        result = qa.run_qa(_ticket(), str(tmp_path))

        assert result.approved is False
        assert "timed out" in result.checks[0].output

    def test_missing_cwd_fails(self, tmp_path):
        """Check with missing working directory fails gracefully."""
        # Don't create ui/ subdir
        qa = QAAgent(checks=["typescript"])
        result = qa.run_qa(_ticket(), str(tmp_path))

        assert result.approved is False
        assert "does not exist" in result.checks[0].output

    @patch("src.swe_team.qa_agent.subprocess.run")
    def test_command_not_found(self, mock_run, tmp_path):
        """FileNotFoundError (command not found) fails gracefully."""
        mock_run.side_effect = FileNotFoundError("npx not found")
        (tmp_path / "ui").mkdir()

        qa = QAAgent(checks=["typescript"])
        result = qa.run_qa(_ticket(), str(tmp_path))

        assert result.approved is False
        assert "not found" in result.checks[0].output.lower()

    @patch("src.swe_team.qa_agent.subprocess.run")
    def test_checks_override_in_run_qa(self, mock_run, tmp_path):
        """Passing checks= to run_qa() overrides instance checks."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="827 passed", stderr=""
        )

        qa = QAAgent(checks=["typescript", "build"])
        result = qa.run_qa(_ticket(), str(tmp_path), checks=["pytest"])

        assert len(result.checks) == 1
        assert result.checks[0].name == "pytest"

    @patch("src.swe_team.qa_agent.subprocess.run")
    def test_summary_includes_failure_output(self, mock_run, tmp_path):
        """Summary includes truncated output for failed checks."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="ERRORS FOUND", stderr="details here"
        )

        qa = QAAgent(checks=["pytest"])
        result = qa.run_qa(_ticket(), str(tmp_path))

        assert "FAIL" in result.summary
        assert "pytest" in result.summary


class TestRegressionBaseline:
    @patch("src.swe_team.qa_agent.subprocess.run")
    def test_regression_pass_above_baseline(self, mock_run, tmp_path):
        """Regression check passes when pass count >= baseline."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="830 passed, 2 warnings in 45.6s", stderr=""
        )

        qa = QAAgent(checks=["regression"], baseline_test_count=827)
        result = qa.run_qa(_ticket(), str(tmp_path))

        assert result.approved is True
        assert result.checks[0].passed is True

    @patch("src.swe_team.qa_agent.subprocess.run")
    def test_regression_fail_below_baseline(self, mock_run, tmp_path):
        """Regression check fails when pass count < baseline."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="800 passed, 27 failed in 45.6s", stderr=""
        )

        qa = QAAgent(checks=["regression"], baseline_test_count=827)
        result = qa.run_qa(_ticket(), str(tmp_path))

        assert result.approved is False
        assert result.checks[0].passed is False

    @patch("src.swe_team.qa_agent.subprocess.run")
    def test_regression_no_baseline_passes(self, mock_run, tmp_path):
        """Without a baseline, regression check only checks exit code."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="500 passed", stderr=""
        )

        qa = QAAgent(checks=["regression"], baseline_test_count=None)
        result = qa.run_qa(_ticket(), str(tmp_path))

        assert result.approved is True

    @patch("src.swe_team.qa_agent.subprocess.run")
    def test_regression_unparseable_output(self, mock_run, tmp_path):
        """Unparseable pytest output fails the regression check when baseline is set."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="no tests ran", stderr=""
        )

        qa = QAAgent(checks=["regression"], baseline_test_count=827)
        result = qa.run_qa(_ticket(), str(tmp_path))

        assert result.approved is False


# ---------------------------------------------------------------------------
# _is_webui_ticket helper tests
# ---------------------------------------------------------------------------

class TestIsWebuiTicket:
    def test_webui_label(self):
        from src.swe_team.reviewer import _is_webui_ticket
        t = _ticket(labels=["webui"])
        assert _is_webui_ticket(t) is True

    def test_frontend_label(self):
        from src.swe_team.reviewer import _is_webui_ticket
        t = _ticket(labels=["frontend"])
        assert _is_webui_ticket(t) is True

    def test_no_webui_label(self):
        from src.swe_team.reviewer import _is_webui_ticket
        t = _ticket(labels=["backend", "api"])
        assert _is_webui_ticket(t) is False

    def test_webui_in_source_module(self):
        from src.swe_team.reviewer import _is_webui_ticket
        t = _ticket(metadata={"resolution_note": "fix_succeeded", "source_module": "webui_dashboard"})
        assert _is_webui_ticket(t) is True

    def test_ui_path_in_proposed_fix(self):
        from src.swe_team.reviewer import _is_webui_ticket
        t = _ticket(proposed_fix="Modified ui/src/App.tsx to fix rendering")
        assert _is_webui_ticket(t) is True

    def test_no_webui_signals(self):
        from src.swe_team.reviewer import _is_webui_ticket
        t = _ticket(labels=[], proposed_fix="Fixed src/swe_team/monitor.py")
        assert _is_webui_ticket(t) is False


# ---------------------------------------------------------------------------
# _run_qa_gate integration tests
# ---------------------------------------------------------------------------

class TestRunQAGate:
    @patch("src.swe_team.qa_agent.QAAgent")
    def test_qa_pass_returns_false(self, MockQA):
        """QA passes → _run_qa_gate returns False (not rejected)."""
        from src.swe_team.reviewer import _run_qa_gate
        mock_qa_instance = MockQA.return_value
        mock_qa_instance.run_qa.return_value = QAResult(approved=True, summary="all good")

        store = MagicMock()
        result = _run_qa_gate(_ticket(), "/tmp", store, dry_run=False)
        assert result is False  # not rejected

    @patch("src.swe_team.qa_agent.QAAgent")
    def test_qa_fail_returns_true(self, MockQA):
        """QA fails → _run_qa_gate returns True (rejected) and transitions ticket."""
        from src.swe_team.reviewer import _run_qa_gate
        mock_qa_instance = MockQA.return_value
        mock_qa_instance.run_qa.return_value = QAResult(
            approved=False, summary="build failed"
        )

        store = MagicMock()
        t = _ticket()
        result = _run_qa_gate(t, "/tmp", store, dry_run=False)
        assert result is True  # rejected
        assert t.status == TicketStatus.IN_DEVELOPMENT
        assert "qa_failures" in t.metadata

    @patch("src.swe_team.qa_agent.QAAgent")
    def test_qa_fail_dry_run_no_mutation(self, MockQA):
        """In dry_run, QA failure returns True but does not mutate ticket."""
        from src.swe_team.reviewer import _run_qa_gate
        mock_qa_instance = MockQA.return_value
        mock_qa_instance.run_qa.return_value = QAResult(
            approved=False, summary="build failed"
        )

        store = MagicMock()
        t = _ticket()
        original_status = t.status
        result = _run_qa_gate(t, "/tmp", store, dry_run=True)
        assert result is True
        assert t.status == original_status  # not mutated
        store.save.assert_not_called()

    @patch("src.swe_team.qa_agent.QAAgent", side_effect=Exception("import failed"))
    def test_qa_exception_allows_approval(self, MockQA):
        """Exception in QA gate → returns False (allow approval to proceed)."""
        from src.swe_team.reviewer import _run_qa_gate
        store = MagicMock()
        result = _run_qa_gate(_ticket(), "/tmp", store, dry_run=False)
        assert result is False  # not rejected — fail-open
