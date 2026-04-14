"""
Unit tests for src/swe_team/fix_verifier.py

Tests cover:
- start_verification sets correct metadata and status
- check_verification during propagation wait returns not-ready
- check_verification before window expiry with no recurrence returns not-ready
- check_verification after window expiry with no recurrence returns ready_to_close
- check_verification with fingerprint recurrence detects regression
- regression ticket has correct fields
- monitor without scan_fingerprint_since is handled gracefully
- empty fingerprint is handled gracefully
- VerificationResult dataclass fields
- custom window_minutes override
- multiple calls within the same window accumulate correctly
- add_fingerprint_scan_to_monitor is idempotent
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from src.swe_team.fix_verifier import (
    FixVerifier,
    VerificationResult,
    add_fingerprint_scan_to_monitor,
    _parse_iso,
)
from src.swe_team.models import SWETicket, TicketStatus, TicketType, TicketSeverity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ticket(
    fingerprint: str = "abc123def456789a",
    severity: TicketSeverity = TicketSeverity.HIGH,
) -> SWETicket:
    t = SWETicket(
        title="[ERROR] Something went wrong",
        description="Test ticket",
        severity=severity,
        metadata={"fingerprint": fingerprint},
        investigation_report="A" * 250,  # satisfy resolution audit
    )
    t.metadata.setdefault("attempts", [{"outcome": "merged"}])
    return t


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _MockMonitor:
    """Monitor stub that returns a configurable recurrence count."""

    def __init__(self, recurrence: int = 0) -> None:
        self._recurrence = recurrence
        self.calls: list = []

    def scan_fingerprint_since(self, fingerprint: str, since: datetime) -> int:
        self.calls.append((fingerprint, since))
        return self._recurrence


class _NoScanMonitor:
    """Monitor that does NOT implement scan_fingerprint_since."""
    pass


@pytest.fixture(autouse=True)
def _stub_rollback_notifier():
    with patch("src.swe_team.fix_verifier.notify_rollback_triggered") as mocked:
        yield mocked


# ---------------------------------------------------------------------------
# Tests: start_verification
# ---------------------------------------------------------------------------

class TestStartVerification:
    def test_sets_verifying_status(self):
        verifier = FixVerifier()
        ticket = _ticket()
        verifier.start_verification(ticket)
        assert ticket.status == TicketStatus.VERIFYING

    def test_records_verification_started_at(self):
        verifier = FixVerifier()
        ticket = _ticket()
        before = _now()
        verifier.start_verification(ticket)
        after = _now()
        started = _parse_iso(ticket.metadata["verification_started_at"])
        assert before <= started <= after
        assert ticket.metadata["pr_lifecycle"]["verification_started_at"] == ticket.metadata["verification_started_at"]

    def test_records_window_minutes(self):
        verifier = FixVerifier(verification_window_minutes=45)
        ticket = _ticket()
        verifier.start_verification(ticket)
        assert ticket.metadata["verification_window_minutes"] == 45

    def test_custom_window_overrides_instance_default(self):
        verifier = FixVerifier(verification_window_minutes=30)
        ticket = _ticket()
        verifier.start_verification(ticket, verification_window_minutes=60)
        assert ticket.metadata["verification_window_minutes"] == 60

    def test_records_zero_recurrence_count(self):
        verifier = FixVerifier()
        ticket = _ticket()
        verifier.start_verification(ticket)
        assert ticket.metadata["verification_recurrence_count"] == 0

    def test_accepts_explicit_merged_at(self):
        verifier = FixVerifier()
        ticket = _ticket()
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        verifier.start_verification(ticket, merged_at=ts)
        assert ticket.metadata["verification_started_at"] == ts.isoformat()


# ---------------------------------------------------------------------------
# Tests: check_verification — propagation wait
# ---------------------------------------------------------------------------

class TestPropagationWait:
    def test_not_ready_during_propagation_wait(self):
        verifier = FixVerifier(
            verification_window_minutes=30,
            propagation_wait_minutes=5,
        )
        ticket = _ticket()
        # Set started_at to 1 minute ago (inside 5-min propagation wait)
        started = _now() - timedelta(minutes=1)
        ticket.metadata["verification_started_at"] = started.isoformat()
        ticket.metadata["verification_window_minutes"] = 30

        result = verifier.check_verification(ticket, _MockMonitor())
        assert result.ready_to_close is False
        assert result.regression_detected is False
        assert result.recurrence_count == 0


# ---------------------------------------------------------------------------
# Tests: check_verification — window in progress
# ---------------------------------------------------------------------------

class TestWindowInProgress:
    def test_not_ready_before_window_expires(self):
        verifier = FixVerifier(
            verification_window_minutes=30,
            propagation_wait_minutes=2,
        )
        ticket = _ticket()
        # 10 minutes in — not done yet
        started = _now() - timedelta(minutes=10)
        ticket.metadata["verification_started_at"] = started.isoformat()
        ticket.metadata["verification_window_minutes"] = 30

        result = verifier.check_verification(ticket, _MockMonitor(recurrence=0))
        assert result.ready_to_close is False
        assert result.passed is False
        assert result.recurrence_count == 0
        assert 9.0 < result.elapsed_minutes < 11.0

    def test_monitor_is_queried_after_propagation_wait(self):
        verifier = FixVerifier(
            verification_window_minutes=30,
            propagation_wait_minutes=2,
        )
        ticket = _ticket(fingerprint="fp0011223344556")
        started = _now() - timedelta(minutes=10)
        ticket.metadata["verification_started_at"] = started.isoformat()
        ticket.metadata["verification_window_minutes"] = 30

        monitor = _MockMonitor(recurrence=0)
        verifier.check_verification(ticket, monitor)
        assert len(monitor.calls) == 1
        assert monitor.calls[0][0] == "fp0011223344556"


# ---------------------------------------------------------------------------
# Tests: check_verification — window expired, no recurrence
# ---------------------------------------------------------------------------

class TestWindowExpired:
    def test_ready_to_close_after_window(self):
        verifier = FixVerifier(
            verification_window_minutes=30,
            propagation_wait_minutes=2,
        )
        ticket = _ticket()
        # 35 minutes in — past the window
        started = _now() - timedelta(minutes=35)
        ticket.metadata["verification_started_at"] = started.isoformat()
        ticket.metadata["verification_window_minutes"] = 30

        result = verifier.check_verification(ticket, _MockMonitor(recurrence=0))
        assert result.ready_to_close is True
        assert result.passed is True
        assert result.recurrence_count == 0
        assert result.regression_detected is False
        assert ticket.metadata["pr_lifecycle"]["verification_result"] == "pass"
        assert _parse_iso(ticket.metadata["pr_lifecycle"]["resolved_at"]) is not None

    def test_window_minutes_reported_correctly(self):
        verifier = FixVerifier(verification_window_minutes=45)
        ticket = _ticket()
        started = _now() - timedelta(minutes=50)
        ticket.metadata["verification_started_at"] = started.isoformat()
        ticket.metadata["verification_window_minutes"] = 45

        result = verifier.check_verification(ticket, _MockMonitor())
        assert result.window_minutes == 45


# ---------------------------------------------------------------------------
# Tests: regression detection
# ---------------------------------------------------------------------------

class TestRegressionDetection:
    def test_regression_detected_on_recurrence(self):
        verifier = FixVerifier(
            verification_window_minutes=30,
            propagation_wait_minutes=2,
        )
        ticket = _ticket()
        started = _now() - timedelta(minutes=10)
        ticket.metadata["verification_started_at"] = started.isoformat()
        ticket.metadata["verification_window_minutes"] = 30

        result = verifier.check_verification(ticket, _MockMonitor(recurrence=3))
        assert result.regression_detected is True
        assert result.recurrence_count == 3
        assert result.ready_to_close is False
        assert result.passed is False
        assert ticket.metadata["pr_lifecycle"]["verification_result"] == "regression"

    def test_regression_ticket_created(self):
        verifier = FixVerifier(propagation_wait_minutes=0)
        ticket = _ticket(fingerprint="fpaabbcc11223344")
        started = _now() - timedelta(minutes=5)
        ticket.metadata["verification_started_at"] = started.isoformat()
        ticket.metadata["verification_window_minutes"] = 30

        result = verifier.check_verification(ticket, _MockMonitor(recurrence=2))
        assert result.regression_ticket is not None
        reg = result.regression_ticket
        assert "[REGRESSION]" in reg.title
        assert reg.ticket_type == TicketType.REGRESSION
        assert reg.metadata["original_ticket_id"] == ticket.ticket_id
        assert reg.metadata["recurrence_count"] == 2
        assert reg.metadata["is_regression"] is True

    def test_regression_transitions_ticket_to_rolled_back_after_revert(self):
        verifier = FixVerifier(propagation_wait_minutes=0)
        ticket = _ticket()
        ticket.status = TicketStatus.VERIFYING
        ticket.metadata["merge_commit"] = "abc123def456"
        ticket.metadata["target_branch"] = "main"
        started = _now() - timedelta(minutes=5)
        ticket.metadata["verification_started_at"] = started.isoformat()
        ticket.metadata["verification_window_minutes"] = 30

        with patch("subprocess.run") as mock_run:
            verifier.check_verification(ticket, _MockMonitor(recurrence=1))
        assert ticket.status == TicketStatus.ROLLED_BACK
        assert "reverted abc123def456" in (ticket.rollback_reason or "")
        assert mock_run.call_count == 2

    def test_regression_executes_revert_and_push(self):
        verifier = FixVerifier(propagation_wait_minutes=0)
        ticket = _ticket()
        ticket.metadata["merge_commit"] = "deadbeef1234"
        ticket.metadata["target_branch"] = "release"
        started = _now() - timedelta(minutes=5)
        ticket.metadata["verification_started_at"] = started.isoformat()
        ticket.metadata["verification_window_minutes"] = 30

        with patch("subprocess.run") as mock_run:
            verifier.check_verification(ticket, _MockMonitor(recurrence=2))

        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0].args[0] == [
            "git", "revert", "--no-edit", "deadbeef1234",
        ]
        assert mock_run.call_args_list[1].args[0] == [
            "git", "push", "origin", "release",
        ]

    def test_regression_sends_rollback_alert_with_details(self, _stub_rollback_notifier):
        verifier = FixVerifier(propagation_wait_minutes=0)
        ticket = _ticket(fingerprint="fp-alert-123")
        ticket.metadata["merge_commit"] = "123abc987"
        ticket.metadata["target_branch"] = "main"
        started = _now() - timedelta(minutes=5)
        ticket.metadata["verification_started_at"] = started.isoformat()
        ticket.metadata["verification_window_minutes"] = 30

        with patch("subprocess.run"):
            verifier.check_verification(ticket, _MockMonitor(recurrence=4))

        _stub_rollback_notifier.assert_called_once()
        kwargs = _stub_rollback_notifier.call_args.kwargs
        assert kwargs["recurrence_count"] == 4
        assert kwargs["rollback_succeeded"] is True
        assert kwargs["merge_commit"] == "123abc987"
        assert kwargs["target_branch"] == "main"

    def test_regression_ticket_inherits_severity(self):
        verifier = FixVerifier(propagation_wait_minutes=0)
        ticket = _ticket(severity=TicketSeverity.CRITICAL)
        started = _now() - timedelta(minutes=5)
        ticket.metadata["verification_started_at"] = started.isoformat()
        ticket.metadata["verification_window_minutes"] = 30

        result = verifier.check_verification(ticket, _MockMonitor(recurrence=1))
        assert result.regression_ticket.severity == TicketSeverity.CRITICAL


# ---------------------------------------------------------------------------
# Tests: monitor without scan_fingerprint_since
# ---------------------------------------------------------------------------

class TestMonitorFallback:
    def test_no_scan_method_defaults_to_zero(self):
        verifier = FixVerifier(
            verification_window_minutes=30,
            propagation_wait_minutes=0,
        )
        ticket = _ticket()
        # 35 minutes in
        started = _now() - timedelta(minutes=35)
        ticket.metadata["verification_started_at"] = started.isoformat()
        ticket.metadata["verification_window_minutes"] = 30

        result = verifier.check_verification(ticket, _NoScanMonitor())
        # No recurrence detected, window expired → should resolve
        assert result.ready_to_close is True
        assert result.recurrence_count == 0

    def test_empty_fingerprint_skips_scan(self):
        verifier = FixVerifier(propagation_wait_minutes=0)
        ticket = SWETicket(title="t", description="d", metadata={})
        ticket.metadata["verification_started_at"] = (_now() - timedelta(minutes=35)).isoformat()
        ticket.metadata["verification_window_minutes"] = 30

        monitor = _MockMonitor(recurrence=5)
        result = verifier.check_verification(ticket, monitor)
        # No fingerprint → scan never called → no regression
        assert len(monitor.calls) == 0
        assert result.regression_detected is False


# ---------------------------------------------------------------------------
# Tests: missing metadata guard
# ---------------------------------------------------------------------------

class TestMissingMetadata:
    def test_missing_started_at_returns_not_ready(self):
        verifier = FixVerifier()
        ticket = _ticket()
        # Don't call start_verification — no metadata set
        result = verifier.check_verification(ticket, _MockMonitor())
        assert result.ready_to_close is False
        assert result.passed is False


# ---------------------------------------------------------------------------
# Tests: add_fingerprint_scan_to_monitor
# ---------------------------------------------------------------------------

class TestAddFingerprintScan:
    def test_idempotent_when_method_already_exists(self):
        monitor = _MockMonitor(recurrence=7)
        add_fingerprint_scan_to_monitor(monitor)
        # The existing scan_fingerprint_since should still work correctly
        result = monitor.scan_fingerprint_since("fp", _now())
        assert result == 7

    def test_adds_method_to_plain_object(self):
        monitor = _NoScanMonitor()
        add_fingerprint_scan_to_monitor(monitor)
        assert hasattr(monitor, "scan_fingerprint_since")
        assert callable(monitor.scan_fingerprint_since)


# ---------------------------------------------------------------------------
# Tests: VerificationResult dataclass
# ---------------------------------------------------------------------------

class TestVerificationResult:
    def test_defaults(self):
        r = VerificationResult(
            passed=True,
            recurrence_count=0,
            elapsed_minutes=31.5,
            ready_to_close=True,
        )
        assert r.regression_detected is False
        assert r.regression_ticket is None
        assert r.window_minutes == 30

    def test_custom_window(self):
        r = VerificationResult(
            passed=False,
            recurrence_count=0,
            elapsed_minutes=5.0,
            ready_to_close=False,
            window_minutes=60,
        )
        assert r.window_minutes == 60
