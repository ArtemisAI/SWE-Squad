"""
Investigation agent for the Autonomous SWE Team.

IMPORTANT — PRIMARY SERVICE:
    Claude Code CLI (/usr/bin/claude) is the ONLY investigation engine.
    No other LLM (Gemini, OpenCode, etc.) is ever used as a primary investigator.
    Fallback agents are ONLY invoked when Claude Code is rate-limited (429) and
    ONLY for non-code tasks (summarisation, doc lookup). They NEVER replace Claude.

Runs a diagnostic prompt via Claude Code CLI and attaches the resulting
report to the ticket for downstream development automation.
"""

from __future__ import annotations

import logging
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

from src.swe_team.embeddings import embed_ticket
from src.swe_team.model_boundary import validate_model_for_task
from src.swe_team.preflight import PreflightCheck
from src.swe_team.rbac_middleware import require_permission
from src.swe_team.remote_logs import fetch_worker_logs
from src.swe_team.models import (
    EngineHandover,
    HandoverConstraints,
    InvestigationPhaseOutput,
    SWETicket,
    TicketSeverity,
    TicketStatus,
    TicketType,
)
from src.swe_team.providers.coding_engine.base import CodingEngine
from src.swe_team.providers.log_query.base import LogEntry, LogQueryProvider
from src.swe_team.providers.env.base import EnvProvider, EnvSpec
from src.swe_team.providers.issue_tracker.base import IssueTracker
from src.swe_team.providers.notification.base import NotificationProvider
from src.swe_team.providers.env.dotenv_provider import DotenvEnvProvider
from src.swe_team.providers.repomap.base import RepoMapProvider
from src.swe_team.providers.repomap.ctags_provider import CtagsRepoMapProvider
from src.swe_team.proxy_model_policy import ProxyModelPolicyResolver
from src.swe_team.rate_limiter import (
    EngineCooldownManager,
    ExponentialBackoff,
    MonthlyLimitExhausted,
    RateLimitCooldown,
    RateLimitExhausted,
    RateLimitTracker,
)
from src.swe_team.supabase_store import SupabaseTicketStore
from src.swe_team.token_tracker import AdaptiveTimeout

logger = logging.getLogger(__name__)

try:
    from memory.src.client import MemoryClient
except ImportError:
    MemoryClient = None  # type: ignore[assignment,misc]


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token."""
    return max(1, len(text) // 4)


# Phrases that indicate a fallback agent sent a new-session
# introduction / greeting rather than an investigation report.
# These must never be forwarded to Telegram or stored as investigation reports.
_INTRODUCTION_MARKERS = (
    "i just came online",
    "just came online",
    "blank slate",
    "no memory bank",
    "no idea who you are",
    "i'm a blank slate",
    "i am a blank slate",
    "no context yet",
)


def _is_fallback_introduction(text: str) -> bool:
    """Return True if *text* looks like a fallback agent greeting/introduction.

    Kimi and similar stateless agents sometimes open a new session with a
    self-introduction instead of answering the prompt. These messages must
    not be stored as investigation reports or forwarded to Telegram.
    """
    lower = text.lower()
    return any(marker in lower for marker in _INTRODUCTION_MARKERS)


# Regex patterns for Claude CLI tool-call artifacts that may leak into the
# ``result`` field when the model invokes tools during an investigation.
# These should never appear in GitHub comments or Telegram alerts.

# Matches a [Tool: <name>] header followed by one or more non-blank lines
# (e.g. Input/Output/Result fields), stopping at the next blank line.
# This prevents the regex from consuming the real report text that follows.
_TOOL_CALL_BLOCK_RE = re.compile(
    r"\[Tool:\s*[^\]]+\]\s*\n(?:(?!^\s*$)[^\n]+\n)*\n?",
    re.MULTILINE,
)
# Matches any remaining standalone [Tool: ...] header lines
_TOOL_HEADER_RE = re.compile(r"^\[Tool:\s*[^\]]+\]\s*$", re.MULTILINE)
# Matches JSON-only lines that are tool-call input artifacts (e.g. {"command": "..."})
_BARE_JSON_LINE_RE = re.compile(r"^(?:Input|Output|Result):\s*\{.*\}\s*$", re.MULTILINE)


def _sanitize_report(text: str) -> str:
    """Strip Claude CLI tool-call artifacts from investigation report text.

    When Claude uses tools during investigation, the raw ``result`` field from
    ``--output-format json`` may include interleaved tool-call blocks such as::

        [Tool: Bash]
        Input: {"command": "git ls-remote ..."}

    These must not appear in GitHub comments or Telegram notifications.
    """
    text = _TOOL_CALL_BLOCK_RE.sub("", text)
    text = _TOOL_HEADER_RE.sub("", text)
    text = _BARE_JSON_LINE_RE.sub("", text)
    # Collapse runs of 3+ blank lines down to 2, keeping paragraph separation
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_valid_report(text: str) -> bool:
    """Return True when text appears to be a real investigation report."""
    sanitized = _sanitize_report(text)
    if not sanitized:
        return False

    lowered = sanitized.lower()
    if '"stop_reason":"error"' in lowered or '"subtype":"error_during_execution"' in lowered:
        return False

    try:
        parsed = json.loads(sanitized)
        if isinstance(parsed, dict) and parsed.get("type") == "result":
            return False
    except json.JSONDecodeError:
        pass

    if len(sanitized) < 200:
        return False

    required_sections = (
        # Bug investigation sections
        "Root Cause", "Affected Files", "Fix Plan",
        # Feature/enhancement investigation sections
        "Implementation Plan", "Files to Create", "Files to Modify",
        "Design", "Phase 1", "Phase 2", "API", "Interface",
        "What was implemented", "Files Changed",
    )
    if not any(section.lower() in lowered for section in required_sections):
        return False

    return True


# Type alias for fallback agent adapters (duck-typed — must have .invoke())
_FallbackAgent = Any


class _SafeFormatDict(dict):
    """Dict that returns '{key}' for missing keys, preventing KeyError from stray
    curly braces in user-supplied content (error logs, descriptions, etc.)."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"

_DEFAULT_PROGRAM_PATH = Path("config/swe_team/programs/investigate.md")
_ORCHESTRATE_PROGRAM_PATH = Path("config/swe_team/programs/orchestrate.md")
_FEATURE_PROGRAM_PATH = Path("config/swe_team/programs/feature.md")
_DEFAULT_TIMEOUT = int(os.environ.get("SWE_INVESTIGATION_TIMEOUT", 900))
_OPUS_TIMEOUT = int(os.environ.get("SWE_OPUS_TIMEOUT", 1800))

_adaptive_sonnet_timeout = AdaptiveTimeout(_DEFAULT_TIMEOUT, min_val=60, max_val=3600, window=20, min_samples=5)
_adaptive_opus_timeout = AdaptiveTimeout(_OPUS_TIMEOUT, min_val=120, max_val=7200, window=20, min_samples=5)
# Model tier defaults — always read from env so the orchestrator can override at runtime.
# These are ONLY used when no ModelConfig is injected (e.g. unit tests).
# Never add model name literals anywhere else in this file.
_MODEL_T1 = os.environ.get("SWE_MODEL_T1", "opus")
_MODEL_T2 = os.environ.get("SWE_MODEL_T2", "sonnet")
_MODEL_T3 = os.environ.get("SWE_MODEL_T3", "haiku")
_DEFAULT_MAX_PER_CYCLE = 5
_SEMANTIC_INVESTIGATION_CHARS = 400
_SEMANTIC_FIX_CHARS = 200


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token (GPT/Claude heuristic)."""
    return max(1, len(text) // 4)


class InvestigatorAgent:
    """Investigate triaged tickets using Claude Code CLI."""

    AGENT_NAME = "swe_investigator"

    def __init__(
        self,
        *,
        program_path: Path | str = _DEFAULT_PROGRAM_PATH,
        claude_path: str = "",  # Deprecated: kept for backward compat; all calls go through CodingEngine
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        max_per_cycle: int = _DEFAULT_MAX_PER_CYCLE,
        store: Optional[object] = None,
        memory_top_k: int = 5,
        memory_similarity_floor: float = 0.75,
        model_config: Optional[object] = None,
        rate_limit_config: Optional[object] = None,
        rate_limit_tracker: Optional[RateLimitTracker] = None,
        fallback_agents: Optional[List[_FallbackAgent]] = None,
        repo_paths: Optional[List[dict]] = None,
        env_provider: Optional[EnvProvider] = None,
        repo_map_provider: Optional[RepoMapProvider] = None,
        notifier: Optional[NotificationProvider] = None,
        issue_tracker: Optional[IssueTracker] = None,
        engine: Optional[CodingEngine] = None,
        log_query_provider: Optional[LogQueryProvider] = None,
        rbac_engine: Optional[object] = None,
        preflight: Optional[PreflightCheck] = None,
        worker_module_map: Optional[dict] = None,
        cooldown_manager: Optional[EngineCooldownManager] = None,
        team_id: str = "",
        memory_client: Optional[object] = None,
    ) -> None:
        self._program_path = Path(program_path)
        self._timeout = timeout_seconds
        self._max_per_cycle = max_per_cycle
        self._store = store
        self._memory_top_k = memory_top_k
        self._memory_similarity_floor = memory_similarity_floor
        self._program_cache: Optional[str] = None
        self._model_config = model_config
        self._fallback_agents: List[_FallbackAgent] = fallback_agents or []
        self._repo_paths: List[dict] = repo_paths or []
        self._env_provider: EnvProvider = env_provider or DotenvEnvProvider()
        self._repo_map_provider: RepoMapProvider = repo_map_provider or CtagsRepoMapProvider()
        self._notifier: Optional[NotificationProvider] = notifier
        self._issue_tracker: Optional[IssueTracker] = issue_tracker
        if engine is not None:
            self._engine: CodingEngine = engine
        else:
            from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine
            self._engine = ClaudeCodeEngine()
        self._log_query_provider: Optional[LogQueryProvider] = log_query_provider
        self._session_store = None  # Lazy-initialized SessionStore
        self._proxy_policy = ProxyModelPolicyResolver()
        # RBAC — optional, backward compatible
        self._rbac_engine = rbac_engine
        self._agent_name: str = self.AGENT_NAME
        # Injected preflight — optional, backward compatible
        self._preflight: Optional[PreflightCheck] = preflight
        # Worker module map — config-driven, no hardcoded hostnames
        if worker_module_map is not None:
            self._MODULE_WORKER_MAP = dict(worker_module_map)
        else:
            self._MODULE_WORKER_MAP = {}
        self._team_id = team_id or os.environ.get("SWE_TEAM_ID", "default")
        self._memory_client = memory_client
        self._cooldown_manager = cooldown_manager or EngineCooldownManager(
            store=store,
            team_id=self._team_id,
        )

        # Rate limit backoff
        rl = rate_limit_config
        self._backoff = ExponentialBackoff(
            max_retries=getattr(rl, "max_retries_on_429", 5) if rl else 5,
            initial_delay=getattr(rl, "initial_backoff_seconds", 60) if rl else 60,
            max_delay=getattr(rl, "max_backoff_seconds", 900) if rl else 900,
            tracker=rate_limit_tracker,
            engine_name=getattr(self._engine, "name", "claude"),
            cooldown_manager=self._cooldown_manager,
        )

    def investigate_batch(
        self,
        tickets: Iterable[SWETicket],
        *,
        limit: Optional[int] = None,
        on_complete: Optional[Callable[[SWETicket], None]] = None,
        max_workers: int = 8,
    ) -> List[SWETicket]:
        """Investigate eligible tickets in parallel, returning those updated.

        Args:
            tickets: Tickets to investigate.
            limit: Max tickets to process (defaults to self._max_per_cycle).
            on_complete: Called immediately after each ticket finishes — use for
                per-ticket persistence so results don't wait for the full batch.
            max_workers: Thread pool size (each thread runs one Claude CLI subprocess).
        """
        candidates = []
        max_items = limit if limit is not None else self._max_per_cycle
        for ticket in tickets:
            if len(candidates) >= max_items:
                break
            if self._eligible(ticket):
                candidates.append(ticket)

        updated: List[SWETicket] = []

        def _run(ticket: SWETicket) -> Optional[SWETicket]:
            try:
                if self.investigate(ticket):
                    return ticket
            except Exception:
                logger.exception("Investigation failed for ticket %s", ticket.ticket_id)
            return None

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run, t): t for t in candidates}
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    updated.append(result)
                    if self._store is not None:
                        try:
                            self._store.add(result)
                        except Exception:
                            logger.exception("Failed to persist ticket %s", result.ticket_id)
                    if on_complete is not None:
                        try:
                            on_complete(result)
                        except Exception:
                            logger.exception("on_complete callback failed for ticket %s", result.ticket_id)

        return updated

    @require_permission("investigation")
    def investigate(self, ticket: SWETicket, *, prompt_override: Optional[str] = None) -> bool:
        """Run an investigation for a single ticket.

        For CRITICAL tickets or escalations, Opus is used with the full
        orchestration program — it handles investigation, planning, fixing,
        verification, and documentation in one session using sub-agents.

        Args:
            ticket: The ticket to investigate.
            prompt_override: If provided, use this prompt instead of building
                one from the template.  Used by the failure feedback loop to
                inject developer failure context into a re-investigation.
        """
        # Preflight: validate execution context before starting investigation
        if self._preflight is not None:
            preflight_result = self._preflight.run()
            if not preflight_result.passed:
                logger.warning(
                    "Preflight FAILED for investigation of ticket %s: %s",
                    ticket.ticket_id,
                    preflight_result.summary(),
                )
                ticket.metadata["preflight_failure"] = preflight_result.failures
                return False

        reinvestigation = prompt_override is not None
        if not self._eligible(ticket, reinvestigation=reinvestigation):
            return False

        started_at = datetime.now(timezone.utc).isoformat()
        # Only transition if not already in INVESTIGATING (re-investigation
        # callers pre-transition the ticket).
        if ticket.status != TicketStatus.INVESTIGATING:
            ticket.transition(TicketStatus.INVESTIGATING)
        ticket.metadata["last_heartbeat"] = started_at

        model = self._select_model(ticket)

        if prompt_override is not None:
            prompt = prompt_override
            timeout = self._timeout
        # Feature/enhancement tickets get the feature prompt, not bug investigation
        elif self._is_feature_ticket(ticket):
            prompt = self._build_feature_prompt(ticket)
            timeout = self._timeout
        # Opus gets the orchestration program (full lifecycle with sub-agents)
        # Sonnet gets the investigation-only program
        elif model == self._tier_model("t1_heavy"):
            prompt = self._build_orchestration_prompt(ticket)
            timeout = _OPUS_TIMEOUT
        else:
            prompt = self._build_prompt(ticket)
            timeout = self._timeout

        if prompt is None:
            self._record_failure(ticket, started_at, "Prompt template missing")
            return False

        # Claude Code CLI is the ONLY investigation engine — no routing to Gemini or
        # any other model here. Fallback agents are only tried below on RateLimitExhausted.
        cwd = self._repo_cwd(ticket)

        # Session lifecycle: check for resumable sessions when using CodingEngine
        session_record = None
        resume_session_id = None
        if self._engine is not None:
            try:
                from src.swe_team.session_store import SessionStore
                if self._session_store is None:
                    self._session_store = SessionStore()
                existing = self._session_store.get_by_ticket(ticket.ticket_id)
                suspended = [s for s in existing if s.status == "suspended" and s.agent_type == "investigator"]
                if suspended and hasattr(self._engine, "resume"):
                    resume_session_id = suspended[0].session_id
                    session_record = suspended[0]
                    self._session_store.update_status(resume_session_id, "active")
                    logger.info("Resuming session %s for ticket %s", resume_session_id, ticket.ticket_id)
                else:
                    session_record = self._session_store.create(ticket.ticket_id, "investigator")
                    logger.info("Created session %s for ticket %s", session_record.session_id, ticket.ticket_id)
            except Exception:
                logger.debug("Session store unavailable, proceeding without session tracking", exc_info=True)

        logger.info(
            "Investigating ticket %s via Claude CLI (model=%s, cwd=%s)",
            ticket.ticket_id, model, cwd or "SWE-Squad",
        )
        engine_name = getattr(self._engine, "name", "claude")
        if not self._cooldown_manager.should_use_engine(
            engine_name,
            probe_fn=getattr(self._engine, "health_check", None),
        ):
            logger.warning(
                "Primary engine %s is in cooldown for ticket %s — trying fallback chain",
                engine_name,
                ticket.ticket_id,
            )
            fallback_result = self._try_fallback_agents(prompt, ticket)
            if fallback_result is not None:
                stdout, stderr = fallback_result, ""
                duration_s = 0.0
                report = _sanitize_report(stdout)
                if report and not _is_fallback_introduction(report):
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
                    self._notify_investigation(ticket)
                    return True
            cooldown_seconds = max(60.0, self._cooldown_manager.remaining_cooldown_seconds(engine_name))
            raise RateLimitCooldown(
                f"Engine {engine_name} in cooldown; fallback unavailable for {ticket.ticket_id}",
                cooldown_seconds=cooldown_seconds,
                global_pause=False,
                engine_name=engine_name,
                status=str(self._cooldown_manager.get_status(engine_name).get("status", "")),
            )
        start = time.monotonic()
        try:
            _sid = session_record.session_id if session_record else None
            if resume_session_id and self._engine is not None and hasattr(self._engine, "resume"):
                try:
                    stdout, stderr = self._backoff.execute(
                        lambda: self._run_claude(prompt, model=model, timeout=timeout, cwd=cwd, session_id=resume_session_id, resume=True),
                        context=model,
                    )
                except (OSError, RuntimeError) as resume_exc:
                    # Session ID may be stale (created on another VM or in a
                    # cleaned-up worktree).  Retry with a fresh session before
                    # falling through to model-level fallback.
                    if "resume" in str(resume_exc).lower() or "session" in str(resume_exc).lower():
                        logger.warning(
                            "Session resume failed for ticket %s (session %s): %s — retrying with fresh session",
                            ticket.ticket_id, resume_session_id, resume_exc,
                        )
                        resume_session_id = None
                        stdout, stderr = self._backoff.execute(
                            lambda: self._run_claude(prompt, model=model, timeout=timeout, cwd=cwd, session_id=_sid),
                            context=model,
                        )
                    else:
                        raise  # Not a session issue — let outer handler deal with it
            else:
                stdout, stderr = self._backoff.execute(
                    lambda: self._run_claude(prompt, model=model, timeout=timeout, cwd=cwd, session_id=_sid),
                    context=model,
                )
        except RateLimitExhausted as exc:
            cooldown_row = self._cooldown_manager.mark_failure(
                engine_name,
                exc,
                fallback_engine=str(ticket.metadata.get("fallback_agent_used") or ""),
            )
            # Try fallback agents before giving up
            fallback_result = self._try_fallback_agents(prompt, ticket)
            if fallback_result is not None:
                stdout, stderr = fallback_result, ""
                # Fall through to success handling below
                duration_s = time.monotonic() - start
                report = _sanitize_report(stdout)
                if report and not _is_fallback_introduction(report):
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
                    self._notify_investigation(ticket)
                    return True
                elif report:
                    logger.warning(
                        "Fallback agent returned introduction/greeting for ticket %s — "
                        "suppressing Telegram notification and treating as failure.",
                        ticket.ticket_id,
                    )

            self._record_failure(ticket, started_at, str(exc))
            ticket.metadata["rate_limited"] = True
            ticket.metadata["rate_limited_at"] = datetime.now(timezone.utc).isoformat()
            if isinstance(exc, MonthlyLimitExhausted):
                ticket.metadata["rate_limit_type"] = "monthly_exhausted"
                if getattr(exc, "reset_at", ""):
                    ticket.metadata["rate_limit_reset_at"] = exc.reset_at
            else:
                ticket.metadata["rate_limit_type"] = "transient"
            ticket.metadata["engine_cooldown_status"] = cooldown_row.get("status", "rate_limited")
            self._send_rate_limit_alert(ticket, exc)
            cooldown_seconds = max(60.0, self._cooldown_manager.remaining_cooldown_seconds(engine_name))
            raise RateLimitCooldown(
                f"Rate limit exhausted during investigation of {ticket.ticket_id}; "
                f"pausing engine {engine_name} for {cooldown_seconds:.0f}s",
                cooldown_seconds=cooldown_seconds,
                global_pause=False,
                engine_name=engine_name,
                status=str(cooldown_row.get("status", "rate_limited")),
                reset_at=str(cooldown_row.get("reset_at", "")),
                fallback_engine=str(ticket.metadata.get("fallback_agent_used", "")),
            )
        except subprocess.TimeoutExpired as exc:
            self._record_timeout(ticket, started_at, timeout, model)
            return False
        except (OSError, RuntimeError) as exc:
            # Primary model failed (e.g. external API token expired, model not found).
            # Fall back to local claude-sonnet-4-6 which works without an external API key.
            _LOCAL_FALLBACK_MODEL = "claude-sonnet-4-6"
            if model != _LOCAL_FALLBACK_MODEL:
                logger.warning(
                    "Investigating ticket %s: model '%s' failed (%s) — retrying with local %s",
                    ticket.ticket_id, model, exc, _LOCAL_FALLBACK_MODEL,
                )
                try:
                    stdout, stderr = self._run_claude(
                        prompt, model=_LOCAL_FALLBACK_MODEL, timeout=timeout, cwd=cwd
                    )
                    model = _LOCAL_FALLBACK_MODEL  # update for metadata below
                except (OSError, RuntimeError) as exc2:
                    self._record_failure(ticket, started_at, str(exc2))
                    return False
            else:
                self._record_failure(ticket, started_at, str(exc))
                return False
        except Exception as exc:
            # Catch-all for unexpected errors (e.g. 403 token_rejected from Gemini API,
            # urllib.error.HTTPError, or any other exception from the model call).
            # Fall back to local claude-sonnet-4-6 which does not require an external API key.
            _LOCAL_FALLBACK_MODEL = "claude-sonnet-4-6"
            if model != _LOCAL_FALLBACK_MODEL:
                logger.warning(
                    "Investigating ticket %s: model '%s' raised unexpected %s (%s) — "
                    "retrying with local %s",
                    ticket.ticket_id, model, type(exc).__name__, exc, _LOCAL_FALLBACK_MODEL,
                )
                try:
                    stdout, stderr = self._run_claude(
                        prompt, model=_LOCAL_FALLBACK_MODEL, timeout=timeout, cwd=cwd
                    )
                    model = _LOCAL_FALLBACK_MODEL  # update for metadata below
                except Exception as exc2:
                    self._record_failure(ticket, started_at, str(exc2))
                    return False
            else:
                self._record_failure(ticket, started_at, str(exc))
                return False

        duration_s = time.monotonic() - start
        report = _sanitize_report(stdout)
        if not report:
            self._record_failure(ticket, started_at, "Empty investigation report")
            return False
        if not _is_valid_report(report):
            self._record_failure(ticket, started_at, "Invalid investigation report")
            return False

        cost = _parse_cost(stderr) or _parse_cost(stdout)

        # Record token usage (best-effort, never blocks investigation)
        try:
            from src.swe_team.token_tracker import TokenTracker
            tracker = TokenTracker()
            er = getattr(self, "_last_engine_result", None)
            input_tokens = er.input_tokens if (er and er.input_tokens is not None) else _estimate_tokens(prompt)
            output_tokens = er.output_tokens if (er and er.output_tokens is not None) else _estimate_tokens(stdout)
            cache_read = er.cache_read_tokens if (er and er.cache_read_tokens is not None) else 0
            cache_creation = er.cache_creation_tokens if (er and er.cache_creation_tokens is not None) else 0
            tracker.record(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                task="investigate",
                ticket_id=ticket.ticket_id if hasattr(ticket, 'ticket_id') else "",
                session_id=ticket.metadata.get("trace_id", "") if hasattr(ticket, 'metadata') else "",
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
            )
        except Exception:
            pass  # Token tracking is best-effort, never blocks

        # Record dollar-denominated cost (best-effort, never blocks investigation)
        try:
            _cost_tracker = getattr(self, "_cost_tracker", None)
            if _cost_tracker is not None:
                _team_id = getattr(self, "_team_id", "") or ""
                _ticket_id = ticket.ticket_id if hasattr(ticket, "ticket_id") else ""
                er = getattr(self, "_last_engine_result", None)
                _in = er.input_tokens if (er and er.input_tokens is not None) else _estimate_tokens(prompt)
                _out = er.output_tokens if (er and er.output_tokens is not None) else _estimate_tokens(stdout)
                _cost_tracker.record_cost(
                    team_id=_team_id,
                    model=model,
                    input_tokens=_in,
                    output_tokens=_out,
                    operation="investigate",
                    ticket_id=_ticket_id,
                )
        except Exception:
            pass  # Cost tracking is best-effort, never blocks

        ticket.investigation_report = report
        self._cooldown_manager.mark_healthy(engine_name)
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
        affected_files = []
        try:
            for line in report.splitlines():
                line = line.strip()
                if line.startswith(("/", "./")) and "/" in line:
                    affected_files.append(line)
            if not affected_files:
                affected_files = list(ticket.metadata.get("files_touched", []) or [])
        except Exception:
            affected_files = []
        handover_output = InvestigationPhaseOutput(
            root_cause=(ticket.description or "").strip() or "Investigation completed",
            affected_files=affected_files,
            suggested_fix=(report[:1200]).strip(),
            confidence=0.9 if report else 0.0,
        )
        handover = EngineHandover(
            task_id=ticket.ticket_id,
            phase="investigate",
            source_engine=getattr(self._engine, "name", "unknown"),
            target_engine=ticket.metadata.get("target_engine_develop", "claude"),
            timestamp=datetime.now(timezone.utc).isoformat(),
            context=handover_output.to_dict(),
            constraints=HandoverConstraints(
                budget_remaining_usd=float(ticket.metadata.get("budget_remaining_usd", 0.0) or 0.0),
                time_limit_seconds=int(ticket.metadata.get("handover_time_limit_seconds", self._timeout) or self._timeout),
                model_tier="T1" if model == self._tier_model("t2_standard") else "T3",
                retry_count=int(ticket.metadata.get("investigation_attempts", 0) or 0),
                max_retries=int(ticket.metadata.get("max_investigation_retries", 3) or 3),
            ),
        )
        ticket.metadata["handover_investigate_to_develop"] = handover.to_dict()
        if isinstance(self._store, SupabaseTicketStore):
            try:
                self._store.log_handover(handover)
            except Exception:
                logger.warning("Failed to log investigation handover for %s", ticket.ticket_id, exc_info=True)

        # Mark session as completed; capture actual Claude session UUID for resume
        if session_record and self._session_store:
            try:
                # Capture the real Claude CLI session UUID from engine result
                er = getattr(self, "_last_engine_result", None)
                real_session_id = getattr(er, "session_id", None) if er else None
                if real_session_id and real_session_id != session_record.session_id:
                    self._session_store.update_session_id(session_record.session_id, real_session_id)
                    ticket.metadata["claude_session_id"] = real_session_id
                    logger.info("Captured Claude session UUID %s for ticket %s", real_session_id, ticket.ticket_id)
                else:
                    ticket.metadata["claude_session_id"] = session_record.session_id
                self._session_store.update_status(
                    real_session_id or session_record.session_id, "completed"
                )
            except Exception:
                logger.debug("Failed to mark session completed", exc_info=True)

        issue_number = ticket.metadata.get("github_issue")
        if issue_number:
            self._comment_on_issue(issue_number, ticket)

        if self._memory_client is not None:
            try:
                self._memory_client.record_investigation_result(
                    session_id=f"inv-{ticket.ticket_id}",
                    ticket_id=ticket.ticket_id,
                    report=ticket.investigation_report or "",
                    project=ticket.metadata.get("repo", "") if ticket.metadata else "",
                    cwd=str(cwd) if cwd else "",
                )
            except Exception:
                pass

        self._notify_investigation(ticket)
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
                # Fallback agents (non-Claude) are only allowed for
                # read-only tasks like investigation — never for code generation
                allowed, reason = validate_model_for_task(agent_name, "investigate")
                if not allowed:
                    logger.warning(
                        "Fallback agent %s blocked for investigation: %s",
                        agent_name, reason,
                    )
                    continue

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

    def _is_feature_ticket(self, ticket: SWETicket) -> bool:
        """Detect feature/enhancement tickets from type, labels, or title."""
        if hasattr(ticket, 'ticket_type') and ticket.ticket_type in (TicketType.FEATURE, TicketType.ENHANCEMENT):
            return True
        labels = [l.lower() for l in getattr(ticket, "labels", []) or []]
        if any(l in labels for l in ("enhancement", "feature", "foundation", "integration")):
            return True
        title_lower = (ticket.title or "").lower()
        return any(tag in title_lower for tag in ("[foundation]", "[feature]", "[integration]"))

    def _eligible(self, ticket: SWETicket, *, reinvestigation: bool = False) -> bool:
        if ticket.severity not in (TicketSeverity.CRITICAL, TicketSeverity.HIGH, TicketSeverity.MEDIUM):
            return False
        # UMBRELLA / tracking issues are not actionable bugs
        if "UMBRELLA" in (ticket.title or "").upper():
            return False
        # For re-investigation after developer failure, allow tickets that
        # already have a report and have been transitioned back to INVESTIGATING.
        if reinvestigation:
            if ticket.status != TicketStatus.INVESTIGATING:
                return False
            return True
        if ticket.investigation_report:
            # If the ticket already has a report but was never transitioned to
            # INVESTIGATION_COMPLETE (e.g. a crash between report assignment and
            # status persistence), repair the status in-place so the runner's
            # developer backlog can pick it up.  Still return False so we don't
            # re-run the investigation.
            if ticket.status not in (
                TicketStatus.INVESTIGATION_COMPLETE,
                TicketStatus.IN_DEVELOPMENT,
                TicketStatus.RESOLVED,
                TicketStatus.FAILED,
            ):
                logger.warning(
                    "Ticket %s has investigation_report but status=%s — "
                    "auto-transitioning to INVESTIGATION_COMPLETE",
                    ticket.ticket_id,
                    ticket.status.value,
                )
                ticket.transition(TicketStatus.INVESTIGATION_COMPLETE)
                # Persist the repair immediately so it's visible to developer backlog
                if self._store is not None:
                    try:
                        self._store.add(ticket)
                        logger.info(
                            "Persisted status repair for ticket %s → INVESTIGATION_COMPLETE",
                            ticket.ticket_id,
                        )
                    except Exception:
                        logger.exception("Failed to persist status repair for %s", ticket.ticket_id)
            return False
        if ticket.status not in (
            TicketStatus.OPEN,
            TicketStatus.TRIAGED,
            TicketStatus.INVESTIGATING,
        ):
            return False
        return True

    def _generate_repo_map_context(self, ticket: SWETicket) -> str:
        """Generate a repo-map section for the investigation prompt."""
        try:
            repo_path = self._repo_cwd(ticket) or Path(".")
            rmap = self._repo_map_provider.generate(repo_path, max_tokens=2000)
            text = rmap.to_prompt_string(max_chars=8000)
            if text:
                return f"## Repository Structure Map\n{text}\n\n"
        except Exception:
            logger.debug("Repo-map generation failed (non-fatal)", exc_info=True)
        return ""

    def _build_prompt(self, ticket: SWETicket) -> Optional[str]:
        template = self._load_program(self._program_path)
        if not template:
            return None
        error_log = ticket.error_log or "No error log provided."
        # Inject repo structure map at the top of the error context
        repo_map_ctx = self._generate_repo_map_context(ticket)
        if repo_map_ctx:
            error_log = f"{repo_map_ctx}{error_log}"
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
        issue_type = ticket.ticket_type.value if hasattr(ticket.ticket_type, "value") else str(ticket.ticket_type)
        severity = ticket.severity.value if hasattr(ticket.severity, "value") else str(ticket.severity)
        labels = ", ".join(ticket.labels) if ticket.labels else "none"
        try:
            replacements = dict(
                error_log=error_log,
                source_module=module,
                issue_type=issue_type,
                severity=severity,
                title=ticket.title,
                description=ticket.description or "No description provided.",
                labels=labels,
            )
            return template.format_map(
                _SafeFormatDict(replacements)
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Invalid investigate.md template: %s", exc)
            return None

    # Worker name aliases keyed by common source_module patterns.
    # Populated from config (monitor.worker_module_map) at construction time.
    # Falls back to an empty dict — override via swe_team.yaml or constructor arg.
    _MODULE_WORKER_MAP: dict[str, list[str]] = {}

    def _query_logs_via_provider(
        self,
        service: Optional[str] = None,
        level: Optional[str] = None,
        since_minutes: int = 60,
    ) -> List[LogEntry]:
        """Query logs through the LogQueryProvider if configured.

        Returns an empty list when no provider is set or on error.
        """
        if self._log_query_provider is None:
            return []
        try:
            return self._log_query_provider.query_logs(
                service=service, level=level, since_minutes=since_minutes,
            )
        except Exception:
            logger.warning("LogQueryProvider.query_logs failed, falling back to SSH", exc_info=True)
            return []

    def _fetch_worker_logs(self, ticket: SWETicket) -> Optional[str]:
        """Pull fresh logs from workers relevant to this ticket.

        When a LogQueryProvider is configured it is tried first.  SSH-based
        ``fetch_worker_logs`` is used as a fallback (or complement).  Results
        from both sources are merged and deduplicated by message text so the
        investigation prompt never contains duplicate entries.

        Uses ticket metadata (source_worker) or source_module to identify
        which worker(s) to query. Returns combined log text or None.
        """
        # --- Provider path ---------------------------------------------------
        service_hint = (
            ticket.metadata.get("source_worker")
            or ticket.source_module
            or None
        )
        provider_entries = self._query_logs_via_provider(
            service=service_hint, level="ERROR", since_minutes=60,
        )
        provider_parts: list[str] = []
        provider_messages: set[str] = set()
        if provider_entries:
            for entry in provider_entries:
                provider_messages.add(entry.message)
            formatted = "\n".join(
                f"[{e.timestamp}] {e.level} {e.source}: {e.message}"
                for e in provider_entries
            )
            provider_parts.append(f"### LogQueryProvider\n```\n{formatted[-8000:]}\n```")

        # --- SSH path (existing behaviour) ------------------------------------
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

        ssh_parts: list[str] = []
        if workers_to_check:
            # Deduplicate while preserving order
            seen: set[str] = set()
            unique_workers = []
            for w in workers_to_check:
                if w not in seen:
                    seen.add(w)
                    unique_workers.append(w)

            for w in unique_workers[:3]:  # cap at 3 workers
                try:
                    logs = fetch_worker_logs(w, since_minutes=60, max_lines=300)
                    if logs:
                        # Deduplicate lines already present via provider
                        if provider_messages:
                            deduped_lines = [
                                line for line in logs.splitlines()
                                if not any(msg in line for msg in provider_messages)
                            ]
                            logs = "\n".join(deduped_lines)
                        if logs.strip():
                            ssh_parts.append(f"### {w}\n```\n{logs[-8000:]}\n```")
                except Exception:
                    logger.warning("Failed to fetch logs from worker %s", w, exc_info=True)

        # --- Merge results ----------------------------------------------------
        parts = provider_parts + ssh_parts
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
            return template.format_map(_SafeFormatDict(
                title=ticket.title,
                severity=ticket.severity.value,
                source_module=ticket.source_module or "unknown",
                description=description,
                investigation_report=ticket.investigation_report or "No prior investigation.",
                ticket_id=ticket.ticket_id,
                branch=ticket.metadata.get("branch", ""),
            ))
        except (KeyError, ValueError) as exc:
            logger.warning("Invalid orchestrate.md template: %s", exc)
            return self._build_prompt(ticket)

    def _build_feature_prompt(self, ticket: SWETicket) -> Optional[str]:
        """Build the feature/enhancement investigation prompt."""
        template = self._load_program(_FEATURE_PROGRAM_PATH)
        if not template:
            # Fall back to investigation program if feature.md is missing
            return self._build_prompt(ticket)
        description = ticket.description or ""
        similar_context = self._semantic_memory_context(ticket)
        if similar_context:
            description = f"{description}\n\n{similar_context}"
        try:
            return template.format_map(_SafeFormatDict(
                ticket_id=ticket.ticket_id,
                title=ticket.title,
                ticket_type=ticket.ticket_type.value,
                source_module=ticket.source_module or "unknown",
                description=description,
                investigation_report=ticket.investigation_report or "No prior investigation.",
            ))
        except (KeyError, ValueError) as exc:
            logger.warning("Invalid feature.md template: %s", exc)
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
            # --- memory service context ---
            if self._memory_client is not None:
                try:
                    repo = ticket.metadata.get("repo", "") if ticket.metadata else ""
                    mem_ctx = self._memory_client.get_investigation_context(
                        ticket.title,
                        project=repo or ticket.source_module or "SWE-Squad",
                    )
                    if mem_ctx:
                        lines.append("\n## Memory Service Context\n" + mem_ctx)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Memory context injection failed: %s", exc)
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
        """Select Claude model tier based on severity, attempt count, and timeout history.

        Claude Code CLI is the ONLY engine used here — Gemini/other models are never
        selected by this method. Routing:
        - MEDIUM/LOW → Sonnet (t2_standard)
        - HIGH → Sonnet (t2_standard)
        - CRITICAL, first attempt → Sonnet (t2_standard) — try cheap first
        - CRITICAL, retry after Sonnet failure → Opus (t1_heavy) escalation
        - CRITICAL, 2+ timeouts → Sonnet fallback (GH #41 fix preserved)
        - Regressions → Opus (t1_heavy)
        """
        heavy = self._tier_model("t1_heavy")
        standard = self._tier_model("t2_standard")

        # After 2 timeouts on heavy tier, fall back to standard (Sonnet)
        timeout_count = ticket.metadata.get("investigation_timeout_count", 0)
        if timeout_count >= 2:
            logger.info(
                "Ticket %s has %d investigation timeouts — falling back to %s",
                ticket.ticket_id, timeout_count, standard,
            )
            return standard

        # Regressions always route to heavy tier (regardless of severity)
        if ticket.metadata.get("is_regression"):
            return heavy

        # MEDIUM → Sonnet. Claude Code is the sole investigation engine at all severities.
        if ticket.severity == TicketSeverity.MEDIUM:
            return standard

        if ticket.severity == TicketSeverity.CRITICAL:
            # Escalate to Opus only on retry (prior investigation failed)
            inv = ticket.metadata.get("investigation", {})
            if inv.get("status") == "failed":
                return heavy
            # First attempt: use Sonnet (cheaper, faster)
            return standard

        # HIGH → Sonnet (standard tier)
        # Also handles escalation for HIGH after failure
        inv = ticket.metadata.get("investigation") or {}
        if inv.get("status") == "failed":
            return heavy
        return standard

    def _tier_model(self, tier: str) -> str:
        """Return resolved model string for a config tier.

        If proxy-policy mode is enabled, short names and failing models are
        automatically resolved according to `proxy_model_policy.yaml`.
        """
        if tier == "t1_heavy":
            raw = self._model_config.t1_heavy if self._model_config else _MODEL_T1
        elif tier == "t2_standard":
            raw = self._model_config.t2_standard if self._model_config else _MODEL_T2
        else:
            raw = self._model_config.t3_fast if self._model_config else _MODEL_T3
        return self._proxy_policy.resolve(raw, tier=tier)

    def _get_repo_path(self, repo: str) -> Optional[Path]:
        """Get local path for a repo from config (swe_team.yaml repos list).

        Lookup order:
        1. ``repos`` list from SWETeamConfig (loaded from swe_team.yaml)
        2. ``SWE_REPO_PATH`` environment variable (single-repo fallback)
        """
        # Config-driven lookup (repos list from swe_team.yaml, passed via repo_paths)
        for repo_cfg in self._repo_paths:
            if repo_cfg.get("name") == repo:
                path = Path(repo_cfg.get("local_path", ""))
                if path.is_dir():
                    return path
        # Fallback: environment variable
        env_path = os.environ.get("SWE_REPO_PATH")
        if env_path:
            p = Path(env_path)
            if p.is_dir():
                return p
        return None

    def _repo_cwd(self, ticket: "SWETicket") -> Optional[Path]:
        """Return the local clone path for the ticket's repo, or None."""
        repo = ticket.metadata.get("repo") or ticket.metadata.get("github_repo")
        if not repo:
            return None
        path = self._get_repo_path(repo)
        if path and path.is_dir():
            return path
        logger.warning("investigator: repo '%s' not cloned locally — running in SWE-Squad root", repo)
        return None

    def _run_claude(
        self, prompt: str, *, model: str = _MODEL_T2, timeout: Optional[int] = None,
        cwd: Optional[Path] = None,
        session_id: Optional[str] = None,
        resume: bool = False,
    ) -> tuple[str, str]:
        """Run Claude CLI and return ``(stdout, stderr)``.

        When using the engine, the last :class:`EngineResult` is stored in
        ``self._last_engine_result`` so callers can access telemetry fields.
        """
        from src.swe_team.providers.coding_engine.base import EngineResult as _ER  # noqa: F811

        effective_timeout = timeout or self._timeout
        if resume and session_id and hasattr(self._engine, "resume"):
            result = self._engine.resume(
                session_id,
                prompt,
                model=model,
                timeout=effective_timeout,
                cwd=str(cwd) if cwd else None,
                env=self._env_provider.build_env(EnvSpec(role="claude_cli")),
            )
        else:
            result = self._engine.run(
                prompt,
                model=model,
                timeout=effective_timeout,
                cwd=str(cwd) if cwd else None,
                env=self._env_provider.build_env(EnvSpec(role="claude_cli")),
                **({"session_id": session_id} if session_id else {}),
            )
        if not result.success:
            err_msg = result.stderr.strip()
            if not err_msg:
                err_msg = f"Claude CLI failed (rc={result.returncode}). Output: {result.stdout.strip()[:500]}"
            raise RuntimeError(err_msg)
        self._last_engine_result = result
        return result.stdout, result.stderr

    def _comment_on_issue(self, issue_number: int, ticket: SWETicket) -> None:
        report = _sanitize_report(ticket.investigation_report or "")
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
        if self._issue_tracker:
            # Issue tracker should ideally handle idempotency too,
            # but for now we fallback to the default GitHub implementation
            self._issue_tracker.comment(str(issue_number), body)
        else:
            from src.swe_team.github_integration import (
                comment_on_issue,
                find_comment_by_text,
                update_github_comment,
            )
            
            # Search for an existing report for this ticket on this issue
            ticket_repo = ticket.metadata.get("repo", "") if ticket.metadata else ""
            marker = f"**Ticket ID:** `{ticket.ticket_id}`"
            comment_id = find_comment_by_text(issue_number, marker, repo=ticket_repo)

            if comment_id:
                logger.info("Updating existing investigation report for ticket %s", ticket.ticket_id)
                update_github_comment(comment_id, body, repo=ticket_repo)
            else:
                logger.info("Posting new investigation report for ticket %s", ticket.ticket_id)
                comment_on_issue(issue_number, body, repo=ticket_repo)

    def _notify_investigation(self, ticket: SWETicket) -> None:
        """Notify about investigation completion via the injected provider."""
        if self._notifier:
            if not ticket.investigation_report:
                return
            if ticket.severity.value != "critical":
                return
            module = ticket.source_module or "unknown"
            severity = ticket.severity.value.upper()
            title = ticket.title[:80]
            report = ticket.investigation_report.strip()
            summary = report.splitlines()[0][:200] if report else "Report generated"
            message = (
                f"<b>Investigation complete</b>\n\n"
                f"<b>[{severity}]</b> {title}\n"
                f"Module: {module}\n"
                f"Ticket: <code>{ticket.ticket_id}</code>\n\n"
                f"<b>Summary:</b> {summary}"
            )
            self._notifier.send_alert(message, level="info")
        else:
            from src.swe_team.notifier import notify_investigation_summary  # noqa: PLC0415
            notify_investigation_summary(ticket)

    def _send_rate_limit_alert(self, ticket: SWETicket, exc: Exception) -> None:
        """Send a Telegram alert when rate limits are exhausted."""
        message = (
            "<b>Rate Limit Exhausted</b>\n\n"
            f"Ticket: <code>{ticket.ticket_id}</code>\n"
            f"Title: {ticket.title[:80]}\n"
            f"Error: {str(exc)[:200]}"
        )
        if self._notifier:
            self._notifier.send_alert(message, level="critical")
        else:
            from src.swe_team.telegram import send_message  # noqa: PLC0415
            try:
                send_message(message, parse_mode="HTML")
            except Exception:
                logger.exception("Failed to send rate limit alert for %s", ticket.ticket_id)

    def _suspend_session(self, ticket_id: str) -> None:
        """Mark any active session for this ticket as suspended (resumable)."""
        if self._session_store is None:
            return
        try:
            sessions = self._session_store.get_by_ticket(ticket_id)
            for s in sessions:
                if s.status == "active":
                    self._session_store.update_status(s.session_id, "suspended")
        except Exception:
            logger.debug("Failed to suspend session for %s", ticket_id, exc_info=True)

    def _record_failure(
        self, ticket: SWETicket, started_at: str, error: str
    ) -> None:
        self._suspend_session(ticket.ticket_id)
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

        Also suspends any active session so it can be resumed next cycle.

        After ``_MAX_INVESTIGATION_TIMEOUTS`` total timeouts the ticket gets a
        stub investigation report so it stops being re-picked by ``_eligible``.
        Before that threshold the report is left empty so the next cycle can
        retry (with Sonnet fallback after 2 Opus timeouts — see ``_select_model``).
        """
        self._suspend_session(ticket.ticket_id)
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
