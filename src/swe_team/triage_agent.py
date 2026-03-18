"""
Ticket Triage Agent for the Autonomous SWE Team.

Receives ``ISSUE_DETECTED`` events (or raw ``SWETicket`` objects),
classifies them by severity and module, and assigns them to the
appropriate investigation or development agents.

Emits ``TRIAGE_COMPLETE`` events once assignment is done.

HITL Detection
--------------
Before routing to an investigator, triage checks for patterns that are
inherently un-automatable (external account access, credential rotation,
CAPTCHA, regulatory decisions, etc.).  These tickets are flagged with
``metadata["needs_hitl"] = True`` and assigned to the configured HITL
assignee (``SWE_HITL_ASSIGNEE`` env var, default ``ArtemisAI``) so the
automation pipeline skips them and GitHub surfaces the escalation.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from src.swe_team.config import SWETeamConfig
from src.swe_team.events import SWEEvent, SWEEventType
from src.swe_team.models import (
    AgentRole,
    SWEAgentConfig,
    SWETicket,
    TicketSeverity,
    TicketStatus,
    TicketType,
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

# ---------------------------------------------------------------------------
# HITL detection patterns — (regex/keyword, human-readable reason)
# Ordered from highest to lowest urgency.
# ---------------------------------------------------------------------------
_HITL_PATTERNS: List[Tuple[str, str]] = [
    # Security / wrong account
    (r"sec p0", "SEC P0 security issue — immediate human review required"),
    (r"morpheus.*account|wrong.*account|sent from.*account", "Applications sent from incorrect account — human must audit and correct"),
    (r"hardcoded.*credential|credential.*hardcoded|hardcoded.*password", "Hardcoded credential found — human must rotate secret and audit exposure"),
    (r"chrome profile.*credent|credent.*chrome profile", "Credentials in Chrome profile — human must clear and re-authenticate"),
    # External account / login
    (r"sign.?in required|login required|must.*log.?in|ats.*sign.?in", "External ATS sign-in required — agent cannot authenticate to third-party accounts"),
    (r"captcha|recaptcha", "CAPTCHA encountered — requires human intervention or anti-CAPTCHA service configuration"),
    (r"otp.*fail|magic link.*fail|email.*verif.*fail", "OTP/magic link authentication failing — human must verify email access and environment"),
    # Credential/key rotation
    (r"api key.*401|401.*api key|all.*keys.*401|proxy.*401", "API keys returning 401 — human must rotate or re-issue credentials"),
    (r"password.*reset|forgot.*password|stale.*password", "External account password management — agent cannot reset passwords on third-party platforms"),
    # Environment configuration
    (r"gog_keyring_password.*not|keyring.*not.*load|env.*not.*set.*password", "Required environment variable not configured — human must set credentials on target machine"),
    (r"missing.*env.*var|env var.*not.*configured|not.*set.*env", "Missing environment variable — human must configure deployment environment"),
    # Regulatory/compliance decisions
    (r"eeo|demographic.*field|equal.*opportun", "EEO/demographic fields — requires human compliance decision, cannot be automated"),
    (r"behavioral.*question.*skip|skip.*behavioral.*question", "Behavioral questions skipped without HITL — human must define policy for these question types"),
    # Repeated regression / chronic failure
    (r"\[regression\].*\[regression\]", "Double-regression nesting — chronic failure loop, requires human architectural review"),
    # External platform account creation
    (r"account creation.*block|external.*platform.*account|create.*account.*ats", "External platform requires new account — agent cannot self-register on third-party services"),
    # Infrastructure / deploy (requires human approval)
    (r"proxmox|vm setup|new vm|deploy.*container.*vm", "Infrastructure deployment — requires human approval and access to target VM/hypervisor"),
]

# Pre-compile for performance
_HITL_COMPILED = [(re.compile(pat, re.IGNORECASE), reason) for pat, reason in _HITL_PATTERNS]

# Threshold: if a ticket already has this many dev failures, escalate to human
_MAX_AUTO_ATTEMPTS = 3

# Type classification patterns — checked in order (first match wins)
_TYPE_CLASSIFICATION: List[Tuple[TicketType, List[str]]] = [
    (TicketType.SECURITY, ["sec p0", "security", "vulnerability", "cve", "credential leak", "hardcoded password", "injection"]),
    (TicketType.REGRESSION, ["regression", "broke in", "used to work", "worked before", "broke after"]),
    (TicketType.INFRASTRUCTURE, ["deploy", "vm setup", "docker", "container", "proxmox", "kubernetes", "ci/cd", "pipeline setup", "server", "new vm", "infrastructure"]),
    (TicketType.DOCUMENTATION, ["docs", "documentation", "readme", "wiki", "comment", "docstring", "example", "tutorial"]),
    (TicketType.QUESTION, ["how do", "how to", "what is", "why does", "question:", "help:", "explain"]),
    (TicketType.FEATURE, ["feat:", "feature:", "add ", "implement ", "create ", "new ", "build ", "develop ", "support for", "ability to", "would like", "please add", "request:"]),
    (TicketType.ENHANCEMENT, ["improve", "enhance", "optimize", "refactor", "upgrade", "update", "better", "faster", "cleaner"]),
    (TicketType.BUG, ["bug", "error", "exception", "traceback", "fails", "broken", "crash", "fix:", "issue:", "problem"]),
]


class TriageAgent:
    """Classifies and routes tickets to the right SWE sub-team.

    Triage rules (in order):
    1. HITL detection → flag and assign to human:{SWE_HITL_ASSIGNEE} (skip automation)
    2. Already-flagged tickets (needs_hitl in metadata) → reassert assignment
    3. CRITICAL tickets → first available investigator (any speciality)
    4. Module-specific tickets → specialised investigator if available
    5. Fallback → first enabled investigator
    """

    AGENT_NAME = "swe_triage"
    def __init__(self, config: SWETeamConfig) -> None:
        self._config = config
        self._investigators = config.get_agents_by_role(AgentRole.INVESTIGATOR)
        _assignee = os.environ.get("SWE_HITL_ASSIGNEE", "ArtemisAI")
        self.HUMAN_ASSIGNEE = f"human:{_assignee}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def triage(self, ticket: SWETicket) -> SWETicket:
        """Classify *ticket* and assign it to an investigator or human.

        Mutates the ticket in-place (status → TRIAGED, assigned_to set)
        and returns it for chaining.
        """
        # Classify issue type first
        ticket.ticket_type = self._classify_type(ticket)
        logger.info("Classified ticket %s as type=%s", ticket.ticket_id, ticket.ticket_type.value)

        # Check for HITL conditions first — these bypass the investigator queue
        hitl, reason = self._detect_hitl(ticket)
        if hitl:
            ticket.metadata["needs_hitl"] = True
            ticket.metadata["hitl_reason"] = reason
            ticket.assigned_to = self.HUMAN_ASSIGNEE
            ticket.transition(TicketStatus.TRIAGED)
            logger.warning(
                "HITL escalation: ticket %s (%s) → %s | %s",
                ticket.ticket_id,
                ticket.severity.value,
                self.HUMAN_ASSIGNEE,
                reason,
            )
            return ticket

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
    # HITL detection
    # ------------------------------------------------------------------

    def _detect_hitl(self, ticket: SWETicket) -> Tuple[bool, str]:
        """Return (True, reason) if this ticket requires human intervention.

        Checks:
        1. Already flagged in metadata (from a previous cycle or dev escalation)
        2. Chronic dev failure (>= _MAX_AUTO_ATTEMPTS with no resolution)
        3. Keyword/pattern matching against title + description + error log
        """
        # Already flagged
        if ticket.metadata.get("needs_hitl"):
            return True, ticket.metadata.get("hitl_reason", "Previously escalated to HITL")

        # Chronic failure — too many failed dev attempts
        attempts = ticket.metadata.get("attempts", [])
        if len(attempts) >= _MAX_AUTO_ATTEMPTS:
            failed = [a for a in attempts if not a.get("success")]
            if len(failed) >= _MAX_AUTO_ATTEMPTS:
                return True, (
                    f"Chronic failure: {len(failed)} consecutive dev attempts failed. "
                    "Requires human review of root cause."
                )

        # Pattern matching across ticket text
        text = " ".join(filter(None, [
            ticket.title,
            ticket.description,
            ticket.error_log,
            str(ticket.metadata),
        ])).lower()

        for pattern, reason in _HITL_COMPILED:
            if pattern.search(text):
                return True, reason

        return False, ""

    # ------------------------------------------------------------------
    # Type classification
    # ------------------------------------------------------------------

    def _classify_type(self, ticket: SWETicket) -> TicketType:
        """Classify the ticket type from its content."""
        # If there's an error log, it's a bug/regression
        if ticket.error_log and len(ticket.error_log.strip()) > 10:
            if any(w in (ticket.error_log or "").lower() for w in ["regression", "broke", "used to"]):
                return TicketType.REGRESSION
            return TicketType.BUG

        # Pattern match on title + description
        text = f"{ticket.title or ''} {ticket.description or ''} {' '.join(ticket.labels or [])}".lower()
        for ticket_type, patterns in _TYPE_CLASSIFICATION:
            if any(p in text for p in patterns):
                return ticket_type

        # If no error log and no clear type, classify as FEATURE (assume new work)
        return TicketType.FEATURE

    # ------------------------------------------------------------------
    # Investigator routing
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
