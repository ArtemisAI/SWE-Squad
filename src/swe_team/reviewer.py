"""ReviewerAgent — enforces resolution_audit() gate on IN_REVIEW → RESOLVED.

For each IN_REVIEW ticket:
1. Run resolution_audit(); if it fails, send the ticket back to IN_DEVELOPMENT.
2. Call CodeReviewerAgent.review() which handles push → PR → diff review → merge → close.
3. Approved tickets are transitioned to RESOLVED by CodeReviewerAgent.
4. Rejected tickets are bounced to IN_DEVELOPMENT (or HITL after max_rejections).

Returns (resolved_list, rejected_list, hitl_list).
"""

from __future__ import annotations

import logging
from typing import List, Tuple

from src.swe_team.models import SWETicket, TicketStatus

logger = logging.getLogger("swe_team.reviewer")


class ReviewerAgent:
    """Promote IN_REVIEW tickets to RESOLVED (or back to IN_DEVELOPMENT)."""

    def __init__(self, model: str = "haiku", timeout: int = 30, repo_root: str = "") -> None:
        self.model = model
        self.timeout = timeout
        self.repo_root = repo_root

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def review_batch(
        self,
        tickets: List[SWETicket],
        store,
        dry_run: bool = False,
    ) -> Tuple[List[SWETicket], List[SWETicket], List[SWETicket]]:
        """Review a batch of IN_REVIEW tickets.

        Parameters
        ----------
        tickets:
            Tickets in IN_REVIEW status to process.
        store:
            Ticket store with a ``save`` / ``add`` method.
        dry_run:
            If True, log decisions but do not mutate or persist anything.

        Returns
        -------
        (resolved_list, rejected_list, hitl_list)
        """
        from src.swe_team.code_reviewer import CodeReviewerAgent

        resolved: List[SWETicket] = []
        rejected: List[SWETicket] = []
        hitl: List[SWETicket] = []

        code_reviewer = CodeReviewerAgent(model=self.model)

        for ticket in tickets:
            logger.info("Reviewer: evaluating ticket %s (%s)", ticket.ticket_id, ticket.severity.value)

            # ── Step 1: resolution audit ──────────────────────────────
            audit_ok, audit_reason = ticket.resolution_audit()
            if not audit_ok:
                logger.warning(
                    "Reviewer: audit FAILED for %s — sending back to IN_DEVELOPMENT. Reason: %s",
                    ticket.ticket_id,
                    audit_reason,
                )
                if not dry_run:
                    ticket.metadata["review_feedback"] = f"Resolution audit failed: {audit_reason}"
                    ticket.transition(TicketStatus.IN_DEVELOPMENT)
                    _store_save(store, ticket)
                rejected.append(ticket)
                continue

            logger.info("Reviewer: audit passed for %s (%s)", ticket.ticket_id, audit_reason)

            # ── Step 2: CodeReviewerAgent — push, PR, diff review, merge ─
            if dry_run:
                # In dry_run mode just log and assume approved
                logger.info(
                    "Reviewer: dry_run — skipping CodeReviewerAgent for %s",
                    ticket.ticket_id,
                )
                resolved.append(ticket)
                continue

            approved, feedback = code_reviewer.review(
                ticket, store, repo_root=self.repo_root
            )

            if approved:
                logger.info(
                    "Reviewer: CodeReviewer APPROVED ticket %s. Feedback: %s",
                    ticket.ticket_id,
                    feedback[:120],
                )
                resolved.append(ticket)
            else:
                # Check if escalated to HITL
                if ticket.metadata.get("needs_hitl"):
                    logger.warning(
                        "Reviewer: ticket %s escalated to HITL. Feedback: %s",
                        ticket.ticket_id,
                        feedback[:120],
                    )
                    hitl.append(ticket)
                else:
                    logger.warning(
                        "Reviewer: CodeReviewer REJECTED ticket %s. Feedback: %s",
                        ticket.ticket_id,
                        feedback[:120],
                    )
                    rejected.append(ticket)

        logger.info(
            "Reviewer batch complete: resolved=%d rejected=%d hitl=%d",
            len(resolved),
            len(rejected),
            len(hitl),
        )
        return resolved, rejected, hitl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store_save(store, ticket: SWETicket) -> None:
    """Persist ticket using whichever save method the store exposes."""
    if hasattr(store, "save"):
        store.save(ticket)
    elif hasattr(store, "add"):
        store.add(ticket)
    else:
        logger.error("Store has no save/add method — ticket %s not persisted", ticket.ticket_id)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
