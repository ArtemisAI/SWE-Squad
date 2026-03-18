"""
Role-Based Access Control for SWE-Squad agents.

Loads role definitions from config/swe_team/roles.yaml and enforces
permissions at every pipeline stage. Deny-by-default: if a permission
is not explicitly granted, it is denied.

SEC-68: This module exists because a rogue agent (OpenClaw) was able to
generate code, create PRs, and self-merge without any permission check.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_ROLES_PATH = Path(__file__).resolve().parents[2] / "config" / "swe_team" / "roles.yaml"


class PermissionDeniedError(Exception):
    """Raised when an agent attempts an unauthorized action."""
    pass


class AgentRole:
    """Parsed role definition for a single agent."""

    def __init__(self, name: str, data: Dict[str, Any]):
        self.name = name
        self.description = data.get("description", "")
        self.enabled = data.get("enabled", True)
        self.permissions: set = set(data.get("permissions", []))
        self.deny: set = set(data.get("deny", []))
        self.models: List[str] = data.get("models", [])
        self.merge_policy: Dict = data.get("merge_policy", {})

    def has_permission(self, task: str) -> bool:
        """Check if this role grants the given permission."""
        if not self.enabled:
            return False
        # Explicit deny always wins
        if task in self.deny:
            return False
        return task in self.permissions


class RBACEngine:
    """Loads roles and enforces permissions."""

    def __init__(self, roles_path: Optional[Path] = None):
        self._path = roles_path or _ROLES_PATH
        self._roles: Dict[str, AgentRole] = {}
        self._overrides: List[Dict] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.warning("RBAC roles file not found at %s — deny-by-default active", self._path)
            return
        try:
            with open(self._path) as f:
                data = yaml.safe_load(f) or {}
            for name, role_data in data.get("roles", {}).items():
                self._roles[name] = AgentRole(name, role_data)
            self._overrides = data.get("overrides", [])
            logger.info("RBAC loaded: %d roles, %d overrides", len(self._roles), len(self._overrides))
        except Exception:
            logger.exception("Failed to load RBAC roles — deny-by-default active")

    def reload(self) -> None:
        """Reload roles from disk."""
        self._roles.clear()
        self._overrides.clear()
        self._load()

    def get_role(self, agent_name: str) -> Optional[AgentRole]:
        return self._roles.get(agent_name)

    def list_roles(self) -> Dict[str, AgentRole]:
        return dict(self._roles)

    def check_permission(
        self,
        agent_name: str,
        task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Check if agent_name is allowed to perform task.

        Returns (allowed, reason).
        Deny-by-default: unknown agents or unlisted permissions are denied.
        """
        context = context or {}

        role = self._roles.get(agent_name)
        if not role:
            logger.warning("RBAC: unknown agent '%s' denied task '%s' (deny-by-default)", agent_name, task)
            return False, f"Unknown agent '{agent_name}' — not in roles.yaml"

        if not role.enabled:
            return False, f"Agent '{agent_name}' is disabled in roles.yaml"

        # Check explicit deny first
        if task in role.deny:
            logger.critical(
                "RBAC DENIED: agent '%s' attempted '%s' — explicitly denied in roles.yaml",
                agent_name, task,
            )
            return False, f"Agent '{agent_name}' is explicitly denied permission '{task}'"

        # Check base permission
        if role.has_permission(task):
            return True, "granted"

        # Check overrides
        for override in self._overrides:
            condition = override.get("condition", {})
            allowed_agents = override.get("allow_agents", [])

            if agent_name not in allowed_agents:
                continue

            # Check if task matches
            task_cond = condition.get("task")
            if task_cond:
                tasks = task_cond if isinstance(task_cond, list) else [task_cond]
                if task not in tasks:
                    continue

            # Check severity condition if present
            sev_cond = condition.get("severity")
            if sev_cond and context.get("severity"):
                sevs = sev_cond if isinstance(sev_cond, list) else [sev_cond]
                if context["severity"] not in sevs:
                    continue

            # Check enforce: strict (blocks everyone else)
            if override.get("enforce") == "strict" and agent_name not in allowed_agents:
                continue

            logger.info("RBAC override: agent '%s' granted '%s' via rule '%s'",
                        agent_name, task, override.get("rule", "unnamed"))
            return True, f"granted via override: {override.get('rule', '')}"

        # Deny by default
        logger.warning("RBAC: agent '%s' denied task '%s' (not in permissions list)", agent_name, task)
        return False, f"Agent '{agent_name}' does not have permission '{task}'"

    def enforce(
        self,
        agent_name: str,
        task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Enforce permission — raises PermissionDeniedError if denied."""
        allowed, reason = self.check_permission(agent_name, task, context)
        if not allowed:
            raise PermissionDeniedError(f"RBAC: {reason}")


# Module-level singleton for convenience
_engine: Optional[RBACEngine] = None


def get_rbac_engine() -> RBACEngine:
    global _engine
    if _engine is None:
        _engine = RBACEngine()
    return _engine


def check_permission(agent_name: str, task: str, context: Optional[Dict] = None) -> Tuple[bool, str]:
    return get_rbac_engine().check_permission(agent_name, task, context)


def enforce_permission(agent_name: str, task: str, context: Optional[Dict] = None) -> None:
    get_rbac_engine().enforce(agent_name, task, context)
