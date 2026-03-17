"""
Tests for the enhanced Telegram module:
  - TelegramBot command registry and routing
  - Rich message formatting (inline keyboards)
  - send_photo, send_document, edit_message with mocked urllib
  - Voice stubs (STT/TTS) return appropriate not-implemented responses
  - _api_request and _multipart_request low-level helpers
"""

from __future__ import annotations

import logging
logging.logAsyncioTasks = False

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus


# ======================================================================
# Helper: mock a successful Telegram API response
# ======================================================================

def _mock_urlopen_ok(result=None):
    """Return a mock context-manager for urllib.request.urlopen."""
    if result is None:
        result = {"message_id": 42}
    fake = MagicMock()
    fake.read.return_value = json.dumps({"ok": True, "result": result}).encode("utf-8")
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    return fake


def _mock_urlopen_fail():
    """Return a mock that simulates ok=false."""
    fake = MagicMock()
    fake.read.return_value = json.dumps({"ok": False, "description": "Bad Request"}).encode("utf-8")
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    return fake


# ======================================================================
# TelegramBot — command registry and routing
# ======================================================================


class TestTelegramBotRegistry:
    """Test the command registry pattern."""

    def test_register_and_handle(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        bot.register("/ping", lambda args: "pong!")

        result = bot.handle_command("/ping")
        assert result == "pong!"

    def test_handle_command_with_args(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        bot.register("/echo", lambda args: f"echo: {args}")

        result = bot.handle_command("/echo hello world")
        assert result == "echo: hello world"

    def test_handle_unknown_command(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        result = bot.handle_command("/unknown_xyz")
        assert result is None

    def test_handle_non_command(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        result = bot.handle_command("just regular text")
        assert result is None

    def test_handle_strips_bot_username(self):
        """Commands like /status@mybot should still route to /status."""
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        result = bot.handle_command("/status@MyBotName")
        assert result is not None
        assert "status" in result.lower() or "Status" in result

    def test_register_must_start_with_slash(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        with pytest.raises(ValueError, match="must start with '/'"):
            bot.register("badcommand", lambda args: "nope")

    def test_list_commands(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        commands = bot.list_commands()
        assert "/help" in commands
        assert "/status" in commands
        assert "/tickets" in commands
        assert "/investigate" in commands
        assert "/summary" in commands

    def test_list_commands_includes_custom(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        bot.register("/custom", lambda args: "custom")
        commands = bot.list_commands()
        assert "/custom" in commands

    def test_handle_command_case_insensitive(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        bot.register("/ping", lambda args: "pong!")

        # Commands are lowered before lookup
        result = bot.handle_command("/PING")
        assert result == "pong!"

    def test_handle_command_leading_whitespace(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        bot.register("/ping", lambda args: "pong!")

        result = bot.handle_command("  /ping  ")
        assert result == "pong!"

    def test_handler_exception_returns_error_string(self):
        from src.swe_team.telegram import TelegramBot

        def failing_handler(args):
            raise RuntimeError("boom")

        bot = TelegramBot()
        bot.register("/fail", failing_handler)

        result = bot.handle_command("/fail")
        assert "failed" in result.lower()
        assert "boom" in result


# ======================================================================
# Built-in command handlers
# ======================================================================


class TestBuiltinCommands:
    """Test the built-in /help, /status, /tickets, /investigate, /summary."""

    def test_help_lists_commands(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        result = bot.handle_command("/help")
        assert "/status" in result
        assert "/tickets" in result
        assert "/investigate" in result
        assert "/summary" in result
        assert "/help" in result

    def test_help_shows_custom_commands(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        bot.register("/deploy", lambda args: "deploying")
        result = bot.handle_command("/help")
        assert "/deploy" in result

    def test_status_no_provider(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        result = bot.handle_command("/status")
        assert "No status provider" in result

    def test_status_with_provider(self):
        from src.swe_team.telegram import TelegramBot

        provider = lambda: {"tickets_open": 5, "gate": "pass", "last_cycle": "2026-03-17"}
        bot = TelegramBot(status_provider=provider)
        result = bot.handle_command("/status")
        assert "System Status" in result
        assert "tickets_open" in result
        assert "5" in result

    def test_status_provider_failure(self):
        from src.swe_team.telegram import TelegramBot

        def bad_provider():
            raise RuntimeError("db down")

        bot = TelegramBot(status_provider=bad_provider)
        result = bot.handle_command("/status")
        assert "Failed" in result

    def test_tickets_no_store(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        result = bot.handle_command("/tickets")
        assert "No ticket store" in result

    def test_tickets_empty(self):
        from src.swe_team.telegram import TelegramBot

        store = MagicMock()
        store.list_open.return_value = []
        bot = TelegramBot(ticket_store=store)
        result = bot.handle_command("/tickets")
        assert "No open tickets" in result

    def test_tickets_with_data(self):
        from src.swe_team.telegram import TelegramBot

        t1 = SWETicket(title="Critical API failure", description="d", severity=TicketSeverity.CRITICAL)
        t2 = SWETicket(title="Minor UI bug", description="d", severity=TicketSeverity.LOW)
        store = MagicMock()
        store.list_open.return_value = [t1, t2]

        bot = TelegramBot(ticket_store=store)
        result = bot.handle_command("/tickets")
        assert "Open Tickets (2)" in result
        assert "Critical API failure" in result
        assert "Minor UI bug" in result

    def test_tickets_truncates_long_list(self):
        from src.swe_team.telegram import TelegramBot

        tickets = [
            SWETicket(title=f"Ticket {i}", description="d", severity=TicketSeverity.MEDIUM)
            for i in range(25)
        ]
        store = MagicMock()
        store.list_open.return_value = tickets

        bot = TelegramBot(ticket_store=store)
        result = bot.handle_command("/tickets")
        assert "and 5 more" in result

    def test_investigate_no_ticket_id(self):
        from src.swe_team.telegram import TelegramBot

        store = MagicMock()
        bot = TelegramBot(ticket_store=store)
        result = bot.handle_command("/investigate")
        assert "Usage" in result

    def test_investigate_no_store(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        result = bot.handle_command("/investigate T-001")
        assert "No ticket store" in result

    def test_investigate_ticket_found(self):
        from src.swe_team.telegram import TelegramBot

        ticket = SWETicket(title="API timeout", description="d", severity=TicketSeverity.HIGH)
        store = MagicMock()
        store.get.return_value = ticket

        bot = TelegramBot(ticket_store=store)
        result = bot.handle_command(f"/investigate {ticket.ticket_id}")
        assert "Investigation queued" in result
        assert "API timeout" in result

    def test_investigate_ticket_not_found(self):
        from src.swe_team.telegram import TelegramBot

        store = MagicMock()
        store.get.return_value = None

        bot = TelegramBot(ticket_store=store)
        result = bot.handle_command("/investigate T-NONEXISTENT")
        assert "not found" in result.lower()

    def test_summary_no_store(self):
        from src.swe_team.telegram import TelegramBot

        bot = TelegramBot()
        result = bot.handle_command("/summary")
        assert "No ticket store" in result

    def test_summary_empty(self):
        from src.swe_team.telegram import TelegramBot

        store = MagicMock()
        store.list_open.return_value = []

        bot = TelegramBot(ticket_store=store)
        result = bot.handle_command("/summary")
        assert "No open tickets" in result

    def test_summary_with_tickets(self):
        from src.swe_team.telegram import TelegramBot

        tickets = [
            SWETicket(title="Bug 1", description="d", severity=TicketSeverity.CRITICAL),
            SWETicket(title="Bug 2", description="d", severity=TicketSeverity.HIGH),
            SWETicket(title="Bug 3", description="d", severity=TicketSeverity.HIGH),
        ]
        store = MagicMock()
        store.list_open.return_value = tickets

        bot = TelegramBot(ticket_store=store)
        result = bot.handle_command("/summary")
        assert "Daily Summary" in result
        assert "3 open ticket" in result
        assert "CRITICAL: 1" in result
        assert "HIGH: 2" in result


# ======================================================================
# Inline keyboard construction
# ======================================================================


class TestInlineKeyboard:
    """Test build_inline_keyboard and build_alert_keyboard."""

    def test_build_inline_keyboard_structure(self):
        from src.swe_team.telegram import build_inline_keyboard

        kb = build_inline_keyboard([
            [{"text": "Button A", "callback_data": "a"}],
            [{"text": "Button B", "callback_data": "b"},
             {"text": "Button C", "url": "https://example.com"}],
        ])

        assert "inline_keyboard" in kb
        rows = kb["inline_keyboard"]
        assert len(rows) == 2
        assert len(rows[0]) == 1
        assert len(rows[1]) == 2
        assert rows[0][0]["text"] == "Button A"
        assert rows[0][0]["callback_data"] == "a"
        assert rows[1][1]["url"] == "https://example.com"

    def test_build_inline_keyboard_empty(self):
        from src.swe_team.telegram import build_inline_keyboard

        kb = build_inline_keyboard([])
        assert kb == {"inline_keyboard": []}

    def test_build_alert_keyboard(self):
        from src.swe_team.telegram import build_alert_keyboard

        kb = build_alert_keyboard("T-001")
        rows = kb["inline_keyboard"]
        assert len(rows) == 2

        # First row: Investigate button
        assert rows[0][0]["text"] == "Investigate"
        assert rows[0][0]["callback_data"] == "investigate:T-001"

        # Second row: Acknowledge + Dismiss
        assert rows[1][0]["text"] == "Acknowledge"
        assert rows[1][0]["callback_data"] == "ack:T-001"
        assert rows[1][1]["text"] == "Dismiss"
        assert rows[1][1]["callback_data"] == "dismiss:T-001"

    def test_alert_keyboard_serializable(self):
        """The keyboard dict must be JSON-serializable for the API payload."""
        from src.swe_team.telegram import build_alert_keyboard

        kb = build_alert_keyboard("T-999")
        serialized = json.dumps(kb)
        deserialized = json.loads(serialized)
        assert deserialized == kb


# ======================================================================
# send_message — enhanced features
# ======================================================================


class TestSendMessageEnhanced:
    """Test new send_message features: reply_to, reply_markup, chat_id override."""

    def test_send_message_with_reply_to(self):
        from src.swe_team.telegram import send_message

        fake = _mock_urlopen_ok()

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "cid"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake) as mock_open:
                result = send_message("Reply text", reply_to_message_id=42)

        assert result is True
        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert body["reply_to_message_id"] == 42

    def test_send_message_with_inline_keyboard(self):
        from src.swe_team.telegram import send_message, build_inline_keyboard

        fake = _mock_urlopen_ok()
        kb = build_inline_keyboard([[{"text": "OK", "callback_data": "ok"}]])

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "cid"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake) as mock_open:
                result = send_message("Choose:", reply_markup=kb)

        assert result is True
        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert "inline_keyboard" in body["reply_markup"]

    def test_send_message_with_chat_id_override(self):
        from src.swe_team.telegram import send_message

        fake = _mock_urlopen_ok()

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "default"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake) as mock_open:
                result = send_message("msg", chat_id="override-123")

        assert result is True
        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert body["chat_id"] == "override-123"

    def test_send_message_without_optional_fields(self):
        """Verify reply_to and reply_markup are omitted when not provided."""
        from src.swe_team.telegram import send_message

        fake = _mock_urlopen_ok()

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "cid"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake) as mock_open:
                send_message("plain message")

        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert "reply_to_message_id" not in body
        assert "reply_markup" not in body


# ======================================================================
# edit_message
# ======================================================================


class TestEditMessage:
    """Test edit_message (live status updates, inspired by OpenClaw streaming)."""

    def test_edit_message_success(self):
        from src.swe_team.telegram import edit_message

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._api_request", return_value={"message_id": 42}) as mock_api:
                result = edit_message("chat123", 42, "Updated text")

        assert result is True
        mock_api.assert_called_once()
        payload = mock_api.call_args[0][1]
        assert payload["chat_id"] == "chat123"
        assert payload["message_id"] == 42
        assert payload["text"] == "Updated text"
        assert payload["parse_mode"] == "HTML"

    def test_edit_message_failure(self):
        from src.swe_team.telegram import edit_message

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._api_request", return_value=None):
                result = edit_message("chat123", 42, "text")

        assert result is False

    def test_edit_message_with_keyboard(self):
        from src.swe_team.telegram import edit_message, build_inline_keyboard

        kb = build_inline_keyboard([[{"text": "Done", "callback_data": "done"}]])

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._api_request", return_value={"message_id": 42}) as mock_api:
                result = edit_message("chat123", 42, "Updated", reply_markup=kb)

        assert result is True
        payload = mock_api.call_args[0][1]
        assert "reply_markup" in payload
        assert payload["reply_markup"]["inline_keyboard"][0][0]["text"] == "Done"


# ======================================================================
# send_photo
# ======================================================================


class TestSendPhoto:
    """Test send_photo with mocked multipart request."""

    def test_send_photo_success(self):
        from src.swe_team.telegram import send_photo

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._multipart_request", return_value={"message_id": 1}) as mock_mp:
                result = send_photo("chat123", b"\x89PNG\r\n", caption="A chart")

        assert result is True
        mock_mp.assert_called_once()
        method, fields = mock_mp.call_args[0]
        assert method == "sendPhoto"
        assert fields["chat_id"] == "chat123"
        assert fields["caption"] == "A chart"
        assert fields["parse_mode"] == "HTML"
        # photo should be a (filename, bytes, content_type) tuple
        assert isinstance(fields["photo"], tuple)
        assert fields["photo"][1] == b"\x89PNG\r\n"

    def test_send_photo_failure(self):
        from src.swe_team.telegram import send_photo

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._multipart_request", return_value=None):
                result = send_photo("chat123", b"\x89PNG")

        assert result is False

    def test_send_photo_no_caption(self):
        from src.swe_team.telegram import send_photo

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._multipart_request", return_value={"message_id": 1}) as mock_mp:
                result = send_photo("chat123", b"img")

        assert result is True
        fields = mock_mp.call_args[0][1]
        assert "caption" not in fields
        assert "parse_mode" not in fields


# ======================================================================
# send_document
# ======================================================================


class TestSendDocument:
    """Test send_document with mocked multipart request."""

    def test_send_document_success(self):
        from src.swe_team.telegram import send_document

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._multipart_request", return_value={"message_id": 1}) as mock_mp:
                result = send_document("chat123", b"CSV data", "report.csv", caption="Monthly report")

        assert result is True
        method, fields = mock_mp.call_args[0]
        assert method == "sendDocument"
        assert fields["chat_id"] == "chat123"
        assert fields["caption"] == "Monthly report"
        # document should be a tuple
        fname, data, ctype = fields["document"]
        assert fname == "report.csv"
        assert data == b"CSV data"
        assert ctype == "text/csv"

    def test_send_document_failure(self):
        from src.swe_team.telegram import send_document

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._multipart_request", return_value=None):
                result = send_document("chat123", b"data", "file.bin")

        assert result is False

    def test_send_document_pdf_content_type(self):
        from src.swe_team.telegram import send_document

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._multipart_request", return_value={"message_id": 1}) as mock_mp:
                send_document("c", b"pdf", "report.pdf")

        fields = mock_mp.call_args[0][1]
        assert fields["document"][2] == "application/pdf"

    def test_send_document_json_content_type(self):
        from src.swe_team.telegram import send_document

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._multipart_request", return_value={"message_id": 1}) as mock_mp:
                send_document("c", b"{}", "data.json")

        fields = mock_mp.call_args[0][1]
        assert fields["document"][2] == "application/json"

    def test_send_document_txt_content_type(self):
        from src.swe_team.telegram import send_document

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._multipart_request", return_value={"message_id": 1}) as mock_mp:
                send_document("c", b"text", "log.txt")

        fields = mock_mp.call_args[0][1]
        assert fields["document"][2] == "text/plain"

    def test_send_document_unknown_content_type(self):
        from src.swe_team.telegram import send_document

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._multipart_request", return_value={"message_id": 1}) as mock_mp:
                send_document("c", b"binary", "file.xyz")

        fields = mock_mp.call_args[0][1]
        assert fields["document"][2] == "application/octet-stream"

    def test_send_document_no_caption(self):
        from src.swe_team.telegram import send_document

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram._multipart_request", return_value={"message_id": 1}) as mock_mp:
                send_document("c", b"data", "file.bin")

        fields = mock_mp.call_args[0][1]
        assert "caption" not in fields


# ======================================================================
# Voice — send_voice_message (real impl, returns False without creds)
# and transcribe_voice (stub)
# ======================================================================


class TestVoiceFeatures:
    """Test voice integration — send_voice_message returns False without
    TTS credentials; transcribe_voice is still a stub."""

    def test_send_voice_message_no_creds_returns_false(self):
        """send_voice_message returns False when no TTS/Telegram creds."""
        from src.swe_team.telegram import send_voice_message

        env = {k: v for k, v in os.environ.items()
               if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                             "TTS_API_URL", "BASE_LLM_API_URL")}
        with patch.dict(os.environ, env, clear=True):
            result = send_voice_message(chat_id="chat123", text="Hello from SWE-Squad")
        assert result is False

    def test_send_voice_message_empty_text_returns_false(self):
        from src.swe_team.telegram import send_voice_message

        result = send_voice_message(text="")
        assert result is False

    def test_send_voice_message_missing_telegram_creds(self):
        from src.swe_team.telegram import send_voice_message

        env = {k: v for k, v in os.environ.items()
               if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
        with patch.dict(os.environ, env, clear=True):
            result = send_voice_message(text="test")
        assert result is False

    def test_transcribe_voice_returns_none(self):
        from src.swe_team.telegram import transcribe_voice

        result = transcribe_voice("file123")
        assert result is None

    def test_transcribe_voice_default_provider(self):
        from src.swe_team.telegram import transcribe_voice

        result = transcribe_voice("file123", stt_provider="whisper")
        assert result is None

    def test_transcribe_voice_openai_provider(self):
        from src.swe_team.telegram import transcribe_voice

        result = transcribe_voice("file123", stt_provider="openai")
        assert result is None

    def test_transcribe_voice_groq_provider(self):
        from src.swe_team.telegram import transcribe_voice

        result = transcribe_voice("file123", stt_provider="groq")
        assert result is None

    def test_transcribe_voice_deepgram_provider(self):
        from src.swe_team.telegram import transcribe_voice

        result = transcribe_voice("file123", stt_provider="deepgram")
        assert result is None

    def test_transcribe_voice_sherpa_provider(self):
        from src.swe_team.telegram import transcribe_voice

        result = transcribe_voice("file123", stt_provider="sherpa-onnx")
        assert result is None


# ======================================================================
# _api_request low-level helper
# ======================================================================


class TestApiRequest:
    """Test _api_request with mocked urllib."""

    def test_api_request_success(self):
        from src.swe_team.telegram import _api_request

        fake = _mock_urlopen_ok({"message_id": 99})

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake):
                result = _api_request("editMessageText", {"chat_id": "c", "text": "t"})

        assert result == {"message_id": 99}

    def test_api_request_with_explicit_token(self):
        from src.swe_team.telegram import _api_request

        fake = _mock_urlopen_ok({"ok": True})

        with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake) as mock_open:
            result = _api_request("getMe", {}, token="explicit-token")

        assert result is not None
        req = mock_open.call_args[0][0]
        assert "/botexplicit-token/" in req.full_url

    def test_api_request_missing_token(self):
        from src.swe_team.telegram import _api_request

        env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            result = _api_request("getMe", {})

        assert result is None

    def test_api_request_http_error(self):
        import urllib.error
        from src.swe_team.telegram import _api_request

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    url="http://x", code=400, msg="Bad",
                    hdrs=None, fp=MagicMock(read=MagicMock(return_value=b"error")),
                ),
            ):
                result = _api_request("test", {})

        assert result is None

    def test_api_request_url_error(self):
        import urllib.error
        from src.swe_team.telegram import _api_request

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=urllib.error.URLError("refused"),
            ):
                result = _api_request("test", {})

        assert result is None

    def test_api_request_timeout(self):
        from src.swe_team.telegram import _api_request

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=TimeoutError("timeout"),
            ):
                result = _api_request("test", {})

        assert result is None

    def test_api_request_ok_false(self):
        from src.swe_team.telegram import _api_request

        fake = _mock_urlopen_fail()

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake):
                result = _api_request("test", {})

        assert result is None


# ======================================================================
# _multipart_request low-level helper
# ======================================================================


class TestMultipartRequest:
    """Test _multipart_request with mocked urllib."""

    def test_multipart_success(self):
        from src.swe_team.telegram import _multipart_request

        fake = _mock_urlopen_ok({"file_id": "abc"})

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake) as mock_open:
                result = _multipart_request("sendPhoto", {
                    "chat_id": "123",
                    "photo": ("photo.png", b"\x89PNG", "image/png"),
                })

        assert result == {"file_id": "abc"}
        req = mock_open.call_args[0][0]
        assert "multipart/form-data" in req.get_header("Content-type")
        assert b"photo.png" in req.data

    def test_multipart_string_field(self):
        from src.swe_team.telegram import _multipart_request

        fake = _mock_urlopen_ok()

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake) as mock_open:
                _multipart_request("sendPhoto", {
                    "chat_id": "456",
                    "caption": "My caption",
                })

        req = mock_open.call_args[0][0]
        assert b"My caption" in req.data

    def test_multipart_raw_bytes_field(self):
        from src.swe_team.telegram import _multipart_request

        fake = _mock_urlopen_ok()

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake) as mock_open:
                _multipart_request("sendVoice", {
                    "chat_id": "789",
                    "voice": b"\x00\x01\x02\x03",
                })

        req = mock_open.call_args[0][0]
        assert b"\x00\x01\x02\x03" in req.data

    def test_multipart_missing_token(self):
        from src.swe_team.telegram import _multipart_request

        env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            result = _multipart_request("sendPhoto", {"chat_id": "c"})

        assert result is None

    def test_multipart_http_error(self):
        import urllib.error
        from src.swe_team.telegram import _multipart_request

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    url="http://x", code=400, msg="Bad",
                    hdrs=None, fp=MagicMock(read=MagicMock(return_value=b"err")),
                ),
            ):
                result = _multipart_request("sendPhoto", {"chat_id": "c"})

        assert result is None

    def test_multipart_url_error(self):
        import urllib.error
        from src.swe_team.telegram import _multipart_request

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=urllib.error.URLError("refused"),
            ):
                result = _multipart_request("sendPhoto", {"chat_id": "c"})

        assert result is None

    def test_multipart_timeout(self):
        from src.swe_team.telegram import _multipart_request

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch(
                "src.swe_team.telegram.urllib.request.urlopen",
                side_effect=TimeoutError("timeout"),
            ):
                result = _multipart_request("sendPhoto", {"chat_id": "c"})

        assert result is None

    def test_multipart_ok_false(self):
        from src.swe_team.telegram import _multipart_request

        fake = _mock_urlopen_fail()

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            with patch("src.swe_team.telegram.urllib.request.urlopen", return_value=fake):
                result = _multipart_request("sendPhoto", {"chat_id": "c"})

        assert result is None


# ======================================================================
# HTML escape utility
# ======================================================================


class TestEscapeHtml:
    """Test the _esc utility function in telegram module."""

    def test_esc_ampersand(self):
        from src.swe_team.telegram import _esc

        assert _esc("A & B") == "A &amp; B"

    def test_esc_angle_brackets(self):
        from src.swe_team.telegram import _esc

        assert _esc("<script>") == "&lt;script&gt;"

    def test_esc_quotes(self):
        from src.swe_team.telegram import _esc

        assert _esc('say "hello"') == "say &quot;hello&quot;"

    def test_esc_all_together(self):
        from src.swe_team.telegram import _esc

        assert _esc('<a href="x">&</a>') == "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;"

    def test_esc_passthrough(self):
        from src.swe_team.telegram import _esc

        assert _esc("plain text") == "plain text"
