#!/usr/bin/env python3
"""SWE Team Runner — autonomous monitoring, triage, and stability gate."""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List
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

from src.swe_team.config import load_config
from src.swe_team.investigator import InvestigatorAgent
from src.swe_team.models import GovernanceVerdict, SWETicket, TicketSeverity, TicketStatus
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
from src.swe_team.github_integration import create_github_issue, find_existing_issue
from src.swe_team.events import SWEEvent
from src.swe_team.creative_agent import CreativeAgent
from src.swe_team.distiller import TrajectoryDistiller
from src.swe_team.preflight import PreflightCheck
from src.swe_team.model_probe import ModelProbe
from src.a2a.adapters.swe_team import dispatch_swe_events

logger = logging.getLogger("swe_team")


def comment_on_github_issue(issue_number: int, body: str) -> None:
    """Post a status update comment on a GitHub issue."""
    try:
        subprocess.run(
            ["gh", "issue", "comment", str(issue_number), "--body", body],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        logger.exception("Failed to comment on issue #%d", issue_number)


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

        # Check if this fingerprint appears in recent logs
        if fingerprint not in monitor._known and not _fingerprint_in_recent_logs(fingerprint, monitor):
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


def fetch_github_tickets(store, github_account: str = "") -> List[SWETicket]:
    """Fetch open GitHub issues assigned to the team's GitHub account."""
    if not github_account:
        logger.debug("No github_account configured — skipping GitHub issue fetch")
        return []
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--state", "open", "--assignee", github_account,
             "--json", "number,title,body,labels", "--limit", "20"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return []

        issues = json.loads(result.stdout)
        new_tickets: List[SWETicket] = []
        for issue in issues:
            # Check if we already have a ticket for this issue
            fingerprint = f"gh-issue-{issue['number']}"
            if fingerprint in store.known_fingerprints:
                continue

            # Detect severity from labels and title
            label_names = [l.get("name", "").lower() for l in issue.get("labels", [])]
            title_lower = issue["title"].lower()
            if any("critical" in l or "p0" in l for l in label_names) or "p0" in title_lower:
                severity = TicketSeverity.CRITICAL
            elif any("high" in l or "p1" in l for l in label_names) or "p1" in title_lower:
                severity = TicketSeverity.HIGH
            elif any("low" in l for l in label_names) or "p3" in title_lower:
                severity = TicketSeverity.LOW
            else:
                severity = TicketSeverity.HIGH  # Default for assigned issues

            # Detect module from labels
            module = "unknown"
            for l in label_names:
                if "module:" in l:
                    module = l.replace("module:", "").strip()
                    break

            ticket = SWETicket(
                title=f"[GH-{issue['number']}] {issue['title'][:100]}",
                description=(issue.get("body") or "")[:500],
                severity=severity,
                source_module=module,
                metadata={"github_issue": issue["number"], "fingerprint": fingerprint},
            )
            new_tickets.append(ticket)
        return new_tickets
    except Exception:
        logger.exception("Failed to fetch GitHub issues")
        return []


def setup_logging(verbose: bool = False) -> None:
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    handlers = [
        logging.FileHandler(log_dir / "swe_team.log"),
        logging.StreamHandler(),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


# ---------------------------------------------------------------------------
# Heartbeat stall detection
# ---------------------------------------------------------------------------

_STALL_THRESHOLD_HOURS = 2


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


def run_cycle(
    config,
    store,
    dry_run: bool = False,
    creative: bool = False,
) -> Dict[str, Any]:
    """Run one monitor -> triage -> gate cycle."""
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
    preflight = PreflightCheck(
        expected_git_name=os.environ.get("SWE_EXPECTED_GIT_NAME"),
        expected_git_email=os.environ.get("SWE_EXPECTED_GIT_EMAIL"),
        expected_github_account=config.github_account or None,
        expected_repo_root=PROJECT_ROOT if os.environ.get("SWE_GITHUB_REPO") else None,
        required_env_vars=["SWE_TEAM_ID", "SWE_GITHUB_REPO"],
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

    # 0.7. Heartbeat stall detection — reset stuck tickets
    stalled = detect_stalled_tickets(store)
    if stalled:
        logger.info("Reset %d stalled ticket(s)", len(stalled))

    # 0a. Collect remote logs before scanning
    from src.swe_team.remote_logs import collect_remote_logs
    try:
        remote_dirs = collect_remote_logs()
        if remote_dirs:
            config.monitor.log_directories.extend(remote_dirs)
            logger.info("Added %d remote log directories", len(remote_dirs))
    except Exception:
        logger.exception("Remote log collection failed — scanning local only")

    # 0b. Pick up GitHub issues assigned to this team's GitHub account
    gh_tickets = fetch_github_tickets(store, github_account=config.github_account)
    if gh_tickets:
        logger.info("Fetched %d new GitHub issue ticket(s)", len(gh_tickets))
        for gt in gh_tickets:
            issue_num = gt.metadata.get("github_issue")
            if issue_num and not dry_run:
                comment_on_github_issue(
                    issue_num,
                    f"🤖 **SWE Squad picked up this issue.**\n\n"
                    f"Status: `TRIAGED` — queued for investigation.\n"
                    f"Team: `{config.team_id}` | Account: `{config.github_account}`",
                )

    # 1. Monitor: scan logs for new errors
    monitor = MonitorAgent(config.monitor, known_fingerprints=store.known_fingerprints)
    new_tickets = monitor.scan()

    # 1a. Post-fix regression check on recently resolved tickets
    if not dry_run:
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

    if not new_tickets:
        logger.info("No new issues detected")
        return {"new_tickets": 0, "triaged": 0, "investigated": 0, "gate_verdict": "N/A"}

    logger.info("Detected %d new issue(s)", len(new_tickets))
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
    triage = TriageAgent(config)
    triaged = triage.triage_batch(new_tickets)

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

    # 4. Trajectory distiller: attempt deterministic fixes before investigation
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

    # 5. Investigation (HIGH/CRITICAL only, max 5 per cycle)
    investigated: List[SWETicket] = []
    automated_ids = {t.ticket_id for t in automated}
    # Build the filtered list once for investigation.
    pending_investigation = [
        ticket for ticket in triaged if ticket.ticket_id not in automated_ids
    ]
    if pending_investigation and not dry_run:
        investigator = InvestigatorAgent(
            store=store,
            memory_top_k=config.memory.top_k,
            memory_similarity_floor=config.memory.similarity_floor,
            model_config=config.models,
        )
        try:
            investigated = investigator.investigate_batch(pending_investigation, limit=5)
            for ticket in investigated:
                try:
                    store.add(ticket)
                    store_ticket_embedding(
                        store,
                        ticket,
                        enabled=config.memory.store_on_investigation_complete,
                    )
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

    # 5b. Document investigation results on GitHub issues
    for ticket in investigated:
        issue_num = ticket.metadata.get("github_issue")
        if issue_num and ticket.investigation_report:
            comment_on_github_issue(
                issue_num,
                f"## 🔍 Investigation Complete\n\n"
                f"**Status:** `INVESTIGATION_COMPLETE`\n"
                f"**Module:** `{ticket.source_module or 'unknown'}`\n\n"
                f"{ticket.investigation_report[:2000]}",
            )

    # 5c. Dev agent: attempt fixes for investigated tickets
    if investigated and not dry_run:
        from src.swe_team.developer import DeveloperAgent
        dev = DeveloperAgent(repo_root=PROJECT_ROOT, model_config=config.models)
        for ticket in investigated:
            if ticket.investigation_report and ticket.severity.value in ("critical", "high"):
                try:
                    fix_ok = dev.attempt_fix(ticket)
                    store.add(ticket)  # persist fix result
                    issue_num = ticket.metadata.get("github_issue")
                    if fix_ok and ticket.metadata.get("attempts"):
                        last = ticket.metadata["attempts"][-1]
                        branch = last.get("branch", "?")
                        logger.info("Fix succeeded for %s on branch %s", ticket.ticket_id, branch)
                        if issue_num:
                            comment_on_github_issue(
                                issue_num,
                                f"## ✅ Fix Attempted — SUCCESS\n\n"
                                f"**Branch:** `{branch}`\n"
                                f"**Files changed:** {last.get('files_changed', '?')}\n"
                                f"**Lines changed:** {last.get('lines_changed', '?')}\n"
                                f"**Tests:** passing\n\n"
                                f"Fix is on branch `{branch}`. Ready for human review.",
                            )
                    elif issue_num:
                        attempts = ticket.metadata.get("attempts", [])
                        comment_on_github_issue(
                            issue_num,
                            f"## ❌ Fix Attempted — FAILED\n\n"
                            f"**Attempts:** {len(attempts)}/{dev._max_attempts}\n"
                            f"**Last error:** `{attempts[-1].get('error', '?')[:200] if attempts else '?'}`\n\n"
                            f"Escalating to HITL.",
                        )
                except Exception:
                    logger.exception("Dev agent failed for ticket %s", ticket.ticket_id)

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

    result = {
        "new_tickets": len(new_tickets),
        "triaged": len(triaged),
        "investigated": len(investigated),
        "gate_verdict": report.verdict.value,
        "gate_details": report.details,
        "open_tickets": len(store.list_open()),
        "cycle_cost_usd": round(cycle_cost, 4),
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

    # 10. Append session progress log
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

    return result


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
) -> None:
    """Run monitor/triage cycles continuously until signaled to stop."""
    shutdown = threading.Event()

    def _signal_handler(signum, _frame):
        logger.info("Shutdown signal received (%s)", signum)
        shutdown.set()

    prev_sigterm = signal.signal(signal.SIGTERM, _signal_handler)
    prev_sigint = signal.signal(signal.SIGINT, _signal_handler)

    logger.info("SWE Team daemon starting (interval=%ds)", interval_seconds)
    try:
        while not shutdown.is_set():
            try:
                result = run_cycle(config, store, dry_run=dry_run, creative=creative)
            except Exception:
                logger.exception("Unhandled error in SWE team cycle")
                result = {"gate_verdict": "error"}

            try:
                write_status(
                    status_path,
                    cycle_result=result,
                    store=store,
                    interval_seconds=interval_seconds,
                )
            except Exception:
                logger.exception("Failed to write status file")

            if shutdown.wait(timeout=interval_seconds):
                break
    finally:
        signal.signal(signal.SIGTERM, prev_sigterm)
        signal.signal(signal.SIGINT, prev_sigint)
    logger.info("SWE Team daemon stopped")


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
    args = parser.parse_args()

    setup_logging(args.verbose)

    config = load_config(args.config)
    if not config.enabled:
        logger.info("SWE team disabled (enabled=false). Set SWE_TEAM_ENABLED=true to activate.")
        return

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

    if args.daemon:
        interval = args.interval
        if interval is None:
            interval = max(60, int(config.monitor.scan_interval_minutes * 60))
        daemon_loop(
            config,
            store,
            interval_seconds=interval,
            dry_run=args.dry_run,
            creative=args.creative,
        )
        return

    result = run_cycle(config, store, dry_run=args.dry_run, creative=args.creative)

    logger.info(
        "=== Cycle complete: %d new, %d open, gate=%s ===",
        result["new_tickets"],
        result.get("open_tickets", 0),
        result["gate_verdict"],
    )


if __name__ == "__main__":
    main()
