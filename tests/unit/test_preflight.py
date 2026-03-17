"""
Tests for SWE Team pre-flight validation.

Covers:
- All checks pass when context is correct (mocked subprocess for gh/git)
- Failure when git identity does not match
- Failure when required env vars are missing
- Runner skips cycle on preflight failure
- Developer agent blocks ticket on preflight failure
"""

from __future__ import annotations

import logging

logging.logAsyncioTasks = False

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from src.swe_team.preflight import PreflightCheck, PreflightResult


# ======================================================================
# PreflightResult
# ======================================================================

class TestPreflightResult:
    def test_passed_summary(self):
        r = PreflightResult(passed=True, failures=[])
        assert r.summary() == "Preflight OK"

    def test_failed_summary(self):
        r = PreflightResult(passed=False, failures=["bad name", "missing var"])
        assert "bad name" in r.summary()
        assert "missing var" in r.summary()
        assert r.summary().startswith("Preflight FAILED:")


# ======================================================================
# PreflightCheck — all checks pass
# ======================================================================

class TestPreflightAllPass:
    """All preflight checks pass when subprocess and env are correct."""

    def test_all_checks_pass(self, tmp_path):
        expected_root = tmp_path / "repo"
        expected_root.mkdir()

        def fake_run(cmd, **kwargs):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            result = MagicMock()
            result.returncode = 0

            if "git config --get user.name" in cmd_str:
                result.stdout = "swe-bot\n"
                result.stderr = ""
            elif "git config --get user.email" in cmd_str:
                result.stdout = "bot@example.com\n"
                result.stderr = ""
            elif "git rev-parse --show-toplevel" in cmd_str:
                result.stdout = str(expected_root.resolve()) + "\n"
                result.stderr = ""
            elif "gh auth status" in cmd_str:
                result.stdout = "Logged in to github.com as swe-bot-gh"
                result.stderr = ""
            else:
                result.stdout = ""
                result.stderr = ""
            return result

        check = PreflightCheck(
            expected_git_name="swe-bot",
            expected_git_email="bot@example.com",
            expected_github_account="swe-bot-gh",
            expected_repo_root=expected_root,
            required_env_vars=["SWE_TEAM_ID", "SWE_GITHUB_REPO"],
        )

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch.dict(os.environ, {"SWE_TEAM_ID": "team-1", "SWE_GITHUB_REPO": "org/repo"}),
        ):
            result = check.run()

        assert result.passed is True
        assert result.failures == []


# ======================================================================
# PreflightCheck — git identity failures
# ======================================================================

class TestPreflightGitIdentity:
    def test_wrong_git_name(self):
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            cmd_str = " ".join(cmd)
            if "user.name" in cmd_str:
                result.stdout = "wrong-user\n"
            else:
                result.stdout = ""
            result.stderr = ""
            return result

        check = PreflightCheck(
            expected_git_name="swe-bot",
            required_env_vars=[],
        )

        with patch("subprocess.run", side_effect=fake_run):
            failures = check.check_git_identity()

        assert len(failures) == 1
        assert "user.name mismatch" in failures[0]
        assert "swe-bot" in failures[0]
        assert "wrong-user" in failures[0]

    def test_wrong_git_email(self):
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            cmd_str = " ".join(cmd)
            if "user.email" in cmd_str:
                result.stdout = "wrong@email.com\n"
            else:
                result.stdout = ""
            result.stderr = ""
            return result

        check = PreflightCheck(
            expected_git_email="bot@example.com",
            required_env_vars=[],
        )

        with patch("subprocess.run", side_effect=fake_run):
            failures = check.check_git_identity()

        assert len(failures) == 1
        assert "user.email mismatch" in failures[0]

    def test_git_name_not_configured(self):
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = ""
            return result

        check = PreflightCheck(
            expected_git_name="swe-bot",
            required_env_vars=[],
        )

        with patch("subprocess.run", side_effect=fake_run):
            failures = check.check_git_identity()

        assert len(failures) == 1
        assert "not configured" in failures[0]

    def test_skips_when_no_expectation(self):
        """When expected_git_name/email are None, checks are skipped."""
        check = PreflightCheck(required_env_vars=[])
        # No subprocess mock needed — should not be called
        failures = check.check_git_identity()
        assert failures == []


# ======================================================================
# PreflightCheck — env var failures
# ======================================================================

class TestPreflightEnvVars:
    def test_missing_required_env_vars(self):
        check = PreflightCheck(
            required_env_vars=["SWE_TEAM_ID", "SWE_GITHUB_REPO", "NONEXISTENT_VAR"],
        )

        with patch.dict(os.environ, {"SWE_TEAM_ID": "team-1"}, clear=False):
            # Ensure SWE_GITHUB_REPO and NONEXISTENT_VAR are not set
            env = os.environ.copy()
            env.pop("SWE_GITHUB_REPO", None)
            env.pop("NONEXISTENT_VAR", None)
            with patch.dict(os.environ, env, clear=True):
                failures = check.check_env_vars()

        assert len(failures) == 2
        assert any("SWE_GITHUB_REPO" in f for f in failures)
        assert any("NONEXISTENT_VAR" in f for f in failures)

    def test_all_env_vars_present(self):
        check = PreflightCheck(
            required_env_vars=["SWE_TEAM_ID", "SWE_GITHUB_REPO"],
        )

        with patch.dict(os.environ, {
            "SWE_TEAM_ID": "team-1",
            "SWE_GITHUB_REPO": "org/repo",
        }):
            failures = check.check_env_vars()

        assert failures == []

    def test_empty_env_var_counts_as_missing(self):
        check = PreflightCheck(
            required_env_vars=["SWE_TEAM_ID"],
        )

        with patch.dict(os.environ, {"SWE_TEAM_ID": ""}, clear=False):
            failures = check.check_env_vars()

        assert len(failures) == 1
        assert "SWE_TEAM_ID" in failures[0]


# ======================================================================
# PreflightCheck — working directory failures
# ======================================================================

class TestPreflightWorkingDirectory:
    def test_wrong_repo_root(self, tmp_path):
        expected = tmp_path / "expected-repo"
        expected.mkdir()
        actual = tmp_path / "wrong-repo"
        actual.mkdir()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = str(actual.resolve()) + "\n"
            result.stderr = ""
            return result

        check = PreflightCheck(
            expected_repo_root=expected,
            required_env_vars=[],
        )

        with patch("subprocess.run", side_effect=fake_run):
            failures = check.check_working_directory()

        assert len(failures) == 1
        assert "Repo root mismatch" in failures[0]

    def test_not_a_git_repo(self):
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 128
            result.stdout = ""
            result.stderr = "fatal: not a git repository"
            return result

        check = PreflightCheck(
            expected_repo_root=Path("/some/path"),
            required_env_vars=[],
        )

        with patch("subprocess.run", side_effect=fake_run):
            failures = check.check_working_directory()

        assert len(failures) == 1
        assert "Not inside a git repository" in failures[0]

    def test_skips_when_no_expected_root(self):
        check = PreflightCheck(required_env_vars=[])
        failures = check.check_working_directory()
        assert failures == []


# ======================================================================
# PreflightCheck — GitHub auth failures
# ======================================================================

class TestPreflightGitHubAuth:
    def test_wrong_github_account(self):
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = "Logged in to github.com as wrong-account"
            result.stderr = ""
            return result

        check = PreflightCheck(
            expected_github_account="swe-bot",
            required_env_vars=[],
        )

        with patch("subprocess.run", side_effect=fake_run):
            failures = check.check_github_auth()

        assert len(failures) == 1
        assert "GitHub account mismatch" in failures[0]
        assert "swe-bot" in failures[0]

    def test_gh_auth_fails(self):
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "not logged in"
            return result

        check = PreflightCheck(
            expected_github_account="swe-bot",
            required_env_vars=[],
        )

        with patch("subprocess.run", side_effect=fake_run):
            failures = check.check_github_auth()

        assert len(failures) == 1
        assert "gh auth status failed" in failures[0]

    def test_gh_not_found(self):
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("gh not found")

        check = PreflightCheck(
            expected_github_account="swe-bot",
            required_env_vars=[],
        )

        with patch("subprocess.run", side_effect=fake_run):
            failures = check.check_github_auth()

        assert len(failures) == 1
        assert "gh CLI not found" in failures[0]

    def test_skips_when_no_expected_account(self):
        check = PreflightCheck(required_env_vars=[])
        failures = check.check_github_auth()
        assert failures == []


# ======================================================================
# PreflightCheck — full run() integration
# ======================================================================

class TestPreflightFullRun:
    def test_run_returns_failure_when_any_check_fails(self):
        check = PreflightCheck(
            expected_git_name="swe-bot",
            required_env_vars=["NONEXISTENT_VAR_XYZ"],
        )

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            cmd_str = " ".join(cmd)
            if "user.name" in cmd_str:
                result.stdout = "wrong\n"
            else:
                result.stdout = ""
            result.stderr = ""
            return result

        env = os.environ.copy()
        env.pop("NONEXISTENT_VAR_XYZ", None)
        with (
            patch("subprocess.run", side_effect=fake_run),
            patch.dict(os.environ, env, clear=True),
        ):
            result = check.run()

        assert result.passed is False
        assert len(result.failures) >= 2  # git name + env var


# ======================================================================
# Runner integration — skips cycle on preflight failure
# ======================================================================

class TestRunnerPreflightIntegration:
    def test_run_cycle_skips_on_preflight_failure(self):
        """run_cycle returns early with preflight_failed gate_verdict."""
        import scripts.ops.swe_team_runner as runner

        config = MagicMock()
        config.github_account = ""
        store = MagicMock()

        failed_result = PreflightResult(
            passed=False,
            failures=["git user.name mismatch: expected 'bot', got 'human'"],
        )

        with (
            patch.object(PreflightCheck, "run", return_value=failed_result),
            patch.dict(os.environ, {}, clear=False),
            patch("scripts.ops.swe_team_runner._send_preflight_alert") as mock_alert,
        ):
            result = runner.run_cycle(config, store, dry_run=False)

        assert result["gate_verdict"] == "preflight_failed"
        assert result["new_tickets"] == 0
        assert "preflight_failures" in result
        assert len(result["preflight_failures"]) == 1
        mock_alert.assert_called_once()


# ======================================================================
# DeveloperAgent integration — blocks ticket on preflight failure
# ======================================================================

class TestDeveloperPreflightIntegration:
    def test_attempt_fix_blocked_on_preflight_failure(self):
        """DeveloperAgent.attempt_fix returns False and marks ticket blocked."""
        from src.swe_team.developer import DeveloperAgent
        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus

        ticket = SWETicket(
            title="Test bug",
            description="Something broke",
            severity=TicketSeverity.HIGH,
            investigation_report="Root cause found",
        )
        ticket.transition(TicketStatus.INVESTIGATION_COMPLETE)

        dev = DeveloperAgent(repo_root="/tmp/fake-repo")

        failed_result = PreflightResult(
            passed=False,
            failures=["Required env var 'SWE_TEAM_ID' is not set"],
        )

        with patch.object(dev, "_run_preflight", return_value=failed_result):
            ok = dev.attempt_fix(ticket)

        assert ok is False
        assert "preflight_failure" in ticket.metadata
        assert "blocked_reason" in ticket.metadata
        assert "SWE_TEAM_ID" in ticket.metadata["blocked_reason"]

    def test_attempt_fix_proceeds_on_preflight_pass(self):
        """DeveloperAgent.attempt_fix proceeds when preflight passes."""
        from src.swe_team.developer import DeveloperAgent
        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus

        ticket = SWETicket(
            title="Test bug",
            description="Something broke",
            severity=TicketSeverity.HIGH,
            investigation_report="Root cause found",
        )
        ticket.transition(TicketStatus.INVESTIGATION_COMPLETE)

        dev = DeveloperAgent(repo_root="/tmp/fake-repo")

        passed_result = PreflightResult(passed=True, failures=[])

        with (
            patch.object(dev, "_run_preflight", return_value=passed_result),
            patch.object(dev, "_ensure_branch", return_value="swe-fix/test"),
            patch.object(dev, "_build_prompt", return_value="fix it"),
            patch.object(dev, "_run_claude"),
            patch.object(dev, "_run_tests", return_value=(True, "")),
            patch.object(dev, "_diff_stats", return_value=(10, ["file.py"])),
            patch("src.swe_team.developer.check_fix_complexity", return_value=(True, "")),
            patch.object(dev, "_git", return_value="abc123\n"),
            patch.object(dev, "_record_automation"),
        ):
            ok = dev.attempt_fix(ticket)

        assert ok is True
        assert "preflight_failure" not in ticket.metadata
