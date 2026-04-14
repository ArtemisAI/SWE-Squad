"""Unit tests for DeveloperAgent.

Covers: attempt_fix(), _build_prompt(), _select_model(), _eligible(),
_ensure_branch(), _ensure_worktree(), _fix_loop(), fallback agents,
notifications, and keep/discard loop logic.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, call, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus, TicketType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_developer(**kwargs):
    """Create a DeveloperAgent with safe defaults for unit testing."""
    import os
    from src.swe_team.developer import DeveloperAgent

    ep = MagicMock()
    ep.build_env.return_value = os.environ.copy()

    defaults = dict(
        repo_root=Path("/tmp/fake-repo"),
        program_path=Path("/tmp/fake_fix.md"),
        claude_path="/usr/bin/fake-claude",
        max_attempts=3,
        test_command=["true"],  # Always passes
        env_provider=ep,
    )
    defaults.update(kwargs)
    return DeveloperAgent(**defaults)


def _make_ticket(
    severity=TicketSeverity.HIGH,
    status=TicketStatus.INVESTIGATION_COMPLETE,
    **kwargs,
):
    defaults = dict(
        ticket_id="T-DEV-TEST",
        title="Fix memory leak",
        description="Scraper leaks memory",
        severity=severity,
        status=status,
        investigation_report="Root cause: unclosed sessions. Fix: add close() call. " * 10,
    )
    defaults.update(kwargs)
    return SWETicket(**defaults)


# ---------------------------------------------------------------------------
# 1. attempt_fix() with subprocess success
# ---------------------------------------------------------------------------

class TestAttemptFixSuccess:
    @patch("src.swe_team.developer.check_fix_complexity", return_value=(True, ""))
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_successful_fix_sets_in_review(self, _perm, _boundary, _complexity):
        agent = _make_developer()
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/ticket-T-DEV-TEST"):
                with patch.object(agent, "_run_claude"):
                    with patch.object(agent, "_run_tests", return_value=(True, "")):
                        with patch.object(agent, "_diff_stats", return_value=(10, ["file.py"])):
                            with patch.object(agent, "_git", return_value="abc123\n"):
                                with patch.object(agent, "_record_automation"):
                                    result = agent.attempt_fix(ticket)

        assert result is True
        assert ticket.status == TicketStatus.IN_REVIEW

    @patch("src.swe_team.developer.check_fix_complexity", return_value=(True, ""))
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_successful_fix_records_branch(self, _perm, _boundary, _complexity):
        agent = _make_developer()
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/ticket-T-DEV-TEST"):
                with patch.object(agent, "_run_claude"):
                    with patch.object(agent, "_run_tests", return_value=(True, "")):
                        with patch.object(agent, "_diff_stats", return_value=(5, ["a.py"])):
                            with patch.object(agent, "_git", return_value="def456\n"):
                                with patch.object(agent, "_record_automation"):
                                    agent.attempt_fix(ticket)

        assert ticket.metadata["branch"] == "swe-fix/ticket-T-DEV-TEST"


# ---------------------------------------------------------------------------
# 2. attempt_fix() with subprocess failure
# ---------------------------------------------------------------------------

class TestAttemptFixFailure:
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_all_attempts_fail_sets_failed(self, _perm, _boundary):
        agent = _make_developer(max_attempts=1)
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/ticket-T-DEV-TEST"):
                with patch.object(agent, "_run_claude", side_effect=RuntimeError("CLI crashed")):
                    with patch.object(agent, "_git", return_value="abc123\n"):
                        with patch.object(agent, "_send_telegram"):
                            result = agent.attempt_fix(ticket)

        assert result is False
        assert ticket.status == TicketStatus.FAILED

    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_preflight_failure_returns_false(self, _perm, _boundary):
        agent = _make_developer()
        ticket = _make_ticket()

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=False, failures=["bad git"], summary=lambda: "bad git")
            result = agent.attempt_fix(ticket)

        assert result is False
        assert "preflight_failure" in ticket.metadata


# ---------------------------------------------------------------------------
# 3. _build_fix_prompt() includes investigation_report
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_prompt_includes_investigation_report(self):
        agent = _make_developer()
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket(investigation_report="Root cause: DB connection leak")

        prompt = agent._build_prompt(ticket)
        assert "DB connection leak" in prompt

    def test_prompt_none_when_template_missing(self):
        agent = _make_developer()
        # No cache, no file
        prompt = agent._build_prompt(_make_ticket())
        assert prompt is None

    def test_prompt_includes_last_error_on_retry(self):
        agent = _make_developer()
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        prompt = agent._build_prompt(ticket, last_error="Tests failed: assert 1 == 2", attempt=2)
        assert "Previous Attempt Failed" in prompt
        assert "assert 1 == 2" in prompt


# ---------------------------------------------------------------------------
# 4. Worktree acquisition
# ---------------------------------------------------------------------------

class TestWorktree:
    @patch("src.swe_team.developer.check_fix_complexity", return_value=(True, ""))
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_worktree_mode_calls_ensure_worktree(self, _perm, _boundary, _complexity):
        agent = _make_developer(use_worktree=True)
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_worktree", return_value="swe-fix/ticket-T-DEV-TEST") as mock_wt:
                with patch.object(agent, "_run_claude"):
                    with patch.object(agent, "_run_tests", return_value=(True, "")):
                        with patch.object(agent, "_diff_stats", return_value=(5, ["a.py"])):
                            with patch.object(agent, "_git", return_value="abc\n"):
                                with patch.object(agent, "_record_automation"):
                                    with patch.object(agent, "_cleanup_worktree"):
                                        agent.attempt_fix(ticket)

                mock_wt.assert_called_once_with(ticket)


# ---------------------------------------------------------------------------
# 5. Worktree release even on exception
# ---------------------------------------------------------------------------

class TestWorktreeCleanup:
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_worktree_cleaned_up_on_failure(self, _perm, _boundary):
        agent = _make_developer(use_worktree=True, max_attempts=1)
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        agent._active_worktree = Path("/tmp/fake-worktree")
        ticket = _make_ticket()

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_worktree", return_value="swe-fix/ticket-T-DEV-TEST"):
                with patch.object(agent, "_run_claude", side_effect=RuntimeError("boom")):
                    with patch.object(agent, "_git", return_value="abc\n"):
                        with patch.object(agent, "_cleanup_worktree") as mock_cleanup:
                            with patch.object(agent, "_send_telegram"):
                                agent.attempt_fix(ticket)

            mock_cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# 6. Keep/discard loop — tests fail, fix is discarded
# ---------------------------------------------------------------------------

class TestKeepDiscardLoop:
    @patch("src.swe_team.developer.check_fix_complexity", return_value=(True, ""))
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_failed_tests_discard_and_retry(self, _perm, _boundary, _complexity):
        agent = _make_developer(max_attempts=2)
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        git_calls = []

        def mock_git(cmd):
            git_calls.append(cmd)
            if cmd[1] == "rev-parse":
                return "abc123\n"
            return ""

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/test"):
                with patch.object(agent, "_run_claude"):
                    with patch.object(agent, "_run_tests", return_value=(False, "FAILED: assert false")):
                        with patch.object(agent, "_git", side_effect=mock_git):
                            with patch.object(agent, "_send_telegram"):
                                result = agent.attempt_fix(ticket)

        assert result is False
        # Should have called git reset --hard for each failed attempt
        reset_calls = [c for c in git_calls if "reset" in c]
        assert len(reset_calls) == 2  # One reset per attempt


# ---------------------------------------------------------------------------
# 7. Notification on fix
# ---------------------------------------------------------------------------

class TestDeveloperNotification:
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_escalation_sends_telegram(self, _perm, _boundary):
        notifier = MagicMock()
        agent = _make_developer(max_attempts=1, notifier=notifier)
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/test"):
                with patch.object(agent, "_run_claude", side_effect=RuntimeError("boom")):
                    with patch.object(agent, "_git", return_value="abc\n"):
                        agent.attempt_fix(ticket)

        notifier.send_alert.assert_called()


# ---------------------------------------------------------------------------
# 8. Cross-repo ticket
# ---------------------------------------------------------------------------

class TestCrossRepo:
    def test_repos_map_resolves_path(self):
        agent = _make_developer(repos_map={"your-org/example-app": "/opt/linkedai"})
        assert agent._repos_map["your-org/example-app"] == Path("/opt/linkedai")


# ---------------------------------------------------------------------------
# 9. _eligible() checks
# ---------------------------------------------------------------------------

class TestDeveloperEligible:
    def test_no_investigation_report_not_eligible(self):
        agent = _make_developer()
        ticket = _make_ticket(investigation_report=None)
        assert agent._eligible(ticket) is False

    def test_wrong_status_not_eligible(self):
        agent = _make_developer()
        ticket = _make_ticket(status=TicketStatus.OPEN)
        assert agent._eligible(ticket) is False

    def test_investigation_complete_is_eligible(self):
        agent = _make_developer()
        ticket = _make_ticket(status=TicketStatus.INVESTIGATION_COMPLETE)
        assert agent._eligible(ticket) is True

    def test_in_development_is_eligible(self):
        agent = _make_developer()
        ticket = _make_ticket(status=TicketStatus.IN_DEVELOPMENT)
        assert agent._eligible(ticket) is True


# ---------------------------------------------------------------------------
# 10. _select_model() routing
# ---------------------------------------------------------------------------

class TestDeveloperModelSelection:
    def test_critical_uses_heavy(self):
        model_config = MagicMock()
        model_config.t1_heavy = "opus"
        model_config.t2_standard = "sonnet"
        agent = _make_developer(model_config=model_config)

        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        assert agent._select_model(ticket) == "opus"

    def test_high_uses_standard(self):
        model_config = MagicMock()
        model_config.t1_heavy = "opus"
        model_config.t2_standard = "sonnet"
        agent = _make_developer(model_config=model_config)

        ticket = _make_ticket(severity=TicketSeverity.HIGH)
        assert agent._select_model(ticket) == "sonnet"

    def test_escalation_after_failures(self):
        model_config = MagicMock()
        model_config.t1_heavy = "opus"
        model_config.t2_standard = "sonnet"
        agent = _make_developer(model_config=model_config)

        ticket = _make_ticket(severity=TicketSeverity.HIGH)
        ticket.metadata["attempts"] = [
            {"result": "fail"},
            {"result": "fail"},
        ]
        assert agent._select_model(ticket) == "opus"


# ---------------------------------------------------------------------------
# 11. RBAC check
# ---------------------------------------------------------------------------

class TestDeveloperRBAC:
    def test_rbac_denied_raises(self):
        from src.swe_team.developer import DeveloperAgent
        from src.swe_team.rbac_middleware import PermissionDeniedError
        agent = _make_developer()
        ticket = _make_ticket()

        # Set up RBAC engine that denies code_generation
        mock_rbac = MagicMock()
        mock_rbac.check_permission.return_value = (False, "denied by policy")
        agent._rbac_engine = mock_rbac
        agent._agent_name = "test-agent"

        with pytest.raises(PermissionDeniedError, match="RBAC denied"):
            agent.attempt_fix(ticket)


# ---------------------------------------------------------------------------
# 12. No changes produced → BLOCKED
# ---------------------------------------------------------------------------

class TestNoChangesBlocked:
    @patch("src.swe_team.developer.check_fix_complexity", return_value=(True, ""))
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_all_no_changes_sets_blocked(self, _perm, _boundary, _complexity):
        agent = _make_developer(max_attempts=2)
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/test"):
                with patch.object(agent, "_run_claude"):
                    with patch.object(agent, "_run_tests", return_value=(True, "")):
                        with patch.object(agent, "_diff_stats", return_value=(0, [])):
                            with patch.object(agent, "_git", return_value="abc\n"):
                                with patch.object(agent, "_send_telegram"):
                                    result = agent.attempt_fix(ticket)

        assert result is False
        assert ticket.status == TicketStatus.BLOCKED
        assert "no file changes" in ticket.metadata.get("blocked_reason", "").lower()


# ---------------------------------------------------------------------------
# 13. Feature timebox
# ---------------------------------------------------------------------------

class TestTimebox:
    def test_feature_ticket_gets_longer_timebox(self):
        agent = _make_developer()
        ticket = _make_ticket()
        ticket.labels = ["feature"]
        assert agent._timebox_seconds(ticket) == 45 * 60

    def test_bug_ticket_gets_shorter_timebox(self):
        agent = _make_developer()
        ticket = _make_ticket()
        ticket.labels = ["bug"]
        assert agent._timebox_seconds(ticket) == 25 * 60


# ---------------------------------------------------------------------------
# 13b. Targeted test command (issue #294)
# ---------------------------------------------------------------------------

class TestTargetedTestCommand:
    def test_no_source_module_returns_default(self):
        agent = _make_developer()
        assert agent._targeted_test_command(None) == agent._test_command

    def test_empty_source_module_returns_default(self):
        agent = _make_developer()
        assert agent._targeted_test_command("") == agent._test_command

    def test_matching_test_file_scopes_command(self, tmp_path):
        # Create a fake test file to match against
        test_dir = tmp_path / "tests" / "unit"
        test_dir.mkdir(parents=True)
        (test_dir / "test_developer.py").write_text("# test")

        agent = _make_developer(repo_root=tmp_path)
        cmd = agent._targeted_test_command("developer")
        assert str(test_dir / "test_developer.py") in cmd
        assert "-x" in cmd
        assert "--tb=short" in cmd
        assert "--timeout=30" in cmd

    def test_dotted_module_path_extracts_last_component(self, tmp_path):
        test_dir = tmp_path / "tests" / "unit"
        test_dir.mkdir(parents=True)
        (test_dir / "test_monitor_agent.py").write_text("# test")

        agent = _make_developer(repo_root=tmp_path)
        cmd = agent._targeted_test_command("swe_team.monitor_agent")
        assert str(test_dir / "test_monitor_agent.py") in cmd

    def test_no_matching_test_falls_back(self, tmp_path):
        test_dir = tmp_path / "tests" / "unit"
        test_dir.mkdir(parents=True)
        # No test file for "nonexistent_module"

        agent = _make_developer(repo_root=tmp_path)
        cmd = agent._targeted_test_command("nonexistent_module")
        assert cmd == agent._test_command

    def test_source_module_with_py_suffix(self, tmp_path):
        test_dir = tmp_path / "tests" / "unit"
        test_dir.mkdir(parents=True)
        (test_dir / "test_developer.py").write_text("# test")

        agent = _make_developer(repo_root=tmp_path)
        cmd = agent._targeted_test_command("developer.py")
        assert str(test_dir / "test_developer.py") in cmd


# ---------------------------------------------------------------------------
# 14. Fallback agents
# ---------------------------------------------------------------------------

class TestDeveloperFallbackAgents:
    def test_try_fallback_returns_false_when_none(self):
        agent = _make_developer(fallback_agents=None)
        ticket = _make_ticket()
        assert agent._try_fallback_agents("prompt", ticket, 60) is False

    def test_try_fallback_calls_invoke(self):
        mock_agent = MagicMock()
        mock_agent.name = "gemini-cli"
        mock_agent.is_available.return_value = True
        mock_agent.invoke.return_value = None

        agent = _make_developer(fallback_agents=[mock_agent])
        ticket = _make_ticket()

        agent._try_fallback_agents("prompt", ticket, 60)
        mock_agent.invoke.assert_called_once()


# ---------------------------------------------------------------------------
# 15. Rate limit alert
# ---------------------------------------------------------------------------

class TestDeveloperRateLimitAlert:
    def test_rate_limit_alert_via_notifier(self):
        notifier = MagicMock()
        agent = _make_developer(notifier=notifier)
        ticket = _make_ticket()

        agent._send_rate_limit_alert(ticket, Exception("rate limited"))

        notifier.send_alert.assert_called_once()
        msg = notifier.send_alert.call_args[0][0]
        assert "Rate Limit" in msg


# ---------------------------------------------------------------------------
# 16. Push branch to origin after successful commit (#279)
# ---------------------------------------------------------------------------

class TestPushBranchAfterCommit:
    """Verify that the developer pushes the branch to origin after a successful fix."""

    @patch("src.swe_team.developer.check_fix_complexity", return_value=(True, ""))
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_push_called_after_successful_commit(self, _perm, _boundary, _complexity):
        agent = _make_developer()
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        git_calls = []

        def mock_git(cmd):
            git_calls.append(cmd)
            if cmd[1] == "rev-parse":
                return "abc123\n"
            if cmd[1] == "diff" and "--cached" in cmd:
                return "file.py\n"
            return ""

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/ticket-T-DEV-TEST"):
                with patch.object(agent, "_run_claude"):
                    with patch.object(agent, "_run_tests", return_value=(True, "")):
                        with patch.object(agent, "_diff_stats", return_value=(10, ["file.py"])):
                            with patch.object(agent, "_git", side_effect=mock_git):
                                with patch.object(agent, "_record_automation"):
                                    result = agent.attempt_fix(ticket)

        assert result is True
        # Verify git push --force-with-lease was called
        push_calls = [c for c in git_calls if "push" in c]
        assert len(push_calls) == 1
        assert push_calls[0] == ["git", "push", "--force-with-lease", "origin", "swe-fix/ticket-T-DEV-TEST"]

    @patch("src.swe_team.developer.check_fix_complexity", return_value=(True, ""))
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_push_success_sets_pushed_flag(self, _perm, _boundary, _complexity):
        agent = _make_developer()
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        def mock_git(cmd):
            if cmd[1] == "rev-parse":
                return "abc123\n"
            if cmd[1] == "diff" and "--cached" in cmd:
                return "file.py\n"
            return ""

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/ticket-T-DEV-TEST"):
                with patch.object(agent, "_run_claude"):
                    with patch.object(agent, "_run_tests", return_value=(True, "")):
                        with patch.object(agent, "_diff_stats", return_value=(10, ["file.py"])):
                            with patch.object(agent, "_git", side_effect=mock_git):
                                with patch.object(agent, "_record_automation"):
                                    result = agent.attempt_fix(ticket)

        assert result is True
        last_attempt = ticket.metadata["attempts"][-1]
        assert last_attempt.get("pushed") is True

    @patch("src.swe_team.developer.check_fix_complexity", return_value=(True, ""))
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_push_failure_is_non_fatal(self, _perm, _boundary, _complexity):
        """Push failure should NOT prevent the fix from succeeding."""
        agent = _make_developer()
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        call_count = [0]

        def mock_git(cmd):
            if cmd[1] == "rev-parse":
                return "abc123\n"
            if cmd[1] == "diff" and "--cached" in cmd:
                return "file.py\n"
            if cmd[1] == "push":
                raise RuntimeError("remote: permission denied")
            return ""

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/ticket-T-DEV-TEST"):
                with patch.object(agent, "_run_claude"):
                    with patch.object(agent, "_run_tests", return_value=(True, "")):
                        with patch.object(agent, "_diff_stats", return_value=(10, ["file.py"])):
                            with patch.object(agent, "_git", side_effect=mock_git):
                                with patch.object(agent, "_record_automation"):
                                    result = agent.attempt_fix(ticket)

        # Fix should still succeed even if push failed
        assert result is True
        assert ticket.status == TicketStatus.IN_REVIEW
        last_attempt = ticket.metadata["attempts"][-1]
        assert "push_error" in last_attempt
        assert "pushed" not in last_attempt

    @patch("src.swe_team.developer.check_fix_complexity", return_value=(True, ""))
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_push_called_for_fallback_agent_commit(self, _perm, _boundary, _complexity):
        """Push should also be called after a fallback agent's successful commit."""
        from src.swe_team.rate_limiter import RateLimitExhausted
        agent = _make_developer(max_attempts=1)
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        git_calls = []

        def mock_git(cmd):
            git_calls.append(cmd)
            if cmd[1] == "rev-parse":
                return "abc123\n"
            if cmd[1] == "diff" and "--cached" in cmd:
                return "file.py\n"
            return ""

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/ticket-T-DEV-TEST"):
                with patch.object(agent, "_backoff") as mock_backoff:
                    mock_backoff.execute.side_effect = RateLimitExhausted("exhausted")
                    with patch.object(agent, "_try_fallback_agents", return_value=True):
                        with patch.object(agent, "_run_tests", return_value=(True, "")):
                            with patch.object(agent, "_diff_stats", return_value=(5, ["a.py"])):
                                with patch.object(agent, "_git", side_effect=mock_git):
                                    with patch.object(agent, "_record_automation"):
                                        with patch.object(agent, "_send_rate_limit_alert"):
                                            result = agent.attempt_fix(ticket)

        assert result is True
        push_calls = [c for c in git_calls if "push" in c]
        assert len(push_calls) == 1
        assert "--force-with-lease" in push_calls[0]


# ---------------------------------------------------------------------------
# 17. Session continuity — resume on retry (#300)
# ---------------------------------------------------------------------------

class TestSessionContinuity:
    """Verify that dev_session_id is stored and used for resume on retry."""

    @patch("src.swe_team.developer.check_fix_complexity", return_value=(True, ""))
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_session_id_saved_after_engine_run(self, _perm, _boundary, _complexity):
        """After a successful _run_claude, the session_id is stored in ticket metadata."""
        from src.swe_team.providers.coding_engine.base import EngineResult

        mock_engine = MagicMock()
        mock_engine.run.return_value = EngineResult(
            stdout="fix applied", stderr="", returncode=0,
            session_id="sess-abc-123",
        )

        agent = _make_developer(engine=mock_engine)
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/ticket-T-DEV-TEST"):
                with patch.object(agent, "_run_tests", return_value=(True, "")):
                    with patch.object(agent, "_diff_stats", return_value=(10, ["file.py"])):
                        with patch.object(agent, "_git", return_value="abc123\n"):
                            with patch.object(agent, "_record_automation"):
                                result = agent.attempt_fix(ticket)

        assert result is True
        assert ticket.metadata.get("dev_session_id") == "sess-abc-123"

    @patch("src.swe_team.developer.check_fix_complexity", return_value=(True, ""))
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_resume_called_on_retry_with_existing_session(self, _perm, _boundary, _complexity):
        """On attempt 2+, engine.resume() is called with the stored session_id."""
        from src.swe_team.providers.coding_engine.base import EngineResult

        mock_engine = MagicMock()
        # First call (run) fails tests, second call (resume) succeeds
        mock_engine.run.return_value = EngineResult(
            stdout="attempt 1", stderr="", returncode=0,
            session_id="sess-first-attempt",
        )
        mock_engine.resume.return_value = EngineResult(
            stdout="attempt 2 fixed", stderr="", returncode=0,
            session_id="sess-second-attempt",
        )

        agent = _make_developer(engine=mock_engine, max_attempts=2)
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()

        call_count = [0]

        def mock_tests(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return (False, "FAILED: assert 1 == 2")
            return (True, "")

        def mock_git(cmd):
            if cmd[1] == "rev-parse":
                return "abc123\n"
            if cmd[1] == "diff" and "--cached" in cmd:
                return "file.py\n"
            return ""

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/ticket-T-DEV-TEST"):
                with patch.object(agent, "_run_tests", side_effect=mock_tests):
                    with patch.object(agent, "_diff_stats", return_value=(10, ["file.py"])):
                        with patch.object(agent, "_git", side_effect=mock_git):
                            with patch.object(agent, "_record_automation"):
                                result = agent.attempt_fix(ticket)

        assert result is True
        # engine.run was called for attempt 1
        mock_engine.run.assert_called_once()
        # engine.resume was called for attempt 2 with the stored session_id
        mock_engine.resume.assert_called_once()
        resume_args = mock_engine.resume.call_args
        assert resume_args[0][0] == "sess-first-attempt"  # session_id positional arg

    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_resume_failure_falls_back_to_fresh_run(self, _perm, _boundary):
        """If engine.resume() raises, _run_claude falls back to engine.run()."""
        from src.swe_team.providers.coding_engine.base import EngineResult

        mock_engine = MagicMock()
        mock_engine.resume.side_effect = RuntimeError("session expired")
        mock_engine.run.return_value = EngineResult(
            stdout="fresh run", stderr="", returncode=0,
            session_id="sess-fresh",
        )

        agent = _make_developer(engine=mock_engine)
        # Call _run_claude directly with a resume_session_id to test fallback
        agent._run_claude("fix prompt", timeout=60, model="sonnet", resume_session_id="old-sid")

        # resume was attempted first
        mock_engine.resume.assert_called_once()
        # then run was called as fallback
        mock_engine.run.assert_called_once()

    @patch("src.swe_team.developer.check_fix_complexity", return_value=(True, ""))
    @patch("src.swe_team.developer.enforce_code_generation_boundary", return_value="sonnet")
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, "allowed"))
    def test_no_resume_on_first_attempt(self, _perm, _boundary, _complexity):
        """First attempt (attempt_num=0) should NOT try to resume, even if session exists."""
        from src.swe_team.providers.coding_engine.base import EngineResult

        mock_engine = MagicMock()
        mock_engine.run.return_value = EngineResult(
            stdout="first try", stderr="", returncode=0,
            session_id="sess-new",
        )

        agent = _make_developer(engine=mock_engine)
        agent._program_cache = "Fix: {ticket_id} {title} {severity} {source_module} {investigation_report}"
        ticket = _make_ticket()
        # Pre-set a session_id — should be ignored on first attempt
        ticket.metadata["dev_session_id"] = "sess-stale"

        with patch.object(agent, "_run_preflight") as mock_pf:
            mock_pf.return_value = MagicMock(passed=True)
            with patch.object(agent, "_ensure_branch", return_value="swe-fix/ticket-T-DEV-TEST"):
                with patch.object(agent, "_run_tests", return_value=(True, "")):
                    with patch.object(agent, "_diff_stats", return_value=(5, ["a.py"])):
                        with patch.object(agent, "_git", return_value="abc\n"):
                            with patch.object(agent, "_record_automation"):
                                result = agent.attempt_fix(ticket)

        assert result is True
        # run() called, not resume()
        mock_engine.run.assert_called_once()
        mock_engine.resume.assert_not_called()
