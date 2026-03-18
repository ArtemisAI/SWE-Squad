"""
Dashboard data generation for SWE Squad observability.

Queries a ticket store (TicketStore or SupabaseTicketStore) and produces
a structured metrics dict suitable for rendering as JSON, HTML, or
Telegram reports.

Usage::

    from scripts.ops.dashboard_data import generate_dashboard_data
    data = generate_dashboard_data(store)
    # data is a dict ready for json.dumps()
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Project bootstrap ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus

logger = logging.getLogger(__name__)
MAX_WEBUI_TITLE_LENGTH = 120

# Status file path (same as swe_cli.py)
STATUS_PATH = PROJECT_ROOT / "data" / "swe_team" / "status.json"


def _load_status() -> Optional[Dict[str, Any]]:
    """Load data/swe_team/status.json if it exists."""
    if not STATUS_PATH.is_file():
        return None
    try:
        with open(STATUS_PATH) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def _parse_timestamp(ts: str) -> Optional[datetime]:
    """Parse an ISO timestamp, returning None on failure."""
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _ticket_github_url(ticket: SWETicket) -> Optional[str]:
    """Extract the GitHub issue URL from ticket metadata, if present."""
    meta = ticket.metadata or {}
    # Check for explicit github_url
    url = meta.get("github_url") or meta.get("issue_url")
    if url:
        return url
    # Try to construct from github_issue_number
    issue_num = meta.get("github_issue_number") or meta.get("issue_number")
    repo = os.environ.get("SWE_GITHUB_REPO", "")
    if issue_num and repo:
        return f"https://github.com/{repo}/issues/{issue_num}"
    return None


def _bucket_ticket_status(status: TicketStatus) -> str:
    """Map lifecycle statuses into WebUI buckets."""
    if status in {
        TicketStatus.RESOLVED,
        TicketStatus.CLOSED,
        TicketStatus.ROLLED_BACK,
    }:
        return "closed"
    if status in {
        TicketStatus.INVESTIGATING,
        TicketStatus.INVESTIGATION_COMPLETE,
        TicketStatus.IN_DEVELOPMENT,
        TicketStatus.IN_REVIEW,
        TicketStatus.TESTING,
        TicketStatus.DEPLOYING,
        TicketStatus.MONITORING,
    }:
        return "in_progress"
    return "open"


def generate_dashboard_data(
    store,
    *,
    hours: int = 24,
    rate_limit_tracker=None,
) -> Dict[str, Any]:
    """Generate dashboard metrics from the ticket store.

    Parameters
    ----------
    store:
        A ``TicketStore`` or ``SupabaseTicketStore`` instance.
    hours:
        Lookback window for "recent" metrics (default 24h).
    rate_limit_tracker:
        Optional ``RateLimitTracker`` for rate limit event counts.

    Returns
    -------
    dict
        A structured metrics dictionary ready for JSON serialisation.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    # ── Fetch all tickets ──────────────────────────────────────────────
    try:
        all_tickets = store.list_all()
    except Exception as exc:
        logger.warning("Failed to list tickets: %s", exc)
        all_tickets = []

    try:
        open_tickets = store.list_open()
    except Exception as exc:
        logger.warning("Failed to list open tickets: %s", exc)
        open_tickets = []

    try:
        recently_resolved = store.list_recently_resolved(hours=hours)
    except Exception as exc:
        logger.warning("Failed to list recently resolved: %s", exc)
        recently_resolved = []

    # ── Ticket summary ─────────────────────────────────────────────────
    severity_counts: Dict[str, int] = {}
    for t in open_tickets:
        key = t.severity.value
        severity_counts[key] = severity_counts.get(key, 0) + 1

    status_counts: Dict[str, int] = {}
    for t in all_tickets:
        key = t.status.value
        status_counts[key] = status_counts.get(key, 0) + 1

    resolved_count = len([
        t for t in all_tickets if t.status == TicketStatus.RESOLVED
    ])
    investigating_count = len([
        t for t in open_tickets if t.status == TicketStatus.INVESTIGATING
    ])

    ticket_summary = {
        "total": len(all_tickets),
        "open": len(open_tickets),
        "resolved": resolved_count,
        "investigating": investigating_count,
        "by_severity": severity_counts,
        "by_status": status_counts,
    }

    # ── Recent activity (last N hours) ─────────────────────────────────
    recent_activity: List[Dict[str, Any]] = []
    for t in all_tickets:
        updated = _parse_timestamp(t.updated_at)
        if updated and updated >= cutoff:
            entry: Dict[str, Any] = {
                "ticket_id": t.ticket_id,
                "title": t.title[:80],
                "action": t.status.value,
                "severity": t.severity.value,
                "timestamp": t.updated_at,
            }
            gh_url = _ticket_github_url(t)
            if gh_url:
                entry["github_url"] = gh_url
            recent_activity.append(entry)

    # Sort by timestamp descending
    recent_activity.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    # ── Ticket lists for WebUI tabs/actioning ───────────────────────────
    tickets_by_state: Dict[str, List[Dict[str, Any]]] = {
        "open": [],
        "in_progress": [],
        "closed": [],
    }
    for t in all_tickets:
        meta = t.metadata or {}
        gh_url = _ticket_github_url(t)
        issue_num = meta.get("github_issue_number") or meta.get("issue_number")
        issue_num_str = str(issue_num) if issue_num is not None else ""
        ticket_row = {
            "ticket_id": t.ticket_id,
            "title": t.title[:MAX_WEBUI_TITLE_LENGTH],
            "severity": t.severity.value,
            "status": t.status.value,
            "assigned_to": t.assigned_to or "",
            "updated_at": t.updated_at,
            "related_tickets": list(t.related_tickets),
            "github_issue_number": issue_num_str,
            "github_url": gh_url or "",
            "github_actions": {
                "view": gh_url or "",
                "assign": gh_url or "",
                "update": f"{gh_url}/edit" if gh_url else "",
                "comment": f"{gh_url}#new_comment_field" if gh_url else "",
                "link": f"{gh_url}#event-link-issue" if gh_url else "",
            },
        }
        tickets_by_state[_bucket_ticket_status(t.status)].append(ticket_row)

    for bucket in tickets_by_state.values():
        bucket.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

    # ── Agent performance ──────────────────────────────────────────────
    investigations_24h = 0
    for t in all_tickets:
        if t.status in (
            TicketStatus.INVESTIGATION_COMPLETE,
            TicketStatus.IN_DEVELOPMENT,
            TicketStatus.IN_REVIEW,
            TicketStatus.RESOLVED,
        ) and t.investigation_report:
            updated = _parse_timestamp(t.updated_at)
            if updated and updated >= cutoff:
                investigations_24h += 1

    fixes_attempted = len(recently_resolved) + len([
        t for t in all_tickets
        if t.status in (TicketStatus.IN_DEVELOPMENT, TicketStatus.IN_REVIEW, TicketStatus.TESTING)
        and _parse_timestamp(t.updated_at)
        and _parse_timestamp(t.updated_at) >= cutoff  # type: ignore[operator]
    ])

    fixes_succeeded = len([
        t for t in recently_resolved
        if t.test_results and t.test_results.get("status") == "pass"
    ])

    fix_success_rate = (
        round(fixes_succeeded / fixes_attempted, 2) if fixes_attempted > 0 else 0.0
    )

    agent_performance = {
        "investigations_24h": investigations_24h,
        "fixes_attempted_24h": fixes_attempted,
        "fixes_succeeded_24h": fixes_succeeded,
        "fix_success_rate": fix_success_rate,
    }

    # ── Memory stats ───────────────────────────────────────────────────
    total_embeddings = 0
    memory_hits_24h = 0
    confidence_values: List[float] = []

    for t in all_tickets:
        meta = t.metadata or {}
        # Count tickets with embeddings
        if meta.get("has_embedding") or meta.get("embedding_stored"):
            total_embeddings += 1
        # Memory hit tracking
        if meta.get("memory_hit"):
            hit_ts = _parse_timestamp(str(meta.get("memory_hit_at", "")))
            if hit_ts and hit_ts >= cutoff:
                memory_hits_24h += 1
        # Confidence tracking
        fc = meta.get("fix_confidence", {})
        if isinstance(fc, dict) and "confidence" in fc:
            try:
                confidence_values.append(float(fc["confidence"]))
            except (ValueError, TypeError):
                pass

    avg_confidence = (
        round(sum(confidence_values) / len(confidence_values), 2)
        if confidence_values
        else 0.0
    )

    memory_stats = {
        "total_embeddings": total_embeddings,
        "memory_hits_24h": memory_hits_24h,
        "avg_confidence": avg_confidence,
    }

    # ── Rate limit events ──────────────────────────────────────────────
    rate_limit_events_24h = 0
    if rate_limit_tracker:
        try:
            rate_limit_events_24h = len(
                rate_limit_tracker.recent_events(hours=float(hours))
            )
        except Exception:
            pass

    # ── Last cycle info ────────────────────────────────────────────────
    status = _load_status()
    last_cycle: Optional[Dict[str, Any]] = None
    if status:
        last_cycle = {
            "time": status.get("last_cycle"),
            "gate_verdict": status.get("gate_verdict"),
            "tickets_open": status.get("tickets_open"),
            "tickets_investigating": status.get("tickets_investigating"),
            "next_cycle": status.get("next_cycle"),
        }

    return {
        "ticket_summary": ticket_summary,
        "recent_activity": recent_activity,
        "tickets_by_state": tickets_by_state,
        "agent_performance": agent_performance,
        "memory_stats": memory_stats,
        "rate_limit_events_24h": rate_limit_events_24h,
        "last_cycle": last_cycle,
        "generated_at": now.isoformat(),
    }


def format_dashboard_telegram(data: Dict[str, Any]) -> str:
    """Format dashboard data as an HTML Telegram message.

    Parameters
    ----------
    data:
        Output of :func:`generate_dashboard_data`.

    Returns
    -------
    str
        HTML-formatted string for Telegram ``sendMessage``.
    """
    ts = data.get("ticket_summary", {})
    ap = data.get("agent_performance", {})
    ms = data.get("memory_stats", {})
    lc = data.get("last_cycle") or {}

    # Severity emoji mapping
    sev_emoji = {
        "critical": "\U0001f534",  # red circle
        "high": "\U0001f7e0",      # orange circle
        "medium": "\U0001f7e1",    # yellow circle
        "low": "\u26aa",           # white circle
    }

    lines = [
        "<b>\U0001f4ca SWE Squad Dashboard</b>",
        "",
        "<b>Tickets</b>",
        f"  Total: {ts.get('total', 0)} | Open: {ts.get('open', 0)} | "
        f"Resolved: {ts.get('resolved', 0)}",
    ]

    # Severity breakdown
    by_sev = ts.get("by_severity", {})
    if by_sev:
        sev_parts = []
        for sev in ("critical", "high", "medium", "low"):
            count = by_sev.get(sev, 0)
            if count:
                emoji = sev_emoji.get(sev, "")
                sev_parts.append(f"{emoji} {sev.upper()}: {count}")
        if sev_parts:
            lines.append("  " + " | ".join(sev_parts))

    # Agent performance
    lines.extend([
        "",
        "<b>Agent Performance (24h)</b>",
        f"  Investigations: {ap.get('investigations_24h', 0)}",
        f"  Fixes attempted: {ap.get('fixes_attempted_24h', 0)}",
        f"  Fixes succeeded: {ap.get('fixes_succeeded_24h', 0)}",
        f"  Success rate: {ap.get('fix_success_rate', 0):.0%}",
    ])

    # Rate limits
    rl = data.get("rate_limit_events_24h", 0)
    if rl:
        lines.extend([
            "",
            f"<b>Rate limit events (24h):</b> {rl}",
        ])

    # Memory stats
    if ms.get("total_embeddings", 0) > 0:
        lines.extend([
            "",
            "<b>Semantic Memory</b>",
            f"  Embeddings: {ms.get('total_embeddings', 0)}",
            f"  Memory hits (24h): {ms.get('memory_hits_24h', 0)}",
            f"  Avg confidence: {ms.get('avg_confidence', 0):.2f}",
        ])

    # Last cycle
    if lc:
        verdict = lc.get("gate_verdict", "N/A")
        lines.extend([
            "",
            "<b>Last Cycle</b>",
            f"  Time: {lc.get('time', 'N/A')}",
            f"  Gate: <b>{_esc(str(verdict))}</b>",
        ])

    # Open tickets with GitHub links
    recent = data.get("recent_activity", [])
    open_recent = [
        a for a in recent
        if a.get("action") not in ("resolved", "closed", "acknowledged")
    ][:5]
    if open_recent:
        lines.extend(["", "<b>Recent Activity</b>"])
        for a in open_recent:
            sev = a.get("severity", "medium")
            emoji = sev_emoji.get(sev, "")
            title = _esc(a.get("title", "")[:60])
            line = f"  {emoji} [{a.get('action', '')}] {title}"
            gh_url = a.get("github_url")
            if gh_url:
                line += f"\n    <a href=\"{_esc(gh_url)}\">View issue</a>"
            lines.append(line)

    lines.append(f"\nGenerated: {data.get('generated_at', 'N/A')[:19]}Z")

    return "\n".join(lines)


def render_dashboard_html(data: Dict[str, Any]) -> str:
    """Render the dashboard data into a self-contained HTML page.

    Reads the template from ``templates/dashboard.html`` and injects the
    JSON data inline.  If the template is not found, returns a minimal
    fallback page.

    Parameters
    ----------
    data:
        Output of :func:`generate_dashboard_data`.

    Returns
    -------
    str
        Complete HTML document string.
    """
    template_path = PROJECT_ROOT / "templates" / "dashboard.html"
    if not template_path.is_file():
        # Fallback: minimal HTML with JSON dump
        json_str = json.dumps(data, indent=2)
        return (
            "<!DOCTYPE html><html><head><title>SWE Squad Dashboard</title></head>"
            f"<body><h1>SWE Squad Dashboard</h1><pre>{json_str}</pre></body></html>"
        )

    template = template_path.read_text()
    json_str = json.dumps(data)
    # Replace the placeholder in the template
    html = template.replace("__DASHBOARD_DATA__", json_str)
    return html


def _esc(text: str) -> str:
    """Escape HTML for Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
