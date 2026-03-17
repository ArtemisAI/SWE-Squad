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

from src.swe_team.github_integration import comment_on_issue
from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.notifier import notify_investigation_summary

logger = logging.getLogger(__name__)

_DEFAULT_PROGRAM_PATH = Path("config/swe_team/programs/investigate.md")
_ORCHESTRATE_PROGRAM_PATH = Path("config/swe_team/programs/orchestrate.md")
_DEFAULT_CLAUDE_PATH = "/usr/bin/claude"
_DEFAULT_TIMEOUT = 120
_OPUS_TIMEOUT = 600  # Opus gets 10 min — it orchestrates multiple sub-agents
_DEFAULT_MAX_PER_CYCLE = 5


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
    ) -> None:
        self._program_path = Path(program_path)
        self._claude_path = claude_path
        self._timeout = timeout_seconds
        self._max_per_cycle = max_per_cycle
        self._program_cache: Optional[str] = None

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
        module = ticket.source_module or "unknown"
        try:
            return template.format(error_log=error_log, source_module=module)
        except (KeyError, ValueError) as exc:
            logger.warning("Invalid investigate.md template: %s", exc)
            return None

    def _build_orchestration_prompt(self, ticket: SWETicket) -> Optional[str]:
        """Build the full orchestration prompt for Opus."""
        template = self._load_program(_ORCHESTRATE_PROGRAM_PATH)
        if not template:
            # Fall back to investigation-only program
            return self._build_prompt(ticket)
        try:
            return template.format(
                title=ticket.title,
                severity=ticket.severity.value,
                source_module=ticket.source_module or "unknown",
                description=ticket.description or "",
                investigation_report=ticket.investigation_report or "No prior investigation.",
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Invalid orchestrate.md template: %s", exc)
            return self._build_prompt(ticket)

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
        """Opus for CRITICAL bugs, sonnet for everything else."""
        if ticket.severity == TicketSeverity.CRITICAL:
            return "opus"
        # Escalate to opus if a previous investigation failed
        inv = ticket.metadata.get("investigation", {})
        if inv.get("status") == "failed":
            return "opus"
        return "sonnet"

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
