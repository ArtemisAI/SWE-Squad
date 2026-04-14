"""
Telegram notification integration for SWE Team events.

Sends alerts for new high/critical tickets, stability gate blocks,
and daily summaries of open ticket counts.

Uses ``src.swe_team.telegram.send_message()`` — a synchronous,
stdlib-only Telegram Bot API client.  All public functions in this
module are synchronous and safe to call from the CLI runner.
"""

from __future__ import annotations

import logging
import time
import threading
from typing import List, Optional

from src.swe_team.models import SWETicket, StabilityReport, TicketSeverity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global rate limiter — prevents Telegram floods
# ---------------------------------------------------------------------------
# Tracks last send time per alert "key" (first 60 chars of message).
# Same alert type won't fire more than once per _RATE_LIMIT_SECONDS.
_RATE_LIMIT_SECONDS = 300  # 5 minutes between identical alert types
_rate_limit_lock = threading.Lock()
_rate_limit_cache: dict = {}  # key -> last_sent_timestamp
# Hard cap: no more than this many messages per minute across all types
_MAX_MESSAGES_PER_MINUTE = 6
_send_timestamps: list = []  # rolling window of send times


def _is_rate_limited(message: str) -> bool:
    """Return True if this message type was sent too recently or global cap hit."""
    now = time.monotonic()
    key = message[:80]  # group by alert type/content prefix
    with _rate_limit_lock:
        # Check per-type cooldown
        last = _rate_limit_cache.get(key, 0)
        if now - last < _RATE_LIMIT_SECONDS:
            logger.debug("Telegram rate-limited (cooldown): %s", key[:40])
            return True
        # Check global per-minute cap
        cutoff = now - 60
        _send_timestamps[:] = [t for t in _send_timestamps if t > cutoff]
        if len(_send_timestamps) >= _MAX_MESSAGES_PER_MINUTE:
            logger.warning("Telegram global cap hit (%d/min) — suppressing", _MAX_MESSAGES_PER_MINUTE)
            return True
        # Allow send — update tracking
        _rate_limit_cache[key] = now
        _send_timestamps.append(now)
        return False

# Severity emoji mapping
_SEVERITY_EMOJI = {
    TicketSeverity.CRITICAL: "\U0001f534",  # red circle
    TicketSeverity.HIGH: "\U0001f7e0",      # orange circle
    TicketSeverity.MEDIUM: "\U0001f7e1",    # yellow circle
    TicketSeverity.LOW: "\u26aa",           # white circle
}


def _send_direct(message: str) -> bool:
    """Send a Telegram message directly via the standalone Bot API client.

    Returns True on success, False otherwise.  Never raises.
    """
    from src.swe_team.telegram import send_message

    try:
        return send_message(message, parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram send failed: %s", exc)
        return False


def _send_via_openclaw(message: str) -> bool:
    """Send a message through the OpenClaw A2A gateway to Telegram.

    Uses the OpenClaw CLI (``docker exec openclaw openclaw message send``)
    which routes through the already-configured @SWEQuadBOT connection.

    Returns True on success, False if OpenClaw is unavailable or the send
    fails.  Never raises.
    """
    import subprocess
    from src.swe_team.telegram import _rate_limited as _tg_rate_limited

    # Apply the same process-wide rate limiter used by send_message() so that
    # OpenClaw sends are counted against the global cap and per-type cooldown.
    if _tg_rate_limited(message):
        logger.debug("OpenClaw send suppressed by telegram rate limiter")
        return False  # Let _send() fall through to _send_direct (also rate-limited there)

    chat_id = "7783327749"
    try:
        result = subprocess.run(
            [
                "docker", "exec", "openclaw",
                "openclaw", "message", "send",
                "--channel", "telegram",
                "--target", chat_id,
                "--message", message,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and "Sent via Telegram" in result.stdout:
            logger.debug("OpenClaw gateway delivered message successfully")
            return True
        logger.warning(
            "OpenClaw send failed (rc=%d): %s %s",
            result.returncode,
            result.stdout[:200],
            result.stderr[:200],
        )
        return False
    except FileNotFoundError:
        logger.warning("docker not found — OpenClaw gateway unavailable")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("OpenClaw send timed out")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenClaw gateway error: %s", exc)
        return False


def _send(message: str) -> bool:
    """Send via OpenClaw gateway (preferred) or direct Telegram fallback.

    Applies global rate limiting before sending to prevent alert floods.
    Returns True on success, False otherwise.  Never raises.
    """
    if _is_rate_limited(message):
        return False
    if _send_via_openclaw(message):
        return True
    # Fallback: direct Telegram Bot API
    return _send_direct(message)


def notify_new_tickets(tickets: List[SWETicket]) -> None:
    """Send Telegram alert for new CRITICAL tickets.

    Only tickets with severity CRITICAL are included.
    Multiple tickets are grouped into a single message.
    """
    important = [
        t for t in tickets
        if t.severity in (TicketSeverity.CRITICAL,)
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


def notify_daily_summary(store, *, cost_total: Optional[float] = None) -> None:
    """Daily summary of open tickets by severity.

    Parameters
    ----------
    store:
        A ``TicketStore`` instance to query.
    cost_total:
        Optional total estimated cost (USD) for the reporting period.
    """
    all_open = store.list_open()
    if not all_open:
        msg = "<b>\U0001f4cb SWE Daily Summary</b>\n\nNo open tickets."
        if cost_total is not None:
            msg += f"\n\n<b>Estimated cost:</b> ${cost_total:.2f}"
        _send(msg)
        return

    counts: dict[TicketSeverity, int] = {}
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

    # Cost summary
    if cost_total is not None:
        lines.append("")
        lines.append(f"<b>Estimated cost:</b> ${cost_total:.2f}")

    message = "\n".join(lines)
    _send(message)


def notify_investigation_summary(ticket: SWETicket) -> None:
    """Send Telegram summary of an investigation report."""
    if not ticket.investigation_report:
        return
    if ticket.severity.value != "critical":
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


def notify_regression_hitl(ticket: SWETicket) -> None:
    """HITL escalation for a fingerprint that has regressed 3+ times.

    Sends an urgent Telegram alert requesting human intervention.
    """
    fp = ticket.metadata.get("fingerprint", "unknown")
    regressions = ticket.metadata.get("fix_confidence", {}).get("regressions", 0)
    parent_id = ticket.metadata.get("regression_of", "unknown")
    module = ticket.source_module or "unknown"

    lines = [
        "<b>\U0001f6a8\U0001f6a8 HITL ESCALATION — Repeated Regression</b>",
        "",
        f"<b>Fingerprint:</b> <code>{_esc(fp)}</code>",
        f"<b>Regressions:</b> {regressions}",
        f"<b>Parent ticket:</b> <code>{_esc(parent_id)}</code>",
        f"<b>Module:</b> {_esc(module)}",
        f"<b>Title:</b> {_esc(ticket.title[:100])}",
        "",
        "This fingerprint has regressed 3+ times. Automated fixes are not holding. "
        "Human review is required.",
    ]
    message = "\n".join(lines)
    _send(message)


def notify_rollback_triggered(
    *,
    ticket: SWETicket,
    regression_ticket: SWETicket,
    recurrence_count: int,
    rollback_succeeded: bool,
    merge_commit: str = "",
    target_branch: str = "main",
) -> None:
    """Send Telegram alert when VERIFYING regression triggers rollback."""
    fp = ticket.metadata.get("fingerprint", "unknown")
    module = ticket.source_module or "unknown"
    rollback_state = "succeeded" if rollback_succeeded else "failed"
    commit_text = merge_commit or "unknown"

    lines = [
        "<b>↩️ Post-merge regression detected</b>",
        "",
        f"<b>Ticket:</b> <code>{_esc(ticket.ticket_id)}</code>",
        f"<b>Regression ticket:</b> <code>{_esc(regression_ticket.ticket_id)}</code>",
        f"<b>Fingerprint:</b> <code>{_esc(fp)}</code>",
        f"<b>Recurrences:</b> {recurrence_count}",
        f"<b>Module:</b> {_esc(module)}",
        f"<b>Merge commit:</b> <code>{_esc(commit_text)}</code>",
        f"<b>Branch:</b> <code>{_esc(target_branch)}</code>",
        f"<b>Rollback:</b> {_esc(rollback_state)}",
    ]
    message = "\n".join(lines)
    _send(message)


def notify_cycle_summary(
    *,
    new_tickets: int = 0,
    triaged: int = 0,
    investigated: int = 0,
    fixes_attempted: int = 0,
    fixes_succeeded: int = 0,
    gate_verdict: str = "N/A",
    cost_usd: Optional[float] = None,
    silent: bool = False,
) -> None:
    """Send a concise cycle summary to Telegram.

    Designed to be called after each run_cycle() completes.
    Pass silent=True to suppress sending (e.g. for per-cycle calls).
    """
    if silent:
        return
    lines = [
        "<b>\U0001f504 SWE Cycle Summary</b>",
        "",
        f"New tickets: {new_tickets}",
        f"Triaged: {triaged}",
        f"Investigated: {investigated}",
        f"Fixes attempted: {fixes_attempted}",
        f"Fixes succeeded: {fixes_succeeded}",
        f"Gate: <b>{_esc(gate_verdict)}</b>",
    ]
    if cost_usd is not None:
        lines.append(f"Cycle cost: ${cost_usd:.2f}")
    message = "\n".join(lines)
    _send(message)


def notify_status(status_data: dict) -> None:
    """Send the current status.json contents as a formatted Telegram message.

    Parameters
    ----------
    status_data:
        A dict (typically loaded from ``status.json``).
    """
    lines = [
        "<b>\U0001f4ca SWE Status Report</b>",
        "",
    ]
    for key, value in sorted(status_data.items()):
        lines.append(f"<b>{_esc(str(key))}:</b> {_esc(str(value))}")
    message = "\n".join(lines)
    _send(message)


def aggregate_daily_costs(store) -> float:
    """Sum cost_usd from investigation metadata across all tickets updated today.

    Looks at ``ticket.metadata["investigation"]["cost_usd"]`` and
    ``ticket.metadata["cycle_costs"]`` entries.

    Returns the total cost in USD (0.0 if none found).
    """
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0

    all_tickets = store.list_all() if hasattr(store, "list_all") else []
    for ticket in all_tickets:
        # Investigation cost
        inv = ticket.metadata.get("investigation", {})
        completed = inv.get("completed_at", "")
        if completed.startswith(today) and inv.get("cost_usd"):
            try:
                total += float(inv["cost_usd"])
            except (ValueError, TypeError):
                pass

        # Cycle costs appended by the runner
        for entry in ticket.metadata.get("cycle_costs", []):
            if str(entry.get("date", "")).startswith(today):
                try:
                    total += float(entry.get("cost_usd", 0))
                except (ValueError, TypeError):
                    pass

    return round(total, 4)


def _esc(text: str) -> str:
    """Escape HTML for Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
