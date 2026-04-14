"""
Multi-project registry for SWE-Squad.

Loads per-project definitions from config/projects/*.yaml.
Provides project isolation: credentials, paths, and budgets
never cross project boundaries.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECTS_DIR = Path(__file__).resolve().parents[3] / "config" / "projects"


@dataclass
class ProjectCredentials:
    """Per-project credential references (env var names, not values)."""
    github_token_env: str = ""
    api_keys: List[Dict[str, str]] = field(default_factory=list)

    def validate(self) -> List[str]:
        """Return list of missing env vars."""
        missing = []
        if self.github_token_env and not os.environ.get(self.github_token_env):
            missing.append(self.github_token_env)
        for key_def in self.api_keys:
            env = key_def.get("env", "")
            if env and not os.environ.get(env):
                missing.append(env)
        return missing


@dataclass
class ProjectBudget:
    """Per-project budget configuration."""
    daily_cap_usd: float = 0.0
    monthly_cap_usd: float = 0.0
    alert_threshold_pct: int = 80


@dataclass
class ProjectInfra:
    """Per-project infrastructure definition."""
    ssh_config: str = ""
    ssh_key: str = ""
    workers: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class Project:
    """A registered project with its configuration."""
    name: str = ""
    repo: str = ""
    local_path: str = ""
    credentials: ProjectCredentials = field(default_factory=ProjectCredentials)
    budget: ProjectBudget = field(default_factory=ProjectBudget)
    infrastructure: ProjectInfra = field(default_factory=ProjectInfra)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        proj = data.get("project", {})
        creds = data.get("credentials", {})
        budget = data.get("budget", {})
        infra = data.get("infrastructure", {})
        return cls(
            name=proj.get("name", ""),
            repo=proj.get("repo", ""),
            local_path=proj.get("local_path", ""),
            credentials=ProjectCredentials(
                github_token_env=creds.get("github_token_env", ""),
                api_keys=creds.get("api_keys", []),
            ),
            budget=ProjectBudget(
                daily_cap_usd=float(budget.get("daily_cap_usd", 0)),
                monthly_cap_usd=float(budget.get("monthly_cap_usd", 0)),
                alert_threshold_pct=int(budget.get("alert_threshold_pct", 80)),
            ),
            infrastructure=ProjectInfra(
                ssh_config=infra.get("ssh_config", ""),
                ssh_key=infra.get("ssh_key", ""),
                workers=infra.get("workers", []),
            ),
        )


class ProjectRegistry:
    """Discovers and manages project definitions."""

    def __init__(self, projects_dir: Optional[Path] = None):
        self._dir = projects_dir or _PROJECTS_DIR
        self._projects: Dict[str, Project] = {}
        self._load()

    def _load(self) -> None:
        if not self._dir.exists():
            logger.info("No projects directory at %s — creating", self._dir)
            self._dir.mkdir(parents=True, exist_ok=True)
            return
        for f in sorted(self._dir.glob("*.yaml")):
            try:
                with open(f) as fh:
                    data = yaml.safe_load(fh) or {}
                proj = Project.from_dict(data)
                if proj.name:
                    self._projects[proj.name] = proj
                    logger.info("Registered project: %s (%s)", proj.name, proj.repo)
            except Exception:
                logger.exception("Failed to load project from %s", f)

    def get(self, name: str) -> Optional[Project]:
        return self._projects.get(name)

    def get_by_repo(self, repo: str) -> Optional[Project]:
        for p in self._projects.values():
            if p.repo == repo:
                return p
        return None

    def list_projects(self) -> List[Project]:
        return list(self._projects.values())

    def validate_all(self) -> Dict[str, List[str]]:
        """Validate all projects. Returns {project_name: [missing_env_vars]}."""
        results = {}
        for name, proj in self._projects.items():
            missing = proj.credentials.validate()
            if missing:
                results[name] = missing
        return results

    def reload(self) -> None:
        self._projects.clear()
        self._load()
