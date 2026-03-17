"""
CI/CD Governance for the Autonomous SWE Team.

Provides deployment rules, rollback logic, and integration checks.
Works alongside the Ralph-Wiggum stability gate to enforce:

* Sandboxed testing before production injection
* Automated rollback on post-deploy regressions
* Audit trail for every deployment decision
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from src.swe_team.events import SWEEvent, SWEEventType
from src.swe_team.models import (
    GovernanceVerdict,
    SWETicket,
    StabilityReport,
    TicketStatus,
)

logger = logging.getLogger(__name__)

_DEPENDENCY_FILES = frozenset(
    {
        "requirements.txt",
        "requirements.in",
        "pyproject.toml",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "setup.cfg",
        "setup.py",
    }
)


def check_fix_complexity(
    files_changed: Sequence[str],
    lines_changed: int,
    *,
    max_files: int = 5,
    max_lines: int = 200,
    allowed_modules: Optional[Set[str]] = None,
) -> Tuple[bool, str]:
    """Validate fix complexity against SWE team constraints.

    Returns a tuple of (is_valid, reason) where reason provides the
    first validation failure or "ok" when the fix passes all gates.

    Parameters:
        files_changed: Relative file paths from ``git diff --name-only``.
        lines_changed: Total lines changed (added + removed).
        max_files: Maximum number of files allowed in the fix.
        max_lines: Maximum total line changes allowed in the fix.
        allowed_modules: Optional set of allowed module names (e.g. {"swe_team"}).

    Example:
        ok, reason = check_fix_complexity(
            ["src/swe_team/runner.py", "tests/unit/test_swe_team.py"],
            120,
            allowed_modules={"swe_team"},
        )
    """
    if not files_changed:
        return False, "No files changed"
    if len(files_changed) > max_files:
        return False, f"Too many files changed ({len(files_changed)} > {max_files})"
    if lines_changed > max_lines:
        return False, f"Too many lines changed ({lines_changed} > {max_lines})"

    normalized = {Path(f).as_posix() for f in files_changed}
    if normalized & _DEPENDENCY_FILES:
        return False, "Dependency changes are not allowed"

    modules = {_module_for_path(f) for f in files_changed}
    allowed = set(allowed_modules or set())
    if allowed:
        allowed.add("tests")
        extra = {m for m in modules if m not in allowed}
        if extra:
            return False, f"Cross-module changes detected: {', '.join(sorted(extra))}"
    else:
        core_modules = {m for m in modules if m != "tests"}
        if len(core_modules) > 1:
            return False, "Cross-module changes detected"

    return True, "ok"


def _module_for_path(path: str) -> str:
    parts = Path(path).parts
    if not parts:
        return "unknown"
    if parts[0] == "src" and len(parts) > 1:
        return parts[1]
    if parts[0] == "tests":
        return "tests"
    if parts[0] == "scripts":
        return "scripts"
    return parts[0]


# ---------------------------------------------------------------------------
# Deployment record
# ---------------------------------------------------------------------------

@dataclass
class DeploymentRecord:
    """Immutable record of a single deployment attempt."""

    deployment_id: str = field(
        default_factory=lambda: uuid.uuid4().hex[:12]
    )
    ticket_id: str = ""
    branch: str = ""
    status: str = "pending"  # pending | deploying | deployed | rolled_back
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    completed_at: Optional[str] = None
    rollback_reason: Optional[str] = None
    test_results: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "ticket_id": self.ticket_id,
            "branch": self.branch,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "rollback_reason": self.rollback_reason,
            "test_results": self.test_results,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeploymentRecord":
        return cls(
            deployment_id=data.get("deployment_id", uuid.uuid4().hex[:12]),
            ticket_id=data.get("ticket_id", ""),
            branch=data.get("branch", ""),
            status=data.get("status", "pending"),
            started_at=data.get(
                "started_at", datetime.now(timezone.utc).isoformat()
            ),
            completed_at=data.get("completed_at"),
            rollback_reason=data.get("rollback_reason"),
            test_results=data.get("test_results"),
            metadata=data.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# Governance engine
# ---------------------------------------------------------------------------

class DeploymentGovernor:
    """Decides whether a deployment can proceed and tracks outcomes.

    Lifecycle:
    1. ``can_deploy()`` — pre-flight check (stability gate must pass)
    2. ``start_deployment()`` — record the attempt
    3. ``complete_deployment()`` — mark success
    4. ``rollback()`` — revert and record the reason
    """

    def __init__(self) -> None:
        self._records: List[DeploymentRecord] = []

    @property
    def records(self) -> List[DeploymentRecord]:
        return list(self._records)

    def can_deploy(self, stability: StabilityReport) -> bool:
        """Return ``True`` only if the stability gate passed."""
        if stability.verdict == GovernanceVerdict.BLOCK:
            logger.warning(
                "Deployment blocked by stability gate: %s",
                stability.details,
            )
            return False
        return True

    def start_deployment(
        self, ticket_id: str, branch: str = ""
    ) -> DeploymentRecord:
        """Create a new deployment record in ``deploying`` state."""
        rec = DeploymentRecord(
            ticket_id=ticket_id, branch=branch, status="deploying"
        )
        self._records.append(rec)
        logger.info(
            "Deployment %s started for ticket %s",
            rec.deployment_id,
            ticket_id,
        )
        return rec

    def complete_deployment(
        self,
        deployment_id: str,
        *,
        test_results: Optional[Dict[str, Any]] = None,
    ) -> Optional[DeploymentRecord]:
        """Mark a deployment as successfully ``deployed``."""
        rec = self._find(deployment_id)
        if rec is None:
            logger.error("Deployment %s not found", deployment_id)
            return None
        rec.status = "deployed"
        rec.completed_at = datetime.now(timezone.utc).isoformat()
        rec.test_results = test_results
        logger.info("Deployment %s completed", deployment_id)
        return rec

    def rollback(
        self, deployment_id: str, *, reason: str = ""
    ) -> Optional[DeploymentRecord]:
        """Revert a deployment and record the reason."""
        rec = self._find(deployment_id)
        if rec is None:
            logger.error("Deployment %s not found for rollback", deployment_id)
            return None
        rec.status = "rolled_back"
        rec.completed_at = datetime.now(timezone.utc).isoformat()
        rec.rollback_reason = reason
        logger.warning(
            "Deployment %s rolled back: %s", deployment_id, reason
        )
        return rec

    def build_deploy_event(
        self, record: DeploymentRecord
    ) -> SWEEvent:
        """Create an A2A event for a deployment outcome."""
        success = record.status == "deployed"
        return SWEEvent.deploy_complete(
            ticket_id=record.ticket_id,
            source_agent="deployer",
            deployment_id=record.deployment_id,
            success=success,
        )

    def build_rollback_event(
        self, record: DeploymentRecord
    ) -> SWEEvent:
        """Create an A2A event for a rollback."""
        return SWEEvent.rollback_triggered(
            ticket_id=record.ticket_id,
            source_agent="deployer",
            reason=record.rollback_reason or "",
            deployment_id=record.deployment_id,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find(self, deployment_id: str) -> Optional[DeploymentRecord]:
        for rec in self._records:
            if rec.deployment_id == deployment_id:
                return rec
        return None
