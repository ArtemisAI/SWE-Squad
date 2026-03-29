"""Tests for RepoRouter and sandbox boundary guard."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.swe_team.repo_router import RepoRouter, ResolvedRepo
from src.swe_team.preflight import PreflightCheck


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_REPOS = [
    {"name": "test-org/SWE-Sandbox", "local_path": "/home/agent/Projects/SWE-Sandbox", "priority": "medium"},
    {"name": "test-org/SWE-Sandbox-HealthTrack", "local_path": "/home/agent/Projects/SWE-Sandbox-HealthTrack", "priority": "medium"},
    {"name": "test-org/SWE-Sandbox-ShopStream", "local_path": "/home/agent/Projects/SWE-Sandbox-ShopStream", "priority": "medium"},
]


def _make_ticket(repo: str = "") -> object:
    """Create a minimal ticket-like object."""
    class FakeTicket:
        def __init__(self, repo_name: str):
            self.metadata = {"repo": repo_name} if repo_name else {}
    return FakeTicket(repo)


# ---------------------------------------------------------------------------
# RepoRouter.resolve()
# ---------------------------------------------------------------------------


class TestRepoRouterResolve:
    def test_resolve_known_repo(self):
        router = RepoRouter(SAMPLE_REPOS)
        ticket = _make_ticket("test-org/SWE-Sandbox-HealthTrack")
        result = router.resolve(ticket)
        assert result.repo_name == "test-org/SWE-Sandbox-HealthTrack"
        assert result.local_path == Path("/home/agent/Projects/SWE-Sandbox-HealthTrack")

    def test_resolve_unknown_repo_raises(self):
        router = RepoRouter(SAMPLE_REPOS)
        ticket = _make_ticket("test-org/my-app")
        with pytest.raises(ValueError, match="not in the configured sandbox list"):
            router.resolve(ticket)

    def test_resolve_no_metadata_falls_back_to_first(self):
        router = RepoRouter(SAMPLE_REPOS)
        ticket = _make_ticket("")
        result = router.resolve(ticket)
        assert result.repo_name == "test-org/SWE-Sandbox"

    def test_resolve_empty_config_raises(self):
        router = RepoRouter([])
        ticket = _make_ticket("")
        with pytest.raises(ValueError, match="No sandbox repos configured"):
            router.resolve(ticket)

    def test_resolve_returns_resolved_repo_dataclass(self):
        router = RepoRouter(SAMPLE_REPOS)
        ticket = _make_ticket("test-org/SWE-Sandbox")
        result = router.resolve(ticket)
        assert isinstance(result, ResolvedRepo)


# ---------------------------------------------------------------------------
# RepoRouter.build_repos_map()
# ---------------------------------------------------------------------------


class TestBuildReposMap:
    def test_build_repos_map_returns_all_repos(self):
        router = RepoRouter(SAMPLE_REPOS)
        repos_map = router.build_repos_map()
        assert len(repos_map) == 3
        assert "test-org/SWE-Sandbox" in repos_map
        assert "test-org/SWE-Sandbox-HealthTrack" in repos_map

    def test_build_repos_map_values_are_paths(self):
        router = RepoRouter(SAMPLE_REPOS)
        repos_map = router.build_repos_map()
        for path in repos_map.values():
            assert isinstance(path, Path)

    def test_build_repos_map_empty_config(self):
        router = RepoRouter([])
        assert router.build_repos_map() == {}


# ---------------------------------------------------------------------------
# RepoRouter.is_sandbox_path()
# ---------------------------------------------------------------------------


class TestIsSandboxPath:
    def test_path_inside_sandbox(self):
        router = RepoRouter(SAMPLE_REPOS)
        assert router.is_sandbox_path(Path("/home/agent/Projects/SWE-Sandbox/src/main.py")) is True

    def test_path_is_sandbox_root(self):
        router = RepoRouter(SAMPLE_REPOS)
        assert router.is_sandbox_path(Path("/home/agent/Projects/SWE-Sandbox")) is True

    def test_path_outside_sandbox(self):
        router = RepoRouter(SAMPLE_REPOS)
        assert router.is_sandbox_path(Path("/home/agent/Projects/my-app")) is False

    def test_production_repo_rejected(self):
        router = RepoRouter(SAMPLE_REPOS)
        assert router.is_sandbox_path(Path("/home/agent/Projects/example-dir")) is False


# ---------------------------------------------------------------------------
# RepoRouter.repo_names
# ---------------------------------------------------------------------------


class TestRepoNames:
    def test_repo_names_returns_all(self):
        router = RepoRouter(SAMPLE_REPOS)
        names = router.repo_names
        assert len(names) == 3
        assert "test-org/SWE-Sandbox" in names


# ---------------------------------------------------------------------------
# PreflightCheck.check_sandbox_boundary()
# ---------------------------------------------------------------------------


class TestSandboxBoundaryPreflight:
    def test_sandbox_check_passes_when_inside(self):
        check = PreflightCheck(
            expected_repo_root=Path("/home/agent/Projects/SWE-Sandbox"),
            sandbox_paths=[Path("/home/agent/Projects/SWE-Sandbox")],
        )
        failures = check.check_sandbox_boundary()
        assert failures == []

    def test_sandbox_check_fails_when_outside(self):
        check = PreflightCheck(
            expected_repo_root=Path("/home/agent/Projects/my-app"),
            sandbox_paths=[Path("/home/agent/Projects/SWE-Sandbox")],
        )
        failures = check.check_sandbox_boundary()
        assert len(failures) == 1
        assert "outside all configured sandbox paths" in failures[0]

    def test_sandbox_check_skipped_when_no_paths(self):
        check = PreflightCheck(
            expected_repo_root=Path("/home/agent/Projects/my-app"),
        )
        failures = check.check_sandbox_boundary()
        assert failures == []

    def test_sandbox_check_skipped_when_no_repo_root(self):
        check = PreflightCheck(
            sandbox_paths=[Path("/home/agent/Projects/SWE-Sandbox")],
        )
        failures = check.check_sandbox_boundary()
        assert failures == []

    def test_sandbox_check_multiple_paths(self):
        check = PreflightCheck(
            expected_repo_root=Path("/home/agent/Projects/SWE-Sandbox-HealthTrack"),
            sandbox_paths=[
                Path("/home/agent/Projects/SWE-Sandbox"),
                Path("/home/agent/Projects/SWE-Sandbox-HealthTrack"),
            ],
        )
        failures = check.check_sandbox_boundary()
        assert failures == []

    def test_sandbox_rejects_production_path(self):
        check = PreflightCheck(
            expected_repo_root=Path("/home/agent/Projects/SWE-Squad-DEV"),
            sandbox_paths=[
                Path("/home/agent/Projects/SWE-Sandbox"),
                Path("/home/agent/Projects/SWE-Sandbox-HealthTrack"),
            ],
        )
        failures = check.check_sandbox_boundary()
        assert len(failures) == 1


# ---------------------------------------------------------------------------
# claim_ticket fail-closed (verify the fix)
# ---------------------------------------------------------------------------


class TestClaimTicketFailClosed:
    def test_claim_returns_false_on_error(self):
        """Verify claim_ticket returns False (not True) when RPC fails."""
        from unittest.mock import patch, MagicMock
        from src.swe_team.supabase_store import SupabaseTicketStore

        store = SupabaseTicketStore.__new__(SupabaseTicketStore)
        store._team_id = "test"

        with patch.object(store, "_request", side_effect=ConnectionError("down")):
            result = store.claim_ticket("ticket-1", "agent-1")
        assert result is False
