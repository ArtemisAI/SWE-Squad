"""Rich terminal output helpers for swe_cli.py.

Provides beautiful tables, panels, and color-coded output when the ``rich``
library is available.  Every public function accepts the same data that the
plain-text renderers in swe_cli.py already produce, so callers simply branch
on ``HAS_RICH`` and call the appropriate renderer.

Graceful degradation: if ``rich`` is not installed, ``HAS_RICH`` is False
and none of the render functions should be called.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# Shared console instance — respects NO_COLOR env var at import time
console = Console(no_color=bool(os.environ.get("NO_COLOR"))) if HAS_RICH else None  # type: ignore[assignment]

# ── Severity colour map ──────────────────────────────────────────────────────

_SEV_STYLE = {
    "critical": "bold red",
    "high": "yellow",
    "medium": "blue",
    "low": "green",
}

_STATUS_STYLE = {
    "open": "bold white",
    "investigating": "cyan",
    "investigation_complete": "cyan",
    "in_development": "magenta",
    "in_review": "magenta",
    "resolved": "green",
    "closed": "dim",
    "acknowledged": "dim",
}

_GATE_STYLE = {
    "PASS": "bold green",
    "WARN": "bold yellow",
    "BLOCK": "bold red",
}


def _styled(text: str, style: str) -> str:
    """Wrap *text* in Rich markup only if *style* is non-empty."""
    if style:
        return f"[{style}]{text}[/{style}]"
    return str(text)


def _gate_style(gate: str) -> str:
    """Look up gate verdict style, case-insensitive."""
    return _GATE_STYLE.get(gate, "") or _GATE_STYLE.get(gate.upper(), "")


# ── Renderers ────────────────────────────────────────────────────────────────


def render_status(data: Dict[str, Any], status: Optional[Dict[str, Any]]) -> None:
    """Render the ``status`` subcommand output as a Rich panel."""
    counts = data.get("ticket_counts", {})

    # Build the body
    lines: list[str] = []
    if status:
        lines.append(f"Last cycle:     {status.get('last_cycle', 'N/A')}")
        gate = str(status.get("gate_verdict", "N/A"))
        lines.append(f"Gate verdict:   {_styled(gate, _gate_style(gate))}")
        lines.append(f"Next cycle:     {status.get('next_cycle', 'N/A')}")
    else:
        lines.append("[dim]Status file not found (no cycle has run yet)[/dim]")

    lines.append("")
    lines.append(f"Total tickets:  {counts.get('total', 0)}")
    lines.append(f"Open:           {_styled(str(counts.get('open', 0)), 'bold white')}")
    lines.append(f"Investigating:  {_styled(str(counts.get('investigating', 0)), 'cyan')}")
    lines.append(f"In development: {_styled(str(counts.get('in_development', 0)), 'magenta')}")
    lines.append(f"Resolved:       {_styled(str(counts.get('resolved', 0)), 'green')}")

    body = "\n".join(lines)
    console.print(Panel(body, title="[bold]SWE Squad Status[/bold]", border_style="blue"))


def render_tickets(tickets: list, truncate_fn) -> None:
    """Render the ``tickets`` subcommand output as a Rich table."""
    table = Table(title="SWE Squad Tickets", show_lines=False, border_style="dim")
    table.add_column("Ticket ID", style="bold", no_wrap=True)
    table.add_column("Severity", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Assigned To", no_wrap=True)
    table.add_column("Title", overflow="ellipsis")

    for t in tickets:
        sev_val = t.severity.value
        st_val = t.status.value

        table.add_row(
            t.ticket_id,
            _styled(sev_val.upper(), _SEV_STYLE.get(sev_val, "")),
            _styled(st_val, _STATUS_STYLE.get(st_val, "")),
            t.assigned_to or "-",
            truncate_fn(t.title, 50),
        )

    console.print(table)
    console.print(f"\n[bold]{len(tickets)}[/bold] ticket(s)")


def render_issues(issues: List[Dict[str, Any]], truncate_fn) -> None:
    """Render the ``issues`` subcommand output as a Rich table."""
    table = Table(title="GitHub Issues", show_lines=False, border_style="dim")
    table.add_column("#", style="bold cyan", no_wrap=True)
    table.add_column("Created", no_wrap=True)
    table.add_column("Labels")
    table.add_column("Title", overflow="ellipsis")

    for issue in issues:
        num = str(issue.get("number", "?"))
        title = issue.get("title", "")
        created = issue.get("createdAt", "")[:10]
        label_names = [la.get("name", "") for la in issue.get("labels", [])]
        labels_str = ", ".join(label_names) if label_names else "-"
        table.add_row(f"#{num}", created, truncate_fn(labels_str, 28), truncate_fn(title, 50))

    console.print(table)
    console.print(f"\n[bold]{len(issues)}[/bold] issue(s)")


def render_repos(repos: List[Dict[str, Any]], truncate_fn) -> None:
    """Render the ``repos`` subcommand output as a Rich table."""
    table = Table(title="Repositories", show_lines=False, border_style="dim")
    table.add_column("Name", style="bold", overflow="ellipsis")
    table.add_column("Visibility", no_wrap=True)
    table.add_column("Permission", no_wrap=True)

    for repo in repos:
        name = repo.get("name", "?")
        vis = repo.get("visibility", "?")
        perm = repo.get("viewerPermission", "?")
        vis_style = "green" if vis == "PUBLIC" else "yellow"
        table.add_row(
            truncate_fn(name, 38),
            _styled(vis, vis_style),
            perm,
        )

    console.print(table)
    console.print(f"\n[bold]{len(repos)}[/bold] repo(s)")


def render_summary(data: Dict[str, Any]) -> None:
    """Render the ``summary`` subcommand as Rich panels."""
    from datetime import datetime, timezone

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sev_counts = data.get("severity_counts", {})
    st_counts = data.get("status_counts", {})
    fixes = data.get("recent_fixes_24h", {})

    # Overview panel
    overview_lines = [
        f"Generated: {now_str}",
        "",
        f"Open tickets: [bold]{data.get('open_tickets', 0)}[/bold]  |  Total: {data.get('total_tickets', 0)}",
    ]
    console.print(Panel("\n".join(overview_lines), title="[bold]SWE Squad Summary[/bold]", border_style="blue"))

    # Severity table
    if sev_counts:
        sev_table = Table(title="By Severity", show_lines=False, border_style="dim")
        sev_table.add_column("Severity", style="bold")
        sev_table.add_column("Count", justify="right")
        for sev in ("critical", "high", "medium", "low"):
            count = sev_counts.get(sev, 0)
            if count:
                sev_table.add_row(_styled(sev.upper(), _SEV_STYLE.get(sev, "")), str(count))
        console.print(sev_table)

    # Status table
    if st_counts:
        st_table = Table(title="By Status", show_lines=False, border_style="dim")
        st_table.add_column("Status", style="bold")
        st_table.add_column("Count", justify="right")
        for st_name, count in sorted(st_counts.items()):
            st_table.add_row(_styled(st_name, _STATUS_STYLE.get(st_name, "")), str(count))
        console.print(st_table)

    # Activity
    activity_lines = [
        f"Recent investigations (24h): {data.get('recent_investigations_24h', 0)}",
        f"Recent fixes (24h): {fixes.get('total', 0)}",
    ]
    if fixes.get("total", 0):
        activity_lines.append(
            f"  [green]Success: {fixes.get('success', 0)}[/green]  |  [red]Failed: {fixes.get('fail', 0)}[/red]"
        )
    gate = data.get("gate_verdict")
    if gate:
        activity_lines.append(f"Last gate verdict: {_styled(str(gate), _gate_style(str(gate)))}")
    last_cycle = data.get("last_cycle")
    if last_cycle:
        activity_lines.append(f"Last cycle: {last_cycle}")

    console.print(Panel("\n".join(activity_lines), title="Activity", border_style="dim"))


def render_project_list(repos: list, truncate_fn) -> None:
    """Render the ``project list`` subcommand as a Rich table."""
    table = Table(title="Configured Projects", show_lines=False, border_style="dim")
    table.add_column("Name", style="bold", overflow="ellipsis")
    table.add_column("Local Path", overflow="ellipsis")
    table.add_column("Priority", no_wrap=True)
    table.add_column("Status", no_wrap=True)

    for r in repos:
        name = r.get("name", "?")
        local_path = r.get("local_path", "-")
        priority = r.get("priority", "medium")
        status = "monitor-only" if r.get("monitor_only", False) else "active"
        pri_style = {"high": "red", "medium": "yellow", "low": "green"}.get(priority, "")
        st_style = "green" if status == "active" else "dim"
        table.add_row(
            truncate_fn(name, 33),
            truncate_fn(local_path, 38),
            _styled(priority, pri_style),
            _styled(status, st_style),
        )

    console.print(table)
    console.print(f"\n[bold]{len(repos)}[/bold] project(s)")


def render_costs(summary: Dict[str, Any]) -> None:
    """Render the ``costs`` subcommand as Rich output."""
    overview = (
        f"Total cost: [bold]${summary['total_cost_usd']:.4f}[/bold]\n"
        f"Today's spend: ${summary['daily_spend']:.4f}\n"
        f"Total records: {summary['total_records']}"
    )
    console.print(Panel(overview, title="[bold]Token Usage & Costs[/bold]", border_style="blue"))

    by_model = summary.get("by_model", {})
    if by_model:
        table = Table(show_lines=False, border_style="dim")
        table.add_column("Model", style="bold")
        table.add_column("Calls", justify="right")
        table.add_column("Input Tokens", justify="right")
        table.add_column("Output Tokens", justify="right")
        table.add_column("Cost", justify="right", style="green")
        for model, data in by_model.items():
            table.add_row(
                model,
                str(data["calls"]),
                str(data["input_tokens"]),
                str(data["output_tokens"]),
                f"${data['cost_usd']:.4f}",
            )
        console.print(table)
    else:
        console.print("[dim]No token usage recorded yet.[/dim]")
