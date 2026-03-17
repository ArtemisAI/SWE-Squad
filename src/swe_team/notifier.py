"""
Telegram notification integration for SWE Team events.

Sends alerts for new high/critical tickets, stability gate blocks,
and daily summaries of open ticket counts.

Uses the project's existing ``send_telegram_alert()`` from
``src.notifications.telegram`` (async, HTML parse mode).  The async
call is wrapped in ``asyncio.run()`` inside ``_send()`` so that all
public functions in this module are synchronous and safe to call from
the CLI runner without event-loop gymnastics.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

from src.swe_team.models import SWETicket, StabilityReport, TicketSeverity

logger = logging.getLogger(__name__)

# Severity emoji mapping
_SEVERITY_EMOJI = {
    TicketSeverity.CRITICAL: "\U0001f534",  # red circle
    TicketSeverity.HIGH: "\U0001f7e0",      # orange circle
    TicketSeverity.MEDIUM: "\U0001f7e1",    # yellow circle
    TicketSeverity.LOW: "\u26aa",           # white circle
}


def _send(message: str) -> bool:
    """Send a Telegram message using the project's async helper.

    Wraps the async ``send_telegram_alert`` in ``asyncio.run()`` so
    callers in synchronous contexts (the CLI runner) can use it directly.
    Returns True on success, False otherwise.
    """
    from src.notifications.telegram import send_telegram_alert

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                send_telegram_alert(message, parse_mode="HTML")
            )
        finally:
            loop.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram send failed: %s", exc)
        return False


def notify_new_tickets(tickets: List[SWETicket]) -> None:
    """Send Telegram alert for new HIGH/CRITICAL tickets.

    Only tickets with severity HIGH or CRITICAL are included.
    Multiple tickets are grouped into a single message.
    """
    important = [
        t for t in tickets
        if t.severity in (TicketSeverity.CRITICAL, TicketSeverity.HIGH)
    ]
    if not important:
        return

    lines = [f"<b>\U0001f6a8 SWE Team — {len(important)} new ticket(s)</b>", ""]
    for t in important:
        emoji = _SEVERITY_EMOJI.get(t.severity, "")
        module = t.source_module or "unknown"
        assignee = t.assigned_to or "unassigned"
        lines.append(
            f"{emoji} <b>[{t.severity.value.upper()}]</b> {_esc(t.title[:80])}\n"
            f"    Module: {_esc(module)} | Assigned: {_esc(assignee)}"
        )

    message = "\n".join(lines)
    _send(message)


def notify_stability_gate(report: StabilityReport) -> None:
    """Alert if the stability gate verdict is BLOCK."""
    from src.swe_team.models import GovernanceVerdict

    if report.verdict != GovernanceVerdict.BLOCK:
        return

    lines = [
        "<b>\U000026d4 Stability Gate BLOCKED</b>",
        "",
        f"Open critical: {report.open_critical}",
        f"Failing tests: {report.failing_tests}",
        f"Details: {_esc(report.details[:300])}",
    ]
    message = "\n".join(lines)
    _send(message)


def notify_daily_summary(store) -> None:
    """Daily summary of open tickets by severity.

    Parameters
    ----------
    store:
        A ``TicketStore`` instance to query.
    """
    all_open = store.list_open()
    if not all_open:
        _send("<b>\U0001f4cb SWE Daily Summary</b>\n\nNo open tickets.")
        return

    counts = {}
    for t in all_open:
        counts[t.severity] = counts.get(t.severity, 0) + 1

    lines = [
        f"<b>\U0001f4cb SWE Daily Summary — {len(all_open)} open ticket(s)</b>",
        "",
    ]
    for sev in (TicketSeverity.CRITICAL, TicketSeverity.HIGH,
                TicketSeverity.MEDIUM, TicketSeverity.LOW):
        count = counts.get(sev, 0)
        if count:
            emoji = _SEVERITY_EMOJI.get(sev, "")
            lines.append(f"{emoji} {sev.value.upper()}: {count}")

    # Status breakdown
    status_counts: dict[str, int] = {}
    for t in all_open:
        status_counts[t.status.value] = status_counts.get(t.status.value, 0) + 1
    if status_counts:
        lines.append("")
        lines.append("<b>By status:</b>")
        for status, count in sorted(status_counts.items()):
            lines.append(f"  {status}: {count}")

    message = "\n".join(lines)
    _send(message)


def notify_investigation_summary(ticket: SWETicket) -> None:
    """Send Telegram summary of an investigation report."""
    if not ticket.investigation_report:
        return

    module = ticket.source_module or "unknown"
    severity = ticket.severity.value.upper()
    title = _esc(ticket.title[:80])
    report = ticket.investigation_report.strip()
    summary = _esc(report.splitlines()[0][:200]) if report else "Report generated"

    lines = [
        "<b>\U0001f50d Investigation complete</b>",
        "",
        f"<b>[{severity}]</b> {title}",
        f"Module: {_esc(module)}",
        f"Ticket: <code>{_esc(ticket.ticket_id)}</code>",
        "",
        f"<b>Summary:</b> {summary}",
    ]
    message = "\n".join(lines)
    _send(message)


def _esc(text: str) -> str:
    """Escape HTML for Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
