"""
Creative agent for the Autonomous SWE Team.

Analyzes resolved ticket patterns and proposes low-severity improvements.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections import Counter
from typing import Iterable, List, Optional

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus

logger = logging.getLogger(__name__)

_REPO = os.environ.get("SWE_GITHUB_REPO", "")
_TITLE_PREFIX = "[SWE-CREATIVE]"


class CreativeAgent:
    """Generate proactive improvement proposals."""

    AGENT_NAME = "swe_creative"

    def propose(self, store, limit: int = 3) -> List[SWETicket]:
        """Generate creative proposals from resolved/closed tickets."""
        closed = [
            t for t in store.list_all()
            if t.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED)
        ]
        if not closed:
            return []

        module_counts = Counter(t.source_module or "unknown" for t in closed)
        existing_titles = {t.title for t in store.list_all()}
        proposals: List[SWETicket] = []

        for module, count in module_counts.most_common(limit):
            title = f"{_TITLE_PREFIX} Prevent recurring {module} issues"
            if title in existing_titles:
                continue
            top_examples = [
                t.title for t in closed if (t.source_module or "unknown") == module
            ][:3]
            description = (
                f"Detected {count} resolved tickets in module '{module}'.\n\n"
                "Top recurring issues:\n- "
                + "\n- ".join(top_examples)
                + "\n\n"
                "Proposal: add preventive checks, alerts, or automated guards "
                "to reduce repeat incidents."
            )
            ticket = SWETicket(
                title=title,
                description=description,
                severity=TicketSeverity.LOW,
                source_module=module,
                labels=["swe-creative", "proposal"],
            )
            ticket.metadata["creative"] = {"module": module, "count": count}
            proposals.append(ticket)

        return proposals

    def publish_proposals(self, proposals: Iterable[SWETicket]) -> List[int]:
        """Create GitHub issues for creative proposals."""
        issue_numbers: List[int] = []
        for ticket in proposals:
            issue_num = self._create_issue(ticket)
            if issue_num:
                ticket.metadata["github_issue"] = issue_num
                issue_numbers.append(issue_num)
        return issue_numbers

    def _create_issue(self, ticket: SWETicket) -> Optional[int]:
        issue_title = ticket.title[:80]
        body_lines = [
            "## SWE Creative Proposal",
            "",
            f"**Ticket ID:** `{ticket.ticket_id}`",
            f"**Severity:** {ticket.severity.value.upper()}",
            f"**Module:** {ticket.source_module or 'unknown'}",
            "",
            ticket.description[:800],
            "",
            "_Requires human approval before implementation._",
        ]
        labels = "swe-team,creative,proposal"

        try:
            result = subprocess.run(
                [
                    "gh", "issue", "create",
                    "--repo", _REPO,
                    "--title", issue_title,
                    "--body", "\n".join(body_lines),
                    "--label", labels,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning(
                    "Creative issue creation failed (rc=%d): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return None
            output = result.stdout.strip()
            if "/issues/" in output:
                return int(output.rsplit("/issues/", 1)[1])
            logger.warning("Could not parse creative issue number: %s", output)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to create creative issue: %s", exc)
            return None
