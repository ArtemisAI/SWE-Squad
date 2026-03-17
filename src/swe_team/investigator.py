"""
Investigation agent for the Autonomous SWE Team.

Runs a diagnostic prompt via Claude Code CLI and attaches the resulting
report to the ticket for downstream development automation.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from src.swe_team.embeddings import embed_ticket
from src.swe_team.github_integration import comment_on_issue
from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.notifier import notify_investigation_summary
from src.swe_team.supabase_store import SupabaseTicketStore

logger = logging.getLogger(__name__)

_DEFAULT_PROGRAM_PATH = Path("config/swe_team/programs/investigate.md")
_ORCHESTRATE_PROGRAM_PATH = Path("config/swe_team/programs/orchestrate.md")
_DEFAULT_CLAUDE_PATH = "/usr/bin/claude"
_DEFAULT_TIMEOUT = 120
_OPUS_TIMEOUT = 600  # Opus gets 10 min — it orchestrates multiple sub-agents
_DEFAULT_MAX_PER_CYCLE = 5
_SEMANTIC_INVESTIGATION_CHARS = 400
_SEMANTIC_FIX_CHARS = 200


class InvestigatorAgent:
    """Investigate triaged tickets using Claude Code CLI."""

    AGENT_NAME = "swe_investigator"

    def __init__(
        self,
        *,
        program_path: Path | str = _DEFAULT_PROGRAM_PATH,
        claude_path: str = _DEFAULT_CLAUDE_PATH,
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        max_per_cycle: int = _DEFAULT_MAX_PER_CYCLE,
        store: Optional[object] = None,
        memory_top_k: int = 5,
        memory_similarity_floor: float = 0.75,
        model_config: Optional[object] = None,
    ) -> None:
        self._program_path = Path(program_path)
        self._claude_path = claude_path
        self._timeout = timeout_seconds
        self._max_per_cycle = max_per_cycle
        self._store = store
        self._memory_top_k = memory_top_k
        self._memory_similarity_floor = memory_similarity_floor
        self._program_cache: Optional[str] = None
        self._model_config = model_config

    def investigate_batch(
        self, tickets: Iterable[SWETicket], *, limit: Optional[int] = None
    ) -> List[SWETicket]:
        """Investigate eligible tickets, returning those updated."""
        updated: List[SWETicket] = []
        max_items = limit if limit is not None else self._max_per_cycle
        for ticket in tickets:
            if len(updated) >= max_items:
                break
            if not self._eligible(ticket):
                continue
            try:
                if self.investigate(ticket):
                    updated.append(ticket)
            except Exception:
                logger.exception("Investigation failed for ticket %s", ticket.ticket_id)
        return updated

    def investigate(self, ticket: SWETicket) -> bool:
        """Run an investigation for a single ticket.

        For CRITICAL tickets or escalations, Opus is used with the full
        orchestration program — it handles investigation, planning, fixing,
        verification, and documentation in one session using sub-agents.
        """
        if not self._eligible(ticket):
            return False

        started_at = datetime.now(timezone.utc).isoformat()
        ticket.transition(TicketStatus.INVESTIGATING)
        ticket.metadata["last_heartbeat"] = started_at

        model = self._select_model(ticket)

        # Opus gets the orchestration program (full lifecycle with sub-agents)
        # Sonnet gets the investigation-only program
        if model == "opus":
            prompt = self._build_orchestration_prompt(ticket)
            timeout = _OPUS_TIMEOUT
        else:
            prompt = self._build_prompt(ticket)
            timeout = self._timeout

        if prompt is None:
            self._record_failure(ticket, started_at, "Prompt template missing")
            return False

        logger.info("Investigating ticket %s via Claude CLI (model=%s)", ticket.ticket_id, model)
        start = time.monotonic()
        try:
            stdout, stderr = self._run_claude(prompt, model=model, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError, RuntimeError) as exc:
            self._record_failure(ticket, started_at, str(exc))
            return False

        duration_s = time.monotonic() - start
        report = stdout.strip()
        if not report:
            self._record_failure(ticket, started_at, "Empty investigation report")
            return False

        cost = _parse_cost(stderr) or _parse_cost(stdout)
        ticket.investigation_report = report
        ticket.transition(TicketStatus.INVESTIGATION_COMPLETE)
        ticket.metadata["investigation"] = {
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "duration_s": round(duration_s, 2),
            "cost_usd": cost,
            "status": "complete",
        }

        issue_number = ticket.metadata.get("github_issue")
        if issue_number:
            self._comment_on_issue(issue_number, ticket)

        notify_investigation_summary(ticket)
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _eligible(self, ticket: SWETicket) -> bool:
        if ticket.severity not in (TicketSeverity.CRITICAL, TicketSeverity.HIGH):
            return False
        if ticket.investigation_report:
            return False
        if ticket.status not in (
            TicketStatus.TRIAGED,
            TicketStatus.INVESTIGATING,
        ):
            return False
        return True

    def _build_prompt(self, ticket: SWETicket) -> Optional[str]:
        template = self._load_program(self._program_path)
        if not template:
            return None
        error_log = ticket.error_log or "No error log provided."
        similar_context = self._semantic_memory_context(ticket)
        if similar_context:
            error_log = f"{error_log}\n\n{similar_context}"
        # Enhance prompt for regression tickets
        if ticket.metadata.get("is_regression"):
            regression_ctx = self._build_regression_context(ticket)
            error_log = f"{error_log}\n\n{regression_ctx}"
        module = ticket.source_module or "unknown"
        try:
            return template.format(error_log=error_log, source_module=module)
        except (KeyError, ValueError) as exc:
            logger.warning("Invalid investigate.md template: %s", exc)
            return None

    @staticmethod
    def _build_regression_context(ticket: SWETicket) -> str:
        """Build additional context for a regression ticket."""
        parent_id = ticket.metadata.get("regression_of", "unknown")
        regressions = ticket.metadata.get("fix_confidence", {}).get("regressions", 0)
        attempts = ticket.metadata.get("fix_confidence", {}).get("attempts", 0)
        lines = [
            "## REGRESSION ALERT",
            "",
            f"This is a REGRESSION of ticket {parent_id}.",
            f"Fix attempts so far: {attempts}",
            f"Times regressed: {regressions}",
            "",
            "The previous fix did not hold. You MUST:",
            "1. Identify why the previous fix failed",
            "2. Check if the fix was reverted or if a new code path reintroduced the bug",
            "3. Propose a more robust fix that addresses the root cause",
        ]
        # Include parent investigation/fix if available in the description
        return "\n".join(lines)

    def _build_orchestration_prompt(self, ticket: SWETicket) -> Optional[str]:
        """Build the full orchestration prompt for Opus."""
        template = self._load_program(_ORCHESTRATE_PROGRAM_PATH)
        if not template:
            # Fall back to investigation-only program
            return self._build_prompt(ticket)
        description = ticket.description or ""
        similar_context = self._semantic_memory_context(ticket)
        if similar_context:
            description = f"{description}\n\n{similar_context}"
        try:
            return template.format(
                title=ticket.title,
                severity=ticket.severity.value,
                source_module=ticket.source_module or "unknown",
                description=description,
                investigation_report=ticket.investigation_report or "No prior investigation.",
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Invalid orchestrate.md template: %s", exc)
            return self._build_prompt(ticket)

    def _semantic_memory_context(self, ticket: SWETicket) -> str:
        if not isinstance(self._store, SupabaseTicketStore):
            return ""
        try:
            emb = embed_ticket(ticket)
            if not emb:
                return ""
            hits = self._store.find_similar(
                emb,
                top_k=self._memory_top_k,
                similarity_floor=self._memory_similarity_floor,
            )
            if not hits:
                return ""
            lines = ["## Semantic Memory — Similar Resolved Tickets\n"]
            for hit in hits:
                hit_ticket_id = hit.get("ticket_id")
                if hit_ticket_id:
                    try:
                        self._store.record_memory_hit(str(hit_ticket_id))
                    except Exception:
                        logger.warning(
                            "Failed to record memory hit for ticket %s",
                            hit_ticket_id,
                            exc_info=True,
                        )
                lines.append(
                    f"### [{hit.get('ticket_id', 'unknown')}] {hit.get('title', 'Untitled')} "
                    f"(similarity={float(hit.get('similarity', 0.0)):.2f})\n"
                    f"**Module**: {hit.get('source_module') or 'unknown'}\n"
                    f"**Investigation**: {(hit.get('investigation_report') or '')[:_SEMANTIC_INVESTIGATION_CHARS]}\n"
                    f"**Fix applied**: {(hit.get('proposed_fix') or 'N/A')[:_SEMANTIC_FIX_CHARS]}\n"
                )
            return "\n".join(lines)
        except Exception as exc:
            logger.warning("Semantic memory lookup failed (non-fatal): %s", exc)
            return ""

    def _load_program(self, path: Path) -> Optional[str]:
        if path == self._program_path and self._program_cache is not None:
            return self._program_cache
        if not path.is_file():
            logger.warning("Program not found: %s", path)
            return None
        text = path.read_text(encoding="utf-8")
        if path == self._program_path:
            self._program_cache = text
        return text

    def _select_model(self, ticket: SWETicket) -> str:
        """Select model from config tiers: t1_heavy for CRITICAL/regressions, t2_standard otherwise."""
        heavy = self._model_config.t1_heavy if self._model_config else "opus"
        standard = self._model_config.t2_standard if self._model_config else "sonnet"
        if ticket.severity == TicketSeverity.CRITICAL:
            return heavy
        # Regressions always route to heavy tier
        if ticket.metadata.get("is_regression"):
            return heavy
        # Escalate to heavy tier if a previous investigation failed
        inv = ticket.metadata.get("investigation", {})
        if inv.get("status") == "failed":
            return heavy
        return standard

    def _run_claude(
        self, prompt: str, *, model: str = "sonnet", timeout: Optional[int] = None
    ) -> tuple[str, str]:
        effective_timeout = timeout or self._timeout
        result = subprocess.run(
            [
                self._claude_path,
                "--print",
                "--dangerously-skip-permissions",
                "--model", model,
            ],
            input=prompt,
            text=True,
            capture_output=True,
            timeout=effective_timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Claude CLI failed")
        return result.stdout, result.stderr

    def _comment_on_issue(self, issue_number: int, ticket: SWETicket) -> None:
        report = ticket.investigation_report or ""
        body = "\n".join(
            [
                "## Investigation report",
                "",
                f"**Ticket ID:** `{ticket.ticket_id}`",
                f"**Module:** {ticket.source_module or 'unknown'}",
                "",
                report,
            ]
        )
        comment_on_issue(issue_number, body)

    def _record_failure(
        self, ticket: SWETicket, started_at: str, error: str
    ) -> None:
        ticket.transition(TicketStatus.TRIAGED)
        ticket.metadata["investigation"] = {
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "duration_s": 0.0,
            "cost_usd": None,
            "status": "failed",
            "error": error,
        }
        logger.warning(
            "Investigation failed for ticket %s: %s", ticket.ticket_id, error
        )


def _parse_cost(text: str) -> Optional[float]:
    """Extract a $ cost from Claude CLI output if present."""
    for line in text.splitlines():
        if "cost" not in line.lower():
            continue
        match = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", line)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                return None
    return None
