"""
Fix Verifier — post-merge verification for the Autonomous SWE Team.

After a developer merges a fix, this module waits for propagation and then
monitors logs for the configured window to confirm the error fingerprint does
NOT recur before marking the ticket as RESOLVED.

Usage::

    from src.swe_team.fix_verifier import FixVerifier

    verifier = FixVerifier(verification_window_minutes=30)
    verifier.start_verification(ticket)          # sets status → VERIFYING

    # On each runner cycle:
    result = verifier.check_verification(ticket, monitor)
    if result.ready_to_close:
        ticket.transition(TicketStatus.RESOLVED)
    elif result.regression_detected:
        # a new regression ticket was created by check_verification
        pass
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from src.swe_team.models import SWETicket, TicketStatus, TicketType
from src.swe_team.monitor_agent import compute_log_fingerprint
from src.swe_team.notifier import notify_rollback_triggered

logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _get_pr_lifecycle(ticket: SWETicket) -> Dict[str, Any]:
    lifecycle = ticket.metadata.get("pr_lifecycle")
    if not isinstance(lifecycle, dict):
        lifecycle = {}
        ticket.metadata["pr_lifecycle"] = lifecycle
    return lifecycle


# ---------------------------------------------------------------------------
# Protocol — keeps the verifier decoupled from any concrete monitor class
# ---------------------------------------------------------------------------

@runtime_checkable
class LogScanner(Protocol):
    """Minimal interface the FixVerifier needs from a monitor."""

    def scan_fingerprint_since(
        self,
        fingerprint: str,
        since: datetime,
    ) -> int:
        """Return the number of times *fingerprint* appears in logs since *since*."""
        ...


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    """Outcome of a single :meth:`FixVerifier.check_verification` call.

    Attributes
    ----------
    passed:
        ``True`` when the verification window expired without recurrence.
    recurrence_count:
        Number of times the error fingerprint was seen in the monitoring
        window.  0 = good.
    elapsed_minutes:
        How many minutes have elapsed since verification was started.
    ready_to_close:
        ``True`` when the ticket may be transitioned to RESOLVED (window
        expired, no recurrences).
    regression_detected:
        ``True`` when the fingerprint recurred — a new regression ticket has
        been created.
    window_minutes:
        The configured verification window in minutes.
    regression_ticket:
        The newly-created regression ticket, if ``regression_detected`` is
        ``True``.
    """

    passed: bool
    recurrence_count: int
    elapsed_minutes: float
    ready_to_close: bool
    regression_detected: bool = False
    window_minutes: int = 30
    regression_ticket: Optional[SWETicket] = None


# ---------------------------------------------------------------------------
# FixVerifier
# ---------------------------------------------------------------------------

_DEFAULT_WINDOW_MINUTES = 30
_PROPAGATION_WAIT_MINUTES = 2   # Grace period for propagate.sh to finish


class FixVerifier:
    """Tracks tickets in the VERIFYING state and decides when to close them.

    Parameters
    ----------
    verification_window_minutes:
        How long to monitor after merge before declaring the fix successful.
    propagation_wait_minutes:
        Minimum gap after merge before scanning begins — gives
        ``propagate.sh`` time to push the fix to all workers.
    """

    def __init__(
        self,
        verification_window_minutes: int = _DEFAULT_WINDOW_MINUTES,
        propagation_wait_minutes: int = _PROPAGATION_WAIT_MINUTES,
    ) -> None:
        self._window_minutes = verification_window_minutes
        self._propagation_wait_minutes = propagation_wait_minutes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_verification(
        self,
        ticket: SWETicket,
        *,
        merged_at: Optional[datetime] = None,
        verification_window_minutes: Optional[int] = None,
    ) -> None:
        """Begin the verification window for *ticket*.

        Sets the ticket status to ``VERIFYING`` and records the merge
        timestamp and configured window in ``ticket.metadata``.

        Parameters
        ----------
        ticket:
            The ticket whose fix was just merged.
        merged_at:
            Timestamp of the merge.  Defaults to *now* (UTC).
        verification_window_minutes:
            Override the instance-level window for this ticket only.
        """
        now = merged_at or datetime.now(timezone.utc)
        window = verification_window_minutes or self._window_minutes

        ticket.status = TicketStatus.VERIFYING
        ticket.updated_at = now.isoformat()
        ticket.metadata["verification_started_at"] = now.isoformat()
        ticket.metadata["verification_window_minutes"] = window
        ticket.metadata["verification_recurrence_count"] = 0
        _get_pr_lifecycle(ticket)["verification_started_at"] = now.isoformat()

        fingerprint = ticket.metadata.get("fingerprint", "")
        if not fingerprint:
            logger.warning(
                "Ticket %s has no fingerprint — verification will rely on title matching",
                ticket.ticket_id,
            )

        logger.info(
            "Verification started for ticket %s (fingerprint=%r, window=%d min)",
            ticket.ticket_id,
            fingerprint,
            window,
        )

    def check_verification(
        self,
        ticket: SWETicket,
        monitor: Any,
    ) -> VerificationResult:
        """Check whether the verification window for *ticket* has passed.

        Parameters
        ----------
        ticket:
            A ticket in ``VERIFYING`` status.
        monitor:
            Any object with a ``scan_fingerprint_since(fingerprint, since)``
            method.  If the object does not implement that method the
            verifier falls back to a recurrence_count of 0 (safe default —
            the window just has to expire).

        Returns
        -------
        VerificationResult
            Describes the current outcome.  Check ``ready_to_close`` to
            decide whether to call ``ticket.transition(RESOLVED)``.
        """
        started_raw = ticket.metadata.get("verification_started_at")
        if not started_raw:
            logger.error(
                "Ticket %s in VERIFYING state but no verification_started_at in metadata",
                ticket.ticket_id,
            )
            return VerificationResult(
                passed=False,
                recurrence_count=0,
                elapsed_minutes=0.0,
                ready_to_close=False,
            )

        started_at = _parse_iso(started_raw)
        window_minutes: int = int(
            ticket.metadata.get("verification_window_minutes", self._window_minutes)
        )
        now = datetime.now(timezone.utc)
        elapsed = (now - started_at).total_seconds() / 60.0

        # Propagation grace period — don't scan yet
        if elapsed < self._propagation_wait_minutes:
            logger.debug(
                "Ticket %s: still in propagation wait (%.1f / %d min)",
                ticket.ticket_id,
                elapsed,
                self._propagation_wait_minutes,
            )
            return VerificationResult(
                passed=False,
                recurrence_count=0,
                elapsed_minutes=elapsed,
                ready_to_close=False,
                window_minutes=window_minutes,
            )

        # Scan for recurrences since verification started
        fingerprint = ticket.metadata.get("fingerprint", "")
        recurrence_count = self._scan_for_recurrence(monitor, fingerprint, started_at)

        # Update stored count
        ticket.metadata["verification_recurrence_count"] = recurrence_count

        if recurrence_count > 0:
            logger.warning(
                "Ticket %s: fingerprint %r recurred %d time(s) — marking regression",
                ticket.ticket_id,
                fingerprint,
                recurrence_count,
            )
            regression_ticket = self._create_regression_ticket(ticket, recurrence_count)
            rollback_succeeded, merge_commit, target_branch = self._rollback_merge_commit(ticket)
            if rollback_succeeded:
                ticket.transition(TicketStatus.ROLLED_BACK)
                ticket.rollback_reason = (
                    f"Regression detected during VERIFYING window; reverted {merge_commit} "
                    f"on {target_branch} after {recurrence_count} recurrence(s)."
                )
            else:
                ticket.transition(TicketStatus.OPEN)
            ticket.metadata["regression_detected"] = True
            ticket.metadata["regression_ticket_id"] = regression_ticket.ticket_id
            _get_pr_lifecycle(ticket)["verification_result"] = "regression"
            ticket.metadata["rollback_attempted"] = True
            ticket.metadata["rollback_succeeded"] = rollback_succeeded

            try:
                notify_rollback_triggered(
                    ticket=ticket,
                    regression_ticket=regression_ticket,
                    recurrence_count=recurrence_count,
                    rollback_succeeded=rollback_succeeded,
                    merge_commit=merge_commit,
                    target_branch=target_branch,
                )
            except Exception:
                logger.exception(
                    "Failed to send rollback notification for ticket %s", ticket.ticket_id
                )
            return VerificationResult(
                passed=False,
                recurrence_count=recurrence_count,
                elapsed_minutes=elapsed,
                ready_to_close=False,
                regression_detected=True,
                window_minutes=window_minutes,
                regression_ticket=regression_ticket,
            )

        window_expired = elapsed >= window_minutes
        if window_expired:
            lifecycle = _get_pr_lifecycle(ticket)
            lifecycle["verification_result"] = "pass"
            lifecycle["resolved_at"] = now.isoformat()
            logger.info(
                "Ticket %s: verification window expired (%.1f min) with 0 recurrences — RESOLVED",
                ticket.ticket_id,
                elapsed,
            )
            return VerificationResult(
                passed=True,
                recurrence_count=0,
                elapsed_minutes=elapsed,
                ready_to_close=True,
                window_minutes=window_minutes,
            )

        logger.debug(
            "Ticket %s: verification in progress (%.1f / %d min, 0 recurrences so far)",
            ticket.ticket_id,
            elapsed,
            window_minutes,
        )
        return VerificationResult(
            passed=False,
            recurrence_count=0,
            elapsed_minutes=elapsed,
            ready_to_close=False,
            window_minutes=window_minutes,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _scan_for_recurrence(
        self,
        monitor: Any,
        fingerprint: str,
        since: datetime,
    ) -> int:
        """Return recurrence count; 0 if monitor doesn't support scanning."""
        if not fingerprint:
            return 0
        if not hasattr(monitor, "scan_fingerprint_since"):
            return 0
        try:
            return int(monitor.scan_fingerprint_since(fingerprint, since))
        except Exception:
            logger.exception(
                "scan_fingerprint_since raised for fingerprint %r — treating as 0",
                fingerprint,
            )
            return 0

    @staticmethod
    def _rollback_merge_commit(ticket: SWETicket) -> tuple[bool, str, str]:
        """Attempt to revert the merged commit and push the revert commit."""
        merge_commit = str(
            ticket.metadata.get("merge_commit")
            or ticket.metadata.get("merge_commit_sha")
            or ticket.metadata.get("merged_commit_sha")
            or ""
        ).strip()
        target_branch = str(ticket.metadata.get("target_branch", "main")).strip() or "main"
        if not merge_commit:
            logger.error(
                "Ticket %s has no merge_commit metadata; skipping rollback",
                ticket.ticket_id,
            )
            return False, "", target_branch

        try:
            subprocess.run(
                ["git", "revert", "--no-edit", merge_commit],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=_REPO_ROOT,
            )
        except subprocess.CalledProcessError as exc:
            logger.exception(
                "git revert failed for ticket %s (commit=%s, branch=%s, stderr=%r)",
                ticket.ticket_id,
                merge_commit,
                target_branch,
                (exc.stderr or "")[:500],
            )
            return False, merge_commit, target_branch
        except Exception:
            logger.exception(
                "Unexpected git revert failure for ticket %s (commit=%s, branch=%s)",
                ticket.ticket_id,
                merge_commit,
                target_branch,
            )
            return False, merge_commit, target_branch

        try:
            subprocess.run(
                ["git", "push", "origin", target_branch],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=_REPO_ROOT,
            )
            return True, merge_commit, target_branch
        except subprocess.CalledProcessError as exc:
            logger.exception(
                "git push failed after rollback for ticket %s (commit=%s, branch=%s, stderr=%r)",
                ticket.ticket_id,
                merge_commit,
                target_branch,
                (exc.stderr or "")[:500],
            )
            return False, merge_commit, target_branch
        except Exception:
            logger.exception(
                "Unexpected git push failure after rollback for ticket %s (commit=%s, branch=%s)",
                ticket.ticket_id,
                merge_commit,
                target_branch,
            )
            return False, merge_commit, target_branch

    @staticmethod
    def _create_regression_ticket(
        original: SWETicket,
        recurrence_count: int,
    ) -> SWETicket:
        """Create a new regression ticket linked to *original*."""
        ticket = SWETicket(
            title=f"[REGRESSION] {original.title}",
            description=(
                f"The fix for ticket `{original.ticket_id}` did not prevent the error "
                f"from recurring. The original fingerprint was detected {recurrence_count} "
                f"time(s) after the fix was merged.\n\n"
                f"Original ticket: {original.ticket_id}\n"
                f"Original title: {original.title}\n"
            ),
            severity=original.severity,
            ticket_type=TicketType.REGRESSION,
            source_module=original.source_module,
            labels=["regression", "auto-detected"],
            metadata={
                "fingerprint": original.metadata.get("fingerprint", ""),
                "original_ticket_id": original.ticket_id,
                "recurrence_count": recurrence_count,
                "is_regression": True,
            },
        )
        return ticket


# ---------------------------------------------------------------------------
# MonitorAgentAdapter — wraps an existing MonitorAgent and implements LogScanner
# ---------------------------------------------------------------------------

class MonitorAgentAdapter:
    """Adapts an existing MonitorAgent to implement the :class:`LogScanner` protocol.

    Instead of monkey-patching the monitor instance, callers should wrap it::

        adapter = MonitorAgentAdapter(monitor)
        result = verifier.check_verification(ticket, adapter)

    The adapter scans ``monitor._config.log_directories`` for the given
    fingerprint hash in lines written after *since*, using the canonical
    :func:`~src.swe_team.monitor_agent.compute_log_fingerprint` function so
    that the hashes are guaranteed to match those produced by MonitorAgent.
    """

    def __init__(self, monitor: Any) -> None:
        self._monitor = monitor

    def scan_fingerprint_since(self, fingerprint: str, since: datetime) -> int:
        """Count occurrences of *fingerprint* in logs written after *since*."""
        count = 0
        try:
            log_dirs = self._monitor._config.log_directories
        except AttributeError:
            return 0

        for log_dir in log_dirs:
            dir_path = Path(log_dir)
            if not dir_path.is_dir():
                continue
            for log_file in sorted(dir_path.rglob("*.log")):
                try:
                    mtime = datetime.fromtimestamp(
                        log_file.stat().st_mtime, tz=timezone.utc
                    )
                    if mtime < since:
                        continue
                    text = log_file.read_text(errors="replace")
                except OSError:
                    continue

                for line in text.splitlines():
                    fp = compute_log_fingerprint(str(log_file), line)
                    if fp == fingerprint:
                        count += 1
        return count


def add_fingerprint_scan_to_monitor(monitor: Any) -> None:
    """Attach ``scan_fingerprint_since`` to *monitor* if not already present.

    Compatibility shim for call sites that pass the raw MonitorAgent instance
    to :meth:`FixVerifier.check_verification`.  Prefer constructing a
    :class:`MonitorAgentAdapter` directly for new code.

    Only attaches the method if the monitor does not already have one.
    """
    if hasattr(monitor, "scan_fingerprint_since"):
        return
    adapter = MonitorAgentAdapter(monitor)
    monitor.scan_fingerprint_since = adapter.scan_fingerprint_since


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 datetime string, always returning UTC-aware datetime."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        # Fallback for formats without timezone
        dt = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
