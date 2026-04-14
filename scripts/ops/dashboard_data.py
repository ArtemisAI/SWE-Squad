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
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# ── Project bootstrap ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketStatus

logger = logging.getLogger(__name__)
MAX_WEBUI_TITLE_LENGTH = 120

# Status file path (same as swe_cli.py)
STATUS_PATH = PROJECT_ROOT / "data" / "swe_team" / "status.json"

_GH_ISSUE_FIELDS = "number,title,url,labels,state,createdAt,updatedAt"


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


def fetch_github_issues(
    repos: List[str],
    max_per_repo: int = 100,
    timeout: int = 30,
) -> Dict[str, List[dict]]:
    """Fetch open issues from multiple GitHub repos via ``gh issue list``.

    Parameters
    ----------
    repos:
        List of ``owner/repo`` strings. Empty/whitespace entries are skipped.
    max_per_repo:
        Maximum issues to fetch per repo.
    timeout:
        Subprocess timeout in seconds.

    Returns
    -------
    dict
        Mapping of ``repo -> [issue_dict, ...]``. Failed or empty repos
        map to ``[]``.
    """
    result: Dict[str, List[dict]] = {}
    for repo in repos:
        if not repo or not repo.strip():

            continue
        try:
            proc = subprocess.run(
                [
                    "gh", "issue", "list",
                    "--repo", repo,
                    "--state", "open",
                    "--limit", str(max_per_repo),
                    "--json", _GH_ISSUE_FIELDS,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0:
                logger.warning("gh issue list failed for %s: %s", repo, proc.stderr)
                result[repo] = []
                continue
            if not proc.stdout.strip():
                result[repo] = []
                continue
            issues = json.loads(proc.stdout)
            result[repo] = issues if isinstance(issues, list) else []
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed fetching issues for %s: %s", repo, exc)
            result[repo] = []
    return result


def _build_github_summary(
    repos: List[str],
    known_fingerprints: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Build a github_summary block from fetched issues."""
    if not repos:
        return {
            "enabled": False,
            "total_open": 0,
            "by_repo": {},
            "orphaned_count": 0,
            "linked_count": 0,
        }

    issues_by_repo = fetch_github_issues(repos)
    fps = known_fingerprints or set()
    total_open = 0
    by_repo: Dict[str, int] = {}
    orphaned = 0
    linked = 0

    for repo, issues in issues_by_repo.items():
        count = len(issues)
        total_open += count
        by_repo[repo] = count
        for issue in issues:
            fp = f"gh-issue-{issue.get('number', 0)}"
            if fp in fps:
                linked += 1
            else:
                orphaned += 1

    return {
        "enabled": True,
        "total_open": total_open,
        "by_repo": by_repo,
        "orphaned_count": orphaned,
        "linked_count": linked,
    }


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return float(sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight)



def generate_dashboard_data(
    store,
    *,
    hours: int = 24,
    rate_limit_tracker=None,
    github_repos: Optional[List[str]] = None,
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
    github_repos:
        Optional list of ``owner/repo`` strings. When provided, fetches open
        GitHub issues and includes a ``github_summary`` key in the output.

    Returns
    -------
    dict
        A structured metrics dictionary ready for JSON serialisation.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    # ── Fetch all tickets ──────────────────────────────────────────────
    # In dashboard mode, prefer the all-teams methods (no team_id filter) so
    # the overview shows tickets from every squad, not just the one whose
    # SWE_TEAM_ID is set on the webui VM.
    try:
        if hasattr(store, "list_all_teams"):
            all_tickets = store.list_all_teams()
        else:
            all_tickets = store.list_all()
    except Exception as exc:
        logger.warning("Failed to list tickets: %s", exc)
        all_tickets = []

    try:
        if hasattr(store, "list_open_all_teams"):
            open_tickets = store.list_open_all_teams()
        else:
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
                "title": (t.title[:117] + "...") if len(t.title) > MAX_WEBUI_TITLE_LENGTH else t.title,
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
            "project_id": t.project_id or meta.get("project_id") or "",
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

    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for bucket in tickets_by_state.values():
        # Sort by updated_at descending first, then stable-sort by severity ascending
        # so critical tickets appear first, with recency as tiebreaker
        bucket.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        bucket.sort(key=lambda x: SEV_ORDER.get(x.get("severity", "low"), 9))

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
        if confidence_values and total_embeddings > 0
        else 0.0
    )

    memory_stats = {
        "total_embeddings": total_embeddings,
        "memory_hits_24h": memory_hits_24h,
        "avg_confidence": avg_confidence,
    }

    # ── PR lifecycle metrics ───────────────────────────────────────────
    review_durations_minutes: List[float] = []
    review_cycle_values: List[float] = []
    created_total = 0
    created_24h = 0
    merged_total = 0
    merged_24h = 0
    reviewed_total = 0
    verification_started_total = 0
    verification_pass_total = 0
    verification_regression_total = 0
    resolved_total = 0

    for t in all_tickets:
        meta = t.metadata if isinstance(t.metadata, dict) else {}
        lifecycle = meta.get("pr_lifecycle")
        if not isinstance(lifecycle, dict):
            continue

        pr_created_at = _parse_timestamp(lifecycle.get("pr_created_at"))
        first_review_at = _parse_timestamp(lifecycle.get("first_review_at"))
        merged_at = _parse_timestamp(lifecycle.get("merged_at"))
        verification_started_at = _parse_timestamp(lifecycle.get("verification_started_at"))
        resolved_at = _parse_timestamp(lifecycle.get("resolved_at"))
        verification_result = str(lifecycle.get("verification_result", "")).lower()

        if pr_created_at:
            created_total += 1
            if pr_created_at >= cutoff:
                created_24h += 1
        if merged_at:
            merged_total += 1
            if merged_at >= cutoff:
                merged_24h += 1
        if first_review_at:
            reviewed_total += 1
            if pr_created_at and first_review_at >= pr_created_at:
                review_durations_minutes.append((first_review_at - pr_created_at).total_seconds() / 60.0)
        if isinstance(lifecycle.get("review_cycles"), int):
            review_cycle_values.append(float(lifecycle["review_cycles"]))
        if verification_started_at:
            verification_started_total += 1
        if verification_result == "pass":
            verification_pass_total += 1
        elif verification_result == "regression":
            verification_regression_total += 1
        if resolved_at:
            resolved_total += 1

    verification_completed = verification_pass_total + verification_regression_total
    pr_lifecycle_metrics = {
        "prs_created_total": created_total,
        "prs_created_24h": created_24h,
        "prs_reviewed_total": reviewed_total,
        "prs_merged_total": merged_total,
        "prs_merged_24h": merged_24h,
        "merge_rate": round((merged_total / created_total), 3) if created_total else 0.0,
        "verification_started_total": verification_started_total,
        "verification_pass_total": verification_pass_total,
        "verification_regression_total": verification_regression_total,
        "verification_pass_rate": round((verification_pass_total / verification_completed), 3)
        if verification_completed
        else 0.0,
        "resolved_total": resolved_total,
        "median_time_to_first_review_minutes": round(statistics.median(review_durations_minutes), 2)
        if review_durations_minutes
        else 0.0,
        "p95_time_to_first_review_minutes": round(_percentile(review_durations_minutes, 95), 2)
        if review_durations_minutes
        else 0.0,
        "median_review_cycles": round(statistics.median(review_cycle_values), 2)
        if review_cycle_values
        else 0.0,
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
    # Compute status.json age so the frontend can show a staleness warning (#115)
    status_age_seconds: Optional[float] = None
    try:
        if STATUS_PATH.is_file():
            status_age_seconds = round(
                (now - datetime.fromtimestamp(STATUS_PATH.stat().st_mtime, tz=timezone.utc)).total_seconds(),
                1,
            )
    except OSError:
        pass

    status = _load_status()
    last_cycle: Optional[Dict[str, Any]] = None
    if status:
        last_cycle = {
            "time": status.get("last_cycle"),
            "gate_verdict": status.get("gate_verdict"),
            # Use live counts so the panel is never stale vs the ticket store (#115)
            "tickets_open": ticket_summary["open"],
            "tickets_investigating": ticket_summary["investigating"],
            "next_cycle": status.get("next_cycle"),
            "status_age_seconds": status_age_seconds,
        }

    # ── BASE_LLM proxy health ──────────────────────────────────────────
    try:
        from src.swe_team.embeddings import get_base_llm_status
        base_llm_status = get_base_llm_status()
    except Exception:
        base_llm_status = "unknown"

    # ── GitHub multi-repo summary ────────────────────────────────────
    known_fps: Set[str] = set()
    try:
        known_fps = getattr(store, "known_fingerprints", set()) or set()
    except Exception:
        pass
    github_summary = _build_github_summary(github_repos or [], known_fps)

    return {
        "ticket_summary": ticket_summary,
        "recent_activity": recent_activity,
        "tickets_by_state": tickets_by_state,
        "agent_performance": agent_performance,
        "memory_stats": memory_stats,
        "pr_lifecycle_metrics": pr_lifecycle_metrics,
        "rate_limit_events_24h": rate_limit_events_24h,
        "last_cycle": last_cycle,
        "base_llm_status": base_llm_status,
        "github_summary": github_summary,
        "generated_at": now.isoformat(),
        "status_age_seconds": status_age_seconds,
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
    pm = data.get("pr_lifecycle_metrics", {})
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

    lines.extend([
        "",
        "<b>PR Lifecycle</b>",
        f"  PRs created (24h): {pm.get('prs_created_24h', 0)}",
        f"  PRs merged (24h): {pm.get('prs_merged_24h', 0)}",
        f"  Merge rate: {pm.get('merge_rate', 0):.0%}",
        f"  Median time to first review: {pm.get('median_time_to_first_review_minutes', 0)} min",
        f"  Verification pass rate: {pm.get('verification_pass_rate', 0):.0%}",
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
