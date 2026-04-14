"""
Repo Router — maps tickets to sandbox repos (fail-closed).

Every ticket must resolve to a configured sandbox repo before any agent
can touch it.  Unknown or unconfigured repos raise ``ValueError``,
preventing accidental work in production repositories.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ResolvedRepo:
    """Result of resolving a ticket to a sandbox repo."""

    repo_name: str
    local_path: Path


class RepoRouter:
    """Routes tickets to sandbox repos based on config.

    Parameters
    ----------
    repos_config:
        List of repo dicts from ``config/swe_team.yaml`` ``repos:`` section.
        Each dict must have ``name`` and ``local_path`` keys.
    """

    def __init__(self, repos_config: List[Dict[str, Any]]) -> None:
        self._repos: Dict[str, Dict[str, Any]] = {}
        for entry in repos_config:
            name = entry.get("name", "")
            if name:
                self._repos[name] = entry

    def resolve(self, ticket: Any) -> ResolvedRepo:
        """Resolve a ticket to its sandbox repo.

        Looks up ``ticket.metadata["repo"]``.  Falls back to the first
        configured repo when the ticket has no ``repo`` metadata.

        Raises
        ------
        ValueError
            If the repo is not in the configured sandbox list (fail-closed).
        """
        repo_name = ""
        if hasattr(ticket, "metadata") and isinstance(ticket.metadata, dict):
            repo_name = ticket.metadata.get("repo", "")

        if not repo_name:
            # Fall back to first configured repo (default sandbox)
            if not self._repos:
                raise ValueError("No sandbox repos configured — cannot route ticket")
            first = next(iter(self._repos.values()))
            repo_name = first["name"]
            logger.warning(
                "Ticket %s has no metadata['repo'] — falling back to default repo '%s'. "
                "This may indicate cross-repo ticket contamination.",
                getattr(ticket, "ticket_id", "unknown"),
                repo_name,
            )

        if repo_name not in self._repos:
            raise ValueError(
                f"Repo '{repo_name}' is not in the configured sandbox list. "
                f"Allowed repos: {list(self._repos.keys())}"
            )

        entry = self._repos[repo_name]
        return ResolvedRepo(
            repo_name=repo_name,
            local_path=Path(entry["local_path"]),
        )

    def build_repos_map(self) -> Dict[str, Path]:
        """Build a ``{repo_name: Path}`` map for agent injection.

        This is the format expected by ``DeveloperAgent(repos_map=...)``
        and ``WorktreeManager.set_repos_map()``.
        """
        return {
            name: Path(entry["local_path"])
            for name, entry in self._repos.items()
        }

    def is_sandbox_path(self, path: Path) -> bool:
        """Return True if *path* is inside a configured sandbox repo."""
        resolved = path.resolve()
        for entry in self._repos.values():
            sandbox = Path(entry["local_path"]).resolve()
            try:
                resolved.relative_to(sandbox)
                return True
            except ValueError:
                continue
        return False

    @property
    def repo_names(self) -> List[str]:
        """Return list of configured sandbox repo names."""
        return list(self._repos.keys())
