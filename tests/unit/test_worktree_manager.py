"""Tests for the WorktreeManager."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from src.swe_team.worktree_manager import WorktreeManager, Worktree


class TestWorktree:
    def test_age_hours_none(self):
        wt = Worktree(path=Path("/tmp/test"), branch="test", acquired_at=None)
        assert wt.age_hours() == 0.0

    def test_age_hours_recent(self):
        import time
        wt = Worktree(path=Path("/tmp/test"), branch="test", acquired_at=time.monotonic())
        assert wt.age_hours() < 0.01


class TestWorktreeManager:
    @patch("src.swe_team.worktree_manager.subprocess.run")
    def test_acquire_creates_worktree(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        manager = WorktreeManager(repo_root="/tmp/test-repo", pool_size=4)
        wt = manager.acquire(ticket_id="test-1", branch="swe-fix/ticket-test-1")

        assert wt.in_use is True
        assert wt.ticket_id == "test-1"
        assert wt.branch == "swe-fix/ticket-test-1"
        assert "test-1" in str(wt.path)

    @patch("src.swe_team.worktree_manager.subprocess.run")
    def test_acquire_pool_exhaustion(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        manager = WorktreeManager(repo_root="/tmp/test-repo", pool_size=1)
        manager.acquire(ticket_id="t-1", branch="branch-1")

        with pytest.raises(RuntimeError, match="pool exhausted"):
            manager.acquire(ticket_id="t-2", branch="branch-2")

    @patch("src.swe_team.worktree_manager.subprocess.run")
    def test_release(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        manager = WorktreeManager(repo_root="/tmp/test-repo", pool_size=4)
        wt = manager.acquire(ticket_id="t-1", branch="b-1")
        assert manager.active_count() == 1

        manager.release(wt)
        assert manager.active_count() == 0

    @patch("src.swe_team.worktree_manager.subprocess.run")
    def test_acquire_returns_existing(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        manager = WorktreeManager(repo_root="/tmp/test-repo", pool_size=4)
        wt1 = manager.acquire(ticket_id="t-1", branch="b-1")
        wt2 = manager.acquire(ticket_id="t-1", branch="b-1")

        assert wt1 is wt2
        assert manager.active_count() == 1

    @patch("src.swe_team.worktree_manager.subprocess.run")
    def test_status(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        manager = WorktreeManager(repo_root="/tmp/test-repo", pool_size=4)
        manager.acquire(ticket_id="t-1", branch="b-1")

        status = manager.status()
        assert status["pool_size"] == 4
        assert status["active"] == 1
        assert len(status["worktrees"]) == 1
        assert status["worktrees"][0]["ticket_id"] == "t-1"

    @patch("src.swe_team.worktree_manager.subprocess.run")
    def test_cleanup_all(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        manager = WorktreeManager(repo_root="/tmp/test-repo", pool_size=4)
        manager.acquire(ticket_id="t-1", branch="b-1")
        manager.acquire(ticket_id="t-2", branch="b-2")

        manager.cleanup_all()
        assert manager.active_count() == 0
