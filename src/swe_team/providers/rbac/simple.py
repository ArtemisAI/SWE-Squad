"""Simple RBAC engine -- enforces team-based permissions via config.

This is a lightweight alternative to the full RBACEngine in agent_rbac.py.
It maps team roles (e.g. "developer", "investigator") to permission sets,
suitable for wiring into agent constructors so that @require_permission
decorators are never silently skipped.

The check_permission() signature returns (bool, str) to match the protocol
expected by rbac_middleware.require_permission.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class SimpleRBACEngine:
    """Permission checks based on team role and agent name."""

    ROLE_PERMISSIONS: Dict[str, Set[str]] = {
        "investigator": {
            "investigation", "triage", "read", "search", "summarization",
        },
        "developer": {
            "investigation", "triage", "code_generation", "push_branch",
            "pr_create", "commit", "branch_create", "read", "search",
        },
        "reviewer": {
            "code_review", "pr_create", "read", "search",
        },
        "orchestrator": {
            "investigation", "triage", "code_generation", "push_branch",
            "pr_create", "commit", "branch_create", "orchestration",
            "code_review", "read", "search",
        },
        "full": {
            "investigation", "triage", "code_generation", "push_branch",
            "pr_create", "commit", "branch_create", "code_review",
            "orchestration", "test", "read", "search", "summarization",
            "dashboard",
        },
        "senior": {
            "investigation", "triage", "code_generation", "push_branch",
            "pr_create", "commit", "branch_create", "code_review",
            "orchestration", "test", "pr_merge", "read", "search",
            "summarization", "dashboard",
        },
    }
    # CRITICAL: pr_merge is ONLY in "senior" role -- developer/reviewer cannot merge

    def __init__(self, team_role: str = "developer") -> None:
        self._role = team_role
        self._permissions: Set[str] = set(self.ROLE_PERMISSIONS.get(team_role, set()))
        if team_role not in self.ROLE_PERMISSIONS:
            logger.warning(
                "SimpleRBACEngine: unknown role %r -- deny-by-default active",
                team_role,
            )

    @property
    def role(self) -> str:
        return self._role

    def check_permission(
        self,
        agent_name: str,
        permission: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Check whether *agent_name* is allowed to perform *permission*.

        Returns ``(allowed, reason)`` -- same contract as
        :meth:`agent_rbac.RBACEngine.check_permission`.
        """
        if permission in self._permissions:
            return True, f"granted (role={self._role})"
        logger.warning(
            "SimpleRBACEngine DENIED: agent=%s permission=%s role=%s",
            agent_name, permission, self._role,
        )
        return False, (
            f"Agent '{agent_name}' role '{self._role}' "
            f"does not include permission '{permission}'"
        )
