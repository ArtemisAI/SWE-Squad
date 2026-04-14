"""Tests for GitWorktreeProvider — scoped .env injection and secure deletion."""

import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.swe_team.providers.env.base import BLOCKED_ENV_VARS, EnvSpec
from src.swe_team.providers.env.dotenv_provider import DotenvEnvProvider
from src.swe_team.providers.workspace.base import WorkspaceInfo, WorkspaceSpec
from src.swe_team.providers.workspace.git_worktree import GitWorktreeProvider
from src.swe_team.worktree_manager import Worktree, WorktreeManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider(
    tmp_path: Path,
    env_provider: DotenvEnvProvider | None = None,
    config: dict | None = None,
) -> GitWorktreeProvider:
    """Create a GitWorktreeProvider with a mocked WorktreeManager."""
    cfg = config or {}
    cfg.setdefault("worktree", {"base_dir": str(tmp_path / "worktrees")})
    provider = GitWorktreeProvider(
        config=cfg,
        env_provider=env_provider,
        repo_root=tmp_path,
    )
    # Replace the real WorktreeManager with a mock
    mock_mgr = MagicMock(spec=WorktreeManager)
    mock_mgr._base_dir = tmp_path / "worktrees"
    mock_mgr._base_dir.mkdir(parents=True, exist_ok=True)
    mock_mgr._worktrees = {}
    provider._worktree_manager = mock_mgr
    return provider


def _stub_acquire(tmp_path: Path, ticket_id: str, branch: str) -> Worktree:
    """Return a fake Worktree whose path is a real directory inside tmp_path."""
    wt_dir = tmp_path / "worktrees" / f"wt-{ticket_id}"
    wt_dir.mkdir(parents=True, exist_ok=True)
    wt = Worktree(
        path=wt_dir,
        branch=branch,
        ticket_id=ticket_id,
        acquired_at=time.monotonic(),
        in_use=True,
    )
    return wt


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCreateWritesEnvFile:
    """test_create_writes_env_file: verify .env exists after create()."""

    def test_create_writes_env_file(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-001", "swe-fix/ticket-T-001")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-001"] = wt

        spec = WorkspaceSpec(ticket_id="T-001", role="developer")
        info = provider.create(spec)

        assert info.env_path is not None
        assert info.env_path.exists()


class TestEnvFilePermissions:
    """test_env_file_has_correct_permissions: verify chmod 600."""

    def test_env_file_has_correct_permissions(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-002", "swe-fix/ticket-T-002")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-002"] = wt

        spec = WorkspaceSpec(ticket_id="T-002", role="developer")
        info = provider.create(spec)

        mode = info.env_path.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


class TestEnvFileContainsRoleScopedVars:
    """test_env_file_contains_role_scoped_vars: developer gets GH_TOKEN."""

    def test_env_file_contains_role_scoped_vars(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"GH_TOKEN": "ghp_test123", "PATH": "/usr/bin", "HOME": "/tmp"}):
            env_prov = DotenvEnvProvider()
            provider = _make_provider(tmp_path, env_provider=env_prov)
            wt = _stub_acquire(tmp_path, "T-003", "swe-fix/ticket-T-003")
            provider._worktree_manager.acquire.return_value = wt
            provider._worktree_manager._worktrees["T-003"] = wt

            spec = WorkspaceSpec(ticket_id="T-003", role="developer")
            info = provider.create(spec)

            content = info.env_path.read_text()
            assert "GH_TOKEN=ghp_test123" in content


class TestBlockedVarsNeverInEnvFile:
    """test_blocked_vars_never_in_env_file: SUPABASE_ANON_KEY etc. never written."""

    def test_blocked_vars_never_in_env_file(self, tmp_path: Path) -> None:
        # Inject blocked vars into os.environ
        blocked_env = {k: "secret_value" for k in BLOCKED_ENV_VARS}
        blocked_env.update({"PATH": "/usr/bin", "HOME": "/tmp"})
        with patch.dict(os.environ, blocked_env, clear=True):
            env_prov = DotenvEnvProvider()
            provider = _make_provider(tmp_path, env_provider=env_prov)
            wt = _stub_acquire(tmp_path, "T-004", "swe-fix/ticket-T-004")
            provider._worktree_manager.acquire.return_value = wt
            provider._worktree_manager._worktrees["T-004"] = wt

            spec = WorkspaceSpec(ticket_id="T-004", role="developer")
            info = provider.create(spec)

            content = info.env_path.read_text()
            for blocked_key in BLOCKED_ENV_VARS:
                # developer role does NOT allowlist these blocked vars
                if blocked_key not in ("GH_TOKEN",):
                    assert blocked_key not in content, f"{blocked_key} found in .env"


class TestReleaseDeletesEnvFile:
    """test_release_deletes_env_file: .env gone after release()."""

    def test_release_deletes_env_file(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-005", "swe-fix/ticket-T-005")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-005"] = wt

        spec = WorkspaceSpec(ticket_id="T-005", role="developer")
        info = provider.create(spec)
        env_path = info.env_path
        assert env_path.exists()

        provider.release("T-005")
        assert not env_path.exists()


class TestReleaseSecureWipe:
    """test_release_secure_wipe: file overwritten with zeros before deletion."""

    def test_release_secure_wipe(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-006", "swe-fix/ticket-T-006")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-006"] = wt

        spec = WorkspaceSpec(ticket_id="T-006", role="developer")
        info = provider.create(spec)
        env_path = info.env_path
        original_size = env_path.stat().st_size
        assert original_size > 0

        # Patch unlink to capture what was written before deletion
        written_bytes = []
        original_write_bytes = Path.write_bytes

        def capturing_write_bytes(self_path: Path, data: bytes) -> int:
            written_bytes.append(data)
            return original_write_bytes(self_path, data)

        with patch.object(Path, "write_bytes", capturing_write_bytes):
            provider.release("T-006")

        # Verify zeros were written
        assert len(written_bytes) >= 1
        assert written_bytes[0] == b"\x00" * original_size


class TestEnvOverridesFromSpec:
    """test_env_overrides_from_spec: WorkspaceSpec.env_overrides appear in .env."""

    def test_env_overrides_from_spec(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-007", "swe-fix/ticket-T-007")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-007"] = wt

        spec = WorkspaceSpec(
            ticket_id="T-007",
            role="developer",
            env_overrides={"CUSTOM_VAR": "custom_value"},
        )
        info = provider.create(spec)
        content = info.env_path.read_text()
        assert "CUSTOM_VAR=custom_value" in content


class TestCleanupStaleRemovesOldWorktrees:
    """test_cleanup_stale_removes_old_worktrees."""

    def test_cleanup_stale_removes_old_worktrees(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-008", "swe-fix/ticket-T-008")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-008"] = wt

        spec = WorkspaceSpec(ticket_id="T-008", role="developer")
        info = provider.create(spec)

        # Backdate created_at to 100 hours ago
        info.created_at = datetime.now(timezone.utc) - timedelta(hours=100)

        cleaned = provider.cleanup_stale(max_age_hours=48)
        assert cleaned == 1
        assert provider.get("T-008") is None


class TestCleanupStaleDeletesEnvFirst:
    """test_cleanup_stale_deletes_env_first: .env deleted before worktree removal."""

    def test_cleanup_stale_deletes_env_first(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-009", "swe-fix/ticket-T-009")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-009"] = wt

        spec = WorkspaceSpec(ticket_id="T-009", role="developer")
        info = provider.create(spec)
        info.created_at = datetime.now(timezone.utc) - timedelta(hours=100)

        call_order = []
        original_secure_delete = GitWorktreeProvider._secure_delete_env

        def tracking_delete(path: Path) -> None:
            call_order.append("secure_delete")
            original_secure_delete(path)

        original_release = provider._worktree_manager.release

        def tracking_release(wt_obj: Worktree) -> None:
            call_order.append("worktree_release")
            original_release(wt_obj)

        provider._worktree_manager.release.side_effect = tracking_release
        with patch.object(GitWorktreeProvider, "_secure_delete_env", staticmethod(tracking_delete)):
            provider.cleanup_stale(max_age_hours=48)

        assert call_order.index("secure_delete") < call_order.index("worktree_release")


class TestListActiveReturnsCreated:
    """test_list_active_returns_created: created workspace appears in list_active()."""

    def test_list_active_returns_created(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-010", "swe-fix/ticket-T-010")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-010"] = wt

        spec = WorkspaceSpec(ticket_id="T-010", role="developer")
        provider.create(spec)

        active = provider.list_active()
        assert len(active) == 1
        assert active[0].ticket_id == "T-010"


class TestGetReturnsWorkspaceInfo:
    """test_get_returns_workspace_info: get() returns correct WorkspaceInfo."""

    def test_get_returns_workspace_info(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-011", "swe-fix/ticket-T-011")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-011"] = wt

        spec = WorkspaceSpec(ticket_id="T-011", role="developer")
        provider.create(spec)

        info = provider.get("T-011")
        assert info is not None
        assert info.workspace_id == "T-011"
        assert info.ticket_id == "T-011"
        assert info.role == "developer"


class TestGetUnknownReturnsNone:
    """test_get_unknown_returns_none: get() returns None for unknown id."""

    def test_get_unknown_returns_none(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        assert provider.get("nonexistent") is None


class TestWorkspaceInfoHasEnvPath:
    """test_workspace_info_has_env_path: WorkspaceInfo.env_path is set."""

    def test_workspace_info_has_env_path(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-013", "swe-fix/ticket-T-013")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-013"] = wt

        spec = WorkspaceSpec(ticket_id="T-013", role="developer")
        info = provider.create(spec)

        assert info.env_path is not None
        assert info.env_path.name == ".env"
        assert str(info.env_path).startswith(str(wt.path))


class TestHealthCheckTrueWhenGitAvailable:
    """test_health_check_true_when_git_available: mocked git -> True."""

    def test_health_check_true_when_git_available(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        with patch("src.swe_team.providers.workspace.git_worktree.shutil.which", return_value="/usr/bin/git"):
            assert provider.health_check() is True

    def test_health_check_false_when_git_missing(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        with patch("src.swe_team.providers.workspace.git_worktree.shutil.which", return_value=None):
            assert provider.health_check() is False


class TestEnvFileInGitignoredDir:
    """test_env_file_in_gitignored_dir: base_dir is under data/ which is gitignored."""

    def test_env_file_in_gitignored_dir(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        gitignore = repo_root / ".gitignore"
        content = gitignore.read_text()
        assert "data/" in content


# ---------------------------------------------------------------------------
# Edge-case tests (5+ additional)
# ---------------------------------------------------------------------------

class TestReleaseTwice:
    """Release the same workspace twice should not raise."""

    def test_release_twice_is_safe(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-020", "swe-fix/ticket-T-020")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-020"] = wt

        spec = WorkspaceSpec(ticket_id="T-020", role="developer")
        provider.create(spec)
        provider.release("T-020")
        # Second release should be a no-op (logs a warning)
        provider.release("T-020")
        assert provider.get("T-020") is None


class TestReleaseUnknown:
    """Release an unknown workspace should not raise."""

    def test_release_unknown_is_safe(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        provider.release("unknown-id")
        # No exception = pass


class TestEmptyOverrides:
    """Empty env_overrides should still produce a valid .env."""

    def test_empty_overrides(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-022", "swe-fix/ticket-T-022")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-022"] = wt

        spec = WorkspaceSpec(ticket_id="T-022", role="developer", env_overrides={})
        info = provider.create(spec)
        assert info.env_path.exists()


class TestCleanupStaleNothingToClean:
    """cleanup_stale with no stale workspaces should return 0."""

    def test_cleanup_stale_returns_zero(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-023", "swe-fix/ticket-T-023")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-023"] = wt

        spec = WorkspaceSpec(ticket_id="T-023", role="developer")
        provider.create(spec)
        # Workspace just created, not stale
        cleaned = provider.cleanup_stale(max_age_hours=48)
        assert cleaned == 0


class TestMultipleWorkspaces:
    """Creating multiple workspaces and listing them."""

    def test_multiple_workspaces(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)

        for i in range(3):
            tid = f"T-03{i}"
            wt = _stub_acquire(tmp_path, tid, f"swe-fix/ticket-{tid}")
            provider._worktree_manager.acquire.return_value = wt
            provider._worktree_manager._worktrees[tid] = wt

            spec = WorkspaceSpec(ticket_id=tid, role="developer")
            provider.create(spec)

        assert len(provider.list_active()) == 3


class TestCreateBranchNaming:
    """Workspace branch follows swe-fix/ticket-{id} pattern."""

    def test_branch_naming(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-040", "swe-fix/ticket-T-040")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-040"] = wt

        spec = WorkspaceSpec(ticket_id="T-040", role="developer")
        info = provider.create(spec)
        assert info.branch == "swe-fix/ticket-T-040"


class TestSecureDeleteNonexistentFile:
    """_secure_delete_env on a nonexistent path should not raise."""

    def test_secure_delete_nonexistent(self, tmp_path: Path) -> None:
        fake_path = tmp_path / "does_not_exist.env"
        GitWorktreeProvider._secure_delete_env(fake_path)
        # No exception = pass


class TestWriteScopedEnvSorted:
    """_write_scoped_env should write keys in sorted order."""

    def test_env_keys_sorted(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        GitWorktreeProvider._write_scoped_env(env_file, {"ZEBRA": "1", "ALPHA": "2", "MIDDLE": "3"})
        lines = env_file.read_text().strip().split("\n")
        keys = [line.split("=")[0] for line in lines]
        assert keys == ["ALPHA", "MIDDLE", "ZEBRA"]


class TestEnvPathPointsInsideWorktree:
    """env_path should be inside the worktree directory."""

    def test_env_path_inside_worktree(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        wt = _stub_acquire(tmp_path, "T-050", "swe-fix/ticket-T-050")
        provider._worktree_manager.acquire.return_value = wt
        provider._worktree_manager._worktrees["T-050"] = wt

        spec = WorkspaceSpec(ticket_id="T-050", role="developer")
        info = provider.create(spec)
        assert info.env_path.parent == info.path


class TestInvestigatorRoleDoesNotGetGhToken:
    """investigator role should NOT receive GH_TOKEN."""

    def test_investigator_no_gh_token(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"GH_TOKEN": "ghp_secret", "PATH": "/usr/bin", "HOME": "/tmp"}):
            env_prov = DotenvEnvProvider()
            provider = _make_provider(tmp_path, env_provider=env_prov)
            wt = _stub_acquire(tmp_path, "T-060", "swe-fix/ticket-T-060")
            provider._worktree_manager.acquire.return_value = wt
            provider._worktree_manager._worktrees["T-060"] = wt

            spec = WorkspaceSpec(ticket_id="T-060", role="investigator")
            info = provider.create(spec)
            content = info.env_path.read_text()
            assert "GH_TOKEN" not in content
