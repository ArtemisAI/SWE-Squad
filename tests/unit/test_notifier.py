"""Unit tests for src/swe_team/notifier.py."""

from __future__ import annotations

from subprocess import TimeoutExpired
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import (
    GovernanceVerdict,
    StabilityReport,
    SWETicket,
    TicketSeverity,
    TicketType,
)
from src.swe_team import notifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(severity=TicketSeverity.CRITICAL, source_module="auth", **kwargs):
    defaults = dict(
        title="Something broke",
        description="Full description",
        severity=severity,
        source_module=source_module,
    )
    defaults.update(kwargs)
    return SWETicket(**defaults)


def _make_stability_report(verdict=GovernanceVerdict.BLOCK, open_critical=2, failing_tests=1):
    return StabilityReport(
        verdict=verdict,
        open_critical=open_critical,
        open_high=0,
        failing_tests=failing_tests,
        details="Some details here",
    )


# ---------------------------------------------------------------------------
# _esc HTML escaping
# ---------------------------------------------------------------------------

class TestEsc:
    def test_escapes_ampersand(self):
        assert notifier._esc("a & b") == "a &amp; b"

    def test_escapes_lt_gt(self):
        assert notifier._esc("<b>bold</b>") == "&lt;b&gt;bold&lt;/b&gt;"

    def test_escapes_double_quote(self):
        assert notifier._esc('say "hi"') == "say &quot;hi&quot;"

    def test_no_special_chars_unchanged(self):
        assert notifier._esc("hello world") == "hello world"


# ---------------------------------------------------------------------------
# _send_via_openclaw
# ---------------------------------------------------------------------------

class TestSendViaOpenclaw:
    # _send_via_openclaw uses `import subprocess` locally inside the function,
    # so subprocess.run is looked up in the subprocess module itself.
    def test_success_returns_true(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "Sent via Telegram"
        proc.stderr = ""
        with patch("subprocess.run", return_value=proc):
            result = notifier._send_via_openclaw("hello")
        assert result is True

    def test_nonzero_returncode_returns_false(self):
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = ""
        proc.stderr = "container not running"
        with patch("subprocess.run", return_value=proc):
            result = notifier._send_via_openclaw("hello")
        assert result is False

    def test_docker_not_found_returns_false(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("docker")):
            result = notifier._send_via_openclaw("hello")
        assert result is False

    def test_timeout_returns_false(self):
        with patch(
            "subprocess.run",
            side_effect=TimeoutExpired(cmd="docker", timeout=30),
        ):
            result = notifier._send_via_openclaw("hello")
        assert result is False

    def test_generic_exception_returns_false(self):
        with patch("subprocess.run", side_effect=OSError("unexpected")):
            result = notifier._send_via_openclaw("hello")
        assert result is False


# ---------------------------------------------------------------------------
# _send — openclaw first, direct fallback
# ---------------------------------------------------------------------------

class TestSend:
    def setup_method(self):
        """Clear rate-limit state between tests."""
        import src.swe_team.notifier as _n
        _n._rate_limit_cache.clear()
        _n._send_timestamps.clear()

    def test_uses_openclaw_when_available(self):
        with patch("src.swe_team.notifier._send_via_openclaw", return_value=True) as mock_claw:
            with patch("src.swe_team.notifier._send_direct") as mock_direct:
                result = notifier._send("test msg")
        assert result is True
        mock_claw.assert_called_once_with("test msg")
        mock_direct.assert_not_called()

    def test_falls_back_to_direct_when_openclaw_fails(self):
        with patch("src.swe_team.notifier._send_via_openclaw", return_value=False):
            with patch("src.swe_team.notifier._send_direct", return_value=True) as mock_direct:
                result = notifier._send("test msg")
        assert result is True
        mock_direct.assert_called_once_with("test msg")

    def test_returns_false_when_both_fail(self):
        with patch("src.swe_team.notifier._send_via_openclaw", return_value=False):
            with patch("src.swe_team.notifier._send_direct", return_value=False):
                result = notifier._send("test msg")
        assert result is False


# ---------------------------------------------------------------------------
# _send_direct
# ---------------------------------------------------------------------------

class TestSendDirect:
    def test_delegates_to_telegram_send_message(self):
        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_tm:
            result = notifier._send_direct("hello telegram")
        assert result is True
        mock_tm.assert_called_once_with("hello telegram", parse_mode="HTML")

    def test_exception_returns_false(self):
        with patch("src.swe_team.telegram.send_message", side_effect=Exception("timeout")):
            result = notifier._send_direct("hello")
        assert result is False


# ---------------------------------------------------------------------------
# notify_new_tickets
# ---------------------------------------------------------------------------

class TestNotifyNewTickets:
    def test_only_critical_triggers_send(self):
        critical = _make_ticket(severity=TicketSeverity.CRITICAL)
        high = _make_ticket(severity=TicketSeverity.HIGH)
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_new_tickets([critical, high])
        mock_send.assert_called_once()

    def test_no_critical_sends_nothing(self):
        high = _make_ticket(severity=TicketSeverity.HIGH)
        medium = _make_ticket(severity=TicketSeverity.MEDIUM)
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_new_tickets([high, medium])
        mock_send.assert_not_called()

    def test_empty_list_sends_nothing(self):
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_new_tickets([])
        mock_send.assert_not_called()

    def test_message_contains_ticket_title(self):
        critical = _make_ticket(severity=TicketSeverity.CRITICAL, title="DB meltdown")
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_new_tickets([critical])
        message = mock_send.call_args[0][0]
        assert "DB meltdown" in message


# ---------------------------------------------------------------------------
# notify_stability_gate
# ---------------------------------------------------------------------------

class TestNotifyStabilityGate:
    def test_block_verdict_sends_alert(self):
        report = _make_stability_report(verdict=GovernanceVerdict.BLOCK)
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_stability_gate(report)
        mock_send.assert_called_once()

    def test_pass_verdict_sends_nothing(self):
        report = _make_stability_report(verdict=GovernanceVerdict.PASS)
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_stability_gate(report)
        mock_send.assert_not_called()

    def test_warn_verdict_sends_nothing(self):
        report = _make_stability_report(verdict=GovernanceVerdict.WARN)
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_stability_gate(report)
        mock_send.assert_not_called()

    def test_block_message_contains_open_critical_count(self):
        report = _make_stability_report(verdict=GovernanceVerdict.BLOCK, open_critical=5)
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_stability_gate(report)
        message = mock_send.call_args[0][0]
        assert "5" in message


# ---------------------------------------------------------------------------
# notify_daily_summary
# ---------------------------------------------------------------------------

class TestNotifyDailySummary:
    def test_sends_no_open_tickets_message(self):
        store = MagicMock()
        store.list_open.return_value = []
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_daily_summary(store)
        mock_send.assert_called_once()
        message = mock_send.call_args[0][0]
        assert "No open tickets" in message

    def test_sends_summary_with_open_tickets(self):
        store = MagicMock()
        t1 = _make_ticket(TicketSeverity.CRITICAL)
        t2 = _make_ticket(TicketSeverity.HIGH)
        store.list_open.return_value = [t1, t2]
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_daily_summary(store)
        mock_send.assert_called_once()
        message = mock_send.call_args[0][0]
        assert "CRITICAL" in message or "HIGH" in message

    def test_includes_cost_when_provided(self):
        store = MagicMock()
        store.list_open.return_value = []
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_daily_summary(store, cost_total=3.50)
        message = mock_send.call_args[0][0]
        assert "3.50" in message


# ---------------------------------------------------------------------------
# notify_investigation_summary
# ---------------------------------------------------------------------------

class TestNotifyInvestigationSummary:
    def test_sends_for_critical_with_report(self):
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        ticket.investigation_report = "Root cause: the database melted."
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_investigation_summary(ticket)
        mock_send.assert_called_once()

    def test_no_report_sends_nothing(self):
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        ticket.investigation_report = None
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_investigation_summary(ticket)
        mock_send.assert_not_called()

    def test_non_critical_sends_nothing(self):
        ticket = _make_ticket(severity=TicketSeverity.HIGH)
        ticket.investigation_report = "Root cause found."
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_investigation_summary(ticket)
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# notify_regression_hitl
# ---------------------------------------------------------------------------

class TestNotifyRegressionHitl:
    def test_always_sends(self):
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        ticket.metadata["fingerprint"] = "fp-abc"
        ticket.metadata["regression_of"] = "T-PARENT"
        ticket.metadata["fix_confidence"] = {"regressions": 3}
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_regression_hitl(ticket)
        mock_send.assert_called_once()
        message = mock_send.call_args[0][0]
        assert "fp-abc" in message
        assert "3" in message


# ---------------------------------------------------------------------------
# notify_rollback_triggered
# ---------------------------------------------------------------------------

class TestNotifyRollbackTriggered:
    def test_includes_regression_details(self):
        ticket = _make_ticket(severity=TicketSeverity.HIGH, ticket_id="T-123")
        ticket.metadata["fingerprint"] = "fp-recur-1"
        regression_ticket = _make_ticket(
            severity=TicketSeverity.HIGH,
            ticket_id="T-999",
            ticket_type=TicketType.REGRESSION,
        )

        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_rollback_triggered(
                ticket=ticket,
                regression_ticket=regression_ticket,
                recurrence_count=2,
                rollback_succeeded=True,
                merge_commit="abc1234",
                target_branch="main",
            )

        mock_send.assert_called_once()
        message = mock_send.call_args[0][0]
        assert "T-123" in message
        assert "T-999" in message
        assert "fp-recur-1" in message
        assert "abc1234" in message
        assert "main" in message
        assert "succeeded" in message


# ---------------------------------------------------------------------------
# notify_cycle_summary
# ---------------------------------------------------------------------------

class TestNotifyCycleSummary:
    def test_sends_cycle_summary(self):
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_cycle_summary(
                new_tickets=2,
                triaged=2,
                investigated=1,
                fixes_attempted=1,
                fixes_succeeded=1,
                gate_verdict="PASS",
            )
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "PASS" in msg

    def test_silent_flag_suppresses_send(self):
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_cycle_summary(silent=True)
        mock_send.assert_not_called()

    def test_includes_cost_when_provided(self):
        with patch("src.swe_team.notifier._send") as mock_send:
            notifier.notify_cycle_summary(cost_usd=1.23)
        msg = mock_send.call_args[0][0]
        assert "1.23" in msg


# ---------------------------------------------------------------------------
# aggregate_daily_costs
# ---------------------------------------------------------------------------

class TestAggregateDailyCosts:
    def test_sums_investigation_costs_today(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        t = _make_ticket()
        t.metadata["investigation"] = {"completed_at": f"{today}T10:00:00", "cost_usd": 0.5}

        store = MagicMock()
        store.list_all.return_value = [t]
        total = notifier.aggregate_daily_costs(store)
        assert total == pytest.approx(0.5, abs=1e-4)

    def test_ignores_old_investigation_costs(self):
        t = _make_ticket()
        t.metadata["investigation"] = {"completed_at": "2020-01-01T10:00:00", "cost_usd": 10.0}

        store = MagicMock()
        store.list_all.return_value = [t]
        total = notifier.aggregate_daily_costs(store)
        assert total == 0.0

    def test_returns_zero_when_no_tickets(self):
        store = MagicMock()
        store.list_all.return_value = []
        total = notifier.aggregate_daily_costs(store)
        assert total == 0.0

    def test_handles_store_without_list_all(self):
        store = MagicMock(spec=[])  # no list_all attribute
        total = notifier.aggregate_daily_costs(store)
        assert total == 0.0
