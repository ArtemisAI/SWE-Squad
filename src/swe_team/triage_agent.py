"""
Ticket Triage Agent for the Autonomous SWE Team.

Receives ``ISSUE_DETECTED`` events (or raw ``SWETicket`` objects),
classifies them by severity and module, and assigns them to the
appropriate investigation or development agents.

Emits ``TRIAGE_COMPLETE`` events once assignment is done.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from src.swe_team.config import SWETeamConfig
from src.swe_team.events import SWEEvent, SWEEventType
from src.swe_team.models import (
    AgentRole,
    SWEAgentConfig,
    SWETicket,
    TicketSeverity,
    TicketStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module → preferred investigator mapping (extensible)
# ---------------------------------------------------------------------------
_MODULE_SPECIALITY: Dict[str, List[str]] = {
    "scraping": ["browser_investigator"],
    "auth": ["browser_investigator"],
    "database": ["db_investigator"],
    "a2a": ["infra_investigator"],
    "evaluation": ["evaluation_investigator"],
    "cv_tailoring": ["content_investigator"],
    "application": ["browser_investigator"],
    "easy_apply": ["browser_investigator"],
}


class TriageAgent:
    """Classifies and routes tickets to the right SWE sub-team.

    Triage rules (in order):
    1. CRITICAL tickets → first available investigator (any speciality)
    2. Module-specific tickets → specialised investigator if available
    3. Fallback → first enabled investigator
    """

    AGENT_NAME = "swe_triage"

    def __init__(self, config: SWETeamConfig) -> None:
        self._config = config
        self._investigators = config.get_agents_by_role(AgentRole.INVESTIGATOR)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def triage(self, ticket: SWETicket) -> SWETicket:
        """Classify *ticket* and assign it to an investigator.

        Mutates the ticket in-place (status → TRIAGED, assigned_to set)
        and returns it for chaining.
        """
        assignee = self._pick_assignee(ticket)
        ticket.assigned_to = assignee
        ticket.transition(TicketStatus.TRIAGED)
        logger.info(
            "Triaged ticket %s (%s) → %s",
            ticket.ticket_id,
            ticket.severity.value,
            assignee or "unassigned",
        )
        return ticket

    def triage_batch(self, tickets: List[SWETicket]) -> List[SWETicket]:
        """Triage a list of tickets, sorting critical-first."""
        priority_order = {
            TicketSeverity.CRITICAL: 0,
            TicketSeverity.HIGH: 1,
            TicketSeverity.MEDIUM: 2,
            TicketSeverity.LOW: 3,
        }
        sorted_tickets = sorted(
            tickets, key=lambda t: priority_order.get(t.severity, 99)
        )
        return [self.triage(t) for t in sorted_tickets]

    def build_events(self, tickets: List[SWETicket]) -> List[SWEEvent]:
        """Emit ``TRIAGE_COMPLETE`` events for triaged tickets."""
        return [
            SWEEvent.triage_complete(
                ticket_id=t.ticket_id,
                source_agent=self.AGENT_NAME,
                assigned_to=t.assigned_to or "",
                severity=t.severity.value,
            )
            for t in tickets
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _pick_assignee(self, ticket: SWETicket) -> Optional[str]:
        """Select the best investigator for *ticket*."""
        if not self._investigators:
            return None

        # 1. Critical → first available
        if ticket.severity == TicketSeverity.CRITICAL:
            return self._investigators[0].name

        # 2. Module-specific
        if ticket.source_module:
            preferred = _MODULE_SPECIALITY.get(ticket.source_module, [])
            for pref in preferred:
                for inv in self._investigators:
                    if inv.name == pref:
                        return inv.name

        # 3. Fallback → first investigator
        return self._investigators[0].name
