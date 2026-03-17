"""
SWE Team adapter for the A2A Hub.

Allows other agents to submit tickets and trigger SWE team workflows via A2A.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from src.a2a.adapters.base import AgentAdapter
from src.a2a.dispatch import dispatch_event
from src.a2a.events import PipelineEvent
from src.a2a.models import (
    AgentCard,
    AgentSkill,
    Artifact,
    DataPart,
    Message,
    Task,
    TaskState,
    TaskStatus,
)
from src.swe_team.config import SWETeamConfig
from src.swe_team.events import SWEEvent
from src.swe_team.investigator import InvestigatorAgent
from src.swe_team.models import SWETicket, TicketStatus
from src.swe_team.monitor_agent import MonitorAgent
from src.swe_team.ralph_wiggum import RalphWiggumGate
from src.swe_team.ticket_store import TicketStore
from src.swe_team.triage_agent import TriageAgent

logger = logging.getLogger(__name__)

_ACTION_ALIASES = {
    "submit_ticket": "triage_ticket",
}


def swe_event_to_pipeline_event(event: SWEEvent) -> PipelineEvent:
    """Convert a SWEEvent into the generic A2A PipelineEvent format."""
    return PipelineEvent(
        event=f"swe_team.{event.event.value}",
        source_stage=event.source_agent,
        payload=event.to_dict(),
    )


async def _dispatch_all(events: List[SWEEvent], agent: Optional[str]) -> None:
    for evt in events:
        await dispatch_event(swe_event_to_pipeline_event(evt), agent=agent)


def _handle_dispatch_task(task: asyncio.Task[bool]) -> None:
    try:
        task.result()
    except Exception as exc:  # noqa: BLE001
        event_name = getattr(task, "event_name", "unknown")
        ticket_id = getattr(task, "ticket_id", "unknown")
        logger.warning(
            "SWE event dispatch failed (%s) for %s ticket_id=%s: %s",
            type(exc).__name__,
            event_name,
            ticket_id,
            exc,
        )


def dispatch_swe_events(events: List[SWEEvent], agent: Optional[str] = None) -> bool:
    """Best-effort dispatch of SWE events to the A2A Hub."""
    if not events:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        for evt in events:
            task = loop.create_task(
                dispatch_event(swe_event_to_pipeline_event(evt), agent=agent)
            )
            task.event_name = evt.event.value  # type: ignore[attr-defined]
            task.ticket_id = evt.ticket_id  # type: ignore[attr-defined]
            task.add_done_callback(_handle_dispatch_task)
        return True
    asyncio.run(_dispatch_all(events, agent))
    return True


class SWETeamAdapter(AgentAdapter):
    """A2A adapter that routes messages to the SWE team toolchain."""

    def __init__(
        self,
        *,
        config: SWETeamConfig,
        store: TicketStore,
        claude_path: str = "/usr/bin/claude",
        base_url: str = "http://localhost:18790",
    ) -> None:
        self._config = config
        self._store = store
        self._claude_path = claude_path
        self._base_url = base_url

    def agent_card(self) -> AgentCard:
        skills = [
            AgentSkill(
                id="monitor_scan",
                name="Monitor Scan",
                description="Scan logs and emit SWE tickets",
                tags=["swe", "monitor", "scan", "tickets"],
            ),
            AgentSkill(
                id="triage_ticket",
                name="Triage Ticket",
                description="Classify and assign a SWE ticket",
                tags=["swe", "triage", "ticket"],
            ),
            AgentSkill(
                id="investigate_ticket",
                name="Investigate Ticket",
                description="Run investigation on a SWE ticket",
                tags=["swe", "investigate", "diagnose"],
            ),
            AgentSkill(
                id="check_stability",
                name="Check Stability",
                description="Run Ralph-Wiggum stability gate check",
                tags=["swe", "stability", "governance"],
            ),
        ]
        return AgentCard(
            name="SWE-Squad",
            description="Autonomous SWE team workflows (monitor, triage, investigate)",
            url=self._base_url,
            version="0.2.0",
            skills=skills,
            provider={},
        )

    def handle_action(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Synchronously execute a SWE team action (test helper; no async work)."""
        action = _ACTION_ALIASES.get(action, action)
        return self._handle_action(action, payload)

    async def handle_message(
        self, message: Message, session_id: Optional[str] = None
    ) -> Task:
        payload = self._extract_payload(message)
        action = payload.get("action") or payload.get("command")
        action = _ACTION_ALIASES.get(action, action)

        task = Task(session_id=session_id)
        task.history.append(message)
        if not action:
            task.status = TaskStatus(
                state=TaskState.FAILED,
                message="Missing action for SWE Team adapter",
            )
            return task
        task.status = TaskStatus(state=TaskState.WORKING)

        try:
            result = self._handle_action(action, payload)
            task.status = TaskStatus(state=TaskState.COMPLETED)
            task.artifacts.append(Artifact(parts=[DataPart(data={
                "action": action,
                "result": result,
            })]))
        except Exception as exc:  # noqa: BLE001
            task.status = TaskStatus(state=TaskState.FAILED, message=str(exc))
            logger.exception("SWE Team adapter failed")

        return task

    def _handle_action(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if action == "monitor_scan":
            return self._monitor_scan()
        if action == "triage_ticket":
            return self._triage_ticket(payload)
        if action == "investigate_ticket":
            return self._investigate_ticket(payload)
        if action == "check_stability":
            return self._check_stability(payload)
        raise ValueError(f"Unknown SWE Team action: {action}")

    def _monitor_scan(self) -> Dict[str, Any]:
        monitor = MonitorAgent(
            self._config.monitor,
            known_fingerprints=self._store.known_fingerprints,
        )
        tickets = monitor.scan()
        for ticket in tickets:
            self._store.add(ticket)
        return {"tickets": [t.to_dict() for t in tickets], "count": len(tickets)}

    def _triage_ticket(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ticket = self._resolve_ticket(payload)
        triage = TriageAgent(self._config)
        triage.triage(ticket)
        self._store.add(ticket)
        return {"ticket": ticket.to_dict()}

    def _investigate_ticket(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ticket = self._resolve_ticket(payload)
        if ticket.status == TicketStatus.OPEN:
            ticket.transition(TicketStatus.TRIAGED)
        investigator = InvestigatorAgent(claude_path=self._claude_path)
        investigated = investigator.investigate(ticket)
        self._store.add(ticket)
        return {"ticket": ticket.to_dict(), "investigated": investigated}

    def _check_stability(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        gate = RalphWiggumGate(self._config.governance)
        report = gate.evaluate(
            self._store.list_open(),
            ci_green=payload.get("ci_green", True),
            failing_tests=int(payload.get("failing_tests", 0)),
        )
        return {"report": report.to_dict()}

    def _resolve_ticket(self, payload: Dict[str, Any]) -> SWETicket:
        ticket_id = payload.get("ticket_id")
        ticket_data = payload.get("ticket")
        if ticket_id:
            ticket = self._store.get(ticket_id)
            if ticket is None:
                raise ValueError(f"Ticket not found: {ticket_id}")
            return ticket
        if isinstance(ticket_data, dict):
            return SWETicket.from_dict(ticket_data)
        raise ValueError("Missing ticket data")

    @staticmethod
    def _extract_payload(message: Message) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        for part in message.parts:
            if isinstance(part, DataPart) and isinstance(part.data, dict):
                payload.update(part.data)
        return payload
