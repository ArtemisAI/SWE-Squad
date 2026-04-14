#!/usr/bin/env python3
"""SWE Team Runner — autonomous monitoring, triage, and stability gate."""

import argparse
import concurrent.futures
import json
import logging
import logging.handlers
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pathlib import Path

# ── Project bootstrap ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

# Load .env
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

# Disable Python 3.12 asyncio task logging (causes segfault in some contexts)
logging.logAsyncioTasks = False

# Early basic logging to capture errors during import/init before setup_logging()
# reconfigures with FileHandler + structured format.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)

from src.swe_team.config import load_config
from src.swe_team.gemini_cli_adapter import GeminiCLIAdapter
from src.swe_team.investigator import InvestigatorAgent
from src.swe_team.models import (
    EngineHandover,
    GovernanceVerdict,
    HandoverConstraints,
    SWETicket,
    TicketSeverity,
    TicketStatus,
    TicketType,
    VerificationPhaseOutput,
)
from src.swe_team.monitor_agent import MonitorAgent
from src.swe_team.triage_agent import TriageAgent
from src.swe_team.ralph_wiggum import RalphWiggumGate
from src.swe_team.ticket_store import TicketStore
from src.swe_team.embeddings import embed_ticket
from src.swe_team.supabase_store import SupabaseTicketStore
from src.swe_team.notifier import (
    notify_new_tickets,
    notify_stability_gate,
    notify_daily_summary,
    notify_regression_hitl,
    notify_cycle_summary,
    notify_status,
    aggregate_daily_costs,
)
from src.swe_team.session import make_session_tag, log_session_start, log_session_end
from src.swe_team.repo_router import RepoRouter
from src.swe_team.github_integration import create_github_issue, find_existing_issue, escalate_to_human, claim_issue, update_github_comment
from src.swe_team.events import SWEEvent
from src.swe_team.creative_agent import CreativeAgent
from src.swe_team.distiller import TrajectoryDistiller
from src.swe_team.preflight import PreflightCheck
from src.swe_team.model_probe import ModelProbe
from src.swe_team.rate_limiter import (
    EngineCooldownManager,
    RateLimitCooldown,
    RateLimitTracker,
    check_cooldown_lockfile,
    write_cooldown_lockfile,
)
from src.swe_team.graph_scoring import priority_score
from src.swe_team.providers.coding_engine import resolve_engine
from src.swe_team.providers.notification import create_notification_provider
from src.swe_team.providers.issue_tracker import create_issue_tracker
from src.swe_team.providers.workspace import create_workspace_provider
from src.swe_team.providers.sandbox import create_sandbox_provider
from src.a2a.adapters.swe_team import dispatch_swe_events, SWETeamAdapter
from src.a2a.server import A2AServer
from src.swe_team.agent_registry import AgentRegistry
from src.swe_team.throttle import (
    ThrottlePolicy, ThrottleContext, TimeBasedAdapter,
    CapacityAdapter, DemandAdapter, days_until_weekly_reset,
)
from src.swe_team.parallel_executor import ParallelExecutor, ExecutionConfig, TaskResult
from src.swe_team.providers.rbac.simple import SimpleRBACEngine
from src.swe_team.worktree_manager import WorktreeManager
from src.swe_team.github_scanner import GitHubIssueScanner, GitHubScannerConfig
from src.swe_team.circuit_breaker import CircuitBreaker
from src.swe_team.queued_dispatcher import QueuedDispatcher
from src.swe_team.fix_verifier import FixVerifier, add_fingerprint_scan_to_monitor
from src.swe_team.dependency_graph import DependencyGraph
from src.swe_team.workflow import PipelineExecutor, load_workflow_definition

logger = logging.getLogger("swe_team")

# ── Rate-limit daemon-wide cooldown ──────────────────────────────────────────
# When RateLimitCooldown is raised (all retries exhausted), this timestamp
# is set so the daemon skips cycles until the cooldown expires.
# This is NOT a circuit-breaker failure — it is external API throttling.
_rate_limit_cooldown_until: float = 0.0

# ── Usage Governor (optional) ────────────────────────────────────────────────
_usage_governor = None


def _init_usage_governor(config) -> None:
    """Optionally create the usage governor from config. No-op if not configured."""
    global _usage_governor
    try:
        providers_cfg = getattr(config, "providers", None)
        if isinstance(providers_cfg, dict):
            gov_cfg = providers_cfg.get("usage_governor")
        else:
            gov_cfg = None
        if not gov_cfg:
            return
        from src.swe_team.providers.usage_governor import create_usage_governor
        _usage_governor = create_usage_governor(gov_cfg)
        logger.info("Usage governor initialised (provider=%s)", gov_cfg.get("provider", "adaptive"))
    except Exception:
        logger.warning("Failed to initialise usage governor (non-fatal)", exc_info=True)

# ── Provider instances (resolved from config at startup) ────────────────────
_notification_provider = None
_issue_tracker = None
_workspace_provider = None
_sandbox_provider = None
_log_query_provider: Optional = None


def _init_providers(config) -> None:
    """Resolve all pluggable providers from config via factory functions.

    Each provider is resolved by reading ``config.providers.<domain>`` and
    extracting the ``provider`` key as the backend name.  The full config dict
    is passed to the factory so it can read any provider-specific settings.

    All failures are non-fatal: a warning is logged and the provider is left as
    None so callers can fall back gracefully or skip optional functionality.
    """
    global _notification_provider, _issue_tracker, _workspace_provider, _sandbox_provider

    providers_cfg = getattr(config, "providers", None)
    if not isinstance(providers_cfg, dict):
        logger.debug("No providers config found — skipping provider init")
        return

    # Notification provider
    try:
        notif_cfg = providers_cfg.get("notification") or {}
        provider_name = notif_cfg.get("provider", "telegram")
        _notification_provider = create_notification_provider(provider_name, notif_cfg)
        logger.info("Notification provider initialised (provider=%s)", provider_name)
    except Exception:
        logger.warning("Failed to initialise notification provider (non-fatal)", exc_info=True)

    # Issue tracker provider
    try:
        tracker_cfg = providers_cfg.get("issue_tracker") or {}
        provider_name = tracker_cfg.get("provider", "github")
        _issue_tracker = create_issue_tracker(provider_name, tracker_cfg)
        logger.info("Issue tracker provider initialised (provider=%s)", provider_name)
    except Exception:
        logger.warning("Failed to initialise issue tracker provider (non-fatal)", exc_info=True)

    # Workspace provider
    try:
        workspace_cfg = providers_cfg.get("workspace") or {}
        provider_name = workspace_cfg.get("provider", "git-worktree")
        _workspace_provider = create_workspace_provider(provider_name, workspace_cfg)
        logger.info("Workspace provider initialised (provider=%s)", provider_name)
    except Exception:
        logger.warning("Failed to initialise workspace provider (non-fatal)", exc_info=True)

    # Sandbox provider
    try:
        sandbox_cfg = providers_cfg.get("sandbox") or {}
        provider_name = sandbox_cfg.get("provider", "local")
        _sandbox_provider = create_sandbox_provider(provider_name, sandbox_cfg)
        logger.info("Sandbox provider initialised (provider=%s)", provider_name)
    except Exception:
        logger.warning("Failed to initialise sandbox provider (non-fatal)", exc_info=True)

    # Usage monitor provider (feeds token data to usage governor)
    try:
        monitor_cfg = providers_cfg.get("usage_monitor") or {}
        if monitor_cfg.get("enabled", False):
            from src.swe_team.providers.usage_monitor import create_provider as create_usage_monitor
            monitor = create_usage_monitor(monitor_cfg)
            if _usage_governor is not None and hasattr(_usage_governor, 'set_token_tracker'):
                _usage_governor.set_token_tracker(monitor)
                logger.info("Usage monitor attached to governor (provider=%s)", monitor_cfg.get("provider", "jsonl"))
    except Exception:
        logger.warning("Failed to initialise usage monitor (non-fatal)", exc_info=True)

    # Log query provider (feeds log data to investigator)
    global _log_query_provider
    try:
        log_cfg = providers_cfg.get("log_query") or {}
        if log_cfg.get("enabled", False):
            from src.swe_team.providers.log_query import create_log_query_provider
            _log_query_provider = create_log_query_provider(log_cfg)
            logger.info("Log query provider initialised (provider=%s)", log_cfg.get("provider", "loki"))
    except Exception:
        logger.warning("Failed to initialise log query provider (non-fatal)", exc_info=True)

    # Task queue provider (optional — enables queue-backed dispatch)
    global _queued_dispatcher
    try:
        queue_cfg = providers_cfg.get("task_queue") or {}
        if queue_cfg.get("enabled", False):
            from src.swe_team.providers.task_queue import create_task_queue
            provider_name = queue_cfg.get("provider", "memory")
            queue = create_task_queue(provider_name, queue_cfg)
            worker_id = os.environ.get("SWE_TEAM_ID", "swe-squad-1")
            _queued_dispatcher = QueuedDispatcher(
                queue, worker_id=worker_id,
                heartbeat_interval_s=queue_cfg.get("heartbeat_interval_s", 45.0),
            )
            logger.info(
                "Task queue dispatcher initialised (provider=%s, worker=%s)",
                provider_name, worker_id,
            )
    except Exception:
        logger.warning("Failed to initialise task queue dispatcher (non-fatal)", exc_info=True)


# Global A2A server reference for clean shutdown
_a2a_server: Optional[A2AServer] = None

# Global parallel executor reference (created on first parallel cycle)
_parallel_executor: Optional[ParallelExecutor] = None
_worktree_manager: Optional[WorktreeManager] = None

# Optional queue-backed dispatcher (created when providers.task_queue is configured)
_queued_dispatcher: Optional[QueuedDispatcher] = None

# Fix 1: Hard cap on total dev sessions per ticket to prevent runaway loops
_MAX_SESSIONS_PER_TICKET = 3


# ---------------------------------------------------------------------------
# Developer failure feedback helpers
# ---------------------------------------------------------------------------

def _build_failure_context(ticket: "SWETicket") -> str:
    """Build a summary of developer failure attempts for re-investigation.

    Extracts attempt records from ``ticket.metadata["attempts"]`` and formats
    them into a concise report that can be appended to a re-investigation
    prompt so the investigator has visibility into what was already tried.
    """
    attempts = ticket.metadata.get("attempts", [])
    lines: List[str] = []
    for i, attempt in enumerate(attempts, 1):
        error = attempt.get("error", "unknown")
        result = attempt.get("result", "fail")
        model = attempt.get("model", "?")
        lines.append(f"**Attempt {i}** (model={model}, result={result}): {error[:300]}")
    return "\n".join(lines) if lines else "No attempt details available"


def _try_reinvestigation(
    ticket: "SWETicket",
    investigator: "InvestigatorAgent",
    dev: Any,
    store: Any,
    max_reinvestigations: int,
) -> bool:
    """Attempt re-investigation after a developer failure.

    Returns True if the subsequent fix attempt succeeded, False otherwise.
    The ticket is persisted after each state change.
    """
    reinvestigation_count = ticket.metadata.get("reinvestigation_count", 0)
    if reinvestigation_count >= max_reinvestigations:
        return False
    if not ticket.metadata.get("attempts"):
        return False

    failure_context = _build_failure_context(ticket)
    original_report = ticket.investigation_report or ""
    enriched_prompt = (
        f"## Re-investigation Required\n\n"
        f"The previous investigation led to a failed fix attempt. "
        f"Please re-analyze with the additional context below.\n\n"
        f"### Original Investigation\n{original_report[:1500]}\n\n"
        f"### Developer Failure Context\n{failure_context}\n\n"
        f"Provide a MORE SPECIFIC root cause analysis and fix strategy."
    )

    ticket.metadata["reinvestigation_count"] = reinvestigation_count + 1
    ticket.transition(TicketStatus.INVESTIGATING)
    store.add(ticket)

    try:
        investigator.investigate(ticket, prompt_override=enriched_prompt)
        store.add(ticket)
        logger.info(
            "Re-investigation #%d complete for %s",
            reinvestigation_count + 1,
            ticket.ticket_id,
        )

        # Re-attempt fix with updated investigation
        if ticket.investigation_report:
            fix_ok = dev.attempt_fix(ticket)
            store.add(ticket)
            if fix_ok:
                logger.info(
                    "Fix succeeded after re-investigation for %s",
                    ticket.ticket_id,
                )
                return True
    except RateLimitCooldown:
        raise  # Must propagate to daemon loop for global cooldown
    except Exception:
        logger.exception("Re-investigation failed for %s", ticket.ticket_id)

    return False


def start_a2a_server(
    config,
    store,
    *,
    host: str = "0.0.0.0",
    port: int = 18790,
) -> A2AServer:
    """Start the A2A server in a background thread.

    Returns the running A2AServer instance.
    """
    global _a2a_server
    adapter = SWETeamAdapter(
        config=config,
        store=store,
        base_url=f"http://{host}:{port}",
    )
    server = A2AServer(adapter=adapter, host=host, port=port)
    server.start()
    _a2a_server = server
    logger.info("A2A server started on %s:%d", host, port)
    return server


def setup_agent_registry(config, store=None) -> AgentRegistry:
    """Set up the agent registry with locally-available agents.

    Registers SWE-Squad itself, Gemini CLI and OpenCode adapters if available.
    Also runs A2A network discovery against configured endpoints.
    """
    from src.a2a.client import A2AClient

    hub_url = getattr(config, "a2a_hub_url", "") or None

    client = A2AClient(timeout=10)
    registry = AgentRegistry(
        ttl_seconds=600,
        discovery_urls=[],   # hub discovery goes through hub_url, not discovery_urls
        a2a_client=client,
        hub_url=hub_url,     # enables hub mode: register + discover via /v1/agents
    )

    # Register SWE-Squad itself first
    if store is not None:
        try:
            from src.a2a.adapters.swe_team import SWETeamAdapter
            swe_adapter = SWETeamAdapter(config=config, store=store)
            registry.register_local(swe_adapter)
            logger.info("Registered local SWE-Squad adapter with A2A hub")
        except Exception:
            logger.debug("SWE-Squad adapter registration failed", exc_info=True)

    # Register local adapters if available
    try:
        from src.a2a.adapters.gemini_adapter import GeminiCLIAdapter
        gemini = GeminiCLIAdapter()
        if gemini.is_available():
            registry.register_local(gemini)
            logger.info("Registered local Gemini CLI adapter")
    except Exception:
        logger.debug("Gemini CLI adapter not available", exc_info=True)

    try:
        from src.a2a.adapters.opencode_adapter import OpenCodeCLIAdapter
        opencode = OpenCodeCLIAdapter()
        if opencode.is_available():
            registry.register_local(opencode)
            logger.info("Registered local OpenCode adapter")
    except Exception:
        logger.debug("OpenCode adapter not available", exc_info=True)

    # Network discovery (best-effort)
    try:
        discovered = registry.discover()
        if discovered:
            logger.info("Discovered %d remote agent(s) via A2A", len(discovered))
    except Exception:
        logger.debug("A2A network discovery failed (non-fatal)", exc_info=True)

    return registry


def comment_on_github_issue(issue_number: int, body: str, repo: str = "") -> Optional[str]:
    """Post or update a status update comment on a GitHub issue.

    Checks if a comment with similar content (marker) already exists to avoid spam.
    Returns the comment ID (from gh output URL) on success, or None on failure.
    """
    try:
        from src.swe_team.github_integration import find_comment_by_text, update_github_comment

        # Use first line as marker for dedup
        marker = body.splitlines()[0] if body.strip() else ""
        if len(marker) > 5:
            # Also include Ticket ID in marker if present for better specificity
            for line in body.splitlines():
                if "**Ticket ID:**" in line:
                    marker = f"{marker}\n{line}"
                    break

            comment_id = find_comment_by_text(issue_number, marker, repo=repo)
            if comment_id:
                logger.info("Updating existing comment %s on GH#%d", comment_id, issue_number)
                if update_github_comment(comment_id, body, repo=repo):
                    return comment_id

        cmd = ["gh", "issue", "comment", str(issue_number), "--body", body]
        if repo:
            cmd += ["--repo", repo]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            logger.warning(
                "gh issue comment failed (rc=%d): %s",
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
            return None
        # gh issue comment prints the comment URL on success; extract the ID
        url = (result.stdout or "").strip()
        if url and "/" in url:
            return url.rsplit("/", 1)[-1]  # e.g. ".../comments/123456" -> "123456"
        return None
    except Exception:
        logger.exception("Failed to comment on issue #%d", issue_number)
        return None


def _post_or_update_status(
    ticket: SWETicket,
    body: str,
    *,
    repo: str = "",
) -> Optional[str]:
    """Fix 2: Route status updates through the progress comment when available.

    If the ticket has a ``progress_comment_id`` (set by ``claim_issue``), update
    that comment in-place via ``update_github_comment`` to avoid notification
    spam.  Falls back to ``comment_on_github_issue`` for tickets without a
    tracked comment.

    Returns the comment ID on success (new or existing), or None.
    """
    issue_num = ticket.metadata.get("github_issue")
    if not issue_num:
        return None
    target_repo = repo or ticket.metadata.get("repo", "")
    # Safety: never post to GitHub without an explicit target repo.
    # Monitor-detected tickets have no repo and must not spam sandbox repos.
    if not target_repo:
        logger.debug("Skipping GH comment for ticket %s — no target repo", ticket.ticket_id)
        return None
    comment_id = ticket.metadata.get("progress_comment_id")
    if comment_id:
        ok = update_github_comment(comment_id, body, repo=target_repo)
        if ok:
            return comment_id
        logger.debug("update_github_comment failed for comment %s — falling back to new comment", comment_id)
    return comment_on_github_issue(issue_num, body, repo=target_repo)


def _log_handover_if_supported(store: object, handover: EngineHandover) -> None:
    """Persist a handover envelope through store audit logging when available."""
    if hasattr(store, "log_handover"):
        try:
            store.log_handover(handover)
        except Exception:
            logger.warning("Failed to log handover for %s", handover.task_id, exc_info=True)


def store_ticket_embedding(
    store: TicketStore | SupabaseTicketStore,
    ticket: SWETicket,
    *,
    enabled: bool = True,
) -> None:
    """Store semantic embedding for an investigated ticket when supported."""
    if (
        not enabled
        or not ticket.investigation_report
        or not isinstance(store, SupabaseTicketStore)
    ):
        return
    try:
        emb = embed_ticket(ticket)
        if emb:
            action = store.store_embedding_with_dedup(ticket, emb)
            logger.info("Embedding memory action=%s for ticket %s", action, ticket.ticket_id)
    except Exception as exc:
        logger.warning("Embedding storage failed (non-fatal): %s", exc)


def write_status(
    status_path: str,
    *,
    cycle_result: Dict[str, Any],
    store: object,
    interval_seconds: int = 0,
) -> None:
    """Write a JSON status file for external monitoring."""
    now = datetime.now(timezone.utc)
    open_tickets = store.list_open() if hasattr(store, "list_open") else []

    investigating = [
        t for t in open_tickets if t.status == TicketStatus.INVESTIGATING
    ]

    next_cycle = None
    if interval_seconds > 0:
        from datetime import timedelta
        next_cycle = (now + timedelta(seconds=interval_seconds)).isoformat()

    status = {
        "last_cycle": now.isoformat(),
        "tickets_open": len(open_tickets),
        "tickets_investigating": len(investigating),
        "gate_verdict": cycle_result.get("gate_verdict", "N/A"),
        "next_cycle": next_cycle,
    }

    # Add governor data if available
    if _usage_governor is not None:
        try:
            from dataclasses import asdict
            status["governor"] = {
                "quota": asdict(_usage_governor.get_quota_status()),
                "decision": asdict(_usage_governor.get_concurrency_decision()),
            }
        except Exception:
            logger.debug("Failed to add governor data to status", exc_info=True)
    if "engine_health" in cycle_result:
        status["engine_health"] = cycle_result.get("engine_health") or []

    p = Path(status_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(status, fh, indent=2)
        tmp.replace(p)
        logger.debug("Status written to %s", p)
    except OSError as exc:
        logger.warning("Failed to write status to %s: %s", p, exc)


def run_test_only_cycle(
    config,
    store,
) -> Dict[str, Any]:
    """Re-run tests on all in_development or in_review tickets.

    Skips monitor/triage — useful for CI integration to verify that
    existing fixes still pass their test suites.
    """
    from src.swe_team.developer import DeveloperAgent

    candidates = store.list_by_status(TicketStatus.IN_DEVELOPMENT) + store.list_by_status(
        TicketStatus.IN_REVIEW
    )

    if not candidates:
        logger.info("test-only: no in_development/in_review tickets found")
        return {"tested": 0, "passed": 0, "failed": 0}

    dev = DeveloperAgent(repo_root=PROJECT_ROOT)
    passed = 0
    failed = 0
    for ticket in candidates:
        logger.info("test-only: running tests for ticket %s", ticket.ticket_id)
        try:
            ok, error = dev._run_tests(
                deadline=__import__("time").monotonic() + 300,
            )
            if ok:
                passed += 1
                ticket.test_results = {"status": "pass"}
                logger.info("test-only: PASS for %s", ticket.ticket_id)
            else:
                failed += 1
                ticket.test_results = {"status": "fail", "error": error}
                logger.warning("test-only: FAIL for %s: %s", ticket.ticket_id, error[:200])
            store.add(ticket)
        except Exception:
            failed += 1
            logger.exception("test-only: error running tests for %s", ticket.ticket_id)

    return {"tested": len(candidates), "passed": passed, "failed": failed}


_SEVERITY_ESCALATION = {
    TicketSeverity.LOW: TicketSeverity.MEDIUM,
    TicketSeverity.MEDIUM: TicketSeverity.HIGH,
    TicketSeverity.HIGH: TicketSeverity.CRITICAL,
    TicketSeverity.CRITICAL: TicketSeverity.CRITICAL,
}


def escalate_severity(severity: TicketSeverity) -> TicketSeverity:
    """Escalate severity by one level (MEDIUM->HIGH, HIGH->CRITICAL, etc.)."""
    return _SEVERITY_ESCALATION.get(severity, severity)


def compute_fix_confidence(attempts: int, regressions: int) -> float:
    """Compute fix confidence as ``1 - (regressions / max(attempts, 1))``."""
    return 1.0 - (regressions / max(attempts, 1))


def check_regressions(
    config,
    store,
    monitor: "MonitorAgent",
) -> List[SWETicket]:
    """Check recently-resolved tickets for regressions.

    For each ticket resolved within ``config.regression_window_hours``,
    look up its fingerprint in recent logs.  If the same fingerprint
    reappears, create a new regression ticket that inherits the parent's
    context with escalated severity.

    Returns the list of newly created regression tickets.
    """
    window = getattr(config, "regression_window_hours", 24)
    recently_resolved = store.list_recently_resolved(hours=window)

    if not recently_resolved:
        logger.debug("No recently resolved tickets to check for regressions")
        return []

    regression_tickets: List[SWETicket] = []

    for parent in recently_resolved:
        fingerprint = parent.metadata.get("fingerprint")
        if not fingerprint:
            continue

        # GitHub-sourced tickets (gh-issue-N) can't regress via log scanning —
        # their fingerprints are never produced by MonitorAgent.scan().
        if fingerprint.startswith("gh-issue-"):
            continue

        # Only regress if the error fingerprint actually reappears in fresh logs.
        # BUG NOTE: do NOT use `fingerprint not in monitor._known` here —
        # known_fingerprints contains ALL tickets (including resolved), so that
        # check is always False and short-circuits the fresh-log test, causing
        # every resolved ticket to be flagged as a regression every cycle.
        if not _fingerprint_in_recent_logs(fingerprint, monitor):
            continue

        # Regression detected — build new ticket
        logger.warning(
            "Regression detected for ticket %s (fingerprint=%s)",
            parent.ticket_id,
            fingerprint,
        )

        # Compute fix confidence tracking
        parent_confidence = parent.metadata.get("fix_confidence", {})
        prev_attempts = parent_confidence.get("attempts", 1)
        prev_regressions = parent_confidence.get("regressions", 0)
        new_regressions = prev_regressions + 1
        new_attempts = prev_attempts + 1
        confidence = compute_fix_confidence(new_attempts, new_regressions)

        new_severity = escalate_severity(parent.severity)

        description_parts = [
            f"Regression of ticket {parent.ticket_id}.",
        ]
        if parent.investigation_report:
            description_parts.append(
                f"\n## Previous Investigation\n{parent.investigation_report[:1000]}"
            )
        if parent.proposed_fix:
            description_parts.append(
                f"\n## Previous Fix\n{parent.proposed_fix[:500]}"
            )

        regression_ticket = SWETicket(
            title=f"[REGRESSION] {parent.title[:100]}",
            description="\n".join(description_parts),
            severity=new_severity,
            source_module=parent.source_module,
            labels=["regression", "auto-detected"],
            metadata={
                "fingerprint": fingerprint,
                "regression_of": parent.ticket_id,
                "is_regression": True,
                "fix_confidence": {
                    "attempts": new_attempts,
                    "regressions": new_regressions,
                    "confidence": confidence,
                },
            },
        )

        regression_tickets.append(regression_ticket)

        # HITL escalation after 3+ regressions
        if new_regressions >= 3:
            try:
                notify_regression_hitl(regression_ticket)
            except Exception:
                logger.exception(
                    "HITL notification failed for regression ticket %s",
                    regression_ticket.ticket_id,
                )

    return regression_tickets


def _fingerprint_in_recent_logs(fingerprint: str, monitor: "MonitorAgent") -> bool:
    """Check if *fingerprint* appears in a fresh log scan.

    Performs a lightweight scan and checks whether the given fingerprint
    is produced by any current log lines.
    """
    # Run a scan with an empty known set so it picks up everything
    from src.swe_team.monitor_agent import MonitorAgent as _MA

    fresh_monitor = _MA(monitor._config, known_fingerprints=set())
    fresh_tickets = fresh_monitor.scan()
    fresh_fps = {
        t.metadata.get("fingerprint") for t in fresh_tickets if t.metadata.get("fingerprint")
    }
    return fingerprint in fresh_fps


def _fetch_github_issues_for_repo(
    repo: str, github_account: str, store, known_fps: set,
) -> List[SWETicket]:
    """Fetch open issues assigned to *github_account* from a single *repo*."""
    try:
        cmd = [
            "gh", "issue", "list",
            "--repo", repo,
            "--state", "open",
            "--assignee", github_account,
            "--json", "number,title,body,labels",
            "--limit", "20",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            logger.debug("gh issue list failed for %s: %s", repo, result.stderr.strip())
            return []

        issues = json.loads(result.stdout)
        new_tickets: List[SWETicket] = []
        for issue in issues:
            fingerprint = f"gh-issue-{repo}-{issue['number']}"
            if fingerprint in known_fps or fingerprint in store.known_fingerprints:
                continue

            label_names = [l.get("name", "").lower() for l in issue.get("labels", [])]
            title_lower = issue["title"].lower()
            if any("critical" in l or "p0" in l for l in label_names) or "p0" in title_lower:
                severity = TicketSeverity.CRITICAL
            elif any("high" in l or "p1" in l for l in label_names) or "p1" in title_lower:
                severity = TicketSeverity.HIGH
            elif any("low" in l for l in label_names) or "p3" in title_lower:
                severity = TicketSeverity.LOW
            else:
                severity = TicketSeverity.HIGH

            module = "unknown"
            for l in label_names:
                if "module:" in l:
                    module = l.replace("module:", "").strip()
                    break

            # Detect ticket type from labels and title
            ticket_type = TicketType.BUG  # default
            if "enhancement" in label_names or "feature" in label_names:
                ticket_type = TicketType.ENHANCEMENT
            elif any(tag in title_lower for tag in ("[foundation]", "[feature]", "[integration]")):
                ticket_type = TicketType.FEATURE

            ticket = SWETicket(
                title=f"[GH-{issue['number']}] {issue['title'][:100]}",
                description=(issue.get("body") or "")[:500],
                severity=severity,
                source_module=module,
                labels=label_names,
                ticket_type=ticket_type,
                metadata={
                    "github_issue": issue["number"],
                    "fingerprint": fingerprint,
                    "repo": repo,
                },
            )
            new_tickets.append(ticket)
            known_fps.add(fingerprint)
        return new_tickets
    except Exception:
        logger.exception("Failed to fetch GitHub issues for %s", repo)
        return []


def fetch_github_tickets(
    store,
    github_account: str = "",
    repos: Optional[List[str]] = None,
) -> List[SWETicket]:
    """Fetch open GitHub issues assigned to the team's GitHub account.

    When *repos* is provided, iterates over each repo.  Otherwise falls back
    to the single ``SWE_GITHUB_REPO`` environment variable for backward
    compatibility.
    """
    if not github_account:
        logger.debug("No github_account configured — skipping GitHub issue fetch")
        return []

    if not repos:
        single = os.environ.get("SWE_GITHUB_REPO", "")
        repos = [single] if single else []

    if not repos:
        logger.debug("No repos configured — skipping GitHub issue fetch")
        return []

    all_tickets: List[SWETicket] = []
    seen_fps: set = set()
    for repo in repos:
        tickets = _fetch_github_issues_for_repo(repo, github_account, store, seen_fps)
        all_tickets.extend(tickets)
        logger.info("Fetched %d assigned issues from %s", len(tickets), repo)

    return all_tickets


# ── GitHub ↔ Supabase reconciliation ─────────────────────────────────────────
_LAST_RECONCILE: float = 0


def _should_reconcile() -> bool:
    """Rate-limit reconciliation to once per hour."""
    global _LAST_RECONCILE
    now = time.time()
    if now - _LAST_RECONCILE > 3600:
        _LAST_RECONCILE = now
        return True
    return False


def _reconcile_github_supabase(store, config, log) -> int:
    """Reconcile open GitHub issues with Supabase ticket status.

    If a GitHub issue is still open but its Supabase ticket is closed/failed,
    reopen the Supabase ticket for retry.  This prevents tickets from permanently
    falling out of the pipeline after transient failures.
    """
    if not hasattr(store, 'list_all'):
        return 0

    all_tickets = store.list_all(limit=500)
    reopened = 0

    for ticket in all_tickets:
        gh_num = ticket.metadata.get("github_issue")
        repo = ticket.metadata.get("repo", "")

        if not gh_num or not repo:
            continue
        if ticket.status.value not in ("closed", "failed"):
            continue

        # Check if the GitHub issue is still open
        try:
            result = subprocess.run(
                ["gh", "issue", "view", str(gh_num), "--repo", repo,
                 "--json", "state", "--jq", ".state"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip() == "OPEN":
                ticket.metadata["dev_attempts"] = 0
                ticket.metadata["investigation_attempts"] = 0
                ticket.metadata["attempt_reset"] = "auto-reconcile: GH issue still open"
                ticket.transition(TicketStatus.OPEN)
                store.add(ticket)
                reopened += 1
                log.info(
                    "Reconcile: reopened ticket %s (GH#%s still open in %s)",
                    ticket.ticket_id[:12], gh_num, repo,
                )
        except (subprocess.TimeoutExpired, OSError):
            continue

    return reopened


def setup_logging(verbose: bool = False, config: Optional[Dict] = None) -> None:
    from src.swe_team.log_formatter import get_formatter, resolve_log_format

    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)

    fmt = resolve_log_format(config)
    formatter = get_formatter(fmt)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "swe_team.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    # Clear any pre-existing handlers to prevent duplicate log lines.
    # Module-level getLogger() calls before setup_logging() can attach
    # default handlers to the root logger via implicit basicConfig().
    root = logging.getLogger()
    root.handlers.clear()

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=[file_handler, stream_handler],
    )


# ---------------------------------------------------------------------------
# Stage and stale-ticket guards
# ---------------------------------------------------------------------------

_STALL_THRESHOLD_HOURS = 2


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def validate_stage_transitions(
    processed_tickets: List[SWETicket],
    *,
    expected_statuses: List[TicketStatus],
    stage_name: str,
    store,
) -> List[str]:
    """Warn when processed tickets are missing or in unexpected statuses."""
    if not processed_tickets:
        return []

    expected_values = {status.value for status in expected_statuses}
    ticket_by_id = {t.ticket_id: t for t in store.list_all()} if hasattr(store, "list_all") else {}
    warnings: List[str] = []

    for ticket in processed_tickets:
        current = ticket_by_id.get(ticket.ticket_id)
        if current is None:
            msg = (
                f"Stage transition validation [{stage_name}]: ticket {ticket.ticket_id} "
                "not found in store after processing"
            )
            logger.warning(msg)
            warnings.append(msg)
            continue
        if current.status.value not in expected_values:
            msg = (
                f"Stage transition validation [{stage_name}]: ticket {ticket.ticket_id} "
                f"ended in {current.status.value}, expected one of {sorted(expected_values)}"
            )
            logger.warning(msg)
            warnings.append(msg)
    return warnings


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Parse ISO timestamp into timezone-aware datetime."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _reset_blocked_tickets(
    store,
    *,
    blocked_ticket_timeout_hours: int = 4,
    blocked_ticket_escalation_hours: int = 24,
) -> List[SWETicket]:
    """Reset blocked tickets when blockers resolve/stall, escalate long-blocked tickets."""
    blocked_tickets = (
        store.get_blocked_tickets()
        if hasattr(store, "get_blocked_tickets")
        else store.list_by_status(TicketStatus.BLOCKED)
    )
    if not blocked_tickets:
        return []

    now = datetime.now(timezone.utc)
    touched: List[SWETicket] = []

    for ticket in blocked_tickets:
        if ticket.status != TicketStatus.BLOCKED or not ticket.blocked_by:
            continue

        changed = False

        for blocker_id in list(ticket.blocked_by):
            blocker = store.get(blocker_id)
            if not blocker:
                continue

            if blocker.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
                logger.info(
                    "Auto-unblocking ticket %s: blocker %s is %s",
                    ticket.ticket_id,
                    blocker_id,
                    blocker.status.value,
                )
                if hasattr(store, "unblock_ticket"):
                    store.unblock_ticket(ticket.ticket_id, blocker_id)
                    refreshed = store.get(ticket.ticket_id)
                    if refreshed:
                        ticket = refreshed
                else:
                    ticket.blocked_by = [b for b in ticket.blocked_by if b != blocker_id]
                    if not ticket.blocked_by:
                        ticket.transition(TicketStatus.TRIAGED)
                    store.add(ticket)
                changed = True
                continue

            blocker_heartbeat = _parse_iso_datetime(
                blocker.metadata.get("last_heartbeat") or blocker.updated_at
            )
            if not blocker_heartbeat:
                continue

            blocker_stuck_hours = (now - blocker_heartbeat).total_seconds() / 3600
            if blocker_stuck_hours >= blocked_ticket_timeout_hours:
                logger.warning(
                    "Auto-unblocking ticket %s: blocker %s stuck in %s for %.1fh (timeout=%dh)",
                    ticket.ticket_id,
                    blocker_id,
                    blocker.status.value,
                    blocker_stuck_hours,
                    blocked_ticket_timeout_hours,
                )
                if hasattr(store, "unblock_ticket"):
                    store.unblock_ticket(ticket.ticket_id, blocker_id)
                    refreshed = store.get(ticket.ticket_id)
                    if refreshed:
                        ticket = refreshed
                else:
                    ticket.blocked_by = [b for b in ticket.blocked_by if b != blocker_id]
                    if not ticket.blocked_by:
                        ticket.transition(TicketStatus.TRIAGED)
                    store.add(ticket)
                timeout_events = ticket.metadata.setdefault("blocked_timeout_events", [])
                timeout_events.append(
                    {
                        "blocker_id": blocker_id,
                        "blocker_status": blocker.status.value,
                        "blocked_hours": round(blocker_stuck_hours, 2),
                        "timeout_hours": blocked_ticket_timeout_hours,
                        "at": now.isoformat(),
                    }
                )
                store.add(ticket)
                changed = True

        blocked_since = _parse_iso_datetime(ticket.updated_at)
        blocked_hours_total = (
            (now - blocked_since).total_seconds() / 3600
            if blocked_since
            else None
        )
        if (
            blocked_hours_total is not None
            and blocked_hours_total >= blocked_ticket_escalation_hours
            and not ticket.metadata.get("needs_hitl")
        ):
            ticket.metadata["needs_hitl"] = True
            ticket.metadata["hitl_reason"] = (
                f"Blocked for {blocked_hours_total:.1f}h "
                f"(threshold {blocked_ticket_escalation_hours}h)"
            )
            ticket.metadata["blocked_timeout_escalated_at"] = now.isoformat()
            ticket.metadata["blocked_timeout_escalation_hours"] = blocked_ticket_escalation_hours
            store.add(ticket)
            logger.warning(
                "HITL escalation: ticket %s blocked for %.1fh (threshold=%dh)",
                ticket.ticket_id,
                blocked_hours_total,
                blocked_ticket_escalation_hours,
            )
            changed = True

        if changed:
            latest = store.get(ticket.ticket_id) or ticket
            touched.append(latest)

    return touched


def detect_stalled_tickets(store) -> List[SWETicket]:
    """Find tickets stuck in investigating/in_development for >2 hours.

    Resets them to OPEN with a stall note.  Returns the list of reset tickets.
    """
    stalled: List[SWETicket] = []
    now = datetime.now(timezone.utc)
    stall_statuses = {TicketStatus.INVESTIGATING, TicketStatus.IN_DEVELOPMENT}

    for ticket in store.list_all():
        if ticket.status not in stall_statuses:
            continue

        # Use last_heartbeat from metadata, falling back to updated_at
        heartbeat_iso = ticket.metadata.get("last_heartbeat") or ticket.updated_at
        try:
            heartbeat = datetime.fromisoformat(heartbeat_iso)
        except (ValueError, TypeError):
            continue

        # Ensure timezone-aware comparison
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)

        hours_since = (now - heartbeat).total_seconds() / 3600
        if hours_since > _STALL_THRESHOLD_HOURS:
            logger.warning(
                "Stalled ticket %s: status=%s, no heartbeat for %.1f hours — resetting to OPEN",
                ticket.ticket_id,
                ticket.status.value,
                hours_since,
            )
            ticket.metadata["stall_detected"] = {
                "previous_status": ticket.status.value,
                "stalled_hours": round(hours_since, 2),
                "detected_at": now.isoformat(),
            }
            ticket.transition(TicketStatus.OPEN)
            try:
                store.add(ticket)
            except Exception:
                logger.exception("Failed to persist stall reset for %s", ticket.ticket_id)
            stalled.append(ticket)

    return stalled


def _reset_stale_tickets(store, config) -> List[SWETicket]:
    """Recover stale tickets based on configured timeouts."""
    now = datetime.now(timezone.utc)
    timeouts = getattr(config, "stale_ticket_timeouts", None)
    inv_hours = int(getattr(timeouts, "investigating_hours", 4))
    dev_hours = int(getattr(timeouts, "in_development_hours", 2))
    review_hours = int(getattr(timeouts, "in_review_hours", 24))
    reset: List[SWETicket] = []

    for ticket in store.list_all():
        age_source = ticket.metadata.get("last_heartbeat") or ticket.updated_at
        last_seen = _parse_timestamp(age_source)
        if last_seen is None:
            continue

        stale_hours = (now - last_seen).total_seconds() / 3600
        if ticket.status == TicketStatus.IN_DEVELOPMENT and stale_hours > dev_hours:
            ticket.metadata["stall_detected"] = {
                "previous_status": TicketStatus.IN_DEVELOPMENT.value,
                "stalled_hours": round(stale_hours, 2),
                "detected_at": now.isoformat(),
            }
            ticket.transition(TicketStatus.INVESTIGATION_COMPLETE)
        elif ticket.status == TicketStatus.INVESTIGATING and stale_hours > inv_hours:
            ticket.metadata["stall_detected"] = {
                "previous_status": TicketStatus.INVESTIGATING.value,
                "stalled_hours": round(stale_hours, 2),
                "detected_at": now.isoformat(),
            }
            ticket.transition(TicketStatus.TRIAGED)
        elif ticket.status == TicketStatus.IN_REVIEW and stale_hours > review_hours:
            ticket.metadata["needs_hitl"] = True
            ticket.metadata["hitl_reason"] = (
                f"Ticket has been IN_REVIEW for {stale_hours:.1f}h (> {review_hours}h timeout)."
            )
            ticket.metadata["stall_detected"] = {
                "previous_status": TicketStatus.IN_REVIEW.value,
                "stalled_hours": round(stale_hours, 2),
                "detected_at": now.isoformat(),
            }
        else:
            continue

        try:
            store.add(ticket)
            reset.append(ticket)
        except Exception:
            logger.exception("Failed to persist stale-ticket recovery for %s", ticket.ticket_id)

    return reset


# ---------------------------------------------------------------------------
# Progress log
# ---------------------------------------------------------------------------

_PROGRESS_LOG_PATH = PROJECT_ROOT / "swe_progress.txt"


def append_progress_log(
    result: Dict[str, Any],
    *,
    done: str = "",
    next_step: str = "",
    blockers: str = "",
) -> None:
    """Append a structured entry to swe_progress.txt (append-only)."""
    ts = datetime.now(timezone.utc).isoformat()
    new_count = result.get("new_tickets", 0)
    open_count = result.get("open_tickets", 0)
    verdict = result.get("gate_verdict", "N/A")

    entry = (
        f"--- CYCLE {ts} | Tickets: {new_count}/{open_count} | Gate: {verdict}\n"
        f"DONE: {done or 'Cycle completed'}\n"
        f"NEXT: {next_step or 'Continue monitoring'}\n"
        f"BLOCKERS: {blockers or 'None'}\n"
        f"---\n"
    )

    try:
        with open(_PROGRESS_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(entry)
    except OSError:
        logger.exception("Failed to write progress log to %s", _PROGRESS_LOG_PATH)


def _send_preflight_alert(failures: List[str]) -> None:
    """Send a Telegram alert when preflight checks fail."""
    from src.swe_team.notifier import _send

    lines = [
        "<b>\u26a0\ufe0f SWE Preflight FAILED</b>",
        "",
        "The SWE Team runner aborted a cycle because pre-flight "
        "validation failed:",
        "",
    ]
    for f in failures:
        lines.append(f"  \u2022 {f}")
    _send("\n".join(lines))


def _filter_dependency_ready_tickets(
    *,
    all_tickets: List[SWETicket],
    candidates: List[SWETicket],
    stage: str,
) -> List[SWETicket]:
    if not candidates:
        return candidates

    graph = DependencyGraph(all_tickets)
    ready = graph.get_ready_tickets(candidates)
    ready_ids = {ticket.ticket_id for ticket in ready}
    blocked = [ticket for ticket in candidates if ticket.ticket_id not in ready_ids]

    for ticket in blocked:
        logger.info(
            "%s dependency-blocked: ticket %s waiting on %s",
            stage,
            ticket.ticket_id,
            ", ".join(graph.unresolved_dependencies(ticket.ticket_id)) or "unknown blockers",
        )
    if blocked:
        logger.info(
            "%s dependency filter: %d blocked, %d eligible",
            stage,
            len(blocked),
            len(ready),
        )
    return ready


_WORKFLOW_NODE_TO_PHASE = {
    "monitor": "monitor",
    "triage": "triage",
    "investigate": "investigate",
    "develop": "develop",
    "review": "review",
    "notify": "notify",
    "stability_gate": "gate",
}


def _resolve_workflow_phase_plan(config, store) -> tuple[Any, List[str], set[str]]:
    """Load team workflow and derive ordered executable phases."""
    team_id = config.team_id or os.environ.get("SWE_TEAM_ID", "")
    workflow = load_workflow_definition(team_id=team_id, store=store)
    ordered_nodes = PipelineExecutor(workflow).topological_order()
    ordered_phases: List[str] = []
    for node_id in ordered_nodes:
        node = next((n for n in workflow.nodes if n.id == node_id), None)
        if node is None:
            continue
        phase = _WORKFLOW_NODE_TO_PHASE.get(node.type)
        if phase and phase not in ordered_phases:
            ordered_phases.append(phase)
    return workflow, ordered_phases, set(ordered_phases)


def run_cycle(
    config,
    store,
    dry_run: bool = False,
    creative: bool = False,
    sandbox_repos_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one monitor -> triage -> gate cycle."""

    cooldown_manager = EngineCooldownManager(
        store=store,
        team_id=os.environ.get("SWE_TEAM_ID", "default"),
    )

    # Legacy fallback for non-Supabase deployments that do not support
    # per-engine cooldown state.
    if not hasattr(store, "list_engine_cooldowns"):
        remaining = check_cooldown_lockfile()
        if remaining is not None:
            logger.warning(
                "Cooldown lockfile active — skipping cycle (%.0fs / %.1f min remaining)",
                remaining,
                remaining / 60,
            )
            return {"gate_verdict": "rate_limit_cooldown", "cooldown_remaining_s": round(remaining)}

    session_tag = make_session_tag(cycle=True)
    log_session_start(session_tag)

    # Rate limit tracker — shared across investigator and developer within this cycle
    rate_limit_tracker = RateLimitTracker()

    # 0. Supabase keep-alive — prevent free-tier pause from inactivity
    if isinstance(store, SupabaseTicketStore):
        try:
            sent = store.keep_alive()
            if sent:
                logger.info("Supabase keep-alive ping sent at cycle start")
        except Exception:
            logger.warning("Supabase keep-alive check failed (non-fatal)", exc_info=True)

    # 0.5. Model probe — validate BASE_LLM models, auto-patch env before any API calls
    try:
        probe = ModelProbe()
        patches = probe.validate_and_patch_env()
        if patches:
            logger.warning("model_probe: patched env vars: %s", patches)
        else:
            logger.debug("model_probe: all configured models available")
    except Exception:
        logger.warning("model_probe: validation failed (non-fatal)", exc_info=True)

    # 0.6. Preflight validation — abort if running in wrong context
    _sandbox_paths = [
        Path(r["local_path"]) for r in config.repos if r.get("local_path")
    ] if config.repos else []
    preflight = PreflightCheck(
        expected_git_name=os.environ.get("SWE_EXPECTED_GIT_NAME"),
        expected_git_email=os.environ.get("SWE_EXPECTED_GIT_EMAIL"),
        expected_github_account=config.github_account or None,
        expected_repo_root=None,  # Runner lives in engine dir, not a sandbox repo
        required_env_vars=["SWE_TEAM_ID", "SWE_GITHUB_REPO"],
        sandbox_paths=_sandbox_paths,
    )
    preflight_result = preflight.run()
    if not preflight_result.passed:
        logger.error("Preflight FAILED — skipping cycle: %s", preflight_result.summary())
        try:
            _send_preflight_alert(preflight_result.failures)
        except Exception:
            logger.exception("Failed to send preflight failure alert")
        return {
            "new_tickets": 0,
            "triaged": 0,
            "investigated": 0,
            "gate_verdict": "preflight_failed",
            "preflight_failures": preflight_result.failures,
        }

    # 0.6b. RBAC Role Gate — determine which modules this instance may execute.
    # Each VM has a team role (full, developer, investigator) defined in
    # config.teams[SWE_TEAM_ID].role. The runner MUST refuse to execute
    # modules the instance is not authorized for. This prevents worker VMs
    # from running orchestrator actions, closing issues, or merging PRs.
    _team_id = config.team_id or os.environ.get("SWE_TEAM_ID", "")
    _team_short = _team_id.replace("swe-squad-", "").replace("swe-squad_", "")
    _teams_dict = config.teams if isinstance(config.teams, dict) else {}
    _team_cfg = _teams_dict.get(_team_short, {})
    if isinstance(_team_cfg, dict):
        _team_role = _team_cfg.get("role", "investigator")
    else:
        _team_role = getattr(_team_cfg, "role", "investigator") if _team_cfg else "investigator"
    _ROLE_PERMISSIONS = {
        "investigator": {"monitor", "triage", "investigate", "github_scan"},
        "developer": {"monitor", "triage", "investigate", "develop", "github_scan"},
        "reviewer": {"monitor", "triage", "investigate", "review", "github_scan"},
        "full": {"monitor", "triage", "investigate", "develop", "review",
                 "orchestrate", "close_issues", "merge", "github_scan"},
    }
    _allowed_phases = _ROLE_PERMISSIONS.get(_team_role, _ROLE_PERMISSIONS["investigator"])
    logger.info(
        "RBAC role gate: team=%s, role=%s, allowed_phases=%s",
        _team_id, _team_role, sorted(_allowed_phases),
    )
    workflow_def, workflow_phase_order, workflow_phase_set = _resolve_workflow_phase_plan(config, store)
    if workflow_phase_set:
        _allowed_phases = _allowed_phases.intersection(workflow_phase_set)
    logger.info(
        "Workflow phase gate: workflow=%s (%s) phase_order=%s effective_allowed=%s",
        workflow_def.name or "unnamed",
        workflow_def.id,
        workflow_phase_order if workflow_phase_order else [],
        sorted(_allowed_phases),
    )

    # 0.7. Stale ticket detection — reset/flag stuck tickets
    stale_tickets = _reset_stale_tickets(store, config)
    if stale_tickets:
        logger.info("Recovered %d stale ticket(s)", len(stale_tickets))

    # 0.7b. Blocked-ticket timeout management
    blocked_resets = _reset_blocked_tickets(
        store,
        blocked_ticket_timeout_hours=config.cycle.blocked_ticket_timeout_hours,
        blocked_ticket_escalation_hours=config.cycle.blocked_ticket_escalation_hours,
    )
    if blocked_resets:
        logger.info("Updated %d blocked ticket(s)", len(blocked_resets))

    # 0.8. Dynamic throttle resolution — compute effective cycle limits
    try:
        _all_open = store.list_open() if hasattr(store, "list_open") else []
        _backlog_size = len(_all_open)
        _backlog_critical = sum(
            1 for t in _all_open if t.severity.value == "critical"
        )
        _now_utc = datetime.now(timezone.utc)
        _ctx = ThrottleContext(
            now_utc=_now_utc,
            api_usage_pct=0.0,
            api_days_to_reset=days_until_weekly_reset(_now_utc),
            backlog_size=_backlog_size,
            backlog_critical=_backlog_critical,
            rate_limit_cooling=rate_limit_tracker.is_cooling_down(),
        )
        if config.throttle.enabled:
            _policy = ThrottlePolicy(
                base_config=config.cycle,
                adapters=[
                    TimeBasedAdapter(config.throttle),
                    CapacityAdapter(config.throttle),
                    DemandAdapter(config.throttle),
                ],
            )
            effective_cycle = _policy.resolve(_ctx)
            logger.info(
                "Throttle active: multiplier=%.3fx, severity=%s, reasons=%s",
                effective_cycle.effective_multiplier,
                effective_cycle.severity_filter,
                "; ".join(effective_cycle.reasons),
            )
        else:
            effective_cycle = config.cycle
            logger.debug("Throttle disabled — using static cycle config")
    except Exception:
        logger.warning("Dynamic throttle resolution failed (non-fatal)", exc_info=True)
        effective_cycle = config.cycle

    # 0.9. Circuit Breaker — skip cycle if failure rate is too high
    circuit = CircuitBreaker()
    if circuit.is_paused:
        logger.error(
            "Circuit breaker is PAUSED (current failure rate %.1f%%; trip threshold 80.0%%). "
            "Skipping cycle to prevent runaway.",
            circuit.failure_rate * 100
        )
        return {
            "new_tickets": 0,
            "triaged": 0,
            "investigated": 0,
            "gate_verdict": "circuit_paused",
            "circuit_failure_rate": circuit.failure_rate,
        }

    # 0.9. Auto-accept GitHub collaboration invites (if enabled)
    if getattr(config, "auto_accept_invites", False):
        try:
            from src.swe_team.github_invites import accept_pending_invites
            _invite_allowlist = getattr(config, "invite_allowlist", None) or None
            _accepted = accept_pending_invites(
                github_account=config.github_account,
                allowlist=_invite_allowlist,
                dry_run=dry_run,
            )
            if _accepted:
                logger.info(
                    "Auto-accepted %d GitHub collaboration invite(s)", len(_accepted)
                )
        except Exception:
            logger.warning("GitHub invite auto-accept failed (non-fatal)", exc_info=True)

    # 0a. Collect remote logs before scanning
    from src.swe_team.remote_logs import collect_remote_logs
    try:
        remote_dirs = collect_remote_logs(nodes=config.monitor.remote_workers or [])
        if remote_dirs:
            config.monitor.log_directories.extend(remote_dirs)
            logger.info("Added %d remote log directories", len(remote_dirs))
    except Exception:
        logger.exception("Remote log collection failed — scanning local only")

    # 0b. Pick up GitHub issues assigned to this team's GitHub account
    # Use explicit github_repos list (not sandbox repos) to avoid cross-contamination
    _gh_repos = config.github_repos or []
    if not _gh_repos:
        # Backward compatibility: fall back to SWE_GITHUB_REPO env var
        _fallback = os.environ.get("SWE_GITHUB_REPO", "")
        _gh_repos = [_fallback] if _fallback else []
    gh_tickets = fetch_github_tickets(
        store, github_account=config.github_account, repos=_gh_repos,
    )
    if gh_tickets:
        logger.info("Fetched %d new GitHub issue ticket(s)", len(gh_tickets))
        for gt in gh_tickets:
            issue_num = gt.metadata.get("github_issue")
            if issue_num and not dry_run:
                _cid = _post_or_update_status(
                    gt,
                    f"🤖 **SWE Squad picked up this issue.**\n\n"
                    f"Status: `TRIAGED` — queued for investigation.\n"
                    f"Team: `{config.team_id}` | Account: `{config.github_account}`",
                    repo=gt.metadata.get("repo", ""),
                )
                if _cid and not gt.metadata.get("progress_comment_id"):
                    gt.metadata["progress_comment_id"] = _cid
                    store.add(gt)

    # 0c. Label-based GitHub issue scan — autonomous backlog pickup (#281)
    # Now iterates over ALL configured repos (multi-repo support).
    try:
        _scan_repos = _gh_repos or []
        if not _scan_repos:
            _fallback = os.environ.get("SWE_GITHUB_REPO", "")
            if _fallback:
                _scan_repos = [_fallback]

        if _scan_repos:
            # Build repo-scoped fingerprints so issues with the same number
            # in different repos do not block each other.
            _known_fps: set = set()
            for _t in store.list_all():
                _gh_num = _t.metadata.get("github_issue")
                _t_repo = _t.metadata.get("repo") or _t.metadata.get("github_repo", "")
                if _gh_num is not None and _t_repo:
                    _known_fps.add(f"gh-issue-{_t_repo}-{_gh_num}")
            for _gt in (gh_tickets or []):
                _gh_num = _gt.metadata.get("github_issue")
                _gt_repo = _gt.metadata.get("repo") or _gt.metadata.get("github_repo", "")
                if _gh_num is not None and _gt_repo:
                    _known_fps.add(f"gh-issue-{_gt_repo}-{_gh_num}")

            for _gh_repo in _scan_repos:
                _scanner_config = GitHubScannerConfig(repo=_gh_repo, enabled=True, github_account=config.github_account)
                _scanner = GitHubIssueScanner(_scanner_config, known_fingerprints=_known_fps)
                _scanner_tickets = _scanner.scan()
                if _scanner_tickets:
                    logger.info("GitHub scanner added %d tickets from %s", len(_scanner_tickets), _gh_repo)
                    _stored_tickets = []
                    for _st in _scanner_tickets:
                        _st.metadata["repo"] = _gh_repo
                        if not dry_run:
                            store.add(_st)
                            _stored_tickets.append(_st)
                        _issue_num = _st.metadata.get("github_issue")
                        if _issue_num and not dry_run:
                            _post_or_update_status(
                                _st,
                                f"SWE Squad picked up this issue (label scan).\n\n"
                                f"Status: `OPEN` -- queued for triage.\n"
                                f"Team: `{config.team_id}`",
                                repo=_gh_repo,
                            )
                    # Persist dedup state only after successful store.add()
                    if _stored_tickets:
                        _scanner.mark_stored(_stored_tickets)
                    gh_tickets = list(gh_tickets or []) + _scanner_tickets
        else:
            logger.debug("No repos configured -- skipping label-based GitHub scan")
    except Exception:
        logger.warning("GitHub label scan failed (non-fatal)", exc_info=True)

    # 0d. Reconcile GitHub ↔ Supabase ticket status (once per hour)
    if not dry_run and _should_reconcile():
        try:
            reopened = _reconcile_github_supabase(store, config, logger)
            if reopened:
                logger.info("Reconciliation reopened %d ticket(s)", reopened)
        except Exception:
            logger.warning("GitHub-Supabase reconciliation failed (non-fatal)", exc_info=True)

    # 1. Monitor: scan logs for new errors
    monitor = MonitorAgent(config.monitor, known_fingerprints=store.known_fingerprints)
    if "monitor" in _allowed_phases:
        new_tickets = monitor.scan()
    else:
        logger.info("Workflow/RBAC: skipping monitor phase")
        new_tickets = []

    # 1a. Post-fix regression check on recently resolved tickets
    if not dry_run and "monitor" in _allowed_phases:
        try:
            regression_tickets = check_regressions(config, store, monitor)
            if regression_tickets:
                logger.info("Detected %d regression(s)", len(regression_tickets))
                new_tickets.extend(regression_tickets)
        except Exception:
            logger.exception("Regression check failed — continuing with normal cycle")

    # Merge GitHub-sourced tickets with log-sourced tickets
    if gh_tickets:
        new_tickets.extend(gh_tickets)

    swe_events: List[SWEEvent] = []

    # Check backlog even when no new tickets detected this cycle
    if not new_tickets:
        logger.info("No new issues detected this cycle — checking stored backlog")

    # 1a. Severity filter — drop tickets below configured threshold
    _SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    sev_floor = _SEV_RANK.get(effective_cycle.severity_filter.lower(), 2)
    before_filter = len(new_tickets)
    new_tickets = [
        t for t in new_tickets
        if _SEV_RANK.get(t.severity.value, 0) >= sev_floor
    ]
    if len(new_tickets) < before_filter:
        logger.info(
            "Severity filter (%s+): dropped %d ticket(s), %d remaining",
            effective_cycle.severity_filter, before_filter - len(new_tickets), len(new_tickets),
        )

    # 1b. Per-cycle cap — process highest-severity tickets first
    if len(new_tickets) > effective_cycle.max_new_tickets_per_cycle:
        new_tickets.sort(key=lambda t: _SEV_RANK.get(t.severity.value, 0), reverse=True)
        skipped = len(new_tickets) - effective_cycle.max_new_tickets_per_cycle
        new_tickets = new_tickets[: effective_cycle.max_new_tickets_per_cycle]
        logger.info(
            "Per-cycle cap (%d): deferred %d ticket(s) to next cycle",
            effective_cycle.max_new_tickets_per_cycle, skipped,
        )

    if not dry_run:
        for ticket in new_tickets:
            swe_events.append(
                SWEEvent.issue_detected(
                    ticket_id=ticket.ticket_id,
                    source_agent="swe_monitor",
                    error_summary=ticket.title[:120],
                    module=ticket.source_module or "",
                    severity=ticket.severity.value,
                )
            )

    # 2. Triage: assign severity and route to specialists
    if "triage" in _allowed_phases:
        triage = TriageAgent(config)
        triaged = triage.triage_batch(new_tickets)
    else:
        logger.info("Workflow/RBAC: skipping triage phase")
        triaged = []

    for ticket in triaged:
        logger.info(
            "  [%s] %s -> assigned to %s",
            ticket.severity.value,
            ticket.title[:80],
            ticket.assigned_to or "unassigned",
        )
        if not dry_run:
            try:
                store.add(ticket)
            except Exception:
                logger.exception("Failed to persist ticket %s", ticket.ticket_id)
            swe_events.append(
                SWEEvent.triage_complete(
                    ticket_id=ticket.ticket_id,
                    source_agent="swe_triage",
                    assigned_to=ticket.assigned_to or "",
                    severity=ticket.severity.value,
                )
            )

    # 3. Notify on new HIGH/CRITICAL tickets
    if triaged and not dry_run:
        important = [t for t in triaged if t.severity.value in ("critical", "high")]
        if important:
            try:
                notify_new_tickets(important)
            except Exception:
                logger.exception("Failed to send new ticket notifications")

    # 4. Create GitHub issues for CRITICAL tickets
    for ticket in triaged:
        if ticket.severity.value == "critical" and not dry_run:
            try:
                existing = find_existing_issue(ticket)
            except Exception:
                logger.exception("GitHub issue lookup failed for %s", ticket.ticket_id)
                existing = None
            if not existing:
                issue_num = create_github_issue(ticket)
                if issue_num:
                    ticket.metadata["github_issue"] = issue_num
                    try:
                        store.add(ticket)  # persist the updated metadata
                    except Exception:
                        logger.exception("Failed to persist GitHub metadata for %s", ticket.ticket_id)

    # 4b. HITL escalation — tickets triage flagged as needing human action
    if not dry_run:
        for ticket in triaged:
            if ticket.metadata.get("needs_hitl"):
                issue_num = ticket.metadata.get("github_issue")
                repo = ticket.metadata.get("repo", "")
                reason = ticket.metadata.get("hitl_reason", "Requires human intervention")
                if issue_num:
                    try:
                        escalate_to_human(issue_num, ticket.ticket_id, reason, repo=repo)
                    except Exception:
                        logger.exception("escalate_to_human failed for %s", ticket.ticket_id)
                logger.warning(
                    "HITL ticket %s excluded from automation queue | %s",
                    ticket.ticket_id, reason[:80],
                )

    # 5. Trajectory distiller: attempt deterministic fixes before investigation
    if not dry_run:
        validate_stage_transitions(
            triaged,
            expected_statuses=[TicketStatus.TRIAGED],
            stage_name="triage",
            store=store,
        )

    automated: List[SWETicket] = []
    if triaged and not dry_run:
        distiller = TrajectoryDistiller(repo_root=PROJECT_ROOT)
        for ticket in triaged:
            if distiller.run_automation(ticket):
                automated.append(ticket)
                try:
                    store.add(ticket)
                except Exception:
                    logger.exception("Failed to persist automation for %s", ticket.ticket_id)

    # 4b. Backlog pickup — pull existing OPEN/TRIAGED tickets from the store
    # so previous cycles' imported backlog is worked on, not just new detections.
    triaged_ids = {t.ticket_id for t in triaged}
    automated_ids = {t.ticket_id for t in automated}
    try:
        stored_open = [
            t for t in store.list_all()
            if t.status in (TicketStatus.OPEN, TicketStatus.TRIAGED)
            and t.ticket_id not in triaged_ids
            and t.ticket_id not in automated_ids
            and _SEV_RANK.get(t.severity.value, 0) >= sev_floor
            and not t.metadata.get("needs_hitl")  # skip tickets awaiting human action
            and not t.investigation_report  # skip already-investigated tickets (_eligible() would reject them anyway)
            and "UMBRELLA" not in (t.title or "").upper()  # skip umbrella tracking issues (_eligible() blocks these too)
        ]
        # Sort by graph-aware priority score if knowledge store is available,
        # otherwise fall back to severity rank
        try:
            from src.swe_team.graph_scoring import priority_score
            kg_store = None
            if isinstance(store, SupabaseTicketStore):
                try:
                    from src.swe_team.knowledge_store import KnowledgeGraphStore
                    kg_store = KnowledgeGraphStore(
                        supabase_url=os.environ.get("SUPABASE_URL", ""),
                        supabase_key=os.environ.get("SUPABASE_ANON_KEY", ""),
                        team_id=config.team_id,
                    )
                except Exception:
                    pass
            stored_open.sort(
                key=lambda t: priority_score(t, graph_store=kg_store),
                reverse=True,
            )
            if kg_store:
                logger.info("Backlog sorted by graph-aware priority score (KnowledgeGraphStore active)")
        except Exception:
            logger.debug("Graph scoring unavailable — falling back to severity sort", exc_info=True)
            stored_open.sort(key=lambda t: _SEV_RANK.get(t.severity.value, 0), reverse=True)
        if stored_open:
            logger.info(
                "Backlog pickup: %d existing OPEN/TRIAGED ticket(s) eligible for investigation",
                len(stored_open),
            )
    except Exception:
        logger.warning("Backlog pickup failed (non-fatal)", exc_info=True)
        stored_open = []

    # 5. Investigation (severity-filtered, capped by cycle config)
    # RBAC: skip if role does not include "investigate"
    if "investigate" not in _allowed_phases:
        logger.info("RBAC: skipping investigation phase (role=%s)", _team_role)
        investigated = []
        pending_investigation = []
    else:
        pass  # continue below
    investigated: List[SWETicket] = []
    # Merge this cycle's triaged with backlog — new tickets get priority
    this_cycle_ids = {t.ticket_id for t in triaged if t.ticket_id not in automated_ids}
    pending_investigation = (
        [t for t in triaged if t.ticket_id not in automated_ids]
        + [t for t in stored_open if t.ticket_id not in this_cycle_ids]
    )

    # ---- Phase-based dependency blocking ----
    # FEATURE tickets are blocked by unresolved FOUNDATION tickets in the same repo.
    # INTEGRATION tickets are blocked by unresolved FEATURE tickets in the same repo.
    _phase_order = {"foundation": 0, "feature": 1, "integration": 2}

    def _detect_phase(t: SWETicket) -> str:
        """Return the phase tag from title or labels, or empty string."""
        title_lower = (t.title or "").lower()
        labels_lower = [l.lower() for l in getattr(t, "labels", []) or []]
        for phase in _phase_order:
            if (
                f"[{phase}]" in title_lower
                or phase in title_lower
                or any(phase in l for l in labels_lower)
            ):
                return phase
        return ""

    def _ticket_repo(t: SWETicket) -> str:
        return t.metadata.get("repo", "") or ""

    # Build unresolved phases per repo
    _unresolved_phases: Dict[str, set] = {}
    for t in pending_investigation:
        ph = _detect_phase(t)
        if ph:
            repo = _ticket_repo(t)
            _unresolved_phases.setdefault(repo, set()).add(ph)

    # Determine which phases are blocked per repo
    _blocked_phases: Dict[str, set] = {}
    for repo, phases in _unresolved_phases.items():
        blocked = set()
        if "foundation" in phases:
            blocked.add("feature")
            blocked.add("integration")
        if "feature" in phases:
            blocked.add("integration")
        _blocked_phases[repo] = blocked

    # Filter out blocked tickets
    _unblocked = []
    _blocked_count = 0
    for t in pending_investigation:
        ph = _detect_phase(t)
        repo = _ticket_repo(t)
        if ph and ph in _blocked_phases.get(repo, set()):
            _blocked_count += 1
            logger.info(
                "Phase-blocked: ticket %s (%s/%s) — waiting for prerequisite phase in repo %s",
                t.ticket_id, ph, (t.title or "")[:60], repo or "(default)",
            )
            continue
        _unblocked.append(t)
    if _blocked_count:
        logger.info("Phase dependency filter: %d tickets blocked, %d eligible", _blocked_count, len(_unblocked))
    pending_investigation = _unblocked
    try:
        _investigation_candidate_count = len(pending_investigation)
        pending_investigation = _filter_dependency_ready_tickets(
            all_tickets=store.list_all(),
            candidates=pending_investigation,
            stage="Investigation",
        )
    except Exception:
        logger.warning(
            "Dependency filter failed for investigation batch (non-fatal, candidates=%d); proceeding without dependency filtering",
            _investigation_candidate_count,
            exc_info=True,
        )

    # Check if there is development work in the store even when investigation backlog is empty
    _has_dev_backlog = False
    if not pending_investigation and not triaged and not new_tickets:
        try:
            _has_dev_backlog = any(
                t.status == TicketStatus.INVESTIGATION_COMPLETE
                and t.investigation_report
                and not (t.assigned_to or "").startswith("human:")
                and not t.metadata.get("needs_hitl")
                for t in store.list_all()
            )
        except Exception:
            pass
    if not pending_investigation and not triaged and not new_tickets and not _has_dev_backlog:
        logger.info("Nothing to do this cycle (no new tickets, no backlog)")
        return {"new_tickets": 0, "triaged": 0, "investigated": 0, "gate_verdict": "N/A", "rate_limit_events": len(rate_limit_tracker.recent_events(hours=1))}
    # Build separate CodingEngines for investigation vs development.
    # Investigation is text-only analysis (no tools by design).
    # Development needs full tool access for code changes.
    _ce_cfg = getattr(config, "providers", {})
    _ce_cfg = _ce_cfg.get("coding_engine", {}) if isinstance(_ce_cfg, dict) else {}
    _engine_provider = _ce_cfg.get("provider", "claude")

    # ── Per-agent engine routing ─────────────────────────────────────────────
    # Each agent role can use a different Claude binary (claude/claudez/claudep).
    # Configured in swe_team.yaml under engine_routing:, overridable via env.
    _engine_routing = getattr(config, "engine_routing", {})
    if not isinstance(_engine_routing, dict):
        _engine_routing = {}
    _inv_binary = os.environ.get("SWE_ENGINE_INVESTIGATE") or _engine_routing.get("investigate", "claudep")
    _dev_binary = os.environ.get("SWE_ENGINE_DEVELOP") or _engine_routing.get("develop", "claude")
    _inv_model = os.environ.get("SWE_INVESTIGATION_MODEL") or config.models.t2_standard
    logger.info(
        "Engine routing: investigate=%s (model=%s), develop=%s (model=%s)",
        _inv_binary, _inv_model, _dev_binary, config.models.t2_standard,
    )

    # Investigation: read-only analysis + research tools (no Edit/Write)
    _inv_tools = (
        "Read,Grep,Glob,"
        "Bash(git log:*),Bash(git diff:*),Bash(git show:*),Bash(git blame:*),"
        "Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(wc:*)"
    )
    investigation_engine = resolve_engine(_engine_provider, {
        **_ce_cfg,
        "default_model": _inv_model,
        "claude_path": shutil.which(_inv_binary) or _inv_binary,
        "allowed_tools": _inv_tools,
    })
    # Development: full tool access for code changes
    _dev_tools = "Read,Edit,Write,Bash(git:*),Bash(pytest:*),Bash(python3:*),Bash(npm:*),Bash(npx:*),Grep,Glob"
    development_engine = resolve_engine(_engine_provider, {
        **_ce_cfg,
        "default_model": config.models.t2_standard,
        "allowed_tools": _dev_tools,
        "claude_path": shutil.which(_dev_binary) or _dev_binary,
    })
    # Health probing for engines currently in cooldown. Successful probes transition
    # to "recovering" so they can be reintroduced gradually.
    cooldown_manager.probe_if_due(
        getattr(investigation_engine, "name", _inv_binary),
        getattr(investigation_engine, "health_check", None),
    )
    cooldown_manager.probe_if_due(
        getattr(development_engine, "name", _dev_binary),
        getattr(development_engine, "health_check", None),
    )
    _engine_rows = cooldown_manager.list_statuses()
    _engine_state = {
        row.get("engine_name"): row
        for row in _engine_rows
        if isinstance(row, dict) and row.get("engine_name")
    }
    _inv_state = _engine_state.get(getattr(investigation_engine, "name", _inv_binary), {})
    _dev_state = _engine_state.get(getattr(development_engine, "name", _dev_binary), {})
    if _inv_state:
        logger.info(
            "Investigation engine state: %s status=%s cooldown_until=%s fallback=%s",
            _inv_state.get("engine_name"),
            _inv_state.get("status"),
            _inv_state.get("cooldown_until"),
            _inv_state.get("fallback_engine"),
        )
    if _dev_state:
        logger.info(
            "Development engine state: %s status=%s cooldown_until=%s fallback=%s",
            _dev_state.get("engine_name"),
            _dev_state.get("status"),
            _dev_state.get("cooldown_until"),
            _dev_state.get("fallback_engine"),
        )

    # ── Usage governor gate — cap concurrency and filter by priority ────────
    if pending_investigation and not dry_run and _usage_governor is not None:
        gov_decision = _usage_governor.get_concurrency_decision()
        logger.info(
            "Governor decision: max_agents=%d, priority_floor=%s, allow_new_work=%s | %s",
            gov_decision.max_parallel_agents,
            gov_decision.priority_floor,
            gov_decision.allow_new_work,
            gov_decision.reason,
        )
        # Filter tickets by priority floor
        _GOV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        gov_floor = _GOV_RANK.get(gov_decision.priority_floor, 0)
        before_gov = len(pending_investigation)
        pending_investigation = [
            t for t in pending_investigation
            if _GOV_RANK.get(t.severity.value, 0) >= gov_floor
        ]
        if len(pending_investigation) < before_gov:
            logger.info(
                "Governor priority filter: dropped %d ticket(s) below %s",
                before_gov - len(pending_investigation),
                gov_decision.priority_floor,
            )
        # Cap by governor concurrency
        if len(pending_investigation) > gov_decision.max_parallel_agents:
            pending_investigation = pending_investigation[:gov_decision.max_parallel_agents]
            logger.info("Governor concurrency cap: %d agents", gov_decision.max_parallel_agents)
        # Block all new work if governor says no
        if not gov_decision.allow_new_work:
            logger.warning("Governor: blocking all new work")
            pending_investigation = []
        # Check alerts
        try:
            for alert in _usage_governor.check_alerts():
                logger.warning("Governor alert: %s", alert)
        except Exception:
            logger.warning("Governor alert check failed", exc_info=True)

    # Pre-initialize so development phase can reference even when no investigation runs
    investigator = None
    investigated: List[SWETicket] = []
    # Enforce max_open_investigating — don't pile on if already at the cap
    if pending_investigation and not dry_run:
        currently_investigating = sum(
            1 for t in store.list_all()
            if t.status == TicketStatus.INVESTIGATING
        )
        slots_free = effective_cycle.max_open_investigating - currently_investigating
        if slots_free <= 0:
            logger.info(
                "max_open_investigating cap (%d) reached — skipping investigation this cycle",
                effective_cycle.max_open_investigating,
            )
            pending_investigation = []
        elif slots_free < len(pending_investigation):
            logger.info(
                "max_open_investigating: %d slot(s) free, capping investigation batch",
                slots_free,
            )
            pending_investigation = pending_investigation[:slots_free]
    if pending_investigation and not dry_run:
        # Reduce batch size if rate limit tracker shows recent cooldown
        investigate_limit = effective_cycle.max_investigations_per_cycle
        if rate_limit_tracker.is_cooling_down():
            investigate_limit = min(2, investigate_limit)
            logger.warning(
                "Rate limit cooldown active — reducing investigation batch to %d",
                investigate_limit,
            )
        if str(_inv_state.get("status", "")) == "recovering":
            investigate_limit = min(1, investigate_limit)
            logger.info(
                "Investigation engine recovering — ramping with %d task this cycle",
                investigate_limit,
            )
        # Build fallback agent chain from config (enabled entries, sorted by priority)
        fallback_agents = []
        for fa_cfg in sorted(
            [fa for fa in config.fallback_agents if fa.enabled],
            key=lambda x: x.priority,
        ):
            if fa_cfg.name == "gemini-cli":
                adapter = GeminiCLIAdapter(
                    command=fa_cfg.command or "/usr/bin/gemini",
                    model=fa_cfg.default_model or "gemini-2.5-flash-thinking",
                    skills=fa_cfg.skills or ["investigate", "review"],
                )
                if adapter.is_available():
                    fallback_agents.append(adapter)
                    logger.info(
                        "Fallback agent registered: gemini-cli (skills=%s)",
                        fa_cfg.skills,
                    )
                else:
                    logger.warning("gemini-cli configured but not found — skipping")

        # Filter pending_investigation by session cap before starting
        active_investigation = []
        for ticket in pending_investigation[:investigate_limit]:
            inv_attempts = ticket.metadata.get("investigation_attempts", 0)
            if inv_attempts >= _MAX_SESSIONS_PER_TICKET:
                logger.warning(
                    "Investigation cap: ticket %s already has %d attempts (cap=%d) — skipping",
                    ticket.ticket_id, inv_attempts, _MAX_SESSIONS_PER_TICKET,
                )
                ticket.transition(TicketStatus.FAILED)
                ticket.metadata["failed_reason"] = (
                    f"Investigation cap reached: {inv_attempts}/{_MAX_SESSIONS_PER_TICKET} attempts exhausted."
                )
                if not dry_run:
                    store.add(ticket)
                continue
            
            # Atomic claim via Supabase advisory lock — prevents cross-VM duplication
            if hasattr(store, 'claim_ticket'):
                agent_id = os.environ.get("SWE_TEAM_ID", "default")
                if not store.claim_ticket(ticket.ticket_id, agent_id):
                    logger.info("Ticket %s claimed by another agent — skipping investigation", ticket.ticket_id)
                    continue

            # Increment investigation attempts counter
            ticket.metadata["investigation_attempts"] = inv_attempts + 1
            active_investigation.append(ticket)

        # Claim each ticket on GitHub before investigation starts
        for ticket in active_investigation:
            issue_num = ticket.metadata.get("github_issue")
            repo = ticket.metadata.get("repo", "")
            if issue_num:
                trace_id = str(uuid.uuid4())[:8]
                ticket_type = getattr(ticket, 'ticket_type', None)
                type_str = ticket_type.value if ticket_type else "unknown"

                # Build checklist based on ticket type
                if type_str in ("feature", "enhancement"):
                    checklist = [
                        "Understand the feature request",
                        "Read existing source module code",
                        "Design implementation approach",
                        "Implement the feature",
                        "Write/update tests",
                        "Run test suite",
                        "Create PR",
                    ]
                else:
                    checklist = [
                        "Read error logs and stack trace",
                        "Identify affected source files",
                        "Find root cause",
                        "Produce investigation report",
                        "Attempt automated fix",
                        "Run test suite",
                        "Create PR",
                    ]

                try:
                    comment_id = claim_issue(
                        issue_number=issue_num,
                        ticket_id=ticket.ticket_id,
                        trace_id=trace_id,
                        ticket_type=type_str,
                        checklist=checklist,
                        repo=repo,
                    )
                    if comment_id:
                        ticket.metadata["progress_comment_id"] = comment_id
                        ticket.metadata["trace_id"] = trace_id
                        store.add(ticket)
                        logger.info("Claimed GH#%d for ticket %s (trace=%s)", issue_num, ticket.ticket_id, trace_id)
                except Exception:
                    logger.exception("claim_issue failed for %s — continuing", ticket.ticket_id)

        # RBAC engines — one per agent role so @require_permission is enforced
        _rbac_investigator = SimpleRBACEngine(team_role="investigator")
        _rbac_developer = SimpleRBACEngine(team_role="developer")
        _rbac_reviewer = SimpleRBACEngine(team_role="reviewer")

        investigator = InvestigatorAgent(
            store=store,
            memory_top_k=config.memory.top_k,
            memory_similarity_floor=config.memory.similarity_floor,
            model_config=config.models,
            rate_limit_config=config.rate_limits,
            rate_limit_tracker=rate_limit_tracker,
            fallback_agents=fallback_agents,
            repo_paths=config.repos,
            engine=investigation_engine,
            log_query_provider=_log_query_provider,
            worker_module_map=config.monitor.worker_module_map,
            rbac_engine=_rbac_investigator,
            cooldown_manager=cooldown_manager,
            team_id=os.environ.get("SWE_TEAM_ID", "default"),
        )

        # Choose execution strategy based on config
        _use_parallel_inv = config.execution.mode in ("parallel", "adaptive")

        if _use_parallel_inv:
            investigated = _run_parallel_investigations(
                config=config,
                store=store,
                investigator=investigator,
                pending=active_investigation,
                swe_events=swe_events,
            )
        else:
            # Sequential mode (legacy) — original behavior
            try:
                investigated = investigator.investigate_batch(
                    active_investigation,
                    limit=investigate_limit,
                )
                for ticket in investigated:
                    try:
                        store.add(ticket)
                        store_ticket_embedding(
                            store,
                            ticket,
                            enabled=config.memory.store_on_investigation_complete,
                        )
                        # Release investigation claim
                        if hasattr(store, 'release_ticket'):
                            store.release_ticket(ticket.ticket_id)
                    except Exception:
                        logger.exception("Failed to persist investigation for %s", ticket.ticket_id)
                    swe_events.append(
                        SWEEvent.investigation_complete(
                            ticket_id=ticket.ticket_id,
                            source_agent="swe_investigator",
                            report=ticket.investigation_report or "",
                        )
                    )
            except Exception:
                logger.exception("Failed to run investigation batch")

    # 5b-safety: Repair tickets that have an investigation_report but were never
    # transitioned to INVESTIGATION_COMPLETE (e.g. due to a crash or exception
    # between report assignment and status persistence in a previous cycle).
    # This ensures they are visible to the developer backlog in 5c.
    if not dry_run:
        try:
            _stale_investigated = [
                t for t in store.list_all()
                if t.investigation_report
                and t.status not in (
                    TicketStatus.INVESTIGATION_COMPLETE,
                    TicketStatus.IN_DEVELOPMENT,
                    TicketStatus.RESOLVED,
                    TicketStatus.FAILED,
                    TicketStatus.CLOSED,
                )
                and not (t.assigned_to or "").startswith("human:")
                and not t.metadata.get("needs_hitl")
            ]
            if _stale_investigated:
                logger.warning(
                    "Safety net: found %d ticket(s) with investigation_report but wrong status — "
                    "auto-transitioning to INVESTIGATION_COMPLETE",
                    len(_stale_investigated),
                )
                for _st in _stale_investigated:
                    try:
                        _st.transition(TicketStatus.INVESTIGATION_COMPLETE)
                        _st.metadata.setdefault("auto_repaired_status", []).append(
                            {
                                "repaired_at": __import__("datetime").datetime.now(
                                    __import__("datetime").timezone.utc
                                ).isoformat(),
                                "reason": "safety_net: had report but stale status",
                            }
                        )
                        store.add(_st)
                        logger.info(
                            "Repaired ticket %s → INVESTIGATION_COMPLETE", _st.ticket_id
                        )
                    except Exception:
                        logger.exception(
                            "Failed to repair stale status for ticket %s", _st.ticket_id
                        )
        except Exception:
            logger.warning("Status-repair safety net failed (non-fatal)", exc_info=True)

    if not dry_run:
        validate_stage_transitions(
            investigated,
            expected_statuses=[
                TicketStatus.INVESTIGATION_COMPLETE,
                TicketStatus.FAILED,
            ],
            stage_name="investigation",
            store=store,
        )

    # 5c. Dev agent: attempt fixes for investigated tickets.
    # RBAC: skip if role does not include "develop"
    if "develop" not in _allowed_phases:
        logger.info("RBAC: skipping development phase (role=%s)", _team_role)
    elif not dry_run:
        try:
            backlog_inv_complete = [
                t for t in store.list_all()
                if t.status == TicketStatus.INVESTIGATION_COMPLETE
                and t.investigation_report
                and not (t.assigned_to or "").startswith("human:")
                and not t.metadata.get("needs_hitl")
                and t.ticket_id not in {x.ticket_id for x in investigated}
            ]
            if backlog_inv_complete:
                logger.info(
                    "Developer backlog: %d investigation_complete ticket(s) from store",
                    len(backlog_inv_complete),
                )
                investigated = investigated + backlog_inv_complete
        except Exception:
            logger.warning("Could not load investigation_complete backlog (non-fatal)", exc_info=True)
        try:
            _development_candidate_count = len(investigated)
            investigated = _filter_dependency_ready_tickets(
                all_tickets=store.list_all(),
                candidates=investigated,
                stage="Development",
            )
        except Exception:
            logger.warning(
                "Dependency filter failed for development batch (non-fatal, candidates=%d); proceeding without dependency filtering",
                _development_candidate_count,
                exc_info=True,
            )

    if investigated and not dry_run:
        from src.swe_team.developer import DeveloperAgent

        _use_parallel_dev = config.execution.mode in ("parallel", "adaptive")

        if _use_parallel_dev:
            _run_parallel_developments(
                config=config,
                store=store,
                effective_cycle=effective_cycle,
                investigated=investigated,
                rate_limit_tracker=rate_limit_tracker,
                coding_engine=development_engine,
                investigator=investigator,
                sandbox_repos_map=sandbox_repos_map,
                rbac_engine=_rbac_developer,
                cooldown_manager=cooldown_manager,
                development_engine_state=_dev_state,
            )
        else:  # sequential legacy path
            # Resolve repo_root per-ticket from sandbox_repos_map (fail-closed: never use PROJECT_ROOT)
            _seq_default_root = PROJECT_ROOT
            if sandbox_repos_map:
                # Use first sandbox repo as default — avoids working in the SWE-Squad repo
                _seq_default_root = Path(next(iter(sandbox_repos_map.values())))
            dev = DeveloperAgent(
                repo_root=_seq_default_root,
                model_config=config.models,
                rate_limit_config=config.rate_limits,
                rate_limit_tracker=rate_limit_tracker,
                engine=development_engine,
                repos_map=sandbox_repos_map,
                rbac_engine=_rbac_developer,
                cooldown_manager=cooldown_manager,
                team_id=os.environ.get("SWE_TEAM_ID", "default"),
            )
            _dev_count = 0
            _dev_limit = effective_cycle.max_developments_per_cycle
            if str(_dev_state.get("status", "")) == "recovering":
                _dev_limit = min(1, _dev_limit)
                logger.info("Development engine recovering — ramping with %d task this cycle", _dev_limit)
            circuit = CircuitBreaker()
            for ticket in investigated:
                if _dev_count >= _dev_limit:
                    logger.info(
                        "max_developments_per_cycle cap (%d) reached — deferring remaining fixes",
                        _dev_limit,
                    )
                    break
                if ticket.investigation_report and ticket.severity.value in ("critical", "high", "medium"):
                    _dev_count += 1
                    # Opus orchestration for CRITICAL tickets
                    if ticket.severity.value == "critical" and not ticket.metadata.get("orchestration_plan"):
                        try:
                            from src.swe_team.orchestrator import OrchestratorAgent
                            orchestrator = OrchestratorAgent(repo_root=PROJECT_ROOT, engine=development_engine)
                            plan = orchestrator.plan(ticket)
                            ticket.metadata["orchestration_plan"] = plan.to_checklist()
                            ticket.metadata["orchestration_subtasks"] = len(plan.sub_tasks)

                            # Post plan as GitHub comment (update existing if possible)
                            issue_num = ticket.metadata.get("github_issue")
                            repo = ticket.metadata.get("repo", "")
                            if issue_num and repo:
                                _post_or_update_status(
                                    ticket,
                                    plan.to_checklist(),
                                    repo=repo,
                                )

                            store.add(ticket)
                            logger.info("Opus orchestration plan created for %s: %d sub-tasks",
                                         ticket.ticket_id, len(plan.sub_tasks))
                        except Exception:
                            logger.exception("Orchestration failed for %s — falling back to direct fix", ticket.ticket_id)

                    try:
                        # Fix 1: Hard cap — skip tickets that already exhausted sessions
                        prior_attempts = len(ticket.metadata.get("attempts", []))
                        if prior_attempts >= _MAX_SESSIONS_PER_TICKET:
                            logger.warning(
                                "Session cap: ticket %s already has %d attempts (cap=%d) — skipping",
                                ticket.ticket_id, prior_attempts, _MAX_SESSIONS_PER_TICKET,
                            )
                            ticket.transition(TicketStatus.FAILED)
                            ticket.metadata["failed_reason"] = (
                                f"Session cap reached: {prior_attempts}/{_MAX_SESSIONS_PER_TICKET} attempts exhausted."
                            )
                            store.add(ticket)
                            continue

                        # Atomic claim via Supabase advisory lock — prevents cross-VM duplication
                        if hasattr(store, 'claim_ticket'):
                            agent_id = os.environ.get("SWE_TEAM_ID", "default")
                            if not store.claim_ticket(ticket.ticket_id, agent_id):
                                logger.info("Ticket %s claimed by another agent — skipping dev", ticket.ticket_id)
                                continue

                        fix_ok = dev.attempt_fix(ticket)
                        # Do NOT count exhausted-attempt skips as circuit breaker failures.
                        # developer._fix_loop returns False with a "exhausted" failed_reason
                        # when the ticket already hit the session cap — this is a skip, not
                        # a real dev failure. Recording it as False inflates failure_rate and
                        # causes the death spiral: breaker trips → 30 min pause → repeat.
                        _seq_failed_reason = ticket.metadata.get("failed_reason", "")
                        _seq_exhausted = (
                            "exhausted" in _seq_failed_reason.lower()
                            or "cap reached" in _seq_failed_reason.lower()
                        )
                        if _seq_exhausted:
                            circuit.record_skip()
                        else:
                            circuit.record_result(fix_ok)
                        store.add(ticket)  # persist fix result
                        # Release claim so other agents can pick up if needed
                        if hasattr(store, 'release_ticket'):
                            store.release_ticket(ticket.ticket_id)
                        issue_num = ticket.metadata.get("github_issue")
                        if fix_ok and ticket.metadata.get("attempts"):
                            last = ticket.metadata["attempts"][-1]
                            branch = last.get("branch", "?")
                            logger.info("Fix succeeded for %s on branch %s", ticket.ticket_id, branch)
                            if issue_num:
                                _post_or_update_status(
                                    ticket,
                                    f"## ✅ Fix Attempted — SUCCESS\n\n"
                                    f"**Branch:** `{branch}`\n"
                                    f"**Files changed:** {last.get('files_changed', '?')}\n"
                                    f"**Lines changed:** {last.get('lines_changed', '?')}\n"
                                    f"**Tests:** passing\n\n"
                                    f"Fix is on branch `{branch}`. Ready for human review.",
                                )
                        else:
                            # --- Failure feedback loop: re-investigate and retry ---
                            max_reinv = getattr(effective_cycle, "max_reinvestigations", 1)
                            if _try_reinvestigation(ticket, investigator, dev, store, max_reinv):
                                fix_ok = True  # succeeded after re-investigation
                                last = (ticket.metadata.get("attempts") or [{}])[-1]
                                branch = last.get("branch", "?")
                                logger.info("Fix succeeded after re-investigation for %s on branch %s", ticket.ticket_id, branch)
                                if issue_num:
                                    _post_or_update_status(
                                        ticket,
                                        f"## ✅ Fix Attempted — SUCCESS (after re-investigation)\n\n"
                                        f"**Branch:** `{branch}`\n"
                                        f"**Files changed:** {last.get('files_changed', '?')}\n"
                                        f"**Lines changed:** {last.get('lines_changed', '?')}\n"
                                        f"**Tests:** passing\n\n"
                                        f"Fix is on branch `{branch}`. Ready for human review.",
                                    )
                            elif issue_num:
                                attempts = ticket.metadata.get("attempts", [])
                                _post_or_update_status(
                                    ticket,
                                    f"## ❌ Fix Attempted — FAILED\n\n"
                                    f"**Attempts:** {len(attempts)}/{dev._max_attempts}\n"
                                    f"**Last error:** `{attempts[-1].get('error', '?')[:200] if attempts else '?'}`\n\n"
                                    f"Escalating to HITL.",
                                )
                    except RateLimitCooldown:
                        raise  # Must propagate to daemon loop for global cooldown
                    except Exception:
                        # CRITICAL: persist FAILED so next cycle doesn't re-queue this ticket
                        logger.exception("Dev agent raised for ticket %s — persisting FAILED to stop retry loop", ticket.ticket_id)
                        from src.swe_team.models import TicketStatus as _TS
                        if ticket.status not in (_TS.FAILED, _TS.BLOCKED, _TS.RESOLVED, _TS.CLOSED):
                            ticket.transition(_TS.FAILED)
                            ticket.metadata["failed_reason"] = "Dev agent raised an unhandled exception — see logs"
                        try:
                            store.add(ticket)
                        except Exception:
                            logger.exception("Could not persist failed ticket %s", ticket.ticket_id)
                        # Release claim on failure too
                        if hasattr(store, 'release_ticket'):
                            try:
                                store.release_ticket(ticket.ticket_id)
                            except Exception:
                                pass
                elif ticket.investigation_report and ticket.severity.value == "low":
                    logger.info("Skipping LOW severity ticket %s — backlog only", ticket.ticket_id)
                    ticket.transition(TicketStatus.CLOSED)
                    ticket.metadata["close_reason"] = "low_severity_auto_close"
                    store.add(ticket)

    if investigated and not dry_run:
        validate_stage_transitions(
            investigated,
            expected_statuses=[
                TicketStatus.IN_REVIEW,
                TicketStatus.FAILED,
                TicketStatus.CLOSED,
                TicketStatus.INVESTIGATION_COMPLETE,
            ],
            stage_name="development",
            store=store,
        )

    # 5d-pre. PR Review Scanner: detect "changes requested" and queue rework
    _gh_repo_for_review = config.github_repo if hasattr(config, "github_repo") else os.environ.get("SWE_GITHUB_REPO", "")
    if _gh_repo_for_review and not dry_run:
        try:
            from src.swe_team.pr_review_scanner import PRReviewScanner, PRReviewScannerConfig
            _pr_scanner_cfg = PRReviewScannerConfig(repo=_gh_repo_for_review, enabled=True)
            _pr_scanner = PRReviewScanner(_pr_scanner_cfg)
            _rework_results = _pr_scanner.scan_changes_requested()
            for _rr in _rework_results:
                if not _rr.ticket_id:
                    continue
                _rw_ticket = store.get(_rr.ticket_id)
                if _rw_ticket is None:
                    logger.debug(
                        "PR review scanner: ticket %s not found in store (PR #%d)",
                        _rr.ticket_id, _rr.pr_number,
                    )
                    continue
                if _rw_ticket.status != TicketStatus.IN_REVIEW:
                    logger.debug(
                        "PR review scanner: ticket %s is %s, not IN_REVIEW — skipping",
                        _rr.ticket_id, _rw_ticket.status.value,
                    )
                    continue
                logger.info(
                    "PR review scanner: ticket %s (PR #%d) has CHANGES_REQUESTED — queuing rework",
                    _rr.ticket_id, _rr.pr_number,
                )
                # Transition: IN_REVIEW → REWORK_REQUESTED → INVESTIGATION_COMPLETE
                # so the developer agent picks it up on the next cycle.
                _rw_ticket.transition(TicketStatus.REWORK_REQUESTED)
                _rw_ticket.metadata["review_feedback"] = _rr.review_comments or (
                    f"Reviewer requested changes on PR #{_rr.pr_number} "
                    f"but provided no inline comments."
                )
                _rw_ticket.metadata["rework_pr_number"] = _rr.pr_number
                _rw_ticket.metadata["rework_pr_branch"] = _rr.head_branch
                # Return ticket to INVESTIGATION_COMPLETE so the dev agent re-attempts
                _rw_ticket.transition(TicketStatus.INVESTIGATION_COMPLETE)
                store.add(_rw_ticket)
                logger.info(
                    "Ticket %s queued for rework (INVESTIGATION_COMPLETE) with review feedback",
                    _rr.ticket_id,
                )
        except Exception:
            logger.exception("PR review scanner raised an unhandled exception — skipping rework step")

    # 5d. Reviewer: promote IN_REVIEW → RESOLVED
    # RBAC: skip if role does not include "review"
    if "review" not in _allowed_phases:
        logger.info("RBAC: skipping review phase (role=%s)", _team_role)
        in_review_tickets = []
    else:
        in_review_tickets = store.list_by_status(TicketStatus.IN_REVIEW)
    if in_review_tickets and not dry_run:
        from src.swe_team.reviewer import ReviewerAgent
        reviewer = ReviewerAgent(
            model=config.models.t3_fast if hasattr(config.models, 't3_fast') else "haiku",
            repo_root=PROJECT_ROOT,
            repos_map=sandbox_repos_map or {},
        )
        resolved_tickets, rejected_tickets, hitl_tickets = reviewer.review_batch(
            in_review_tickets, store
        )
        logger.info(
            "Reviewer: resolved=%d rejected=%d hitl=%d",
            len(resolved_tickets),
            len(rejected_tickets),
            len(hitl_tickets),
        )

        # ── Post-review PR verification (issue #367 safety net) ──────────────
        # Final defence: if any RESOLVED ticket has no pr_url/pr_number it means
        # the PR gate in code_reviewer and the reviewer safety net were both
        # bypassed.  Downgrade those tickets to IN_DEVELOPMENT immediately.
        for ticket in list(resolved_tickets):
            if ticket.status == TicketStatus.RESOLVED and not (
                ticket.metadata.get("pr_url") or ticket.metadata.get("pr_number")
            ):
                logger.warning(
                    "Runner PR-gate: ticket %s is RESOLVED without a PR — "
                    "downgrading to IN_DEVELOPMENT (needs_pr=True)",
                    ticket.ticket_id,
                )
                ticket.metadata["needs_pr"] = True
                ticket.metadata["review_feedback"] = (
                    "Runner PR-gate: resolved without a PR artefact — returned to development."
                )
                try:
                    ticket.transition(TicketStatus.IN_DEVELOPMENT)
                    store.add(ticket)
                except Exception:
                    logger.exception("Runner PR-gate: failed to downgrade ticket %s", ticket.ticket_id)
                resolved_tickets.remove(ticket)
                rejected_tickets.append(ticket)

        for ticket in resolved_tickets:
            issue_num = ticket.metadata.get("github_issue")
            if issue_num:
                _post_or_update_status(
                    ticket,
                    f"## Resolved\n\nTicket `{ticket.ticket_id}` has been automatically resolved "
                    f"after passing review.\n\nInvestigation report: "
                    f"{len(ticket.investigation_report or '')} chars | "
                    f"Fix attempts: {len(ticket.metadata.get('attempts', []))}",
                )
        for ticket in hitl_tickets:
            logger.warning(
                "HITL escalation: ticket %s stuck in review after 3 rejections",
                ticket.ticket_id,
            )

    # 5e. Fix Verifier: advance VERIFYING tickets → RESOLVED or REGRESSION
    verifying_tickets = store.list_by_status(TicketStatus.VERIFYING)
    if verifying_tickets:
        pipeline_cfg = getattr(config, "pipeline", {}) or {}
        if isinstance(pipeline_cfg, dict):
            window_min = int(pipeline_cfg.get("verification_window_minutes", 30))
            prop_wait = int(pipeline_cfg.get("propagation_wait_minutes", 2))
        else:
            window_min = getattr(pipeline_cfg, "verification_window_minutes", 30)
            prop_wait = getattr(pipeline_cfg, "propagation_wait_minutes", 2)

        verifier = FixVerifier(
            verification_window_minutes=window_min,
            propagation_wait_minutes=prop_wait,
        )
        # Attach scan_fingerprint_since to the monitor if needed
        add_fingerprint_scan_to_monitor(monitor)

        for ticket in verifying_tickets:
            try:
                result = verifier.check_verification(ticket, monitor)
                verify_output = VerificationPhaseOutput(
                    verdict="pass" if result.ready_to_close else ("fail" if result.regression_detected else "in_progress"),
                    test_output=(
                        f"recurrence_count={result.recurrence_count}, "
                        f"elapsed_minutes={round(result.elapsed_minutes, 2)}"
                    ),
                    regression_check={
                        "regression_detected": result.regression_detected,
                        "regression_ticket_id": (
                            result.regression_ticket.ticket_id if result.regression_ticket else None
                        ),
                    },
                )
                verify_handover = EngineHandover(
                    task_id=ticket.ticket_id,
                    phase="verify",
                    source_engine=ticket.metadata.get("target_engine_verify", "cline"),
                    target_engine=ticket.metadata.get("target_engine_post_verify", "orchestrator"),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    context=verify_output.to_dict(),
                    constraints=HandoverConstraints(
                        budget_remaining_usd=float(ticket.metadata.get("budget_remaining_usd", 0.0) or 0.0),
                        time_limit_seconds=int(ticket.metadata.get("handover_time_limit_seconds", 1800) or 1800),
                        model_tier="T1",
                        retry_count=int(ticket.metadata.get("verification_retry_count", 0) or 0),
                        max_retries=int(ticket.metadata.get("verification_max_retries", 3) or 3),
                    ),
                )
                ticket.metadata["handover_verify_result"] = verify_handover.to_dict()
                _log_handover_if_supported(store, verify_handover)
                if result.ready_to_close:
                    ticket.metadata.setdefault("resolution_note", "fix_succeeded")
                    ticket.transition(TicketStatus.RESOLVED)
                    logger.info(
                        "Ticket %s RESOLVED after %.1f min verification window",
                        ticket.ticket_id,
                        result.elapsed_minutes,
                    )
                    if not dry_run:
                        store.add(ticket)
                elif result.regression_detected and result.regression_ticket:
                    reg = result.regression_ticket
                    logger.warning(
                        "Regression detected for ticket %s — creating regression ticket %s",
                        ticket.ticket_id,
                        reg.ticket_id,
                    )
                    if not dry_run:
                        store.add(ticket)
                        store.add(reg)
                else:
                    # Still in window — persist updated metadata
                    if not dry_run:
                        store.add(ticket)
            except Exception:
                logger.exception(
                    "Error during fix verification for ticket %s", ticket.ticket_id
                )

    # 6. Stability gate: check if new work should be blocked
    gate = RalphWiggumGate(config.governance)
    report = gate.evaluate(
        store.list_open(),
        ci_green=True,
        failing_tests=0,
    )

    logger.info(
        "Stability gate: %s (%d open, %d critical)",
        report.verdict.value,
        len(store.list_open()),
        report.open_critical,
    )
    if report.verdict.value == "block":
        logger.warning("STABILITY GATE BLOCKED: %s", report.details)
        if not dry_run:
            try:
                notify_stability_gate(report)
            except Exception:
                logger.exception("Failed to send stability gate notification")
    if not dry_run:
        swe_events.append(
            SWEEvent.stability_gate_result(
                ticket_id="stability_gate",
                source_agent="swe_governance",
                verdict=report.verdict.value,
                details=report.details,
            )
        )

    # 7. Creative proposals (low severity) — only when gate is not blocked
    if creative and not dry_run and report.verdict != GovernanceVerdict.BLOCK:
        creative_agent = CreativeAgent()
        proposals = creative_agent.propose(store)
        if proposals:
            for proposal in proposals:
                try:
                    store.add(proposal)
                except Exception:
                    logger.exception("Failed to persist creative proposal %s", proposal.ticket_id)
            try:
                creative_agent.publish_proposals(proposals)
            except Exception:
                logger.exception("Failed to publish creative proposals")

    # 8. Dispatch SWE events to A2A Hub
    if swe_events and not dry_run:
        try:
            dispatch_swe_events(swe_events)
        except Exception:
            logger.exception("Failed to dispatch SWE events to A2A")

    # Aggregate cycle costs from investigation metadata
    cycle_cost = 0.0
    for ticket in investigated:
        inv = ticket.metadata.get("investigation", {})
        cost_val = inv.get("cost_usd")
        if cost_val:
            try:
                cycle_cost += float(cost_val)
            except (ValueError, TypeError):
                pass
            # Append cost entry to ticket metadata for daily aggregation
            ticket.metadata.setdefault("cycle_costs", []).append({
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "cost_usd": cost_val,
                "phase": "investigation",
            })
            try:
                store.add(ticket)
            except Exception:
                logger.exception("Failed to persist cost metadata for %s", ticket.ticket_id)

    rate_limit_events = rate_limit_tracker.recent_events(hours=1)
    if rate_limit_events:
        logger.warning(
            "Rate limit events this cycle: %d", len(rate_limit_events)
        )

    result = {
        "new_tickets": len(new_tickets),
        "triaged": len(triaged),
        "investigated": len(investigated),
        "gate_verdict": report.verdict.value,
        "gate_details": report.details,
        "open_tickets": len(store.list_open()),
        "cycle_cost_usd": round(cycle_cost, 4),
        "rate_limit_events": len(rate_limit_events),
        "engine_health": _engine_rows,
    }

    # 9. Write status file for external monitoring
    if not dry_run:
        try:
            write_status(
                config.ticket_store_path.replace("tickets.json", "status.json"),
                cycle_result=result,
                store=store,
            )
        except Exception:
            logger.exception("Failed to write status file after cycle")

    # 10. Detect and reset stalled tickets
    stalled = detect_stalled_tickets(store)

    # 11. Append session progress log
    done_parts = []
    if new_tickets:
        done_parts.append(f"Detected {len(new_tickets)} issue(s)")
    if triaged:
        done_parts.append(f"triaged {len(triaged)}")
    if investigated:
        done_parts.append(f"investigated {len(investigated)}")
    if stalled:
        done_parts.append(f"reset {len(stalled)} stalled")
    done_summary = ", ".join(done_parts) if done_parts else "No new issues"

    blockers_parts = []
    if report.verdict.value == "block":
        blockers_parts.append(f"Gate blocked: {report.details}")
    blockers_summary = "; ".join(blockers_parts) if blockers_parts else "None"

    append_progress_log(
        result,
        done=done_summary,
        next_step="Continue monitoring" if report.verdict.value != "block" else "Fix blockers before new work",
        blockers=blockers_summary,
    )

    log_session_end(session_tag)
    return result


# ---------------------------------------------------------------------------
# Parallel execution helpers
# ---------------------------------------------------------------------------

def _get_or_create_executor(config) -> ParallelExecutor:
    """Get or create the global ParallelExecutor instance."""
    global _parallel_executor
    if _parallel_executor is None:
        # Only persist to local JSON when NOT using Supabase (which handles its
        # own persistence). Creating a new TicketStore per callback caused
        # concurrent-write corruption (each instance loaded stale state then
        # overwrote with it). Skip entirely when Supabase is active.
        _use_supabase = bool(os.environ.get("SUPABASE_URL"))

        def _persist_ticket(ticket, task_type, success):
            """Per-ticket persistence callback — JSON-store only."""
            if _use_supabase:
                logger.info(
                    "Per-ticket persist: %s %s (%s)",
                    task_type, ticket.ticket_id, "ok" if success else "failed",
                )
                return
            try:
                from src.swe_team.ticket_store import TicketStore
                store_path = config.ticket_store_path
                store = TicketStore(store_path)
                store.add(ticket)
                logger.info(
                    "Per-ticket persist: %s %s (%s)",
                    task_type, ticket.ticket_id, "ok" if success else "failed",
                )
            except Exception:
                logger.exception(
                    "Per-ticket persistence failed for %s", ticket.ticket_id,
                )

        _parallel_executor = ParallelExecutor(
            execution_config=config.execution,
            active_profile="base",
            on_ticket_complete=_persist_ticket,
        )
    return _parallel_executor


def _get_or_create_worktree_manager(config) -> WorktreeManager:
    """Get or create the global WorktreeManager instance."""
    global _worktree_manager
    if _worktree_manager is None:
        profile = config.execution.profiles.get("max", config.execution.profiles.get("base"))
        pool_size = max(
            profile.max_concurrent_investigations + profile.max_concurrent_developments
            if profile else 8,
            4,
        )
        _worktree_manager = WorktreeManager(
            repo_root=PROJECT_ROOT,
            pool_size=pool_size,
        )
    return _worktree_manager


def _run_parallel_investigations(
    *,
    config,
    store,
    investigator,
    pending: List,
    swe_events: List,
) -> List:
    """Run investigations in parallel using the ParallelExecutor.

    When a QueuedDispatcher is active (providers.task_queue.enabled=true),
    tickets are enqueued with severity-based priority and dispatched through
    the queue. This gives us: priority ordering, dead-letter for failures,
    heartbeat for long-running tasks, and future cross-VM dispatch.
    """
    if not pending:
        return []

    executor = _get_or_create_executor(config)

    # In adaptive mode, resolve the right profile based on backlog
    if config.execution.mode == "adaptive":
        try:
            all_open = store.list_open() if hasattr(store, "list_open") else []
            profile_name = executor.resolve_adaptive_profile(backlog_size=len(all_open))
            executor.scale_to(profile_name)
            logger.info(
                "Adaptive mode: selected profile '%s' (backlog=%d)",
                profile_name, len(all_open),
            )
        except Exception:
            logger.warning("Adaptive profile resolution failed — using current profile", exc_info=True)

    logger.info(
        "Starting parallel investigation: %d tickets, profile=%s, max_workers=%d",
        len(pending), executor.active_profile_name,
        executor.active_profile.max_concurrent_investigations,
    )

    # Track tickets by ID for queue lookup
    _ticket_by_id = {t.ticket_id: t for t in pending}

    # ── Queue-backed dispatch path ─────────────────────────────────
    if _queued_dispatcher is not None:
        logger.info("Using queue-backed dispatch for %d investigations", len(pending))
        for ticket in pending:
            _queued_dispatcher.enqueue_investigation(ticket)

        dispatch_results = _queued_dispatcher.dispatch_parallel(
            task_type="investigate",
            worker_fn=lambda t: investigator.investigate(t),
            ticket_lookup=lambda tid: _ticket_by_id.get(tid),
            executor=executor._investigation_pool,
            max_tasks=len(pending),
            timeout_s=1800.0,
        )

        # Log queue health after dispatch
        health = _queued_dispatcher.health()
        logger.info(
            "Queue health after investigation: depth=%d dead_letter=%d",
            health["investigate_depth"], health["dead_letter_count"],
        )

        # Convert DispatchResult → TaskResult-like for downstream processing
        from src.swe_team.parallel_executor import TaskResult
        results = []
        for dr in dispatch_results:
            ticket = _ticket_by_id.get(dr.ticket_id)
            results.append(TaskResult(
                ticket_id=dr.ticket_id,
                task_type="investigation",
                success=dr.success,
                duration_s=dr.duration_s,
                error=dr.error,
                ticket=ticket,
            ))

    # ── Direct executor dispatch path (no queue) ───────────────────
    else:
        futures = []
        for ticket in pending:
            try:
                future = executor.submit_investigation(ticket, investigator)
                futures.append(future)
            except RuntimeError as exc:
                logger.warning("Failed to submit investigation for %s: %s", ticket.ticket_id, exc)

        results = executor.collect_results(futures, timeout=1800)

    investigated = []
    for result in results:
        if result.success and result.ticket:
            investigated.append(result.ticket)
            try:
                store.add(result.ticket)
                store_ticket_embedding(
                    store, result.ticket,
                    enabled=config.memory.store_on_investigation_complete,
                )
            except Exception:
                logger.exception("Failed to persist investigation for %s", result.ticket_id)
            swe_events.append(
                SWEEvent.investigation_complete(
                    ticket_id=result.ticket_id,
                    source_agent="swe_investigator",
                    report=getattr(result.ticket, "investigation_report", "") or "",
                )
            )
        elif not result.success:
            logger.warning(
                "Parallel investigation failed for %s: %s (%.1fs)",
                result.ticket_id, result.error or "unknown", result.duration_s,
            )

    logger.info(
        "Parallel investigation complete: %d/%d succeeded",
        len(investigated), len(pending),
    )
    return investigated


def _run_parallel_developments(
    *,
    config,
    store,
    effective_cycle,
    investigated: List,
    rate_limit_tracker,
    coding_engine=None,
    investigator: Optional["InvestigatorAgent"] = None,
    sandbox_repos_map: Optional[Dict[str, Any]] = None,
    rbac_engine: Optional[object] = None,
    cooldown_manager: Optional[EngineCooldownManager] = None,
    development_engine_state: Optional[Dict[str, Any]] = None,
) -> None:
    """Run developments in parallel using the ParallelExecutor."""
    from src.swe_team.developer import DeveloperAgent

    dev_candidates = [
        t for t in investigated
        if t.investigation_report and t.severity.value in ("critical", "high", "medium")
    ]
    try:
        _parallel_dev_candidate_count = len(dev_candidates)
        dev_candidates = _filter_dependency_ready_tickets(
            all_tickets=store.list_all(),
            candidates=dev_candidates,
            stage="Development",
        )
    except Exception:
        logger.warning(
            "Dependency filter failed for parallel development batch (non-fatal, candidates=%d); proceeding without dependency filtering",
            _parallel_dev_candidate_count,
            exc_info=True,
        )
    _dev_limit = effective_cycle.max_developments_per_cycle
    if str((development_engine_state or {}).get("status", "")) == "recovering":
        _dev_limit = min(1, _dev_limit)
        logger.info("Development engine recovering — ramping with %d task this cycle", _dev_limit)
    dev_candidates = dev_candidates[:_dev_limit]

    if not dev_candidates:
        return

    executor = _get_or_create_executor(config)
    wt_manager = _get_or_create_worktree_manager(config)

    # Resolve adaptive profile for dev-only cycles (same logic as investigation path)
    if config.execution.mode == "adaptive":
        try:
            all_open = store.list_open() if hasattr(store, "list_open") else []
            profile_name = executor.resolve_adaptive_profile(backlog_size=len(all_open))
            executor.scale_to(profile_name)
            logger.info(
                "Adaptive mode: selected profile '%s' for development (backlog=%d)",
                profile_name, len(all_open),
            )
        except Exception:
            logger.warning("Adaptive profile resolution failed for dev — using current profile", exc_info=True)

    logger.info(
        "Starting parallel development: %d tickets, profile=%s",
        len(dev_candidates), executor.active_profile_name,
    )

    # Orchestration pass (sequential — lightweight planning)
    for ticket in dev_candidates:
        if ticket.severity.value == "critical" and not ticket.metadata.get("orchestration_plan"):
            try:
                from src.swe_team.orchestrator import OrchestratorAgent
                orchestrator = OrchestratorAgent(repo_root=PROJECT_ROOT, engine=coding_engine)
                plan = orchestrator.plan(ticket)
                ticket.metadata["orchestration_plan"] = plan.to_checklist()
                ticket.metadata["orchestration_subtasks"] = len(plan.sub_tasks)
                store.add(ticket)
            except Exception:
                logger.exception("Orchestration failed for %s — direct fix", ticket.ticket_id)

    futures_and_wts = []
    circuit = CircuitBreaker()
    for ticket in dev_candidates:
        try:
            # Resolve sandbox repo path — if ticket targets a sandbox repo, work there directly
            ticket_repo = ticket.metadata.get("repo", "")
            sandbox_repo_path = (sandbox_repos_map or {}).get(ticket_repo)
            if sandbox_repo_path and Path(sandbox_repo_path).is_dir():
                # Sandbox ticket: work directly in the sandbox repo (no worktree from SWE-Squad)
                dev = DeveloperAgent(
                    repo_root=Path(sandbox_repo_path),
                    model_config=config.models,
                    rate_limit_config=config.rate_limits,
                    rate_limit_tracker=rate_limit_tracker,
                    use_worktree=True,
                    engine=coding_engine,
                    repos_map=sandbox_repos_map,
                    rbac_engine=rbac_engine,
                    cooldown_manager=cooldown_manager,
                    team_id=os.environ.get("SWE_TEAM_ID", "default"),
                )
                ticket.metadata["repo_path"] = str(sandbox_repo_path)
                future = executor.submit_development(ticket, dev, worktree_path=str(sandbox_repo_path))
                futures_and_wts.append((future, None))  # No worktree to release
                logger.info(
                    "Development for %s routed to sandbox repo %s",
                    ticket.ticket_id, sandbox_repo_path,
                )
            else:
                wt = wt_manager.acquire(
                    ticket_id=ticket.ticket_id,
                    branch=f"swe-fix/ticket-{ticket.ticket_id}",
                )
                dev = DeveloperAgent(
                    repo_root=wt.path,
                    model_config=config.models,
                    rate_limit_config=config.rate_limits,
                    rate_limit_tracker=rate_limit_tracker,
                    use_worktree=True,
                    engine=coding_engine,
                    repos_map=sandbox_repos_map,
                    rbac_engine=rbac_engine,
                    cooldown_manager=cooldown_manager,
                    team_id=os.environ.get("SWE_TEAM_ID", "default"),
                )
                future = executor.submit_development(ticket, dev, worktree_path=str(wt.path))
                futures_and_wts.append((future, wt))
        except RuntimeError as exc:
            logger.warning("Failed to submit development for %s: %s", ticket.ticket_id, exc)

    for future, wt in futures_and_wts:
        timed_out = False
        try:
            result = future.result(timeout=1800)
            ticket = result.ticket
            if ticket:
                store.add(ticket)
                issue_num = ticket.metadata.get("github_issue")
                if result.success and ticket.metadata.get("attempts"):
                    circuit.record_result(True)
                    last = ticket.metadata["attempts"][-1]
                    branch = last.get("branch", "?")
                    # Verify branch was pushed; if developer failed to push, try from worktree
                    push_cwd = str(wt.path) if wt else ticket.metadata.get("repo_path")
                    if not push_cwd:
                        logger.warning("No repo_path in ticket %s metadata — cannot push branch", ticket.ticket_id)
                        continue
                    if not last.get("pushed") and branch != "?":
                        try:
                            subprocess.run(
                                ["git", "push", "--force-with-lease", "origin", branch],
                                cwd=push_cwd, capture_output=True, text=True, timeout=60,
                            )
                            last["pushed"] = True
                            logger.info("Runner pushed branch %s from worktree before release", branch)
                        except Exception:
                            logger.warning("Runner failed to push branch %s before worktree release", branch)
                    logger.info("Fix succeeded for %s on branch %s", ticket.ticket_id, branch)
                    if issue_num:
                        _post_or_update_status(
                            ticket,
                            f"## Fix Attempted — SUCCESS\n\n"
                            f"**Branch:** `{branch}`\n"
                            f"**Files changed:** {last.get('files_changed', '?')}\n"
                            f"**Lines changed:** {last.get('lines_changed', '?')}\n"
                            f"**Tests:** passing\n\n"
                            f"Fix is on branch `{branch}`. Ready for human review.",
                            repo=ticket.metadata.get("repo", ""),
                        )
                else:
                    # --- Failure feedback loop: re-investigate and retry ---
                    # If the ticket failed because the session cap was exhausted, this is a
                    # skip — not a genuine dev failure. Recording it as a failure would inflate
                    # the circuit breaker's failure rate and cause a death spiral.
                    _failed_reason = ticket.metadata.get("failed_reason", "")
                    _attempts_exhausted = (
                        "exhausted" in _failed_reason.lower()
                        or "cap reached" in _failed_reason.lower()
                        or len(ticket.metadata.get("attempts", [])) >= _MAX_SESSIONS_PER_TICKET
                    )
                    if _attempts_exhausted:
                        circuit.record_skip()
                        reinv_ok = False
                    else:
                        reinv_ok = False
                        if investigator is not None:
                            max_reinv = getattr(effective_cycle, "max_reinvestigations", 1)
                            # Create a fresh dev agent for sequential retry (worktree already released)
                            retry_dev = DeveloperAgent(
                                repo_root=PROJECT_ROOT,
                                model_config=config.models,
                                rate_limit_config=config.rate_limits,
                                rate_limit_tracker=rate_limit_tracker,
                                engine=coding_engine,
                                repos_map=sandbox_repos_map,
                                rbac_engine=rbac_engine,
                                cooldown_manager=cooldown_manager,
                                team_id=os.environ.get("SWE_TEAM_ID", "default"),
                            )
                            reinv_ok = _try_reinvestigation(ticket, investigator, retry_dev, store, max_reinv)
                        circuit.record_result(reinv_ok)
                    if reinv_ok:
                        last = (ticket.metadata.get("attempts") or [{}])[-1]
                        branch = last.get("branch", "?")
                        logger.info("Fix succeeded after re-investigation for %s on branch %s", ticket.ticket_id, branch)
                        if issue_num:
                            _post_or_update_status(
                                ticket,
                                f"## Fix Attempted — SUCCESS (after re-investigation)\n\n"
                                f"**Branch:** `{branch}`\n"
                                f"**Files changed:** {last.get('files_changed', '?')}\n"
                                f"**Lines changed:** {last.get('lines_changed', '?')}\n"
                                f"**Tests:** passing\n\n"
                                f"Fix is on branch `{branch}`. Ready for human review.",
                                repo=ticket.metadata.get("repo", ""),
                            )
                    elif issue_num:
                        attempts = ticket.metadata.get("attempts", [])
                        _post_or_update_status(
                            ticket,
                            f"## Fix Attempted — FAILED\n\n"
                            f"**Attempts:** {len(attempts)}\n"
                            f"**Last error:** `{attempts[-1].get('error', '?')[:200] if attempts else '?'}`\n\n"
                            f"Escalating to HITL.",
                            repo=ticket.metadata.get("repo", ""),
                        )
        except concurrent.futures.TimeoutError:
            timed_out = True
            if wt is not None:
                logger.error("Parallel development timed out for worktree %s — thread still running, deferring worktree release", wt.path)
            else:
                logger.error("Parallel development timed out for sandbox repo — thread still running, no worktree to release")
        except Exception:
            if wt is not None:
                logger.exception("Parallel development failed for worktree %s", wt.path)
            else:
                logger.exception("Parallel development failed for sandbox repo — no worktree to release")
        finally:
            if not timed_out:
                # Only release when the thread has actually finished to avoid
                # deleting the worktree directory while the developer thread is
                # still executing inside it (race condition → FileNotFoundError).
                if wt is not None:
                    try:
                        wt_manager.release(wt)
                    except Exception:
                        logger.warning("Failed to release worktree %s", wt.path)
            else:
                # Thread is still running — schedule release after it completes
                def _deferred_release(f, w, mgr):
                    try:
                        f.result(timeout=300)  # wait up to 5 more min
                    except Exception:
                        pass
                    finally:
                        try:
                            mgr.release(w)
                        except Exception:
                            pass
                if wt is not None:
                    import threading as _threading
                    _threading.Thread(target=_deferred_release, args=(future, wt, wt_manager), daemon=True).start()

    logger.info("Parallel development complete")


def bootstrap_cycle(config, store, dry_run: bool = False) -> Dict[str, Any]:
    """Bootstrap scan that acknowledges existing issues."""
    monitor = MonitorAgent(config.monitor, known_fingerprints=store.known_fingerprints)
    baseline = monitor.scan()

    if not baseline:
        logger.info("Bootstrap scan complete — no new issues detected")
        return {"acknowledged": 0}

    triage = TriageAgent(config)
    try:
        triaged = triage.triage_batch(baseline)
    except (RuntimeError, ValueError):
        # Triage is local-only; config or parsing errors are the expected failures.
        logger.exception("Bootstrap triage failed; acknowledging baseline tickets without triage")
        triaged = baseline
    if not triaged:
        # Triage should not drop tickets; as a safety measure, fallback to baseline if it does.
        logger.warning("Bootstrap triage returned no tickets; acknowledging baseline")
        triaged = baseline

    for ticket in triaged:
        ticket.transition(TicketStatus.ACKNOWLEDGED)
        ticket.metadata["bootstrap"] = {
            "acknowledged_at": ticket.updated_at,
        }
        if not dry_run:
            try:
                store.add(ticket)
            except Exception:
                logger.exception("Failed to persist bootstrap ticket %s", ticket.ticket_id)

    logger.info("Bootstrap complete: %d issue(s) acknowledged", len(baseline))
    return {"acknowledged": len(baseline)}


def daemon_loop(
    config,
    store,
    interval_seconds: int,
    dry_run: bool = False,
    creative: bool = False,
    status_path: str = "data/swe_team/status.json",
    max_cycles: Optional[int] = None,
    config_path: Optional[str] = None,
    sandbox_repos_map: Optional[Dict[str, Any]] = None,
) -> None:
    """Run monitor/triage cycles continuously until signaled to stop.

    Args:
        max_cycles: Stop after this many cycles (None = run forever).
                    Useful for cron-launched daemons that should self-terminate.
    """
    shutdown = threading.Event()

    def _signal_handler(signum, _frame):
        logger.info("Shutdown signal received (%s)", signum)
        shutdown.set()

    prev_sigterm = signal.signal(signal.SIGTERM, _signal_handler)
    prev_sigint = signal.signal(signal.SIGINT, _signal_handler)

    cycles_run = 0
    limit_msg = f", max_cycles={max_cycles}" if max_cycles else ""
    logger.info("SWE Team daemon starting (interval=%ds%s)", interval_seconds, limit_msg)

    # Start job scheduler if enabled
    _scheduler = None
    if config.scheduler.enabled:
        try:
            from src.swe_team.scheduler import JobScheduler, JobStore, TimeWindow, ScheduledJob, ScheduleType, JobPriority
            job_store = JobStore(Path(config.scheduler.job_store_path))
            time_window = TimeWindow(
                peak_start_hour=config.scheduler.peak_start_hour,
                peak_end_hour=config.scheduler.peak_end_hour,
                peak_days=[int(d) for d in config.scheduler.peak_days.split(",")],
            )
            _scheduler = JobScheduler(
                store=job_store,
                time_window=time_window,
                max_workers=config.scheduler.max_workers,
                tick_interval=config.scheduler.tick_interval_seconds,
            )
            # Seed default jobs if store is empty
            if not job_store.load_all() and config.scheduler.default_jobs:
                for jdef in config.scheduler.default_jobs:
                    job = ScheduledJob(
                        name=jdef.get("name", ""),
                        schedule_type=ScheduleType(jdef.get("schedule_type", "cron")),
                        cron_expression=jdef.get("cron_expression", ""),
                        priority=JobPriority(jdef.get("priority", "normal")),
                        instructions=jdef.get("instructions", ""),
                        respect_peak_hours=jdef.get("respect_peak_hours", True),
                    )
                    _scheduler.add_job(job)
            _scheduler.start()
            logger.info("Job scheduler started (%d jobs)", len(job_store.load_all()))
        except Exception:
            logger.exception("Failed to start scheduler — continuing without it")

    try:
        while not shutdown.is_set():
            # ── Rate-limit daemon-wide cooldown guard ────────────────────────
            global _rate_limit_cooldown_until
            remaining = _rate_limit_cooldown_until - time.time()
            if remaining > 0:
                logger.warning(
                    "Rate-limit cooldown active — pausing daemon for %.0fs (%.1f min remaining)",
                    remaining,
                    remaining / 60,
                )
                if shutdown.wait(timeout=min(remaining, interval_seconds)):
                    break
                continue
            try:
                try:
                    config = load_config(config_path)
                except Exception:
                    logger.exception("Failed to reload config for daemon cycle; using previous in-memory config")
                result = run_cycle(config, store, dry_run=dry_run, creative=creative, sandbox_repos_map=sandbox_repos_map)
            except RateLimitCooldown as rl_exc:
                remaining_cd = max(0.0, rl_exc.cooldown_until - time.time())
                if rl_exc.global_pause:
                    # Legacy global pause path
                    _rate_limit_cooldown_until = rl_exc.cooldown_until
                    if remaining_cd > 0:
                        write_cooldown_lockfile(remaining_cd)
                    logger.error(
                        "RateLimitCooldown raised — pausing ALL daemon work for %.0fs (%.1f min): %s",
                        remaining_cd,
                        remaining_cd / 60,
                        rl_exc,
                    )
                    result = {"gate_verdict": "rate_limit_cooldown", "rate_limit_events": 1}
                else:
                    logger.warning(
                        "Engine-level cooldown raised (engine=%s status=%s, %.0fs): %s",
                        rl_exc.engine_name or "unknown",
                        rl_exc.status or "cooldown",
                        remaining_cd,
                        rl_exc,
                    )
                    result = {
                        "gate_verdict": "engine_cooldown",
                        "rate_limit_events": 1,
                        "engine": rl_exc.engine_name,
                        "engine_status": rl_exc.status,
                    }
            except Exception:
                logger.exception("Unhandled error in SWE team cycle")
                result = {"gate_verdict": "error"}

            cycles_run += 1

            try:
                write_status(
                    status_path,
                    cycle_result=result,
                    store=store,
                    interval_seconds=interval_seconds,
                )
            except Exception:
                logger.exception("Failed to write status file")

            if max_cycles and cycles_run >= max_cycles:
                logger.info("Reached max_cycles=%d — stopping daemon", max_cycles)
                break

            if shutdown.wait(timeout=interval_seconds):
                break
    finally:
        if _scheduler:
            _scheduler.stop()
        signal.signal(signal.SIGTERM, prev_sigterm)
        signal.signal(signal.SIGINT, prev_sigint)
    logger.info("SWE Team daemon stopped after %d cycle(s)", cycles_run)


def main() -> None:
    parser = argparse.ArgumentParser(description="SWE Team Runner")
    parser.add_argument("--dry-run", action="store_true", help="Scan but don't persist tickets")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument("--config", help="Path to swe_team.yaml")
    parser.add_argument("--summary", action="store_true", help="Send daily summary to Telegram")
    parser.add_argument("--bootstrap", action="store_true", help="Baseline scan and acknowledge existing issues")
    parser.add_argument("--daemon", action="store_true", help="Run continuously in daemon mode")
    parser.add_argument("--creative", action="store_true", help="Generate creative proposals")
    parser.add_argument(
        "--test-only",
        action="store_true",
        help="Skip monitor/triage; re-run tests on in_development/in_review tickets",
    )
    parser.add_argument(
        "--interval",
        type=int,
        help="Seconds between cycles in daemon mode (default: monitor scan interval)",
    )
    parser.add_argument(
        "--report",
        choices=["daily", "cycle", "status"],
        help="Send a Telegram report and exit (daily|cycle|status). Designed for cron.",
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help="Run Supabase keep-alive check and exit. Useful as a standalone cron job.",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        metavar="N",
        help="Stop daemon after N cycles (default: run forever). Useful for cron launchers.",
    )
    parser.add_argument("--scheduler", action="store_true", help="Enable job scheduler")
    parser.add_argument(
        "--a2a",
        action="store_true",
        help="Start the A2A server alongside the cycle loop for agent-to-agent communication.",
    )
    parser.add_argument(
        "--a2a-port",
        type=int,
        default=18790,
        help="Port for the A2A server (default: 18790). Only used with --a2a.",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    config = load_config(args.config)
    if not config.enabled:
        logger.info("SWE team disabled (enabled=false). Set SWE_TEAM_ENABLED=true to activate.")
        return

    # Build repo router from config (sandbox-only routing)
    repo_router = RepoRouter(config.repos)
    sandbox_repos_map = repo_router.build_repos_map()
    logger.info("RepoRouter loaded %d sandbox repo(s): %s", len(sandbox_repos_map), list(sandbox_repos_map.keys()))

    # Optionally initialise usage governor
    _init_usage_governor(config)

    # Resolve pluggable providers from config via factory functions
    _init_providers(config)

    # --scheduler flag overrides config
    if args.scheduler:
        config.scheduler.enabled = True

    logger.info("=== SWE Team Runner starting ===")

    # Auto-select ticket store backend
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_ANON_KEY"):
        store = SupabaseTicketStore(team_id=config.team_id)
        logger.info("Using Supabase ticket store (team=%s)", config.team_id)
    else:
        store = TicketStore(config.ticket_store_path)
        logger.info("Using JSON ticket store (%s)", config.ticket_store_path)

    # --keep-alive mode — ping Supabase and exit (cron-friendly)
    if args.keep_alive:
        if isinstance(store, SupabaseTicketStore):
            sent = store.keep_alive()
            logger.info(
                "=== Keep-alive: %s ===",
                "ping sent" if sent else "skipped (recent activity)",
            )
        else:
            logger.info("=== Keep-alive: not using Supabase — nothing to do ===")
        return

    # --report mode — send a Telegram report and exit (cron-friendly)
    if args.report:
        if args.report == "daily":
            cost = aggregate_daily_costs(store)
            notify_daily_summary(store, cost_total=cost if cost else None)
            logger.info("=== Daily report sent (cost=$%.2f) ===", cost)
        elif args.report == "cycle":
            # Send a cycle summary from the last status.json
            status_path = config.ticket_store_path.replace("tickets.json", "status.json")
            status_data: Dict[str, Any] = {}
            try:
                with open(status_path) as fh:
                    status_data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                logger.warning("Could not read %s for cycle report", status_path)
            notify_cycle_summary(
                new_tickets=status_data.get("tickets_open", 0),
                triaged=0,
                investigated=0,
                gate_verdict=status_data.get("gate_verdict", "N/A"),
            )
            logger.info("=== Cycle report sent ===")
        elif args.report == "status":
            status_path = config.ticket_store_path.replace("tickets.json", "status.json")
            try:
                with open(status_path) as fh:
                    status_data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                status_data = {"error": "Could not read status.json"}
            notify_status(status_data)
            logger.info("=== Status report sent ===")
        return

    # Daily summary mode — send and exit (legacy flag, kept for backwards compat)
    if args.summary:
        cost = aggregate_daily_costs(store)
        notify_daily_summary(store, cost_total=cost if cost else None)
        logger.info("=== Daily summary sent ===")
        return

    # Test-only mode — re-run tests on in-flight tickets and exit
    if args.test_only:
        result = run_test_only_cycle(config, store)
        logger.info(
            "=== Test-only complete: %d tested, %d passed, %d failed ===",
            result["tested"],
            result["passed"],
            result["failed"],
        )
        return

    # Bootstrap mode — acknowledge existing issues and exit
    if args.bootstrap:
        result = bootstrap_cycle(config, store, dry_run=args.dry_run)
        logger.info(
            "=== Bootstrap complete: %d issue(s) acknowledged ===",
            result["acknowledged"],
        )
        return

    # A2A server — start alongside the cycle loop if requested
    a2a_server = None
    if args.a2a:
        try:
            a2a_server = start_a2a_server(config, store, port=args.a2a_port)
            logger.info("A2A server running on port %d", args.a2a_port)
        except Exception:
            logger.exception("Failed to start A2A server — continuing without it")

    # Agent registry — discover available agents
    try:
        registry = setup_agent_registry(config, store=store)
        agents = registry.list_agents(status="online")
        if agents:
            logger.info("Agent registry: %d online agent(s): %s",
                        len(agents), [a["name"] for a in agents])
    except Exception:
        logger.debug("Agent registry setup failed (non-fatal)", exc_info=True)

    if args.daemon:
        interval = args.interval
        if interval is None:
            interval = max(60, int(config.monitor.scan_interval_minutes * 60))
        try:
            daemon_loop(
                config,
                store,
                interval_seconds=interval,
                dry_run=args.dry_run,
                creative=args.creative,
                max_cycles=args.max_cycles,
                config_path=args.config,
                sandbox_repos_map=sandbox_repos_map,
            )
        finally:
            if a2a_server:
                a2a_server.stop()
        return

    try:
        result = run_cycle(config, store, dry_run=args.dry_run, creative=args.creative, sandbox_repos_map=sandbox_repos_map)
    finally:
        if a2a_server:
            a2a_server.stop()

    logger.info(
        "=== Cycle complete: %d new, %d open, gate=%s ===",
        result["new_tickets"],
        result.get("open_tickets", 0),
        result["gate_verdict"],
    )


if __name__ == "__main__":
    main()
