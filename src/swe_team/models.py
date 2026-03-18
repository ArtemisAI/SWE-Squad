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
from typing import Any, ClassVar, Dict, List, Optional, Set, Tuple


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
    NEEDS_INFO = "needs_info"
    BLOCKED = "blocked"
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


class TicketType(str, enum.Enum):
    """Classification of what kind of work this ticket requires."""
    BUG = "bug"
    FEATURE = "feature"
    ENHANCEMENT = "enhancement"
    INFRASTRUCTURE = "infrastructure"
    DOCUMENTATION = "documentation"
    QUESTION = "question"
    SECURITY = "security"
    REGRESSION = "regression"
    UNKNOWN = "unknown"


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
    ticket_type: TicketType = field(default_factory=lambda: TicketType.UNKNOWN)
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
            "ticket_type": self.ticket_type.value,
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
            ticket_type=TicketType(data.get("ticket_type", "unknown")),
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

    # Resolution bypass reasons that satisfy the audit gate without a full report.
    # Any ticket resolved with one of these notes is considered legitimately closed.
    RESOLUTION_BYPASS_REASONS: ClassVar[set] = {
        "false_regression",
        "duplicate",
        "already_fixed_externally",
        "not_reproducible",
        "wont_fix_approved",
        "manual_override",
    }

    def resolution_audit(self) -> tuple[bool, str]:
        """Check whether this ticket may legitimately be closed as RESOLVED.

        Returns (ok, reason).  ``ok=False`` means the transition should be
        blocked; ``reason`` is a human-readable explanation.

        Rules
        -----
        1. A recognised bypass note in ``metadata['resolution_note']`` always
           permits closure regardless of report or attempts.
        2. Otherwise the ticket must have an investigation report of at least
           200 characters.
        3. HIGH / CRITICAL tickets additionally need at least one fix attempt
           OR an explicit bypass note.
        """
        note = str(self.metadata.get("resolution_note", "")).lower()
        if any(r in note for r in self.RESOLUTION_BYPASS_REASONS):
            return True, f"bypass: {note[:80]}"

        report = self.investigation_report or ""
        if len(report) < 200:
            return False, (
                f"investigation_report too short ({len(report)} chars, need ≥200). "
                "Investigate first or set metadata['resolution_note'] to a bypass reason: "
                + ", ".join(sorted(self.RESOLUTION_BYPASS_REASONS))
            )

        if self.severity in (TicketSeverity.HIGH, TicketSeverity.CRITICAL):
            attempts = self.metadata.get("attempts", [])
            if not attempts:
                return False, (
                    f"{self.severity.value.upper()} ticket requires ≥1 fix attempt before RESOLVED. "
                    "Attempts list is empty. Run developer agent or set resolution_note bypass."
                )

        return True, "audit passed"

    def transition(self, new_status: TicketStatus) -> None:
        """Move the ticket to *new_status* and touch the timestamp.

        Raises ``ValueError`` if transitioning to RESOLVED without passing
        the resolution audit.  To force-close, set
        ``ticket.metadata['resolution_note']`` to a bypass reason first.
        """
        if new_status == TicketStatus.RESOLVED:
            ok, reason = self.resolution_audit()
            if not ok:
                raise ValueError(
                    f"Resolution blocked for {self.ticket_id} ({self.severity.value} / "
                    f"{self.status.value}): {reason}"
                )
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


# ---------------------------------------------------------------------------
# Knowledge Graph Models
# ---------------------------------------------------------------------------

class EdgeType(enum.Enum):
    """Types of knowledge graph edges between entities."""
    SIMILAR = "similar"                    # Two tickets with high cosine similarity
    TOUCHES_MODULE = "touches_module"      # Ticket/PR ↔ code module
    BLOCKS = "blocks"                      # Ticket A blocks resolution of B
    RESOLVES = "resolves"                  # PR resolves a ticket
    CONFLICTS_WITH = "conflicts_with"      # Two PRs touching same files
    CAUSED_REGRESSION = "caused_regression"  # PR caused a new ticket


@dataclass
class KnowledgeEdge:
    """A directed relationship between two entities in the knowledge graph."""
    source_id: str
    target_id: str
    edge_type: EdgeType
    confidence: float = 0.0
    discovered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    discovered_by: str = ""       # 'embedding', 'fact_extraction', 'pr_sync', 'investigator'
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "edge_type": self.edge_type.value,
            "confidence": self.confidence,
            "discovered_at": self.discovered_at,
            "discovered_by": self.discovered_by,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeEdge":
        return cls(
            source_id=data["source_id"],
            target_id=data["target_id"],
            edge_type=EdgeType(data.get("edge_type", "similar")),
            confidence=float(data.get("confidence", 0.0)),
            discovered_at=data.get("discovered_at", datetime.now(timezone.utc).isoformat()),
            discovered_by=data.get("discovered_by", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass
class CodeModule:
    """A code module tracked in the knowledge graph."""
    module_id: str                          # e.g. "security.py"
    repo: str = ""                          # "ArtemisAI/LinkedAi"
    file_path: str = ""
    last_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module_id": self.module_id,
            "repo": self.repo,
            "file_path": self.file_path,
            "last_seen": self.last_seen,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CodeModule":
        return cls(
            module_id=data["module_id"],
            repo=data.get("repo", ""),
            file_path=data.get("file_path", ""),
            last_seen=data.get("last_seen", datetime.now(timezone.utc).isoformat()),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ResolutionCluster:
    """A group of tickets sharing a root cause."""
    cluster_id: str
    root_cause: str = ""
    primary_module: str = ""
    ticket_ids: List[str] = field(default_factory=list)
    status: str = "open"                    # 'open', 'investigating', 'resolved'
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "root_cause": self.root_cause,
            "primary_module": self.primary_module,
            "ticket_ids": self.ticket_ids,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResolutionCluster":
        return cls(
            cluster_id=data["cluster_id"],
            root_cause=data.get("root_cause", ""),
            primary_module=data.get("primary_module", ""),
            ticket_ids=data.get("ticket_ids", []),
            status=data.get("status", "open"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
            metadata=data.get("metadata", {}),
        )


@dataclass
class PRNode:
    """A GitHub pull request tracked in the knowledge graph."""
    pr_id: str                              # "ArtemisAI/LinkedAi#142"
    repo: str = ""
    number: int = 0
    branch: str = ""
    title: str = ""
    status: str = "open"                    # 'open', 'merged', 'closed'
    author: str = ""
    files_changed: List[str] = field(default_factory=list)
    ticket_ids: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    merged_at: Optional[str] = None
    review_status: str = "pending"          # 'pending', 'approved', 'changes_requested'
    last_checked: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pr_id": self.pr_id,
            "repo": self.repo,
            "number": self.number,
            "branch": self.branch,
            "title": self.title,
            "status": self.status,
            "author": self.author,
            "files_changed": self.files_changed,
            "ticket_ids": self.ticket_ids,
            "created_at": self.created_at,
            "merged_at": self.merged_at,
            "review_status": self.review_status,
            "last_checked": self.last_checked,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PRNode":
        return cls(
            pr_id=data["pr_id"],
            repo=data.get("repo", ""),
            number=int(data.get("number", 0)),
            branch=data.get("branch", ""),
            title=data.get("title", ""),
            status=data.get("status", "open"),
            author=data.get("author", ""),
            files_changed=data.get("files_changed", []),
            ticket_ids=data.get("ticket_ids", []),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            merged_at=data.get("merged_at"),
            review_status=data.get("review_status", "pending"),
            last_checked=data.get("last_checked", datetime.now(timezone.utc).isoformat()),
            metadata=data.get("metadata", {}),
        )
