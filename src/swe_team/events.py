"""
SWE-team specific pipeline events.

Extends the core A2A ``EventType`` vocabulary with events that the
autonomous SWE team emits and listens to:

    ISSUE_DETECTED → TRIAGE_COMPLETE → INVESTIGATION_COMPLETE →
    DEV_COMPLETE → TEST_COMPLETE → DEPLOY_COMPLETE → ROLLBACK_TRIGGERED

Each event type has a convenience factory on ``SWEEvent``.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class SWEEventType(enum.Enum):
    """Events in the autonomous SWE lifecycle."""

    # Detection & triage
    ISSUE_DETECTED = "issue_detected"
    TRIAGE_COMPLETE = "triage_complete"

    # Investigation
    INVESTIGATION_STARTED = "investigation_started"
    INVESTIGATION_COMPLETE = "investigation_complete"

    # Development
    DEV_STARTED = "dev_started"
    DEV_COMPLETE = "dev_complete"

    # Review
    REVIEW_REQUESTED = "review_requested"
    REVIEW_COMPLETE = "review_complete"

    # Testing
    TEST_STARTED = "test_started"
    TEST_COMPLETE = "test_complete"

    # Deployment & monitoring
    DEPLOY_STARTED = "deploy_started"
    DEPLOY_COMPLETE = "deploy_complete"
    ROLLBACK_TRIGGERED = "rollback_triggered"

    # Governance
    STABILITY_CHECK = "stability_check"
    STABILITY_GATE_RESULT = "stability_gate_result"


@dataclass
class SWEEvent:
    """An event emitted by an SWE team agent."""

    event: SWEEventType
    ticket_id: str
    source_agent: str
    payload: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    target_agents: List[str] = field(default_factory=list)

    # -------------------------------------------------------------------
    # Serialisation
    # -------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event": self.event.value,
            "ticket_id": self.ticket_id,
            "source_agent": self.source_agent,
            "payload": self.payload,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "target_agents": self.target_agents,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SWEEvent":
        return cls(
            event=SWEEventType(data["event"]),
            ticket_id=data["ticket_id"],
            source_agent=data["source_agent"],
            payload=data.get("payload", {}),
            event_id=data.get("event_id", uuid.uuid4().hex[:16]),
            timestamp=data.get(
                "timestamp", datetime.now(timezone.utc).isoformat()
            ),
            target_agents=data.get("target_agents", []),
        )

    # -------------------------------------------------------------------
    # Factory helpers
    # -------------------------------------------------------------------

    @classmethod
    def issue_detected(
        cls,
        ticket_id: str,
        source_agent: str,
        *,
        error_summary: str = "",
        module: str = "",
        severity: str = "medium",
    ) -> "SWEEvent":
        return cls(
            event=SWEEventType.ISSUE_DETECTED,
            ticket_id=ticket_id,
            source_agent=source_agent,
            payload={
                "error_summary": error_summary,
                "module": module,
                "severity": severity,
            },
        )

    @classmethod
    def triage_complete(
        cls,
        ticket_id: str,
        source_agent: str,
        *,
        assigned_to: str = "",
        severity: str = "medium",
    ) -> "SWEEvent":
        return cls(
            event=SWEEventType.TRIAGE_COMPLETE,
            ticket_id=ticket_id,
            source_agent=source_agent,
            payload={"assigned_to": assigned_to, "severity": severity},
        )

    @classmethod
    def investigation_complete(
        cls,
        ticket_id: str,
        source_agent: str,
        *,
        report: str = "",
        root_cause: str = "",
    ) -> "SWEEvent":
        return cls(
            event=SWEEventType.INVESTIGATION_COMPLETE,
            ticket_id=ticket_id,
            source_agent=source_agent,
            payload={"report": report, "root_cause": root_cause},
        )

    @classmethod
    def dev_complete(
        cls,
        ticket_id: str,
        source_agent: str,
        *,
        branch: str = "",
        files_changed: int = 0,
    ) -> "SWEEvent":
        return cls(
            event=SWEEventType.DEV_COMPLETE,
            ticket_id=ticket_id,
            source_agent=source_agent,
            payload={"branch": branch, "files_changed": files_changed},
        )

    @classmethod
    def test_complete(
        cls,
        ticket_id: str,
        source_agent: str,
        *,
        passed: bool = False,
        total: int = 0,
        failures: int = 0,
    ) -> "SWEEvent":
        return cls(
            event=SWEEventType.TEST_COMPLETE,
            ticket_id=ticket_id,
            source_agent=source_agent,
            payload={
                "passed": passed,
                "total": total,
                "failures": failures,
            },
        )

    @classmethod
    def deploy_complete(
        cls,
        ticket_id: str,
        source_agent: str,
        *,
        deployment_id: str = "",
        success: bool = True,
    ) -> "SWEEvent":
        return cls(
            event=SWEEventType.DEPLOY_COMPLETE,
            ticket_id=ticket_id,
            source_agent=source_agent,
            payload={
                "deployment_id": deployment_id,
                "success": success,
            },
        )

    @classmethod
    def rollback_triggered(
        cls,
        ticket_id: str,
        source_agent: str,
        *,
        reason: str = "",
        deployment_id: str = "",
    ) -> "SWEEvent":
        return cls(
            event=SWEEventType.ROLLBACK_TRIGGERED,
            ticket_id=ticket_id,
            source_agent=source_agent,
            payload={
                "reason": reason,
                "deployment_id": deployment_id,
            },
        )

    @classmethod
    def stability_gate_result(
        cls,
        ticket_id: str,
        source_agent: str,
        *,
        verdict: str = "pass",
        details: str = "",
    ) -> "SWEEvent":
        return cls(
            event=SWEEventType.STABILITY_GATE_RESULT,
            ticket_id=ticket_id,
            source_agent=source_agent,
            payload={"verdict": verdict, "details": details},
        )
