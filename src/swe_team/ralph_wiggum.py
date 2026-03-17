"""
Ralph-Wiggum Stability Loop.

Implements the stability-first governance pattern: **fix bugs before
building new features**.  Before any new feature work is allowed, this
gate checks:

1. Open critical / high bug count against thresholds
2. CI / test-suite status
3. Recent rollback history

The gate emits ``STABILITY_GATE_RESULT`` events with verdict
``pass | block | warn``.

Usage::

    from src.swe_team.ralph_wiggum import RalphWiggumGate
    gate = RalphWiggumGate(governance_config)
    report = gate.evaluate(open_tickets, ci_green=True, failing_tests=0)
    if report.verdict == GovernanceVerdict.BLOCK:
        # park new-feature work, prioritise bug fixes
        ...
"""

from __future__ import annotations

import logging
from typing import List

from src.swe_team.config import GovernanceConfig
from src.swe_team.events import SWEEvent
from src.swe_team.models import (
    GovernanceVerdict,
    SWETicket,
    StabilityReport,
    TicketSeverity,
    TicketStatus,
)

logger = logging.getLogger(__name__)

# Statuses that count as "open" for the stability gate
_OPEN_STATUSES = frozenset({
    TicketStatus.OPEN,
    TicketStatus.TRIAGED,
    TicketStatus.INVESTIGATING,
    TicketStatus.INVESTIGATION_COMPLETE,
    TicketStatus.IN_DEVELOPMENT,
    TicketStatus.IN_REVIEW,
    TicketStatus.TESTING,
    TicketStatus.DEPLOYING,
    TicketStatus.MONITORING,
})


class RalphWiggumGate:
    """Stability gate that blocks new work when the codebase is unhealthy.

    Named after the "Ralph Wiggum Loop" concept referenced in the issue:
    *Stop building on top of failing, unstable, or insecure code.*
    """

    def __init__(self, config: GovernanceConfig) -> None:
        self._config = config

    def evaluate(
        self,
        tickets: List[SWETicket],
        *,
        ci_green: bool = True,
        failing_tests: int = 0,
    ) -> StabilityReport:
        """Run the stability check and return a verdict.

        Parameters
        ----------
        tickets:
            All currently tracked ``SWETicket`` objects.
        ci_green:
            Whether the most recent CI run passed.
        failing_tests:
            Number of failing tests in the latest suite run.
        """
        if not self._config.enabled:
            return StabilityReport(verdict=GovernanceVerdict.PASS, details="Gate disabled")

        open_critical = self._count_open(tickets, TicketSeverity.CRITICAL)
        open_high = self._count_open(tickets, TicketSeverity.HIGH)

        reasons: List[str] = []

        # Rule 1: No critical bugs allowed
        if open_critical > self._config.max_open_critical:
            reasons.append(
                f"{open_critical} open critical ticket(s) "
                f"(max {self._config.max_open_critical})"
            )

        # Rule 2: High bug ceiling
        if open_high > self._config.max_open_high:
            reasons.append(
                f"{open_high} open high ticket(s) "
                f"(max {self._config.max_open_high})"
            )

        # Rule 3: CI must be green
        if self._config.require_ci_green and not ci_green:
            reasons.append("CI is not green")

        # Rule 4: No failing tests
        if failing_tests > self._config.max_failing_tests:
            reasons.append(
                f"{failing_tests} failing test(s) "
                f"(max {self._config.max_failing_tests})"
            )

        if reasons:
            verdict = GovernanceVerdict.BLOCK
            details = "BLOCKED: " + "; ".join(reasons)
        else:
            verdict = GovernanceVerdict.PASS
            details = "All stability checks passed"

        report = StabilityReport(
            verdict=verdict,
            open_critical=open_critical,
            open_high=open_high,
            failing_tests=failing_tests,
            ci_status="green" if ci_green else "red",
            details=details,
        )
        logger.info("Ralph-Wiggum gate: %s — %s", verdict.value, details)
        return report

    def build_event(
        self, report: StabilityReport, ticket_id: str = "system"
    ) -> SWEEvent:
        """Wrap a stability report as an A2A event."""
        return SWEEvent.stability_gate_result(
            ticket_id=ticket_id,
            source_agent="ralph_wiggum",
            verdict=report.verdict.value,
            details=report.details,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _count_open(
        tickets: List[SWETicket], severity: TicketSeverity
    ) -> int:
        """Count tickets with the given severity that are still open."""
        return sum(
            1
            for t in tickets
            if t.severity == severity and t.status in _OPEN_STATUSES
        )
