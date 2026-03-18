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
from typing import Any, Iterable, List, Optional

from src.swe_team.embeddings import embed_ticket
from src.swe_team.remote_logs import fetch_worker_logs
from src.swe_team.github_integration import comment_on_issue
from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.notifier import notify_investigation_summary
from src.swe_team.rate_limiter import ExponentialBackoff, RateLimitExhausted, RateLimitTracker
from src.swe_team.supabase_store import SupabaseTicketStore

logger = logging.getLogger(__name__)

# Type alias for fallback agent adapters (duck-typed — must have .invoke())
_FallbackAgent = Any

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
        rate_limit_config: Optional[object] = None,
        rate_limit_tracker: Optional[RateLimitTracker] = None,
        fallback_agents: Optional[List[_FallbackAgent]] = None,
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
        self._fallback_agents: List[_FallbackAgent] = fallback_agents or []

        # Rate limit backoff
        rl = rate_limit_config
        self._backoff = ExponentialBackoff(
            max_retries=getattr(rl, "max_retries_on_429", 3) if rl else 3,
            initial_delay=getattr(rl, "initial_backoff_seconds", 30) if rl else 30,
            max_delay=getattr(rl, "max_backoff_seconds", 300) if rl else 300,
            tracker=rate_limit_tracker,
        )

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

        cwd = self._repo_cwd(ticket)
        logger.info(
            "Investigating ticket %s via Claude CLI (model=%s, cwd=%s)",
            ticket.ticket_id, model, cwd or "SWE-Squad",
        )
        start = time.monotonic()
        try:
            stdout, stderr = self._backoff.execute(
                lambda: self._run_claude(prompt, model=model, timeout=timeout, cwd=cwd),
                context=model,
            )
        except RateLimitExhausted as exc:
            # Try fallback agents before giving up
            fallback_result = self._try_fallback_agents(prompt, ticket)
            if fallback_result is not None:
                stdout, stderr = fallback_result, ""
                # Fall through to success handling below
                duration_s = time.monotonic() - start
                report = stdout.strip()
                if report:
                    ticket.investigation_report = report
                    ticket.transition(TicketStatus.INVESTIGATION_COMPLETE)
                    ticket.metadata["investigation"] = {
                        "started_at": started_at,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "duration_s": round(duration_s, 2),
                        "cost_usd": None,
                        "status": "complete",
                        "fallback_agent": ticket.metadata.get("fallback_agent_used", "unknown"),
                    }
                    notify_investigation_summary(ticket)
                    return True

            self._record_failure(ticket, started_at, str(exc))
            ticket.metadata["rate_limited"] = True
            ticket.metadata["rate_limited_at"] = datetime.now(timezone.utc).isoformat()
            self._send_rate_limit_alert(ticket, exc)
            return False
        except subprocess.TimeoutExpired as exc:
            self._record_timeout(ticket, started_at, timeout, model)
            return False
        except (OSError, RuntimeError) as exc:
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
            "model": model,
            "repo_cwd": str(cwd) if cwd else "SWE-Squad",
            "report_chars": len(report),
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

    def _try_fallback_agents(
        self, prompt: str, ticket: SWETicket
    ) -> Optional[str]:
        """Attempt to use fallback agents when the primary agent is rate-limited.

        Iterates through configured fallback agents and tries each one.
        Returns the response text on success, or None if all fail.
        """
        if not self._fallback_agents:
            return None

        for agent in self._fallback_agents:
            agent_name = getattr(agent, "_name", getattr(agent, "name", "unknown"))
            try:
                # Check availability if the method exists
                if hasattr(agent, "is_available") and not agent.is_available():
                    logger.info("Fallback agent %s not available, skipping", agent_name)
                    continue

                logger.info(
                    "Attempting fallback agent %s for ticket %s",
                    agent_name, ticket.ticket_id,
                )
                result = agent.invoke(prompt, timeout=self._timeout)
                if result and result.strip():
                    ticket.metadata["fallback_agent_used"] = agent_name
                    logger.info(
                        "Fallback agent %s succeeded for ticket %s",
                        agent_name, ticket.ticket_id,
                    )
                    return result
            except Exception:
                logger.warning(
                    "Fallback agent %s failed for ticket %s",
                    agent_name, ticket.ticket_id,
                    exc_info=True,
                )
                continue
        return None

    def _eligible(self, ticket: SWETicket) -> bool:
        if ticket.severity not in (TicketSeverity.CRITICAL, TicketSeverity.HIGH):
            return False
        if ticket.investigation_report:
            return False
        if ticket.status not in (
            TicketStatus.OPEN,
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
        # Pull fresh logs from the source worker if identified
        worker_logs = self._fetch_worker_logs(ticket)
        if worker_logs:
            error_log = f"{error_log}\n\n## Fresh Worker Logs\n{worker_logs}"
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

    # Worker name aliases keyed by common source_module patterns
    _MODULE_WORKER_MAP: dict[str, list[str]] = {
        "browser": ["linkedai-browser-2"],
        "scraper": ["linkedai-bot-2", "linkedai-hp-laptop"],
        "enricher": ["linkedai-hp-laptop"],
        "orchestrator": ["linkedai-hp-laptop"],
        "bot": ["linkedai-bot-2"],
        "linkedin": ["linkedai-browser-2"],
        "google_jobs": ["linkedai-bot-2"],
    }

    def _fetch_worker_logs(self, ticket: SWETicket) -> Optional[str]:
        """Pull fresh logs from workers relevant to this ticket.

        Uses ticket metadata (source_worker) or source_module to identify
        which worker(s) to query. Returns combined log text or None.
        """
        # Explicit worker in ticket metadata takes priority
        worker = ticket.metadata.get("source_worker")
        workers_to_check: list[str] = [worker] if worker else []

        # Fall back to module-based mapping
        if not workers_to_check and ticket.source_module:
            module_lower = ticket.source_module.lower()
            for pattern, worker_names in self._MODULE_WORKER_MAP.items():
                if pattern in module_lower:
                    workers_to_check.extend(worker_names)
                    break

        if not workers_to_check:
            return None

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_workers = []
        for w in workers_to_check:
            if w not in seen:
                seen.add(w)
                unique_workers.append(w)

        parts: list[str] = []
        for w in unique_workers[:3]:  # cap at 3 workers
            try:
                logs = fetch_worker_logs(w, since_minutes=60, max_lines=300)
                if logs:
                    parts.append(f"### {w}\n```\n{logs[-8000:]}\n```")
            except Exception:
                logger.warning("Failed to fetch logs from worker %s", w, exc_info=True)

        return "\n\n".join(parts) if parts else None

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
                ticket_id=ticket.ticket_id,
                branch=ticket.metadata.get("branch", ""),
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
        """Select model from config tiers: t1_heavy for CRITICAL/regressions, t2_standard otherwise.

        After 2+ investigation timeouts on the heavy tier (Opus), fall back to
        the standard tier (Sonnet) which is faster and less likely to timeout.
        """
        heavy = self._model_config.t1_heavy if self._model_config else "opus"
        standard = self._model_config.t2_standard if self._model_config else "sonnet"

        # After 2 timeouts on heavy tier, fall back to standard (Sonnet)
        timeout_count = ticket.metadata.get("investigation_timeout_count", 0)
        if timeout_count >= 2:
            logger.info(
                "Ticket %s has %d investigation timeouts — falling back to %s",
                ticket.ticket_id, timeout_count, standard,
            )
            return standard

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

    # Mapping of GitHub repo slug → local clone path
    _REPO_PATHS: dict[str, Path] = {
        "ArtemisAI/LinkedAi": Path("/home/agent/Projects/LinkedAi"),
        "ArtemisAI/SWE-Squad-DEV": Path("/home/agent/SWE-Squad"),
    }

    def _repo_cwd(self, ticket: "SWETicket") -> Optional[Path]:
        """Return the local clone path for the ticket's repo, or None."""
        repo = ticket.metadata.get("repo") or ticket.metadata.get("github_repo")
        if not repo:
            return None
        path = self._REPO_PATHS.get(repo)
        if path and path.is_dir():
            return path
        logger.warning("investigator: repo '%s' not cloned locally — running in SWE-Squad root", repo)
        return None

    def _run_claude(
        self, prompt: str, *, model: str = "sonnet", timeout: Optional[int] = None,
        cwd: Optional[Path] = None,
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
            cwd=str(cwd) if cwd else None,
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

    @staticmethod
    def _send_rate_limit_alert(ticket: SWETicket, exc: Exception) -> None:
        """Send a Telegram alert when rate limits are exhausted."""
        from src.swe_team.telegram import send_message

        message = (
            "<b>Rate Limit Exhausted</b>\n\n"
            f"Ticket: <code>{ticket.ticket_id}</code>\n"
            f"Title: {ticket.title[:80]}\n"
            f"Error: {str(exc)[:200]}"
        )
        try:
            send_message(message, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to send rate limit alert for %s", ticket.ticket_id)

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

    _MAX_INVESTIGATION_TIMEOUTS = 3

    def _record_timeout(
        self, ticket: SWETicket, started_at: str, timeout: int, model: str
    ) -> None:
        """Handle subprocess timeout: increment counter, write stub report if terminal.

        After ``_MAX_INVESTIGATION_TIMEOUTS`` total timeouts the ticket gets a
        stub investigation report so it stops being re-picked by ``_eligible``.
        Before that threshold the report is left empty so the next cycle can
        retry (with Sonnet fallback after 2 Opus timeouts — see ``_select_model``).
        """
        count = ticket.metadata.get("investigation_timeout_count", 0) + 1
        ticket.metadata["investigation_timeout_count"] = count

        stub = (
            f"Investigation timed out after {timeout}s (model={model}, "
            f"attempt {count}) — requires manual investigation or Sonnet fallback"
        )

        # After max timeouts, write the stub so the ticket stops looping
        if count >= self._MAX_INVESTIGATION_TIMEOUTS:
            ticket.investigation_report = stub

        ticket.transition(TicketStatus.TRIAGED)
        ticket.metadata["investigation"] = {
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "duration_s": float(timeout),
            "cost_usd": None,
            "model": model,
            "status": "timeout",
            "error": f"subprocess.TimeoutExpired after {timeout}s",
            "timeout_count": count,
        }
        logger.warning(
            "Investigation timed out for ticket %s (model=%s, timeout=%ds, count=%d)",
            ticket.ticket_id, model, timeout, count,
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
