"""
Git Worktree Manager for parallel SWE-Squad workers.

Maintains a pool of pre-created worktrees so that parallel investigation
and development workers each get their own isolated working directory.
This prevents git conflicts between concurrent Claude CLI sessions.

Usage::

    from src.swe_team.worktree_manager import WorktreeManager

    manager = WorktreeManager(repo_root="/path/to/your-project", pool_size=4)
    wt = manager.acquire(ticket_id="abc123", branch="swe-fix/ticket-abc123")
    # ... run Claude CLI in wt.path ...
    manager.release(wt)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_WORKTREE_PREFIX = "/tmp/swe-worktree"
_STALE_THRESHOLD_HOURS = 2.0
_DEFAULT_BASE_DIR = "data/worktrees"
_DEFAULT_MAX_AGE_HOURS = 48.0
_DEFAULT_MAX_CONCURRENT = 10


@dataclass
class Worktree:
    """Represents a single git worktree."""

    path: Path
    branch: str
    ticket_id: Optional[str] = None
    acquired_at: Optional[float] = None
    in_use: bool = False
    repo_root: Optional[Path] = None

    def age_hours(self) -> float:
        if self.acquired_at is None:
            return 0.0
        return (time.monotonic() - self.acquired_at) / 3600


class WorktreeManager:
    """Manages a pool of git worktrees for parallel workers.

    Parameters
    ----------
    repo_root:
        Path to the main git repository.
    pool_size:
        Maximum number of worktrees to maintain.
    worktree_base:
        Base directory for worktrees (default: /tmp/swe-worktree).
    """

    def __init__(
        self,
        repo_root: str | Path,
        pool_size: int = 4,
        worktree_base: Optional[str] = None,
        config: Optional[Dict] = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._config = config or {}

        # Resolve base_dir from config → explicit arg → legacy default
        cfg_base = self._config.get("base_dir", "")
        if worktree_base is not None:
            resolved_base = worktree_base
        elif cfg_base:
            resolved_base = cfg_base
        else:
            resolved_base = _DEFAULT_BASE_DIR

        base_path = Path(resolved_base)
        if not base_path.is_absolute():
            base_path = self._repo_root / base_path
        self._base_dir = base_path.resolve()
        if self._repo_root.exists():
            self._base_dir.mkdir(parents=True, exist_ok=True)

        self._worktree_base = worktree_base if worktree_base is not None else str(self._base_dir / "wt")
        self._pool_size = int(self._config.get("max_concurrent", pool_size))
        self._max_age_hours: float = float(self._config.get("max_age_hours", _DEFAULT_MAX_AGE_HOURS))
        self._repos_map: Dict[str, Path] = {}
        self._lock = threading.Lock()
        self._worktrees: Dict[str, Worktree] = {}  # keyed by ticket_id

    def set_repos_map(self, repos: dict) -> None:
        """Set the repos map, converting string values to Path objects.

        Parameters
        ----------
        repos:
            Dict mapping repo names (e.g. "owner/repo") to local clone paths.
        """
        self._repos_map = {name: Path(path) for name, path in repos.items()}

    def repo_root_for(self, repo_name: Optional[str]) -> Path:
        """Return the configured local path for *repo_name*.

        Falls back to ``self._repo_root`` if *repo_name* is None or not found
        in the repos map.
        """
        if repo_name is not None and repo_name in self._repos_map:
            return self._repos_map[repo_name]
        return self._repo_root

    def acquire(
        self,
        ticket_id: str,
        branch: str,
        *,
        base_ref: str = "HEAD",
        repo_name: Optional[str] = None,
    ) -> Worktree:
        """Acquire a worktree for a ticket.

        Creates a new git worktree in a unique directory, checking out
        the specified branch. If the branch already exists, it reuses it.

        Parameters
        ----------
        ticket_id:
            Unique ticket identifier (used for directory naming).
        branch:
            Branch name to create/checkout in the worktree.
        base_ref:
            Git ref to base the new branch on (default: HEAD).

        Returns
        -------
        Worktree object with the path to use as working directory.

        Raises
        ------
        RuntimeError:
            If the worktree pool is exhausted or git commands fail.
        """
        with self._lock:
            # Check if already acquired
            if ticket_id in self._worktrees:
                existing = self._worktrees[ticket_id]
                if existing.in_use:
                    return existing

            # Check pool capacity
            active = sum(1 for wt in self._worktrees.values() if wt.in_use)
            if active >= self._pool_size:
                raise RuntimeError(
                    f"Worktree pool exhausted ({active}/{self._pool_size} in use). "
                    f"Wait for a worker to finish or increase pool_size."
                )

        resolved_repo_root = self.repo_root_for(repo_name)
        worktree_dir = Path(f"{self._worktree_base}-{ticket_id}")

        # Clean up stale worktree from a previous crash
        if worktree_dir.exists():
            try:
                self._git(["git", "worktree", "remove", "--force", str(worktree_dir)], repo_root=resolved_repo_root)
            except RuntimeError:
                shutil.rmtree(worktree_dir, ignore_errors=True)

        # Prune dead worktree references
        try:
            self._git(["git", "worktree", "prune"], repo_root=resolved_repo_root)
        except RuntimeError:
            pass

        # Delete the branch if it exists (to avoid conflicts)
        try:
            self._git(["git", "branch", "-D", branch], repo_root=resolved_repo_root)
        except RuntimeError:
            pass  # Branch doesn't exist yet — fine

        # Create the worktree with a new branch
        try:
            self._git(["git", "worktree", "add", str(worktree_dir), "-b", branch, base_ref], repo_root=resolved_repo_root)
        except RuntimeError as exc:
            # Branch might already exist from a partial cleanup
            if "already exists" in str(exc):
                try:
                    self._git(["git", "worktree", "add", str(worktree_dir), branch], repo_root=resolved_repo_root)
                except RuntimeError:
                    raise RuntimeError(
                        f"Failed to create worktree for {ticket_id}: {exc}"
                    ) from exc
            else:
                raise

        wt = Worktree(
            path=worktree_dir,
            branch=branch,
            ticket_id=ticket_id,
            acquired_at=time.monotonic(),
            in_use=True,
            repo_root=resolved_repo_root,
        )

        with self._lock:
            self._worktrees[ticket_id] = wt

        logger.info(
            "Worktree acquired for %s at %s (branch=%s)",
            ticket_id, worktree_dir, branch,
        )
        return wt

    def release(self, wt: Worktree) -> None:
        """Release a worktree back to the pool.

        Removes the worktree directory and marks it as available.
        """
        ticket_id = wt.ticket_id or ""
        repo_root = wt.repo_root  # may be None → _git falls back to self._repo_root

        try:
            self._git(["git", "worktree", "remove", "--force", str(wt.path)], repo_root=repo_root)
        except RuntimeError:
            logger.warning(
                "Failed to remove worktree %s via git — cleaning up manually",
                wt.path,
            )
            shutil.rmtree(wt.path, ignore_errors=True)
            try:
                self._git(["git", "worktree", "prune"], repo_root=repo_root)
            except RuntimeError:
                pass

        with self._lock:
            wt.in_use = False
            if ticket_id in self._worktrees:
                del self._worktrees[ticket_id]

        logger.info("Worktree released for %s", ticket_id)

    def cleanup_stale(self, threshold_hours: float = _STALE_THRESHOLD_HOURS) -> int:
        """Remove worktrees that have been in use beyond the threshold.

        Returns the number of stale worktrees cleaned up.
        """
        stale: List[Worktree] = []
        with self._lock:
            for wt in self._worktrees.values():
                if wt.in_use and wt.age_hours() > threshold_hours:
                    stale.append(wt)

        cleaned = 0
        for wt in stale:
            logger.warning(
                "Cleaning up stale worktree for %s (age=%.1fh)",
                wt.ticket_id, wt.age_hours(),
            )
            try:
                self.release(wt)
                cleaned += 1
            except Exception:
                logger.exception("Failed to clean up stale worktree %s", wt.path)

        return cleaned

    def cleanup_all(self) -> None:
        """Remove all managed worktrees. Called on shutdown."""
        with self._lock:
            worktrees = list(self._worktrees.values())

        for wt in worktrees:
            try:
                self.release(wt)
            except Exception:
                logger.exception("Failed to clean up worktree %s", wt.path)

        # Final prune to catch any orphans
        try:
            self._git(["git", "worktree", "prune"])
        except RuntimeError:
            pass

    def active_count(self) -> int:
        """Return number of worktrees currently in use."""
        with self._lock:
            return sum(1 for wt in self._worktrees.values() if wt.in_use)

    def status(self) -> Dict:
        """Return worktree pool status."""
        with self._lock:
            active = [
                {
                    "ticket_id": wt.ticket_id,
                    "branch": wt.branch,
                    "path": str(wt.path),
                    "age_hours": round(wt.age_hours(), 2),
                }
                for wt in self._worktrees.values()
                if wt.in_use
            ]
        return {
            "pool_size": self._pool_size,
            "active": len(active),
            "worktrees": active,
        }

    def _git(self, cmd: List[str], repo_root: Optional[Path] = None) -> str:
        result = subprocess.run(
            cmd,
            cwd=repo_root or self._repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git command failed")
        return result.stdout
