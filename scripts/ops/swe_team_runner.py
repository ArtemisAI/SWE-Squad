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
from src.swe_team.supabase_store import SupabaseTicketStore
from src.swe_team.notifier import notify_new_tickets, notify_stability_gate, notify_daily_summary
from src.swe_team.github_integration import create_github_issue, find_existing_issue
from src.swe_team.events import SWEEvent
from src.swe_team.creative_agent import CreativeAgent
from src.swe_team.distiller import TrajectoryDistiller
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


def run_cycle(
    config,
    store,
    dry_run: bool = False,
    creative: bool = False,
) -> Dict[str, Any]:
    """Run one monitor -> triage -> gate cycle."""
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
        investigator = InvestigatorAgent()
        try:
            investigated = investigator.investigate_batch(pending_investigation, limit=5)
            for ticket in investigated:
                try:
                    store.add(ticket)
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
        dev = DeveloperAgent(repo_root=PROJECT_ROOT)
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

    return {
        "new_tickets": len(new_tickets),
        "triaged": len(triaged),
        "investigated": len(investigated),
        "gate_verdict": report.verdict.value,
        "gate_details": report.details,
        "open_tickets": len(store.list_open()),
    }


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
) -> None:
    """Run monitor/triage cycles continuously until signaled to stop."""
    shutdown = threading.Event()

    def _signal_handler(signum, _frame):
        logger.info("Shutdown signal received (%s)", signum)
        shutdown.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    logger.info("SWE Team daemon starting (interval=%ds)", interval_seconds)
    while not shutdown.is_set():
        try:
            run_cycle(config, store, dry_run=dry_run, creative=creative)
        except Exception:
            logger.exception("Unhandled error in SWE team cycle")
        if shutdown.wait(timeout=interval_seconds):
            break
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
        "--interval",
        type=int,
        help="Seconds between cycles in daemon mode (default: monitor scan interval)",
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

    # Daily summary mode — send and exit
    if args.summary:
        notify_daily_summary(store)
        logger.info("=== Daily summary sent ===")
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
