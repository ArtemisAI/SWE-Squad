"""Tests for git worktree isolation in DeveloperAgent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import SWETicket, TicketStatus


def _make_ticket(**kwargs) -> SWETicket:
    defaults = {
        "title": "test bug",
        "description": "desc",
        "ticket_id": "abc12345dead",
        "investigation_report": "Found the bug in X",
        "status": TicketStatus.INVESTIGATION_COMPLETE,
    }
    defaults.update(kwargs)
    return SWETicket(**defaults)


class TestWorktreeConfig:
    """Verify use_worktree flag."""

    def test_default_false(self):
        from src.swe_team.developer import DeveloperAgent
        agent = DeveloperAgent()
        assert agent._use_worktree is False

    def test_explicit_true(self):
        from src.swe_team.developer import DeveloperAgent
        agent = DeveloperAgent(use_worktree=True)
        assert agent._use_worktree is True

    def test_explicit_false(self):
        from src.swe_team.developer import DeveloperAgent
        agent = DeveloperAgent(use_worktree=False)
        assert agent._use_worktree is False


class TestEnsureWorktree:
    """Verify _ensure_worktree creates a worktree directory."""

    @patch("src.swe_team.developer.shutil.rmtree")
    @patch("src.swe_team.developer.subprocess.run")
    def test_ensure_worktree_calls_git(self, mock_run, mock_rmtree):
        from src.swe_team.developer import DeveloperAgent

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        agent = DeveloperAgent(use_worktree=True)
        ticket = _make_ticket()
        branch = agent._ensure_worktree(ticket)
        assert "abc12345dead" in branch
        assert agent._active_worktree is not None

    @patch("src.swe_team.developer.shutil.rmtree")
    @patch("src.swe_team.developer.subprocess.run")
    def test_ensure_worktree_sets_repo_root(self, mock_run, mock_rmtree):
        from src.swe_team.developer import DeveloperAgent

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        agent = DeveloperAgent(use_worktree=True)
        original_root = agent._repo_root
        ticket = _make_ticket()
        agent._ensure_worktree(ticket)
        assert agent._repo_root != original_root


class TestCleanupWorktree:
    """Verify _cleanup_worktree removes worktree."""

    @patch("src.swe_team.developer.shutil.rmtree")
    @patch("src.swe_team.developer.subprocess.run")
    def test_cleanup_removes_worktree(self, mock_run, mock_rmtree):
        from src.swe_team.developer import DeveloperAgent

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        agent = DeveloperAgent(use_worktree=True)
        agent._active_worktree = Path("/tmp/swe-agent-abc12345dead")
        agent._original_repo_root = agent._repo_root
        ticket = _make_ticket()
        agent._cleanup_worktree(ticket)
        assert agent._active_worktree is None

    @patch("src.swe_team.developer.shutil.rmtree")
    @patch("src.swe_team.developer.subprocess.run")
    def test_cleanup_restores_repo_root(self, mock_run, mock_rmtree):
        from src.swe_team.developer import DeveloperAgent

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        agent = DeveloperAgent(use_worktree=True)
        original = agent._repo_root
        agent._active_worktree = Path("/tmp/swe-agent-test")
        agent._original_repo_root = original
        agent._repo_root = Path("/tmp/swe-agent-test")
        ticket = _make_ticket()
        agent._cleanup_worktree(ticket)
        assert agent._repo_root == original


class TestAttemptFixWorktreeIntegration:
    """Verify attempt_fix uses worktree when enabled and cleans up."""

    @patch.object(
        __import__("src.swe_team.developer", fromlist=["DeveloperAgent"]).DeveloperAgent,
        "_fix_loop",
        return_value=True,
    )
    @patch.object(
        __import__("src.swe_team.developer", fromlist=["DeveloperAgent"]).DeveloperAgent,
        "_cleanup_worktree",
    )
    @patch.object(
        __import__("src.swe_team.developer", fromlist=["DeveloperAgent"]).DeveloperAgent,
        "_ensure_worktree",
        return_value="swe-fix/ticket-abc12345dead",
    )
    @patch.object(
        __import__("src.swe_team.developer", fromlist=["DeveloperAgent"]).DeveloperAgent,
        "_run_preflight",
    )
    @patch.object(
        __import__("src.swe_team.developer", fromlist=["DeveloperAgent"]).DeveloperAgent,
        "_eligible",
        return_value=True,
    )
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, ""))
    def test_worktree_cleanup_called(
        self, mock_perm, mock_eligible, mock_preflight, mock_ensure, mock_cleanup, mock_fix
    ):
        from src.swe_team.developer import DeveloperAgent

        mock_preflight.return_value = MagicMock(passed=True)
        agent = DeveloperAgent(use_worktree=True)
        # Simulate _ensure_worktree setting _active_worktree
        def set_worktree(ticket):
            agent._active_worktree = Path("/tmp/swe-agent-test")
            return "swe-fix/ticket-abc12345dead"
        mock_ensure.side_effect = set_worktree

        ticket = _make_ticket()
        result = agent.attempt_fix(ticket)
        assert result is True
        mock_cleanup.assert_called_once()

    @patch.object(
        __import__("src.swe_team.developer", fromlist=["DeveloperAgent"]).DeveloperAgent,
        "_fix_loop",
        side_effect=RuntimeError("boom"),
    )
    @patch.object(
        __import__("src.swe_team.developer", fromlist=["DeveloperAgent"]).DeveloperAgent,
        "_cleanup_worktree",
    )
    @patch.object(
        __import__("src.swe_team.developer", fromlist=["DeveloperAgent"]).DeveloperAgent,
        "_ensure_worktree",
        return_value="swe-fix/ticket-abc12345dead",
    )
    @patch.object(
        __import__("src.swe_team.developer", fromlist=["DeveloperAgent"]).DeveloperAgent,
        "_run_preflight",
    )
    @patch.object(
        __import__("src.swe_team.developer", fromlist=["DeveloperAgent"]).DeveloperAgent,
        "_eligible",
        return_value=True,
    )
    @patch("src.swe_team.agent_rbac.check_permission", return_value=(True, ""))
    def test_cleanup_called_even_on_exception(
        self, mock_perm, mock_eligible, mock_preflight, mock_ensure, mock_cleanup, mock_fix
    ):
        from src.swe_team.developer import DeveloperAgent

        mock_preflight.return_value = MagicMock(passed=True)
        agent = DeveloperAgent(use_worktree=True)
        def set_worktree(ticket):
            agent._active_worktree = Path("/tmp/swe-agent-test")
            return "swe-fix/ticket-abc12345dead"
        mock_ensure.side_effect = set_worktree

        ticket = _make_ticket()
        with pytest.raises(RuntimeError):
            agent.attempt_fix(ticket)
        mock_cleanup.assert_called_once()
