"""
RBAC middleware decorators and context manager for SWE-Squad agents.

Provides:
  - require_permission(task)  — method decorator that enforces RBAC before execution
  - require_sandbox(method)   — decorator that verifies cwd is inside a sandbox path
  - RBACContext               — context manager for scoped RBAC with audit logging

All decorators are backward compatible: if the instance has no _rbac_engine or
_agent_name attribute, the permission check is silently skipped.
"""
from __future__ import annotations

import functools
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SandboxViolationError(Exception):
    """Raised when an agent attempts to work outside its sandbox paths."""
    pass


# Re-export PermissionDeniedError so callers only need to import from here.
from src.swe_team.agent_rbac import PermissionDeniedError  # noqa: E402


def require_permission(task: str, *, fail_action: str = "raise"):
    """Decorator that enforces RBAC before method execution.

    Usage::

        @require_permission("code_generation")
        def attempt_fix(self, ticket):
            ...

    Looks for ``self._rbac_engine`` and ``self._agent_name`` on the instance.
    If either is missing the permission check is skipped (backward compatible).

    Parameters
    ----------
    task:
        The RBAC permission task name (e.g. ``"code_generation"``).
    fail_action:
        What to do when permission is denied.
        - ``"raise"``       — raise :class:`PermissionDeniedError` (default)
        - ``"return_none"`` — return ``None`` silently
        - ``"log_only"``    — log a warning and proceed anyway (audit only)
    """
    if fail_action not in ("raise", "return_none", "log_only"):
        raise ValueError(f"Invalid fail_action: {fail_action!r}. Must be 'raise', 'return_none', or 'log_only'")

    def decorator(method):
        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            rbac_engine = getattr(self, "_rbac_engine", None)
            agent_name = getattr(self, "_agent_name", None)

            if rbac_engine is not None and agent_name is not None:
                allowed, reason = rbac_engine.check_permission(agent_name, task)
                if not allowed:
                    msg = f"RBAC denied: agent '{agent_name}' lacks permission '{task}': {reason}"
                    logger.warning(msg)
                    if fail_action == "raise":
                        raise PermissionDeniedError(msg)
                    elif fail_action == "return_none":
                        return None
                    # "log_only" falls through to execute the method
                else:
                    logger.debug(
                        "RBAC granted: agent '%s' → task '%s' (%s)",
                        agent_name, task, reason,
                    )
            else:
                # No RBAC engine/agent_name present — skip check but warn
                if rbac_engine is None or agent_name is None:
                    logger.warning(
                        "RBAC skip (no engine on %s.%s) — running unprotected",
                        type(self).__name__, method.__name__,
                    )

            return method(self, *args, **kwargs)

        return wrapper
    return decorator


def require_sandbox(method):
    """Decorator that verifies the working directory is inside a sandbox path.

    Looks for ``self._sandbox_paths`` on the instance (a list of :class:`Path`).
    If the attribute is absent or empty the check is skipped.

    The current working directory is determined by:
    1. ``self._repo_root`` if present
    2. ``os.getcwd()`` as fallback

    Raises
    ------
    SandboxViolationError
        If the working directory is outside all configured sandbox paths.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        sandbox_paths: Optional[List[Path]] = getattr(self, "_sandbox_paths", None)
        if not sandbox_paths:
            return method(self, *args, **kwargs)

        cwd_raw = getattr(self, "_repo_root", None) or Path(os.getcwd())
        cwd = Path(cwd_raw).resolve()

        for sandbox in sandbox_paths:
            try:
                cwd.relative_to(Path(sandbox).resolve())
                return method(self, *args, **kwargs)
            except ValueError:
                continue

        raise SandboxViolationError(
            f"Working directory '{cwd}' is outside all sandbox paths: "
            f"{[str(p) for p in sandbox_paths]}. "
            "Agent must operate inside a sandbox repo."
        )

    return wrapper


class RBACContext:
    """Context manager for RBAC-scoped operations with audit logging.

    Usage::

        with RBACContext(rbac_engine, "swe_developer", "code_generation") as ctx:
            # do work — ctx.allowed is True if permission was granted
            ...

    If permission is denied ``__enter__`` raises :class:`PermissionDeniedError`.
    Audit trail entries are written to ``self.audit_trail`` (list of dicts) and
    also logged at INFO level.
    """

    def __init__(
        self,
        rbac_engine: Any,
        agent_name: str,
        task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._engine = rbac_engine
        self._agent_name = agent_name
        self._task = task
        self._context = context or {}
        self.audit_trail: List[Dict[str, Any]] = []
        self.allowed: bool = False

    def __enter__(self) -> "RBACContext":
        import time
        allowed, reason = self._engine.check_permission(
            self._agent_name, self._task, self._context
        )
        self.allowed = allowed

        entry: Dict[str, Any] = {
            "event": "permission_check",
            "agent": self._agent_name,
            "task": self._task,
            "allowed": allowed,
            "reason": reason,
            "ts": time.time(),
        }
        self.audit_trail.append(entry)
        logger.info(
            "RBAC audit: agent=%s task=%s allowed=%s reason=%s",
            self._agent_name, self._task, allowed, reason,
        )

        if not allowed:
            raise PermissionDeniedError(
                f"RBAC: agent '{self._agent_name}' denied task '{self._task}': {reason}"
            )

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        import time
        outcome = "completed" if exc_type is None else f"failed:{exc_type.__name__}"
        entry: Dict[str, Any] = {
            "event": "operation_end",
            "agent": self._agent_name,
            "task": self._task,
            "outcome": outcome,
            "ts": time.time(),
        }
        self.audit_trail.append(entry)
        logger.info(
            "RBAC audit: agent=%s task=%s outcome=%s",
            self._agent_name, self._task, outcome,
        )
        # Never suppress exceptions
        return False
