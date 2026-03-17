"""
Tests for the Telegram notification system overhaul:
  - telegram.py: standalone Bot API client
  - notifier.py: updated to use new telegram module
  - report modes: daily, cycle, status
  - cost aggregation in daily summary
"""

from __future__ import annotations

import logging
logging.logAsyncioTasks = False

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus, StabilityReport, GovernanceVerdict
from src.swe_team.ticket_store import TicketStore


# ======================================================================
# telegram.py — standalone Telegram Bot API client
# ======================================================================


class TestTelegramSendMessage:
    """Test send_message with mocked urllib."""

    def test_send_message_success(self):
        """Successful send returns True."""
        from src.swe_team.telegram import send_message

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"ok": True}).encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:ABC", "TELEGRAM_CHAT_ID": "456"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response) as mock_urlopen:
                result = send_message("Hello world")

        assert result is True
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "/bot123:ABC/sendMessage" in req.full_url
        body = json.loads(req.data.decode("utf-8"))
        assert body["chat_id"] == "456"
        assert body["text"] == "Hello world"
        assert body["parse_mode"] == "HTML"

    def test_send_message_custom_parse_mode(self):
        """Parse mode can be customized."""
        from src.swe_team.telegram import send_message

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"ok": True}).encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "cid"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response):
                result = send_message("test", parse_mode="Markdown")

        assert result is True

    def test_send_message_missing_token(self):
        """Missing TELEGRAM_BOT_TOKEN returns False."""
        from src.swe_team.telegram import send_message

        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "456"}, clear=True):
            result = send_message("Hello")

        assert result is False

    def test_send_message_missing_chat_id(self):
        """Missing TELEGRAM_CHAT_ID returns False."""
        from src.swe_team.telegram import send_message

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:ABC"}, clear=True):
            result = send_message("Hello")

        assert result is False

    def test_send_message_missing_both(self):
        """Both missing returns False."""
        from src.swe_team.telegram import send_message

        env = {k: v for k, v in os.environ.items()
               if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
        with patch.dict(os.environ, env, clear=True):
            result = send_message("Hello")

        assert result is False

    def test_send_message_http_error(self):
        """HTTP error returns False, does not raise."""
        import urllib.error
        from src.swe_team.telegram import send_message

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "cid"}):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    url="http://x", code=403, msg="Forbidden",
                    hdrs=None, fp=MagicMock(read=MagicMock(return_value=b"forbidden")),
                ),
            ):
                result = send_message("Hello")

        assert result is False

    def test_send_message_url_error(self):
        """Connection error (URLError) returns False, does not raise."""
        import urllib.error
        from src.swe_team.telegram import send_message

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "cid"}):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=urllib.error.URLError("Connection refused"),
            ):
                result = send_message("Hello")

        assert result is False

    def test_send_message_timeout(self):
        """Timeout returns False, does not raise."""
        from src.swe_team.telegram import send_message

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "cid"}):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=TimeoutError("timed out"),
            ):
                result = send_message("Hello")

        assert result is False

    def test_send_message_api_returns_not_ok(self):
        """API returning ok=false returns False."""
        from src.swe_team.telegram import send_message

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps(
            {"ok": False, "description": "Bad Request"}
        ).encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "cid"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response):
                result = send_message("Hello")

        assert result is False


# ======================================================================
# notifier.py — correctly calls the new telegram module
# ======================================================================


class TestNotifierUsesTelegram:
    """Verify notifier._send delegates to src.swe_team.telegram.send_message."""

    def test_send_delegates_to_telegram_module(self):
        from src.swe_team.notifier import _send

        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            result = _send("test message")

        assert result is True
        mock_send.assert_called_once_with("test message", parse_mode="HTML")

    def test_send_returns_false_on_failure(self):
        from src.swe_team.notifier import _send

        with patch("src.swe_team.telegram.send_message", return_value=False):
            result = _send("test message")

        assert result is False

    def test_send_catches_exceptions(self):
        from src.swe_team.notifier import _send

        with patch("src.swe_team.telegram.send_message", side_effect=RuntimeError("boom")):
            result = _send("test message")

        assert result is False

    def test_notify_new_tickets_calls_send(self):
        from src.swe_team.notifier import notify_new_tickets

        tickets = [
            SWETicket(title="Critical bug", description="d", severity=TicketSeverity.CRITICAL),
        ]
        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_new_tickets(tickets)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Critical bug" in msg

    def test_notify_new_tickets_skips_low(self):
        from src.swe_team.notifier import notify_new_tickets

        tickets = [
            SWETicket(title="Minor issue", description="d", severity=TicketSeverity.LOW),
        ]
        with patch("src.swe_team.telegram.send_message") as mock_send:
            notify_new_tickets(tickets)

        mock_send.assert_not_called()

    def test_notify_stability_gate_sends_on_block(self):
        from src.swe_team.notifier import notify_stability_gate

        report = StabilityReport(
            verdict=GovernanceVerdict.BLOCK,
            open_critical=2,
            failing_tests=1,
            details="Too many bugs",
        )
        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_stability_gate(report)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "BLOCKED" in msg

    def test_notify_stability_gate_skips_pass(self):
        from src.swe_team.notifier import notify_stability_gate

        report = StabilityReport(verdict=GovernanceVerdict.PASS)
        with patch("src.swe_team.telegram.send_message") as mock_send:
            notify_stability_gate(report)

        mock_send.assert_not_called()

    def test_notify_investigation_summary_sends(self):
        from src.swe_team.notifier import notify_investigation_summary

        ticket = SWETicket(
            title="Test bug",
            description="desc",
            severity=TicketSeverity.HIGH,
            source_module="scraping",
        )
        ticket.investigation_report = "Root cause: bad regex"

        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_investigation_summary(ticket)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Investigation complete" in msg
        assert "Root cause" in msg

    def test_notify_investigation_summary_skips_no_report(self):
        from src.swe_team.notifier import notify_investigation_summary

        ticket = SWETicket(title="Test bug", description="desc")
        with patch("src.swe_team.telegram.send_message") as mock_send:
            notify_investigation_summary(ticket)

        mock_send.assert_not_called()


# ======================================================================
# developer.py — uses new telegram module
# ======================================================================


class TestDeveloperTelegram:
    """Verify developer._send_telegram delegates to new module."""

    def test_developer_send_telegram(self):
        from src.swe_team.developer import DeveloperAgent

        dev = DeveloperAgent(repo_root="/tmp")
        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            dev._send_telegram("<b>test</b>")

        mock_send.assert_called_once_with("<b>test</b>", parse_mode="HTML")

    def test_developer_send_telegram_handles_error(self):
        from src.swe_team.developer import DeveloperAgent

        dev = DeveloperAgent(repo_root="/tmp")
        with patch("src.swe_team.telegram.send_message", side_effect=RuntimeError("fail")):
            # Should not raise
            dev._send_telegram("msg")


# ======================================================================
# Report modes: daily, cycle, status
# ======================================================================


class TestNotifyCycleSummary:
    """Test notify_cycle_summary."""

    def test_cycle_summary_basic(self):
        from src.swe_team.notifier import notify_cycle_summary

        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_cycle_summary(
                new_tickets=3,
                triaged=3,
                investigated=2,
                fixes_attempted=1,
                fixes_succeeded=1,
                gate_verdict="pass",
            )

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Cycle Summary" in msg
        assert "New tickets: 3" in msg
        assert "Investigated: 2" in msg
        assert "Fixes succeeded: 1" in msg
        assert "pass" in msg

    def test_cycle_summary_with_cost(self):
        from src.swe_team.notifier import notify_cycle_summary

        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_cycle_summary(
                new_tickets=1,
                gate_verdict="warn",
                cost_usd=1.23,
            )

        msg = mock_send.call_args[0][0]
        assert "$1.23" in msg

    def test_cycle_summary_no_cost(self):
        from src.swe_team.notifier import notify_cycle_summary

        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_cycle_summary(new_tickets=0, gate_verdict="N/A")

        msg = mock_send.call_args[0][0]
        assert "$" not in msg


class TestNotifyStatus:
    """Test notify_status."""

    def test_status_report(self):
        from src.swe_team.notifier import notify_status

        data = {
            "last_cycle": "2026-03-17T08:00:00",
            "tickets_open": 5,
            "gate_verdict": "pass",
        }
        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_status(data)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Status Report" in msg
        assert "tickets_open" in msg
        assert "5" in msg

    def test_status_report_empty(self):
        from src.swe_team.notifier import notify_status

        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_status({})

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "Status Report" in msg

    def test_status_report_escapes_html(self):
        from src.swe_team.notifier import notify_status

        data = {"key": "<script>alert(1)</script>"}
        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_status(data)

        msg = mock_send.call_args[0][0]
        assert "<script>" not in msg
        assert "&lt;script&gt;" in msg


# ======================================================================
# Cost aggregation
# ======================================================================


class TestAggregateDailyCosts:
    """Test aggregate_daily_costs."""

    def test_no_tickets(self):
        from src.swe_team.notifier import aggregate_daily_costs

        store = MagicMock()
        store.list_all.return_value = []
        assert aggregate_daily_costs(store) == 0.0

    def test_sums_investigation_costs_today(self):
        from src.swe_team.notifier import aggregate_daily_costs

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        t1 = SWETicket(title="a", description="b", metadata={
            "investigation": {
                "completed_at": f"{today}T10:00:00+00:00",
                "cost_usd": 0.50,
            },
        })
        t2 = SWETicket(title="c", description="d", metadata={
            "investigation": {
                "completed_at": f"{today}T11:00:00+00:00",
                "cost_usd": 1.25,
            },
        })
        store = MagicMock()
        store.list_all.return_value = [t1, t2]

        cost = aggregate_daily_costs(store)
        assert cost == 1.75

    def test_ignores_yesterday(self):
        from src.swe_team.notifier import aggregate_daily_costs

        t1 = SWETicket(title="a", description="b", metadata={
            "investigation": {
                "completed_at": "2020-01-01T10:00:00+00:00",
                "cost_usd": 5.00,
            },
        })
        store = MagicMock()
        store.list_all.return_value = [t1]

        cost = aggregate_daily_costs(store)
        assert cost == 0.0

    def test_sums_cycle_costs(self):
        from src.swe_team.notifier import aggregate_daily_costs

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        t1 = SWETicket(title="a", description="b", metadata={
            "cycle_costs": [
                {"date": today, "cost_usd": 0.30, "phase": "investigation"},
                {"date": today, "cost_usd": 0.20, "phase": "investigation"},
                {"date": "2020-01-01", "cost_usd": 9.99, "phase": "investigation"},
            ],
        })
        store = MagicMock()
        store.list_all.return_value = [t1]

        cost = aggregate_daily_costs(store)
        assert cost == 0.5

    def test_handles_invalid_cost(self):
        from src.swe_team.notifier import aggregate_daily_costs

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        t1 = SWETicket(title="a", description="b", metadata={
            "investigation": {
                "completed_at": f"{today}T10:00:00+00:00",
                "cost_usd": "not-a-number",
            },
        })
        store = MagicMock()
        store.list_all.return_value = [t1]

        cost = aggregate_daily_costs(store)
        assert cost == 0.0

    def test_combines_investigation_and_cycle_costs(self):
        from src.swe_team.notifier import aggregate_daily_costs

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        t1 = SWETicket(title="a", description="b", metadata={
            "investigation": {
                "completed_at": f"{today}T10:00:00+00:00",
                "cost_usd": 1.0,
            },
            "cycle_costs": [
                {"date": today, "cost_usd": 0.5, "phase": "investigation"},
            ],
        })
        store = MagicMock()
        store.list_all.return_value = [t1]

        cost = aggregate_daily_costs(store)
        assert cost == 1.5

    def test_store_without_list_all(self):
        """Gracefully handles stores without list_all."""
        from src.swe_team.notifier import aggregate_daily_costs

        store = object()  # no list_all method
        cost = aggregate_daily_costs(store)
        assert cost == 0.0


# ======================================================================
# Daily summary with cost
# ======================================================================


class TestDailySummaryWithCost:
    """Test that daily summary includes cost when provided."""

    def test_daily_summary_includes_cost(self, tmp_path):
        from src.swe_team.notifier import notify_daily_summary

        store_path = str(tmp_path / "tickets.json")
        store = TicketStore(store_path)
        t = SWETicket(title="Bug", description="d", severity=TicketSeverity.HIGH)
        t.transition(TicketStatus.TRIAGED)
        store.add(t)

        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_daily_summary(store, cost_total=3.14)

        msg = mock_send.call_args[0][0]
        assert "$3.14" in msg
        assert "Estimated cost" in msg

    def test_daily_summary_no_cost(self, tmp_path):
        from src.swe_team.notifier import notify_daily_summary

        store_path = str(tmp_path / "tickets.json")
        store = TicketStore(store_path)
        t = SWETicket(title="Bug", description="d", severity=TicketSeverity.HIGH)
        t.transition(TicketStatus.TRIAGED)
        store.add(t)

        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_daily_summary(store)

        msg = mock_send.call_args[0][0]
        assert "Estimated cost" not in msg

    def test_daily_summary_empty_store_with_cost(self, tmp_path):
        from src.swe_team.notifier import notify_daily_summary

        store_path = str(tmp_path / "tickets.json")
        store = TicketStore(store_path)

        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_daily_summary(store, cost_total=0.50)

        msg = mock_send.call_args[0][0]
        assert "No open tickets" in msg
        assert "$0.50" in msg

    def test_daily_summary_empty_store_no_cost(self, tmp_path):
        from src.swe_team.notifier import notify_daily_summary

        store_path = str(tmp_path / "tickets.json")
        store = TicketStore(store_path)

        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_daily_summary(store)

        msg = mock_send.call_args[0][0]
        assert "No open tickets" in msg
        assert "Estimated cost" not in msg


# ======================================================================
# Runner --report modes integration
# ======================================================================


class TestRunnerReportArgParsing:
    """Test that the runner argument parser accepts --report."""

    def test_report_daily_arg(self):
        """Runner parses --report daily."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--report", choices=["daily", "cycle", "status"])
        args = parser.parse_args(["--report", "daily"])
        assert args.report == "daily"

    def test_report_cycle_arg(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--report", choices=["daily", "cycle", "status"])
        args = parser.parse_args(["--report", "cycle"])
        assert args.report == "cycle"

    def test_report_status_arg(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--report", choices=["daily", "cycle", "status"])
        args = parser.parse_args(["--report", "status"])
        assert args.report == "status"

    def test_report_invalid_arg(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--report", choices=["daily", "cycle", "status"])
        with pytest.raises(SystemExit):
            parser.parse_args(["--report", "invalid"])


# ======================================================================
# Regression HITL notification via new telegram module
# ======================================================================


class TestRegressionHitlNotification:
    """Test notify_regression_hitl uses new telegram module."""

    def test_hitl_sends_message(self):
        from src.swe_team.notifier import notify_regression_hitl

        ticket = SWETicket(
            title="[REGRESSION] Bad bug",
            description="desc",
            severity=TicketSeverity.CRITICAL,
            source_module="api",
            metadata={
                "fingerprint": "fp123",
                "regression_of": "parent-001",
                "fix_confidence": {"regressions": 3},
            },
        )
        with patch("src.swe_team.telegram.send_message", return_value=True) as mock_send:
            notify_regression_hitl(ticket)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "HITL ESCALATION" in msg
        assert "fp123" in msg
        assert "parent-001" in msg
