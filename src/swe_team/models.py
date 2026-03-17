"""
Data models for the Autonomous SWE Team.

Defines tickets, agent roles, severity levels, and governance structures
used to coordinate the autonomous development lifecycle:
  detect → triage → investigate → develop → test → deploy → monitor
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TicketSeverity(enum.Enum):
    """Urgency level for an SWE ticket."""
    CRITICAL = "critical"   # Pipeline-breaking, immediate response
    HIGH = "high"           # Significant degradation
    MEDIUM = "medium"       # Non-blocking improvement
    LOW = "low"             # Minor enhancement / tech debt


class TicketStatus(enum.Enum):
    """Lifecycle states of an SWE ticket."""
    OPEN = "open"
    TRIAGED = "triaged"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    INVESTIGATION_COMPLETE = "investigation_complete"
    IN_DEVELOPMENT = "in_development"
    IN_REVIEW = "in_review"
    TESTING = "testing"
    DEPLOYING = "deploying"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    ROLLED_BACK = "rolled_back"
    CLOSED = "closed"


class AgentRole(enum.Enum):
    """Roles within the autonomous SWE team."""
    MONITOR = "monitor"             # Scans logs / metrics for anomalies
    TRIAGE = "triage"               # Classifies and assigns tickets
    INVESTIGATOR = "investigator"   # Diagnoses root cause
    DEVELOPER = "developer"         # Implements fixes / features
    REVIEWER = "reviewer"           # Code review & approval
    TESTER = "tester"               # Runs tests in sandboxed env
    DEPLOYER = "deployer"           # Injects fixes, monitors rollback
    DOCUMENTER = "documenter"       # Keeps docs and tracking current
    CREATIVE = "creative"           # Proposes optimisations / improvements


class GovernanceVerdict(enum.Enum):
    """Outcome of the Ralph-Wiggum stability gate."""
    PASS = "pass"           # All checks green → proceed
    BLOCK = "block"         # Bugs must be fixed first
    WARN = "warn"           # Proceed with caution


# ---------------------------------------------------------------------------
# Core Data Models
# ---------------------------------------------------------------------------

@dataclass
class SWETicket:
    """A work item tracked by the autonomous SWE team."""

    title: str
    description: str
    severity: TicketSeverity = TicketSeverity.MEDIUM
    status: TicketStatus = TicketStatus.OPEN
    ticket_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    assigned_to: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    source_module: Optional[str] = None
    error_log: Optional[str] = None
    related_tickets: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Lifecycle bookkeeping
    investigation_report: Optional[str] = None
    proposed_fix: Optional[str] = None
    test_results: Optional[Dict[str, Any]] = None
    deployment_id: Optional[str] = None
    rollback_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "title": self.title,
            "description": self.description,
            "severity": self.severity.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "assigned_to": self.assigned_to,
            "labels": self.labels,
            "source_module": self.source_module,
            "error_log": self.error_log,
            "related_tickets": self.related_tickets,
            "metadata": self.metadata,
            "investigation_report": self.investigation_report,
            "proposed_fix": self.proposed_fix,
            "test_results": self.test_results,
            "deployment_id": self.deployment_id,
            "rollback_reason": self.rollback_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SWETicket":
        return cls(
            ticket_id=data.get("ticket_id", uuid.uuid4().hex[:12]),
            title=data["title"],
            description=data["description"],
            severity=TicketSeverity(data.get("severity", "medium")),
            status=TicketStatus(data.get("status", "open")),
            created_at=data.get(
                "created_at", datetime.now(timezone.utc).isoformat()
            ),
            updated_at=data.get(
                "updated_at", datetime.now(timezone.utc).isoformat()
            ),
            assigned_to=data.get("assigned_to"),
            labels=data.get("labels", []),
            source_module=data.get("source_module"),
            error_log=data.get("error_log"),
            related_tickets=data.get("related_tickets", []),
            metadata=data.get("metadata", {}),
            investigation_report=data.get("investigation_report"),
            proposed_fix=data.get("proposed_fix"),
            test_results=data.get("test_results"),
            deployment_id=data.get("deployment_id"),
            rollback_reason=data.get("rollback_reason"),
        )

    def transition(self, new_status: TicketStatus) -> None:
        """Move the ticket to *new_status* and touch the timestamp."""
        self.status = new_status
        self.updated_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Agent configuration model
# ---------------------------------------------------------------------------

@dataclass
class SWEAgentConfig:
    """Configuration for a single agent within the SWE team."""

    name: str
    role: AgentRole
    description: str = ""
    model: str = "sonnet"          # LLM model override
    tools: List[str] = field(default_factory=list)
    max_concurrent_tasks: int = 1
    enabled: bool = False
    node: str = "primary"                    # primary | worker

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role.value,
            "description": self.description,
            "model": self.model,
            "tools": self.tools,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "enabled": self.enabled,
            "node": self.node,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SWEAgentConfig":
        return cls(
            name=data["name"],
            role=AgentRole(data["role"]),
            description=data.get("description", ""),
            model=data.get("model", "sonnet"),
            tools=data.get("tools", []),
            max_concurrent_tasks=data.get("max_concurrent_tasks", 1),
            enabled=data.get("enabled", False),
            node=data.get("node", "primary"),
        )


# ---------------------------------------------------------------------------
# Governance / Ralph-Wiggum gate result
# ---------------------------------------------------------------------------

@dataclass
class StabilityReport:
    """Output of the Ralph-Wiggum stability check."""

    verdict: GovernanceVerdict
    open_critical: int = 0
    open_high: int = 0
    failing_tests: int = 0
    ci_status: str = "unknown"
    details: str = ""
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "open_critical": self.open_critical,
            "open_high": self.open_high,
            "failing_tests": self.failing_tests,
            "ci_status": self.ci_status,
            "details": self.details,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StabilityReport":
        return cls(
            verdict=GovernanceVerdict(data["verdict"]),
            open_critical=data.get("open_critical", 0),
            open_high=data.get("open_high", 0),
            failing_tests=data.get("failing_tests", 0),
            ci_status=data.get("ci_status", "unknown"),
            details=data.get("details", ""),
            checked_at=data.get(
                "checked_at", datetime.now(timezone.utc).isoformat()
            ),
        )
