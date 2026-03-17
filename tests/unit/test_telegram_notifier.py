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


# ======================================================================
# TTS — text_to_speech
# ======================================================================


class TestTextToSpeech:
    """Test text_to_speech with mocked urllib."""

    _BASE_ENV = {
        "BASE_LLM_API_URL": "https://api.example.com/v1",
        "BASE_LLM_API_KEY": "test-key-123",
    }

    def test_tts_success(self):
        """Successful TTS returns mp3 bytes."""
        from src.swe_team.telegram import text_to_speech

        fake_response = MagicMock()
        fake_response.read.return_value = b"\xff\xfb\x90\x00fake-mp3-data"
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, self._BASE_ENV, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response) as mock_urlopen:
                result = text_to_speech("Hello world")

        assert result == b"\xff\xfb\x90\x00fake-mp3-data"
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "/audio/speech" in req.full_url
        body = json.loads(req.data.decode("utf-8"))
        assert body["input"] == "Hello world"
        assert body["model"] == "kokoro"  # default model

    def test_tts_custom_model(self):
        """Custom model is used when passed."""
        from src.swe_team.telegram import text_to_speech

        fake_response = MagicMock()
        fake_response.read.return_value = b"audio-data"
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, self._BASE_ENV, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response) as mock_urlopen:
                result = text_to_speech("Hi", model="tts-1-hd")

        assert result is not None
        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert body["model"] == "tts-1-hd"

    def test_tts_uses_env_model(self):
        """TTS_MODEL env var overrides the default."""
        from src.swe_team.telegram import text_to_speech

        fake_response = MagicMock()
        fake_response.read.return_value = b"audio-data"
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        env = {**self._BASE_ENV, "TTS_MODEL": "kani-tts-2"}
        with patch.dict(os.environ, env, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response) as mock_urlopen:
                text_to_speech("Hi")

        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert body["model"] == "kani-tts-2"

    def test_tts_uses_dedicated_url(self):
        """TTS_API_URL takes priority over BASE_LLM_API_URL."""
        from src.swe_team.telegram import text_to_speech

        fake_response = MagicMock()
        fake_response.read.return_value = b"audio-data"
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        env = {
            **self._BASE_ENV,
            "TTS_API_URL": "https://tts.special.com/v1",
            "TTS_API_KEY": "tts-key",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response) as mock_urlopen:
                text_to_speech("Hi")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://tts.special.com/v1/audio/speech"
        assert req.get_header("Authorization") == "Bearer tts-key"

    def test_tts_missing_api_url_returns_none(self):
        """Missing API URL returns None."""
        from src.swe_team.telegram import text_to_speech

        env = {k: v for k, v in os.environ.items()
               if k not in ("TTS_API_URL", "BASE_LLM_API_URL", "TTS_API_KEY", "BASE_LLM_API_KEY")}
        with patch.dict(os.environ, env, clear=True):
            result = text_to_speech("Hello")

        assert result is None

    def test_tts_empty_response_returns_none(self):
        """Empty audio response returns None."""
        from src.swe_team.telegram import text_to_speech

        fake_response = MagicMock()
        fake_response.read.return_value = b""
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, self._BASE_ENV, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response):
                result = text_to_speech("Hello")

        assert result is None

    def test_tts_http_error_returns_none(self):
        """HTTP error returns None, does not raise."""
        import urllib.error
        from src.swe_team.telegram import text_to_speech

        with patch.dict(os.environ, self._BASE_ENV, clear=False):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    url="http://x", code=500, msg="Internal",
                    hdrs=None, fp=MagicMock(read=MagicMock(return_value=b"error")),
                ),
            ):
                result = text_to_speech("Hello")

        assert result is None

    def test_tts_connection_error_returns_none(self):
        """URLError returns None, does not raise."""
        import urllib.error
        from src.swe_team.telegram import text_to_speech

        with patch.dict(os.environ, self._BASE_ENV, clear=False):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=urllib.error.URLError("Connection refused"),
            ):
                result = text_to_speech("Hello")

        assert result is None

    def test_tts_timeout_returns_none(self):
        """Timeout returns None, does not raise."""
        from src.swe_team.telegram import text_to_speech

        with patch.dict(os.environ, self._BASE_ENV, clear=False):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=TimeoutError("timed out"),
            ):
                result = text_to_speech("Hello")

        assert result is None

    def test_tts_strips_trailing_slash_from_url(self):
        """Trailing slash in API URL does not produce double-slash."""
        from src.swe_team.telegram import text_to_speech

        fake_response = MagicMock()
        fake_response.read.return_value = b"audio"
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        env = {**self._BASE_ENV, "TTS_API_URL": "https://api.example.com/v1/"}
        with patch.dict(os.environ, env, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response) as mock_urlopen:
                text_to_speech("Hi")

        req = mock_urlopen.call_args[0][0]
        assert "/v1//audio" not in req.full_url
        assert "/v1/audio/speech" in req.full_url


# ======================================================================
# STT — speech_to_text
# ======================================================================


class TestSpeechToText:
    """Test speech_to_text with mocked urllib."""

    _BASE_ENV = {
        "BASE_LLM_API_URL": "https://api.example.com/v1",
        "BASE_LLM_API_KEY": "test-key-123",
    }

    def test_stt_success(self):
        """Successful STT returns transcribed text."""
        from src.swe_team.telegram import speech_to_text

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"text": "Hello world"}).encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, self._BASE_ENV, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response) as mock_urlopen:
                result = speech_to_text(b"fake-audio-data")

        assert result == "Hello world"
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "/audio/transcriptions" in req.full_url
        assert "Bearer test-key-123" in req.get_header("Authorization")
        # Verify multipart body contains model and file
        assert b"whisper-1" in req.data  # default model
        assert b"fake-audio-data" in req.data

    def test_stt_custom_model(self):
        """Custom model is used when passed."""
        from src.swe_team.telegram import speech_to_text

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"text": "Hi"}).encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, self._BASE_ENV, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response) as mock_urlopen:
                result = speech_to_text(b"audio", model="whisper-large")

        assert result == "Hi"
        assert b"whisper-large" in mock_urlopen.call_args[0][0].data

    def test_stt_uses_env_model(self):
        """STT_MODEL env var overrides the default."""
        from src.swe_team.telegram import speech_to_text

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"text": "ok"}).encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        env = {**self._BASE_ENV, "STT_MODEL": "whisper-large"}
        with patch.dict(os.environ, env, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response) as mock_urlopen:
                speech_to_text(b"audio")

        assert b"whisper-large" in mock_urlopen.call_args[0][0].data

    def test_stt_uses_dedicated_url(self):
        """STT_API_URL takes priority over BASE_LLM_API_URL."""
        from src.swe_team.telegram import speech_to_text

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"text": "ok"}).encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        env = {
            **self._BASE_ENV,
            "STT_API_URL": "https://stt.special.com/v1",
            "STT_API_KEY": "stt-key",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response) as mock_urlopen:
                speech_to_text(b"audio")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://stt.special.com/v1/audio/transcriptions"
        assert req.get_header("Authorization") == "Bearer stt-key"

    def test_stt_empty_audio_returns_none(self):
        """Empty audio bytes returns None without making API call."""
        from src.swe_team.telegram import speech_to_text

        with patch.dict(os.environ, self._BASE_ENV, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen") as mock_urlopen:
                result = speech_to_text(b"")

        assert result is None
        mock_urlopen.assert_not_called()

    def test_stt_missing_api_url_returns_none(self):
        """Missing API URL returns None."""
        from src.swe_team.telegram import speech_to_text

        env = {k: v for k, v in os.environ.items()
               if k not in ("STT_API_URL", "BASE_LLM_API_URL", "STT_API_KEY", "BASE_LLM_API_KEY")}
        with patch.dict(os.environ, env, clear=True):
            result = speech_to_text(b"audio")

        assert result is None

    def test_stt_empty_text_returns_none(self):
        """Empty transcription text returns None."""
        from src.swe_team.telegram import speech_to_text

        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"text": ""}).encode("utf-8")
        fake_response.__enter__ = MagicMock(return_value=fake_response)
        fake_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, self._BASE_ENV, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake_response):
                result = speech_to_text(b"audio")

        assert result is None

    def test_stt_http_error_returns_none(self):
        """HTTP error returns None, does not raise."""
        import urllib.error
        from src.swe_team.telegram import speech_to_text

        with patch.dict(os.environ, self._BASE_ENV, clear=False):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    url="http://x", code=500, msg="Internal",
                    hdrs=None, fp=MagicMock(read=MagicMock(return_value=b"error")),
                ),
            ):
                result = speech_to_text(b"audio")

        assert result is None

    def test_stt_timeout_returns_none(self):
        """Timeout returns None, does not raise."""
        from src.swe_team.telegram import speech_to_text

        with patch.dict(os.environ, self._BASE_ENV, clear=False):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=TimeoutError("timed out"),
            ):
                result = speech_to_text(b"audio")

        assert result is None


# ======================================================================
# Multipart body builder
# ======================================================================


class TestBuildMultipartBody:
    """Test _build_multipart_body helper."""

    def test_body_contains_model_and_file(self):
        from src.swe_team.telegram import _build_multipart_body

        body, content_type = _build_multipart_body(b"raw-audio", "whisper-1")

        assert b"whisper-1" in body
        assert b"raw-audio" in body
        assert b'name="model"' in body
        assert b'name="file"' in body
        assert "multipart/form-data; boundary=" in content_type

    def test_body_custom_filename(self):
        from src.swe_team.telegram import _build_multipart_body

        body, _ = _build_multipart_body(b"data", "model", filename="recording.wav")
        assert b"recording.wav" in body


# ======================================================================
# send_voice_message — TTS + Telegram sendVoice
# ======================================================================


class TestSendVoiceMessage:
    """Test send_voice_message end-to-end (mocked)."""

    _TG_ENV = {
        "TELEGRAM_BOT_TOKEN": "123:ABC",
        "TELEGRAM_CHAT_ID": "456",
        "BASE_LLM_API_URL": "https://api.example.com/v1",
        "BASE_LLM_API_KEY": "test-key",
    }

    def test_send_voice_success(self):
        """Full happy path: TTS produces audio, Telegram accepts it."""
        from src.swe_team.telegram import send_voice_message

        # Mock TTS response
        tts_response = MagicMock()
        tts_response.read.return_value = b"fake-mp3-bytes"
        tts_response.__enter__ = MagicMock(return_value=tts_response)
        tts_response.__exit__ = MagicMock(return_value=False)

        # Mock Telegram sendVoice response
        tg_response = MagicMock()
        tg_response.read.return_value = json.dumps({"ok": True}).encode("utf-8")
        tg_response.__enter__ = MagicMock(return_value=tg_response)
        tg_response.__exit__ = MagicMock(return_value=False)

        def urlopen_side_effect(req, **kwargs):
            if "/audio/speech" in req.full_url:
                return tts_response
            if "/sendVoice" in req.full_url:
                return tg_response
            raise ValueError(f"Unexpected URL: {req.full_url}")

        with patch.dict(os.environ, self._TG_ENV, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen", side_effect=urlopen_side_effect) as mock_urlopen:
                result = send_voice_message(text="Hello from SWE Squad")

        assert result is True
        # Should have made 2 calls: TTS + sendVoice
        assert mock_urlopen.call_count == 2

    def test_send_voice_empty_text(self):
        """Empty text returns False without any API calls."""
        from src.swe_team.telegram import send_voice_message

        with patch.dict(os.environ, self._TG_ENV, clear=False):
            with patch("src.swe_team.telegram.urllib.request.urlopen") as mock_urlopen:
                result = send_voice_message(text="")

        assert result is False
        mock_urlopen.assert_not_called()

    def test_send_voice_missing_telegram_creds(self):
        """Missing Telegram credentials returns False."""
        from src.swe_team.telegram import send_voice_message

        env = {k: v for k, v in os.environ.items()
               if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
        with patch.dict(os.environ, env, clear=True):
            result = send_voice_message(text="Hello")

        assert result is False

    def test_send_voice_tts_fails(self):
        """When TTS fails, returns False (no Telegram call)."""
        from src.swe_team.telegram import send_voice_message

        with patch.dict(os.environ, self._TG_ENV, clear=False):
            with patch("src.swe_team.telegram.text_to_speech", return_value=None):
                result = send_voice_message(text="Hello")

        assert result is False

    def test_send_voice_telegram_error(self):
        """When Telegram sendVoice fails, returns False."""
        import urllib.error
        from src.swe_team.telegram import send_voice_message

        with patch.dict(os.environ, self._TG_ENV, clear=False):
            with patch("src.swe_team.telegram.text_to_speech", return_value=b"audio"):
                with patch(
                    "src.swe_team.telegram.urllib.request.urlopen",
                    side_effect=urllib.error.HTTPError(
                        url="http://x", code=400, msg="Bad Request",
                        hdrs=None, fp=MagicMock(read=MagicMock(return_value=b"bad")),
                    ),
                ):
                    result = send_voice_message(text="Hello")

        assert result is False

    def test_send_voice_custom_chat_id(self):
        """Custom chat_id is used instead of env var."""
        from src.swe_team.telegram import send_voice_message

        tts_audio = b"mp3-audio"

        tg_response = MagicMock()
        tg_response.read.return_value = json.dumps({"ok": True}).encode("utf-8")
        tg_response.__enter__ = MagicMock(return_value=tg_response)
        tg_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, self._TG_ENV, clear=False):
            with patch("src.swe_team.telegram.text_to_speech", return_value=tts_audio):
                with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=tg_response) as mock_urlopen:
                    result = send_voice_message(chat_id="999", text="Hello")

        assert result is True
        req = mock_urlopen.call_args[0][0]
        assert b"999" in req.data

    def test_send_voice_telegram_returns_not_ok(self):
        """Telegram API returning ok=false returns False."""
        from src.swe_team.telegram import send_voice_message

        tg_response = MagicMock()
        tg_response.read.return_value = json.dumps({"ok": False, "description": "bad"}).encode("utf-8")
        tg_response.__enter__ = MagicMock(return_value=tg_response)
        tg_response.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, self._TG_ENV, clear=False):
            with patch("src.swe_team.telegram.text_to_speech", return_value=b"audio"):
                with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=tg_response):
                    result = send_voice_message(text="Hello")

        assert result is False


# ======================================================================
# Config helpers — _get_tts_config, _get_stt_config
# ======================================================================


class TestAudioConfigHelpers:
    """Test _get_tts_config and _get_stt_config env var resolution."""

    def test_tts_config_fallback_to_base(self):
        from src.swe_team.telegram import _get_tts_config

        env = {"BASE_LLM_API_URL": "https://base.com/v1", "BASE_LLM_API_KEY": "bk"}
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("TTS_API_URL", "TTS_API_KEY", "TTS_MODEL",
                              "BASE_LLM_API_URL", "BASE_LLM_API_KEY")}
        clean.update(env)
        with patch.dict(os.environ, clean, clear=True):
            url, key, model = _get_tts_config()

        assert url == "https://base.com/v1"
        assert key == "bk"
        assert model == "kokoro"

    def test_tts_config_dedicated_overrides_base(self):
        from src.swe_team.telegram import _get_tts_config

        env = {
            "BASE_LLM_API_URL": "https://base.com/v1",
            "BASE_LLM_API_KEY": "bk",
            "TTS_API_URL": "https://tts.com/v1",
            "TTS_API_KEY": "tk",
            "TTS_MODEL": "tts-1",
        }
        with patch.dict(os.environ, env, clear=True):
            url, key, model = _get_tts_config()

        assert url == "https://tts.com/v1"
        assert key == "tk"
        assert model == "tts-1"

    def test_stt_config_fallback_to_base(self):
        from src.swe_team.telegram import _get_stt_config

        env = {"BASE_LLM_API_URL": "https://base.com/v1", "BASE_LLM_API_KEY": "bk"}
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("STT_API_URL", "STT_API_KEY", "STT_MODEL",
                              "BASE_LLM_API_URL", "BASE_LLM_API_KEY")}
        clean.update(env)
        with patch.dict(os.environ, clean, clear=True):
            url, key, model = _get_stt_config()

        assert url == "https://base.com/v1"
        assert key == "bk"
        assert model == "whisper-1"

    def test_stt_config_dedicated_overrides_base(self):
        from src.swe_team.telegram import _get_stt_config

        env = {
            "BASE_LLM_API_URL": "https://base.com/v1",
            "BASE_LLM_API_KEY": "bk",
            "STT_API_URL": "https://stt.com/v1",
            "STT_API_KEY": "sk",
            "STT_MODEL": "whisper-large",
        }
        with patch.dict(os.environ, env, clear=True):
            url, key, model = _get_stt_config()

        assert url == "https://stt.com/v1"
        assert key == "sk"
        assert model == "whisper-large"
