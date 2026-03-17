#!/usr/bin/env python3
"""SWE Squad CLI — lightweight operational queries and reporting.

Standalone CLI tool (argparse, no external deps beyond python-dotenv)
for querying system status, listing tickets, generating summaries,
and sending reports via Telegram.

Usage:
    swe_cli.py status [--json]
    swe_cli.py tickets [--status STATUS] [--severity SEV] [--team TEAM] [--json]
    swe_cli.py issues [--json]
    swe_cli.py repos [--json]
    swe_cli.py summary [--json]
    swe_cli.py report {daily|status|cycle}
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Project bootstrap ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

# Load .env (same pattern as swe_team_runner.py)
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=True)
except ImportError:
    pass  # dotenv optional at import time; needed at runtime for credentials

logger = logging.getLogger("swe_cli")

# ── Constants ─────────────────────────────────────────────────────────────────
STATUS_PATH = PROJECT_ROOT / "data" / "swe_team" / "status.json"
TICKETS_PATH = PROJECT_ROOT / "data" / "swe_team" / "tickets.json"


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _load_status() -> Optional[Dict[str, Any]]:
    """Load data/swe_team/status.json if it exists."""
    if not STATUS_PATH.is_file():
        return None
    try:
        with open(STATUS_PATH) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def _get_ticket_store():
    """Get a TicketStore instance (JSON-backed)."""
    from src.swe_team.ticket_store import TicketStore
    return TicketStore(str(TICKETS_PATH))


def _truncate(text: str, width: int) -> str:
    """Truncate text to *width* characters, adding ellipsis if needed."""
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def _run_gh(args: List[str], timeout: int = 15) -> Optional[str]:
    """Run a ``gh`` CLI command and return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning("gh command failed: %s", result.stderr.strip())
            return None
        return result.stdout
    except FileNotFoundError:
        logger.warning("GitHub CLI (gh) not found in PATH")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("gh command timed out after %ds", timeout)
        return None
    except Exception as exc:
        logger.warning("gh command error: %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Subcommands
# ══════════════════════════════════════════════════════════════════════════════

def cmd_status(args: argparse.Namespace) -> int:
    """Show current system status."""
    status = _load_status()

    # Also query ticket store for live counts
    try:
        store = _get_ticket_store()
        all_tickets = store.list_all()
        open_tickets = store.list_open()
    except Exception:
        all_tickets = []
        open_tickets = []

    from src.swe_team.models import TicketStatus

    counts = {
        "total": len(all_tickets),
        "open": len([t for t in open_tickets if t.status == TicketStatus.OPEN]),
        "investigating": len(
            [t for t in open_tickets if t.status == TicketStatus.INVESTIGATING]
        ),
        "in_development": len(
            [t for t in open_tickets if t.status == TicketStatus.IN_DEVELOPMENT]
        ),
        "resolved": len(
            [t for t in all_tickets if t.status == TicketStatus.RESOLVED]
        ),
    }

    data = {
        "last_cycle": status.get("last_cycle") if status else None,
        "gate_verdict": status.get("gate_verdict") if status else None,
        "next_cycle": status.get("next_cycle") if status else None,
        "tickets_open": status.get("tickets_open") if status else len(open_tickets),
        "tickets_investigating": (
            status.get("tickets_investigating") if status else counts["investigating"]
        ),
        "ticket_counts": counts,
    }

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    # Formatted text output
    print("=== SWE Squad Status ===\n")
    if status:
        print(f"  Last cycle:     {status.get('last_cycle', 'N/A')}")
        print(f"  Gate verdict:   {status.get('gate_verdict', 'N/A')}")
        print(f"  Next cycle:     {status.get('next_cycle', 'N/A')}")
    else:
        print("  Status file:    not found (no cycle has run yet)")
    print()
    print(f"  Total tickets:  {counts['total']}")
    print(f"  Open:           {counts['open']}")
    print(f"  Investigating:  {counts['investigating']}")
    print(f"  In development: {counts['in_development']}")
    print(f"  Resolved:       {counts['resolved']}")
    return 0


def cmd_tickets(args: argparse.Namespace) -> int:
    """List tickets with optional filters."""
    from src.swe_team.models import TicketSeverity, TicketStatus

    try:
        store = _get_ticket_store()
    except Exception as exc:
        print(f"Error loading ticket store: {exc}", file=sys.stderr)
        return 1

    tickets = store.list_all()

    # Apply filters
    if args.status:
        try:
            target_status = TicketStatus(args.status)
        except ValueError:
            print(f"Unknown status: {args.status}", file=sys.stderr)
            print(
                f"Valid statuses: {', '.join(s.value for s in TicketStatus)}",
                file=sys.stderr,
            )
            return 1
        tickets = [t for t in tickets if t.status == target_status]

    if args.severity:
        try:
            target_sev = TicketSeverity(args.severity)
        except ValueError:
            print(f"Unknown severity: {args.severity}", file=sys.stderr)
            print(
                f"Valid severities: {', '.join(s.value for s in TicketSeverity)}",
                file=sys.stderr,
            )
            return 1
        tickets = [t for t in tickets if t.severity == target_sev]

    if args.team:
        tickets = [t for t in tickets if t.assigned_to == args.team]

    if not args.status:
        # Default: show only open tickets (not resolved/closed/acknowledged)
        closed_statuses = {
            TicketStatus.RESOLVED,
            TicketStatus.CLOSED,
            TicketStatus.ACKNOWLEDGED,
        }
        tickets = [t for t in tickets if t.status not in closed_statuses]

    if args.json:
        print(json.dumps([t.to_dict() for t in tickets], indent=2))
        return 0

    if not tickets:
        print("No tickets found matching filters.")
        return 0

    # Tabular output
    header = f"{'TICKET_ID':<14} {'SEVERITY':<10} {'STATUS':<20} {'ASSIGNED_TO':<16} {'TITLE'}"
    print(header)
    print("-" * len(header))
    for t in tickets:
        print(
            f"{t.ticket_id:<14} "
            f"{t.severity.value:<10} "
            f"{t.status.value:<20} "
            f"{(t.assigned_to or '-'):<16} "
            f"{_truncate(t.title, 50)}"
        )

    print(f"\n{len(tickets)} ticket(s)")
    return 0


def cmd_issues(args: argparse.Namespace) -> int:
    """List GitHub issues assigned to the team."""
    github_account = os.environ.get("SWE_GITHUB_ACCOUNT", "")
    github_repo = os.environ.get("SWE_GITHUB_REPO", "")

    if not github_account:
        print("SWE_GITHUB_ACCOUNT not set in environment.", file=sys.stderr)
        return 1

    gh_args = [
        "issue", "list",
        "--state", "open",
        "--assignee", github_account,
        "--json", "number,title,labels,createdAt",
    ]
    if github_repo:
        gh_args.extend(["--repo", github_repo])

    output = _run_gh(gh_args)
    if output is None:
        print("Failed to fetch GitHub issues.", file=sys.stderr)
        return 1

    try:
        issues = json.loads(output)
    except json.JSONDecodeError:
        print("Failed to parse GitHub CLI output.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(issues, indent=2))
        return 0

    if not issues:
        print("No open issues assigned to the team.")
        return 0

    header = f"{'#':<8} {'CREATED':<12} {'LABELS':<30} {'TITLE'}"
    print(header)
    print("-" * len(header))
    for issue in issues:
        num = issue.get("number", "?")
        title = issue.get("title", "")
        created = issue.get("createdAt", "")[:10]
        label_names = [l.get("name", "") for l in issue.get("labels", [])]
        labels_str = ", ".join(label_names) if label_names else "-"
        print(
            f"#{num:<7} "
            f"{created:<12} "
            f"{_truncate(labels_str, 28):<30} "
            f"{_truncate(title, 50)}"
        )

    print(f"\n{len(issues)} issue(s)")
    return 0


def cmd_repos(args: argparse.Namespace) -> int:
    """List repos the bot account has access to."""
    gh_args = [
        "repo", "list",
        "--json", "name,visibility,viewerPermission",
        "--limit", "50",
    ]

    output = _run_gh(gh_args)
    if output is None:
        print("Failed to list repos.", file=sys.stderr)
        return 1

    try:
        repos = json.loads(output)
    except json.JSONDecodeError:
        print("Failed to parse GitHub CLI output.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(repos, indent=2))
        return 0

    if not repos:
        print("No repos found.")
        return 0

    header = f"{'NAME':<40} {'VISIBILITY':<14} {'PERMISSION'}"
    print(header)
    print("-" * len(header))
    for repo in repos:
        name = repo.get("name", "?")
        vis = repo.get("visibility", "?")
        perm = repo.get("viewerPermission", "?")
        print(f"{_truncate(name, 38):<40} {vis:<14} {perm}")

    print(f"\n{len(repos)} repo(s)")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    """Generate a summary report."""
    from src.swe_team.models import TicketSeverity, TicketStatus

    try:
        store = _get_ticket_store()
    except Exception as exc:
        print(f"Error loading ticket store: {exc}", file=sys.stderr)
        return 1

    all_tickets = store.list_all()
    open_tickets = store.list_open()
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    # Counts by severity
    severity_counts: Dict[str, int] = {}
    for t in open_tickets:
        severity_counts[t.severity.value] = severity_counts.get(t.severity.value, 0) + 1

    # Counts by status
    status_counts: Dict[str, int] = {}
    for t in open_tickets:
        status_counts[t.status.value] = status_counts.get(t.status.value, 0) + 1

    # Recent investigations (last 24h)
    recent_investigations = []
    for t in all_tickets:
        if t.status in (
            TicketStatus.INVESTIGATION_COMPLETE,
            TicketStatus.IN_DEVELOPMENT,
            TicketStatus.IN_REVIEW,
            TicketStatus.RESOLVED,
        ) and t.investigation_report:
            try:
                updated = datetime.fromisoformat(t.updated_at)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated >= cutoff_24h:
                    recent_investigations.append(t)
            except (ValueError, TypeError):
                pass

    # Recent fixes (last 24h)
    recent_resolved = store.list_recently_resolved(hours=24)
    fix_success = len([t for t in recent_resolved if t.test_results and t.test_results.get("status") == "pass"])
    fix_fail = len([t for t in recent_resolved if t.test_results and t.test_results.get("status") == "fail"])

    # Gate verdict history (from status.json)
    status = _load_status()

    data = {
        "generated_at": now.isoformat(),
        "open_tickets": len(open_tickets),
        "total_tickets": len(all_tickets),
        "severity_counts": severity_counts,
        "status_counts": status_counts,
        "recent_investigations_24h": len(recent_investigations),
        "recent_fixes_24h": {
            "total": len(recent_resolved),
            "success": fix_success,
            "fail": fix_fail,
        },
        "gate_verdict": status.get("gate_verdict") if status else None,
        "last_cycle": status.get("last_cycle") if status else None,
    }

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    # Formatted text output
    print("=== SWE Squad Summary ===")
    print(f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}\n")

    print(f"Open tickets: {len(open_tickets)}  |  Total: {len(all_tickets)}")
    print()

    print("By severity:")
    for sev in ("critical", "high", "medium", "low"):
        count = severity_counts.get(sev, 0)
        if count:
            print(f"  {sev.upper():<10} {count}")
    if not severity_counts:
        print("  (none)")

    print("\nBy status:")
    for st, count in sorted(status_counts.items()):
        print(f"  {st:<22} {count}")
    if not status_counts:
        print("  (none)")

    print(f"\nRecent investigations (24h): {len(recent_investigations)}")
    for t in recent_investigations[:5]:
        print(f"  - [{t.severity.value.upper()}] {_truncate(t.title, 60)}")

    print(f"\nRecent fixes (24h): {len(recent_resolved)}")
    if recent_resolved:
        print(f"  Success: {fix_success}  |  Failed: {fix_fail}")

    if status:
        print(f"\nLast gate verdict: {status.get('gate_verdict', 'N/A')}")
        print(f"Last cycle: {status.get('last_cycle', 'N/A')}")

    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Send a report via Telegram."""
    report_type = args.report_type

    if report_type == "daily":
        return _report_daily()
    elif report_type == "status":
        return _report_status()
    elif report_type == "cycle":
        return _report_cycle()
    else:
        print(f"Unknown report type: {report_type}", file=sys.stderr)
        print("Valid types: daily, status, cycle", file=sys.stderr)
        return 1


def _send_telegram(message: str) -> bool:
    """Send a Telegram message, trying swe_team.notifier first."""
    try:
        from src.swe_team.notifier import _send
        return _send(message)
    except ImportError:
        logger.warning("Notifier module not available")
        return False


def _report_daily() -> int:
    """Send daily summary report via Telegram."""
    try:
        store = _get_ticket_store()
        from src.swe_team.notifier import notify_daily_summary
        notify_daily_summary(store)
        print("Daily summary sent.")
        return 0
    except Exception as exc:
        print(f"Failed to send daily summary: {exc}", file=sys.stderr)
        return 1


def _report_status() -> int:
    """Send current status snapshot via Telegram."""
    status = _load_status()
    if not status:
        msg = "<b>SWE Squad Status</b>\n\nNo status file found. No cycles have run yet."
    else:
        msg = (
            f"<b>SWE Squad Status</b>\n\n"
            f"Last cycle: {status.get('last_cycle', 'N/A')}\n"
            f"Open tickets: {status.get('tickets_open', '?')}\n"
            f"Investigating: {status.get('tickets_investigating', '?')}\n"
            f"Gate verdict: {status.get('gate_verdict', 'N/A')}\n"
            f"Next cycle: {status.get('next_cycle', 'N/A')}"
        )

    ok = _send_telegram(msg)
    if ok:
        print("Status report sent.")
        return 0
    else:
        print("Failed to send status report.", file=sys.stderr)
        return 1


def _report_cycle() -> int:
    """Send last cycle results via Telegram."""
    status = _load_status()
    if not status:
        print("No status file found — no cycle results to report.", file=sys.stderr)
        return 1

    msg = (
        f"<b>SWE Squad — Last Cycle</b>\n\n"
        f"Time: {status.get('last_cycle', 'N/A')}\n"
        f"Open tickets: {status.get('tickets_open', '?')}\n"
        f"Investigating: {status.get('tickets_investigating', '?')}\n"
        f"Gate verdict: <b>{status.get('gate_verdict', 'N/A')}</b>"
    )

    ok = _send_telegram(msg)
    if ok:
        print("Cycle report sent.")
        return 0
    else:
        print("Failed to send cycle report.", file=sys.stderr)
        return 1


# ══════════════════════════════════════════════════════════════════════════════
# CLI entry point
# ══════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI."""
    parser = argparse.ArgumentParser(
        prog="swe_cli",
        description="SWE Squad CLI — operational queries and reporting",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Debug logging"
    )

    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    # ── status ────────────────────────────────────────────────────────────
    sp_status = subparsers.add_parser("status", help="Current system status")
    sp_status.add_argument("--json", action="store_true", help="JSON output")
    sp_status.set_defaults(func=cmd_status)

    # ── tickets ───────────────────────────────────────────────────────────
    sp_tickets = subparsers.add_parser("tickets", help="List tickets")
    sp_tickets.add_argument("--status", help="Filter by status (e.g. investigating)")
    sp_tickets.add_argument("--severity", help="Filter by severity (e.g. critical)")
    sp_tickets.add_argument("--team", help="Filter by assigned team/agent")
    sp_tickets.add_argument("--json", action="store_true", help="JSON output")
    sp_tickets.set_defaults(func=cmd_tickets)

    # ── issues ────────────────────────────────────────────────────────────
    sp_issues = subparsers.add_parser(
        "issues", help="List GitHub issues assigned to the team"
    )
    sp_issues.add_argument("--json", action="store_true", help="JSON output")
    sp_issues.set_defaults(func=cmd_issues)

    # ── repos ─────────────────────────────────────────────────────────────
    sp_repos = subparsers.add_parser(
        "repos", help="List repos the bot account has access to"
    )
    sp_repos.add_argument("--json", action="store_true", help="JSON output")
    sp_repos.set_defaults(func=cmd_repos)

    # ── summary ───────────────────────────────────────────────────────────
    sp_summary = subparsers.add_parser("summary", help="Generate summary report")
    sp_summary.add_argument("--json", action="store_true", help="JSON output")
    sp_summary.set_defaults(func=cmd_summary)

    # ── report ────────────────────────────────────────────────────────────
    sp_report = subparsers.add_parser("report", help="Send report via Telegram")
    sp_report.add_argument(
        "report_type",
        choices=["daily", "status", "cycle"],
        help="Report type: daily, status, or cycle",
    )
    sp_report.set_defaults(func=cmd_report)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
