"""Unit tests for src/swe_team/github_integration.py."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from subprocess import TimeoutExpired
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team import github_integration


@pytest.fixture(autouse=True)
def _reset_github_circuit_breaker():
    github_integration._reset_github_circuit_breaker_state()
    yield
    github_integration._reset_github_circuit_breaker_state()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(severity=TicketSeverity.CRITICAL, **kwargs):
    defaults = dict(
        title="Something broke in prod",
        description="Full description here",
        severity=severity,
        source_module="auth",
        error_log="Traceback: RuntimeError",
        metadata={"repo": "owner/repo"},
    )
    defaults.update(kwargs)
    return SWETicket(**defaults)


def _proc(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# create_github_issue
# ---------------------------------------------------------------------------

class TestCreateGithubIssue:
    def test_returns_none_for_low_severity(self):
        ticket = _make_ticket(severity=TicketSeverity.LOW)
        result = github_integration.create_github_issue(ticket)
        assert result is None

    def test_returns_none_for_medium_severity(self):
        ticket = _make_ticket(severity=TicketSeverity.MEDIUM)
        result = github_integration.create_github_issue(ticket)
        assert result is None

    def test_success_critical_returns_issue_number(self):
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/42\n",
            )
            result = github_integration.create_github_issue(ticket)
        assert result == 42

    def test_success_high_severity_returns_issue_number(self):
        ticket = _make_ticket(severity=TicketSeverity.HIGH)
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/7\n",
            )
            result = github_integration.create_github_issue(ticket)
        assert result == 7

    def test_nonzero_returncode_returns_none(self):
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=1, stderr="authentication required")
            result = github_integration.create_github_issue(ticket)
        assert result is None

    def test_unparseable_output_returns_none(self):
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0, stdout="something unexpected\n")
            result = github_integration.create_github_issue(ticket)
        assert result is None

    def test_timeout_exception_returns_none(self):
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.side_effect = TimeoutExpired(cmd="gh", timeout=30)
            result = github_integration.create_github_issue(ticket)
        assert result is None

    def test_generic_exception_returns_none(self):
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("gh not found")
            result = github_integration.create_github_issue(ticket)
        assert result is None

    def test_fingerprint_included_in_body(self):
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        ticket.metadata["fingerprint"] = "abc123"
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/5\n",
            )
            github_integration.create_github_issue(ticket)
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        body_idx = cmd.index("--body") + 1
        assert "fingerprint:abc123" in cmd[body_idx]

    def test_auth_failure_calls_record_auth_failure(self):
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        provider = MagicMock()
        github_integration.set_auth_provider(provider)
        try:
            with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
                mock_run.return_value = _proc(returncode=1, stderr="401 unauthorized")
                github_integration.create_github_issue(ticket)
            provider.record_auth_failure.assert_called_once()
        finally:
            github_integration.set_auth_provider(None)

    def test_auth_success_calls_record_auth_success(self):
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        provider = MagicMock()
        github_integration.set_auth_provider(provider)
        try:
            with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
                mock_run.return_value = _proc(
                    returncode=0,
                    stdout="https://github.com/owner/repo/issues/10\n",
                )
                github_integration.create_github_issue(ticket)
            provider.record_auth_success.assert_called_once_with("github")
        finally:
            github_integration.set_auth_provider(None)


# ---------------------------------------------------------------------------
# comment_on_issue
# ---------------------------------------------------------------------------

class TestCommentOnIssue:
    def test_success_returns_true(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0)
            result = github_integration.comment_on_issue(42, "Great job!", repo="owner/repo")
        assert result is True

    def test_nonzero_returncode_returns_false(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=1, stderr="repo not found")
            result = github_integration.comment_on_issue(42, "hello", repo="owner/repo")
        assert result is False

    def test_exception_returns_false(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.side_effect = TimeoutExpired(cmd="gh", timeout=15)
            result = github_integration.comment_on_issue(99, "hi", repo="owner/repo")
        assert result is False

    def test_correct_issue_number_in_command(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0)
            github_integration.comment_on_issue(77, "test body", repo="owner/repo")
        cmd = mock_run.call_args[0][0]
        assert "77" in cmd

    def test_no_repo_returns_false(self):
        """comment_on_issue must refuse when no repo is provided."""
        result = github_integration.comment_on_issue(42, "hello")
        assert result is False


class TestGithubCircuitBreaker:
    def test_trips_after_three_failures_and_pauses_operations(self):
        with (
            patch("src.swe_team.github_integration.subprocess.run", return_value=_proc(returncode=1, stderr="auth failed")) as mock_run,
            patch("src.swe_team.github_integration._send_telegram_alert") as mock_alert,
        ):
            assert github_integration.comment_on_issue(1, "a", repo="owner/repo") is False
            assert github_integration.comment_on_issue(2, "b", repo="owner/repo") is False
            assert github_integration.comment_on_issue(3, "c", repo="owner/repo") is False
            assert github_integration._GH_PAUSED_UNTIL is not None
            assert github_integration.comment_on_issue(4, "d", repo="owner/repo") is False

        assert mock_run.call_count == 3
        mock_alert.assert_called_once()

    def test_auto_retries_after_pause_window(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        github_integration._GH_PAUSED_UNTIL = now - timedelta(seconds=1)
        github_integration._GH_CONSECUTIVE_FAILURES = 3
        github_integration._GH_FIRST_FAILURE_AT = now - timedelta(minutes=5)
        with (
            patch("src.swe_team.github_integration._now_utc", return_value=now),
            patch("src.swe_team.github_integration.subprocess.run", return_value=_proc(returncode=0)),
        ):
            assert github_integration.comment_on_issue(10, "ok", repo="owner/repo") is True
        assert github_integration._GH_PAUSED_UNTIL is None
        assert github_integration._GH_CONSECUTIVE_FAILURES == 0

    def test_sends_hitl_escalation_after_thirty_minutes(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        github_integration._GH_FIRST_FAILURE_AT = now - timedelta(minutes=31)
        github_integration._GH_CONSECUTIVE_FAILURES = 3
        github_integration._GH_PAUSED_UNTIL = now + timedelta(minutes=1)
        with (
            patch("src.swe_team.github_integration._now_utc", return_value=now),
            patch("src.swe_team.github_integration._send_telegram_alert") as mock_alert,
        ):
            assert github_integration.comment_on_issue(10, "still paused", repo="owner/repo") is False
        assert mock_alert.called
        assert "HITL escalation" in mock_alert.call_args_list[-1].args[0]


# ---------------------------------------------------------------------------
# update_github_comment
# ---------------------------------------------------------------------------

class TestUpdateGithubComment:
    def test_success_returns_true(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0)
            result = github_integration.update_github_comment(
                101, "new body", repo="owner/repo"
            )
        assert result is True

    def test_nonzero_returncode_returns_false(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=1, stderr="not found")
            result = github_integration.update_github_comment(
                101, "body", repo="owner/repo"
            )
        assert result is False

    def test_missing_repo_returns_false_without_subprocess(self):
        original_repo = github_integration._REPO
        github_integration._REPO = ""
        try:
            with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
                result = github_integration.update_github_comment(101, "body", repo="")
            mock_run.assert_not_called()
            assert result is False
        finally:
            github_integration._REPO = original_repo

    def test_zero_comment_id_returns_false(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            result = github_integration.update_github_comment(0, "body", repo="owner/repo")
        assert result is False

    def test_timeout_returns_false(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.side_effect = TimeoutExpired(cmd="gh", timeout=20)
            result = github_integration.update_github_comment(
                55, "body", repo="owner/repo"
            )
        assert result is False


# ---------------------------------------------------------------------------
# find_existing_issue
# ---------------------------------------------------------------------------

class TestFindExistingIssue:
    def test_finds_by_fingerprint(self):
        ticket = _make_ticket(severity=TicketSeverity.HIGH, title="DB timeout")
        ticket.metadata["fingerprint"] = "fp-xyz"
        issues = [
            {"number": 12, "title": "[SWE-AUTO] DB timeout", "body": "<!-- fingerprint:fp-xyz -->"},
        ]
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0, stdout=json.dumps(issues))
            result = github_integration.find_existing_issue(ticket)
        assert result == 12

    def test_finds_by_title_fallback(self):
        ticket = _make_ticket(severity=TicketSeverity.HIGH, title="Database failure")
        issues = [
            {"number": 99, "title": "[SWE-AUTO] Database failure in prod", "body": "no fp"},
        ]
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0, stdout=json.dumps(issues))
            result = github_integration.find_existing_issue(ticket)
        assert result == 99

    def test_returns_none_when_no_match(self):
        ticket = _make_ticket(severity=TicketSeverity.HIGH, title="Unique title xyz")
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0, stdout=json.dumps([]))
            result = github_integration.find_existing_issue(ticket)
        assert result is None

    def test_returns_none_on_subprocess_failure(self):
        ticket = _make_ticket(severity=TicketSeverity.HIGH)
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=1, stderr="error")
            result = github_integration.find_existing_issue(ticket)
        assert result is None

    def test_returns_none_on_exception(self):
        ticket = _make_ticket(severity=TicketSeverity.HIGH)
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("gh not found")
            result = github_integration.find_existing_issue(ticket)
        assert result is None


# ---------------------------------------------------------------------------
# escalate_to_human
# ---------------------------------------------------------------------------

class TestEscalateToHuman:
    def test_no_repo_returns_false(self):
        original = github_integration._REPO
        github_integration._REPO = ""
        try:
            result = github_integration.escalate_to_human(1, "T-001", "manual review needed", repo="")
            assert result is False
        finally:
            github_integration._REPO = original

    def test_success_when_comment_succeeds(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0)
            result = github_integration.escalate_to_human(
                10, "T-001", "needs creds", repo="owner/repo"
            )
        assert result is True

    def test_returns_false_when_comment_fails(self):
        call_count = [0]
        def side_effect(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _proc(returncode=1, stderr="auth error")
            return _proc(returncode=0)

        with patch("src.swe_team.github_integration.subprocess.run", side_effect=side_effect):
            result = github_integration.escalate_to_human(
                10, "T-001", "reason", repo="owner/repo"
            )
        assert result is False


# ---------------------------------------------------------------------------
# claim_issue
# ---------------------------------------------------------------------------

class TestClaimIssue:
    def test_returns_existing_comment_id_without_posting(self):
        with patch(
            "src.swe_team.github_integration._find_existing_swe_comment",
            return_value=12345,
        ) as mock_existing, patch(
            "src.swe_team.github_integration.subprocess.run"
        ) as mock_run:
            result = github_integration.claim_issue(
                issue_number=303,
                ticket_id="T-303",
                trace_id="trace303",
                ticket_type="bug",
                checklist=["Step A"],
                repo="owner/repo",
            )

        mock_existing.assert_called_once_with(303, "owner/repo")
        mock_run.assert_not_called()
        assert result == 12345

    def test_posts_comment_and_returns_parsed_id(self):
        with patch(
            "src.swe_team.github_integration._find_existing_swe_comment",
            return_value=None,
        ), patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(
                returncode=0,
                stdout=json.dumps({"id": 777}),
            )
            result = github_integration.claim_issue(
                issue_number=303,
                ticket_id="T-303",
                trace_id="trace303",
                ticket_type="bug",
                checklist=["Step A"],
                repo="owner/repo",
            )

        assert result == 777


# ---------------------------------------------------------------------------
# find_comment_by_text
# ---------------------------------------------------------------------------

class TestFindCommentByText:
    def test_returns_comment_id_when_found(self):
        comments = [
            {"id": 100, "body": "some header\nstuff"},
            {"id": 200, "body": "## Status Update\n**Ticket ID:** `T-1`"},
        ]
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(
                returncode=0,
                stdout=json.dumps(comments),
            )
            result = github_integration.find_comment_by_text(42, "Ticket ID", repo="owner/repo")
        assert result == 200

    def test_returns_none_when_not_found(self):
        comments = [{"id": 100, "body": "unrelated comment"}]
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(
                returncode=0,
                stdout=json.dumps(comments),
            )
            result = github_integration.find_comment_by_text(42, "missing text", repo="owner/repo")
        assert result is None

    def test_returns_none_on_subprocess_failure(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=1, stderr="error")
            result = github_integration.find_comment_by_text(42, "text", repo="owner/repo")
        assert result is None

    def test_returns_none_when_no_repo(self):
        original = github_integration._REPO
        github_integration._REPO = ""
        try:
            result = github_integration.find_comment_by_text(42, "text", repo="")
            assert result is None
        finally:
            github_integration._REPO = original


# ---------------------------------------------------------------------------
# _post_or_update_status (runner helper)
# ---------------------------------------------------------------------------

class TestPostOrUpdateStatus:
    """Test that _post_or_update_status routes through progress_comment_id."""

    def test_updates_existing_comment_when_progress_id_set(self):
        """When ticket has progress_comment_id, update_github_comment is called."""
        ticket = _make_ticket()
        ticket.metadata["github_issue"] = 42
        ticket.metadata["progress_comment_id"] = 999
        ticket.metadata["repo"] = "owner/repo"

        with patch("scripts.ops.swe_team_runner.update_github_comment") as mock_update:
            mock_update.return_value = True
            from scripts.ops.swe_team_runner import _post_or_update_status
            _post_or_update_status(ticket, "## Status\nAll good")

        mock_update.assert_called_once_with(999, "## Status\nAll good", repo="owner/repo")

    def test_falls_back_to_new_comment_when_no_progress_id(self):
        """When ticket has no progress_comment_id, falls back to comment_on_github_issue."""
        ticket = _make_ticket()
        ticket.metadata["github_issue"] = 42
        ticket.metadata["repo"] = "owner/repo"

        with patch("scripts.ops.swe_team_runner.comment_on_github_issue") as mock_comment, \
             patch("scripts.ops.swe_team_runner.update_github_comment") as mock_update:
            from scripts.ops.swe_team_runner import _post_or_update_status
            _post_or_update_status(ticket, "## Status\nNew info")

        mock_update.assert_not_called()
        mock_comment.assert_called_once_with(42, "## Status\nNew info", repo="owner/repo")

    def test_falls_back_when_update_fails(self):
        """When update_github_comment returns False, falls back to comment_on_github_issue."""
        ticket = _make_ticket()
        ticket.metadata["github_issue"] = 42
        ticket.metadata["progress_comment_id"] = 999
        ticket.metadata["repo"] = "owner/repo"

        with patch("scripts.ops.swe_team_runner.update_github_comment") as mock_update, \
             patch("scripts.ops.swe_team_runner.comment_on_github_issue") as mock_comment:
            mock_update.return_value = False
            from scripts.ops.swe_team_runner import _post_or_update_status
            _post_or_update_status(ticket, "## Fail\nSomething")

        mock_update.assert_called_once()
        mock_comment.assert_called_once()

    def test_noop_when_no_github_issue(self):
        """When ticket has no github_issue, nothing is called."""
        ticket = _make_ticket()

        with patch("scripts.ops.swe_team_runner.update_github_comment") as mock_update, \
             patch("scripts.ops.swe_team_runner.comment_on_github_issue") as mock_comment:
            from scripts.ops.swe_team_runner import _post_or_update_status
            _post_or_update_status(ticket, "## Status\nShould not post")

        mock_update.assert_not_called()
        mock_comment.assert_not_called()

    def test_uses_explicit_repo_over_ticket_metadata(self):
        """When repo kwarg is passed, it takes precedence over ticket metadata."""
        ticket = _make_ticket()
        ticket.metadata["github_issue"] = 42
        ticket.metadata["progress_comment_id"] = 999
        ticket.metadata["repo"] = "wrong/repo"

        with patch("scripts.ops.swe_team_runner.update_github_comment") as mock_update:
            mock_update.return_value = True
            from scripts.ops.swe_team_runner import _post_or_update_status
            _post_or_update_status(ticket, "body", repo="correct/repo")

        mock_update.assert_called_once_with(999, "body", repo="correct/repo")


# ---------------------------------------------------------------------------
# close_github_issue
# ---------------------------------------------------------------------------

class TestCloseGithubIssue:
    def test_success_returns_true(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0)
            result = github_integration.close_github_issue("owner/repo", 42)
        assert result is True

    def test_nonzero_returncode_returns_false(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=1, stderr="issue already closed")
            result = github_integration.close_github_issue("owner/repo", 42)
        assert result is False

    def test_no_repo_returns_false_without_subprocess(self):
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            result = github_integration.close_github_issue("", 42)
        mock_run.assert_not_called()
        assert result is False

    def test_exception_returns_false(self):
        from subprocess import TimeoutExpired
        with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
            mock_run.side_effect = TimeoutExpired(cmd="gh", timeout=15)
            result = github_integration.close_github_issue("owner/repo", 42)
        assert result is False

    def test_with_comment_posts_comment_then_closes(self):
        """When a comment is provided, both comment and close commands are issued."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _proc(returncode=0)

        with patch("src.swe_team.github_integration.subprocess.run", side_effect=fake_run):
            result = github_integration.close_github_issue(
                "owner/repo", 10, comment="Resolved by SWE-Squad"
            )

        assert result is True
        assert len(calls) == 2
        # First call: comment
        assert "comment" in calls[0]
        assert "Resolved by SWE-Squad" in calls[0]
        # Second call: close
        assert "close" in calls[1]

    def test_without_comment_only_closes(self):
        """When no comment is provided, only the close command is issued."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _proc(returncode=0)

        with patch("src.swe_team.github_integration.subprocess.run", side_effect=fake_run):
            result = github_integration.close_github_issue("owner/repo", 5)

        assert result is True
        assert len(calls) == 1
        assert "close" in calls[0]

    def test_comment_failure_still_attempts_close(self):
        """Even if the comment fails, the close command is still issued."""
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _proc(returncode=1, stderr="comment failed")
            return _proc(returncode=0)

        with patch("src.swe_team.github_integration.subprocess.run", side_effect=fake_run):
            result = github_integration.close_github_issue(
                "owner/repo", 7, comment="Resolved"
            )

        assert result is True
        assert call_count[0] == 2

    def test_auth_failure_records_auth_failure(self):
        provider = MagicMock()
        github_integration.set_auth_provider(provider)
        try:
            with patch("src.swe_team.github_integration.subprocess.run") as mock_run:
                mock_run.return_value = _proc(returncode=1, stderr="401 unauthorized")
                github_integration.close_github_issue("owner/repo", 99)
            provider.record_auth_failure.assert_called_once()
        finally:
            github_integration.set_auth_provider(None)

    def test_close_command_includes_issue_number_and_repo(self):
        """The gh close command must include the correct issue number and repo."""
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return _proc(returncode=0)

        with patch("src.swe_team.github_integration.subprocess.run", side_effect=fake_run):
            github_integration.close_github_issue("myorg/myrepo", 123)

        close_cmd = captured[-1]
        assert "123" in close_cmd
        assert "myorg/myrepo" in close_cmd
