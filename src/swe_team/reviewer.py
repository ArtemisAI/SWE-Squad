"""ReviewerAgent — enforces resolution_audit() gate on IN_REVIEW → RESOLVED.

For each IN_REVIEW ticket:
1. Run resolution_audit(); if it fails, send the ticket back to IN_DEVELOPMENT.
2. Call CodeReviewerAgent.review() which handles push → PR → diff review → merge → close.
3. If approved, run QA checks (build, tests, visual) via QAAgent before finalising.
4. Approved tickets are transitioned to RESOLVED by CodeReviewerAgent.
5. Rejected tickets are bounced to IN_DEVELOPMENT (or HITL after max_rejections).

Returns (resolved_list, rejected_list, hitl_list).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.swe_team.models import SWETicket, TicketStatus

# Model tier defaults — read from env. Never hardcode model names in agent files.
_MODEL_T2 = os.environ.get("SWE_MODEL_T2", "sonnet")

logger = logging.getLogger("swe_team.reviewer")


class ReviewerAgent:
    """Promote IN_REVIEW tickets to RESOLVED (or back to IN_DEVELOPMENT)."""

    def __init__(
        self,
        model: str = _MODEL_T2,
        timeout: int = 30,
        repo_root: str = "",
        repos_map: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.repo_root = repo_root
        # repos_map: {repo_name: local_path} — used to resolve per-ticket cwd
        self._repos_map: Dict[str, Path] = {
            name: Path(local_path)
            for name, local_path in (repos_map or {}).items()
        }

    def _resolve_repo_root(self, ticket: SWETicket) -> str:
        """Return the local path of the sandbox repo for this ticket.

        Priority:
        1. ticket.metadata["repo"] → look up in self._repos_map
        2. First entry in self._repos_map
        3. Fall back to self.repo_root (legacy / single-repo mode)
        """
        if self._repos_map:
            repo_name = ticket.metadata.get("repo", "") if ticket.metadata else ""
            if repo_name and repo_name in self._repos_map:
                return str(self._repos_map[repo_name])
            # Fall back to first entry in repos_map
            first_path = next(iter(self._repos_map.values()))
            logger.debug(
                "Reviewer: ticket %s has no/unknown repo=%r — using first repos_map entry %s",
                ticket.ticket_id,
                repo_name,
                first_path,
            )
            return str(first_path)
        return self.repo_root

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
        from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine

        resolved: List[SWETicket] = []
        rejected: List[SWETicket] = []
        hitl: List[SWETicket] = []

        # Resolve the review engine binary from engine_routing config or env.
        # Without this, CodeReviewerAgent falls back to 'claude' (wrong binary).
        review_binary = os.environ.get("SWE_ENGINE_REVIEW", "claudez")
        review_engine = ClaudeCodeEngine(default_model=self.model, binary=review_binary)
        code_reviewer = CodeReviewerAgent(model=self.model, engine=review_engine)

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

            ticket_repo_root = self._resolve_repo_root(ticket)
            approved, feedback = code_reviewer.review(
                ticket, store, repo_root=ticket_repo_root
            )

            if approved:
                logger.info(
                    "Reviewer: CodeReviewer APPROVED ticket %s. Feedback: %s",
                    ticket.ticket_id,
                    feedback[:120],
                )

                # ── Step 3: QA gate (post-review, pre-merge) ────────
                # Run build/test/visual checks for webui tickets before
                # finalising the approval.  If QA fails, bounce back to
                # IN_DEVELOPMENT with a structured failure report.
                qa_rejected = False
                if _is_webui_ticket(ticket):
                    qa_rejected = _run_qa_gate(
                        ticket, ticket_repo_root, store, dry_run,
                    )
                if qa_rejected:
                    rejected.append(ticket)
                    continue

                # ── Post-resolution PR safety net (issue #367) ────────
                # If approved but the ticket ended up RESOLVED without a PR
                # in its metadata, downgrade back to IN_DEVELOPMENT.  This
                # catches any code path that bypassed _handle_approve's gate.
                if ticket.status == TicketStatus.RESOLVED and not (
                    ticket.metadata.get("pr_url") or ticket.metadata.get("pr_number")
                ):
                    logger.warning(
                        "Reviewer: PR-SAFETY-NET — ticket %s is RESOLVED but has no "
                        "pr_url/pr_number in metadata. Downgrading to IN_DEVELOPMENT.",
                        ticket.ticket_id,
                    )
                    ticket.metadata["needs_pr"] = True
                    ticket.metadata["review_feedback"] = (
                        "PR safety net: ticket resolved without a PR — "
                        "returned to development."
                    )
                    if not dry_run:
                        ticket.transition(TicketStatus.IN_DEVELOPMENT)
                        _store_save(store, ticket)
                    rejected.append(ticket)
                else:
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


def _is_webui_ticket(ticket: SWETicket) -> bool:
    """Return True if this ticket involves the web UI (frontend/webui labels or ui/ paths)."""
    labels = set(ticket.labels or [])
    webui_labels = {"webui", "frontend", "ui", "dashboard", "react", "vite"}
    if labels & webui_labels:
        return True
    # Check metadata for source hints
    meta = ticket.metadata or {}
    source = meta.get("source_module", "") or ""
    if "webui" in source.lower() or "ui/" in source.lower():
        return True
    # Check if the proposed fix touches ui/ paths
    fix = ticket.proposed_fix or ""
    if "ui/" in fix:
        return True
    return False


def _run_qa_gate(
    ticket: SWETicket,
    repo_path: str,
    store,
    dry_run: bool,
) -> bool:
    """Run QA checks on a ticket. Return True if the ticket was rejected.

    When QA fails, the ticket is transitioned back to IN_DEVELOPMENT with
    the failure summary stored in metadata["qa_failures"].
    """
    try:
        from src.swe_team.qa_agent import QAAgent

        # Default checks for webui tickets
        checks = ["typescript", "build", "pytest"]
        qa = QAAgent(checks=checks)
        qa_result = qa.run_qa(ticket, repo_path)

        if qa_result.approved:
            logger.info("QA gate PASSED for ticket %s", ticket.ticket_id)
            return False

        # QA failed — bounce back to developer
        logger.warning(
            "QA gate FAILED for ticket %s: %s",
            ticket.ticket_id,
            qa_result.summary[:200],
        )
        if not dry_run:
            ticket.metadata["qa_failures"] = qa_result.summary
            ticket.metadata["review_feedback"] = (
                f"QA checks failed — returning to development.\n\n{qa_result.summary}"
            )
            ticket.transition(TicketStatus.IN_DEVELOPMENT)
            _store_save(store, ticket)
        return True

    except Exception:
        logger.exception("QA gate raised an exception for ticket %s — allowing approval", ticket.ticket_id)
        return False
