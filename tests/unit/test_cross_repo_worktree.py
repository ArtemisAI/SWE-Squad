"""Tests for cross-repo worktree support (GitHub issue #136)."""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.worktree_manager import WorktreeManager, Worktree


class TestWorktreeManagerReposMap:
    """Verify repos_map routing in WorktreeManager."""

    def test_set_repos_map(self):
        manager = WorktreeManager(repo_root="/home/agent/SWE-Squad")
        manager.set_repos_map({"your-org/example-app": "/home/agent/Projects/example-app"})
        assert "your-org/example-app" in manager._repos_map
        assert manager._repos_map["your-org/example-app"] == Path("/home/agent/Projects/example-app")

    def test_repo_root_for_known_repo(self):
        manager = WorktreeManager(repo_root="/home/agent/SWE-Squad")
        manager.set_repos_map({"your-org/example-app": "/home/agent/Projects/example-app"})
        result = manager.repo_root_for("your-org/example-app")
        assert result == Path("/home/agent/Projects/example-app")

    def test_repo_root_for_unknown_repo_falls_back(self):
        manager = WorktreeManager(repo_root="/home/agent/SWE-Squad")
        manager.set_repos_map({"your-org/example-app": "/home/agent/Projects/example-app"})
        result = manager.repo_root_for("your-org/SWE-Squad")
        assert result == Path("/home/agent/SWE-Squad")

    def test_repo_root_for_none_falls_back(self):
        manager = WorktreeManager(repo_root="/home/agent/SWE-Squad")
        result = manager.repo_root_for(None)
        assert result == Path("/home/agent/SWE-Squad")

    def test_repos_map_empty_by_default(self):
        manager = WorktreeManager(repo_root="/home/agent/SWE-Squad")
        assert manager._repos_map == {}

    @patch("src.swe_team.worktree_manager.subprocess.run")
    def test_acquire_uses_linked_ai_root(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        manager = WorktreeManager(repo_root="/home/agent/SWE-Squad")
        manager.set_repos_map({"your-org/example-app": "/home/agent/Projects/example-app"})
        wt = manager.acquire(
            ticket_id="linkedai-42",
            branch="claude/fix-linkedai-42",
            repo_name="your-org/example-app",
        )
        assert wt.repo_root == Path("/home/agent/Projects/example-app")
        for call_args in mock_run.call_args_list:
            cwd = call_args[1].get("cwd")
            if cwd is not None:
                assert cwd == Path("/home/agent/Projects/example-app"), (
                    f"Expected git to run in example-app root, got {cwd}"
                )

    @patch("src.swe_team.worktree_manager.subprocess.run")
    def test_acquire_uses_default_root_for_no_repo_name(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        manager = WorktreeManager(repo_root="/home/agent/SWE-Squad")
        manager.set_repos_map({"your-org/example-app": "/home/agent/Projects/example-app"})
        wt = manager.acquire(
            ticket_id="swe-99",
            branch="claude/fix-swe-99",
            repo_name=None,
        )
        assert wt.repo_root == Path("/home/agent/SWE-Squad")

    @patch("src.swe_team.worktree_manager.subprocess.run")
    def test_release_uses_worktree_repo_root(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        manager = WorktreeManager(repo_root="/home/agent/SWE-Squad")
        manager.set_repos_map({"your-org/example-app": "/home/agent/Projects/example-app"})
        wt = Worktree(
            path=Path("/tmp/swe-worktree-linkedai-42"),
            branch="claude/fix-linkedai-42",
            ticket_id="linkedai-42",
            acquired_at=time.monotonic(),
            in_use=True,
            repo_root=Path("/home/agent/Projects/example-app"),
        )
        manager._worktrees["linkedai-42"] = wt

        manager.release(wt)

        for call_args in mock_run.call_args_list:
            cwd = call_args[1].get("cwd")
            if cwd is not None:
                assert cwd == Path("/home/agent/Projects/example-app"), (
                    f"Expected example-app root during release, got {cwd}"
                )


class TestDeveloperAgentReposMap:
    """Verify DeveloperAgent passes ticket repo to worktree creation."""

    def test_repos_map_stored_on_init(self):
        from src.swe_team.developer import DeveloperAgent

        agent = DeveloperAgent(
            repos_map={"your-org/example-app": "/home/agent/Projects/example-app"},
        )
        assert "your-org/example-app" in agent._repos_map
        assert agent._repos_map["your-org/example-app"] == Path("/home/agent/Projects/example-app")

    def test_repos_map_empty_by_default(self):
        from src.swe_team.developer import DeveloperAgent

        agent = DeveloperAgent()
        assert agent._repos_map == {}

    @patch("src.swe_team.developer.shutil.rmtree")
    @patch("src.swe_team.developer.subprocess.run")
    def test_ensure_worktree_linkedai_uses_correct_root(self, mock_run, mock_rmtree, tmp_path):
        from src.swe_team.developer import DeveloperAgent
        from src.swe_team.models import SWETicket, TicketStatus

        swe_root = tmp_path / "SWE-Squad"
        swe_root.mkdir()
        app_root = tmp_path / "Projects" / "example-app"
        app_root.mkdir(parents=True)

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        agent = DeveloperAgent(
            repo_root=str(swe_root),
            use_worktree=True,
            repos_map={"your-org/example-app": str(app_root)},
        )
        ticket = SWETicket(
            title="example-app bug",
            description="desc",
            ticket_id="linkedai-gh-42",
            investigation_report="Found bug",
            status=TicketStatus.INVESTIGATION_COMPLETE,
            metadata={"repo": "your-org/example-app"},
        )
        branch = agent._ensure_worktree(ticket)
        assert "linkedai-gh-42" in branch
        for call_args in mock_run.call_args_list:
            cwd = call_args[1].get("cwd")
            if cwd is not None:
                assert cwd == app_root, (
                    f"Expected example-app root, got {cwd}"
                )

    @patch("src.swe_team.developer.shutil.rmtree")
    @patch("src.swe_team.developer.subprocess.run")
    def test_ensure_worktree_no_repo_metadata_uses_default(self, mock_run, mock_rmtree, tmp_path):
        from src.swe_team.developer import DeveloperAgent
        from src.swe_team.models import SWETicket, TicketStatus

        swe_root = tmp_path / "SWE-Squad"
        swe_root.mkdir()
        app_root = tmp_path / "Projects" / "example-app"
        app_root.mkdir(parents=True)

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        agent = DeveloperAgent(
            repo_root=str(swe_root),
            use_worktree=True,
            repos_map={"your-org/example-app": str(app_root)},
        )
        ticket = SWETicket(
            title="SWE-Squad bug",
            description="desc",
            ticket_id="swe-gh-99",
            investigation_report="Found bug",
            status=TicketStatus.INVESTIGATION_COMPLETE,
        )
        branch = agent._ensure_worktree(ticket)
        assert "swe-gh-99" in branch
        for call_args in mock_run.call_args_list:
            cwd = call_args[1].get("cwd")
            if cwd is not None:
                assert cwd == swe_root, (
                    f"Expected SWE-Squad root, got {cwd}"
                )
