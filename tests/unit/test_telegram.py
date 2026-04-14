"""Unit tests for src/swe_team/telegram.py — stdlib Telegram Bot API client."""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.swe_team import telegram as tg
from src.swe_team.telegram import (
    _esc,
    _api_request,
    _multipart_request,
    send_message,
    send_photo,
    send_document,
    build_inline_keyboard,
    build_alert_keyboard,
    text_to_speech,
    speech_to_text,
    TelegramBot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_urlopen_response(body: dict, status: int = 200):
    """Return a context-manager mock that simulates urllib.request.urlopen."""
    encoded = json.dumps(body).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = encoded
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_http_error(code: int, body: str = "error"):
    """Return a urllib.error.HTTPError with a readable body."""
    err = urllib.error.HTTPError(
        url="http://example.com",
        code=code,
        msg=body,
        hdrs={},
        fp=io.BytesIO(body.encode()),
    )
    return err


# ---------------------------------------------------------------------------
# Tests: _esc
# ---------------------------------------------------------------------------

class TestEsc(unittest.TestCase):
    def test_ampersand(self):
        assert _esc("a&b") == "a&amp;b"

    def test_less_than(self):
        assert _esc("a<b") == "a&lt;b"

    def test_greater_than(self):
        assert _esc("a>b") == "a&gt;b"

    def test_double_quote(self):
        assert _esc('say "hi"') == "say &quot;hi&quot;"

    def test_combined(self):
        result = _esc('<script>alert("xss")</script>')
        assert "&lt;script&gt;" in result
        assert "&quot;xss&quot;" in result

    def test_plain_text_unchanged(self):
        assert _esc("hello world") == "hello world"


# ---------------------------------------------------------------------------
# Tests: _api_request
# ---------------------------------------------------------------------------

class TestApiRequest(unittest.TestCase):
    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_success_returns_result(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response(
            {"ok": True, "result": {"message_id": 42}}
        )
        result = _api_request("sendMessage", {"chat_id": "123", "text": "hi"}, token="tok")
        assert result == {"message_id": 42}

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_ok_false_returns_none(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response(
            {"ok": False, "description": "Bad Request"}
        )
        result = _api_request("sendMessage", {}, token="tok")
        assert result is None

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_http_error_returns_none(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(403)
        result = _api_request("sendMessage", {}, token="tok")
        assert result is None

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_url_error_returns_none(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("network unreachable")
        result = _api_request("sendMessage", {}, token="tok")
        assert result is None

    def test_missing_token_returns_none(self):
        # Ensure env var is absent
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            result = _api_request("sendMessage", {}, token=None)
        assert result is None

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_auth_provider_called_on_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response(
            {"ok": True, "result": {"message_id": 1}}
        )
        mock_provider = MagicMock()
        tg.set_auth_provider(mock_provider)
        try:
            _api_request("sendMessage", {}, token="tok")
            mock_provider.record_auth_success.assert_called_once_with("telegram")
        finally:
            tg.set_auth_provider(None)

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_auth_provider_called_on_401(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(401, "Unauthorized")
        mock_provider = MagicMock()
        tg.set_auth_provider(mock_provider)
        try:
            _api_request("sendMessage", {}, token="tok")
            mock_provider.record_auth_failure.assert_called_once()
        finally:
            tg.set_auth_provider(None)


# ---------------------------------------------------------------------------
# Tests: send_message
# ---------------------------------------------------------------------------

class TestSendMessage(unittest.TestCase):
    def setUp(self):
        import src.swe_team.telegram as _tg
        _tg._rl_cache.clear()
        _tg._rl_timestamps.clear()

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_send_message_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"ok": True})
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "token123", "TELEGRAM_CHAT_ID": "chat456"},
        ):
            assert send_message("Hello") is True

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_send_message_ok_false(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"ok": False})
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "token123", "TELEGRAM_CHAT_ID": "chat456"},
        ):
            assert send_message("Hello") is False

    def test_send_message_missing_token(self):
        env = {"TELEGRAM_CHAT_ID": "chat456"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            assert send_message("Hello") is False

    def test_send_message_missing_chat_id(self):
        env = {"TELEGRAM_BOT_TOKEN": "token123"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("TELEGRAM_CHAT_ID", None)
            assert send_message("Hello") is False

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_send_message_http_error(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(429)
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "token123", "TELEGRAM_CHAT_ID": "chat456"},
        ):
            assert send_message("Hello") is False

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_send_message_url_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("timeout")
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "token123", "TELEGRAM_CHAT_ID": "chat456"},
        ):
            assert send_message("Hello") is False

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_send_message_with_reply_to(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"ok": True})
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "token123", "TELEGRAM_CHAT_ID": "chat456"},
        ):
            assert send_message("Reply", reply_to_message_id=99) is True

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_send_message_with_reply_markup(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"ok": True})
        keyboard = build_inline_keyboard([[{"text": "OK", "callback_data": "ok"}]])
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "token123", "TELEGRAM_CHAT_ID": "chat456"},
        ):
            assert send_message("Pick one", reply_markup=keyboard) is True

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_send_message_overriding_chat_id(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"ok": True})
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "token123", "TELEGRAM_CHAT_ID": "default"},
        ):
            assert send_message("Hi", chat_id="override_chat") is True
        # Verify the request body used "override_chat"
        call_args = mock_urlopen.call_args
        req_obj = call_args[0][0]
        body = json.loads(req_obj.data.decode())
        assert body["chat_id"] == "override_chat"


# ---------------------------------------------------------------------------
# Tests: send_photo
# ---------------------------------------------------------------------------

class TestSendPhoto(unittest.TestCase):
    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_send_photo_success(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response(
            {"ok": True, "result": {"message_id": 5}}
        )
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            result = send_photo("chat1", b"\x89PNG\r\n\x1a\n")
        assert result is True

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_send_photo_with_caption(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response(
            {"ok": True, "result": {"message_id": 6}}
        )
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            result = send_photo("chat1", b"\x89PNG\r\n", caption="Chart")
        assert result is True

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_send_photo_failure(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"ok": False})
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
            result = send_photo("chat1", b"\x89PNG\r\n")
        assert result is False

    def test_send_photo_missing_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            result = send_photo("chat1", b"data")
        assert result is False


# ---------------------------------------------------------------------------
# Tests: build_inline_keyboard / build_alert_keyboard
# ---------------------------------------------------------------------------

class TestKeyboards(unittest.TestCase):
    def test_build_inline_keyboard_structure(self):
        kb = build_inline_keyboard([
            [{"text": "A", "callback_data": "a"}],
            [{"text": "B", "callback_data": "b"}],
        ])
        assert "inline_keyboard" in kb
        assert len(kb["inline_keyboard"]) == 2

    def test_build_alert_keyboard_ticket_id(self):
        kb = build_alert_keyboard("T-001")
        flat_buttons = [btn for row in kb["inline_keyboard"] for btn in row]
        callback_data = [b["callback_data"] for b in flat_buttons]
        assert "investigate:T-001" in callback_data
        assert "ack:T-001" in callback_data
        assert "dismiss:T-001" in callback_data


# ---------------------------------------------------------------------------
# Tests: TelegramBot command registry
# ---------------------------------------------------------------------------

class TestTelegramBot(unittest.TestCase):
    def test_init_registers_builtin_commands(self):
        bot = TelegramBot()
        cmds = bot.list_commands()
        assert "/help" in cmds
        assert "/status" in cmds
        assert "/tickets" in cmds
        assert "/investigate" in cmds
        assert "/summary" in cmds

    def test_register_custom_command(self):
        bot = TelegramBot()
        bot.register("/ping", lambda args: "pong")
        assert bot.handle_command("/ping") == "pong"

    def test_register_requires_slash(self):
        bot = TelegramBot()
        with self.assertRaises(ValueError):
            bot.register("noslash", lambda args: "")

    def test_handle_unknown_command_returns_none(self):
        bot = TelegramBot()
        assert bot.handle_command("/unknown_xyz") is None

    def test_handle_non_command_returns_none(self):
        bot = TelegramBot()
        assert bot.handle_command("plain text") is None

    def test_handle_command_strips_bot_name(self):
        bot = TelegramBot()
        bot.register("/ping", lambda args: "pong")
        assert bot.handle_command("/ping@mybot") == "pong"

    def test_cmd_help_lists_builtins(self):
        bot = TelegramBot()
        resp = bot.handle_command("/help")
        assert "/status" in resp
        assert "/tickets" in resp

    def test_cmd_status_no_provider(self):
        bot = TelegramBot()
        resp = bot.handle_command("/status")
        assert "No status provider" in resp

    def test_cmd_status_with_provider(self):
        bot = TelegramBot(status_provider=lambda: {"gate": "PASS", "tickets": 3})
        resp = bot.handle_command("/status")
        assert "gate" in resp.lower() or "PASS" in resp

    def test_cmd_tickets_no_store(self):
        bot = TelegramBot()
        resp = bot.handle_command("/tickets")
        assert "No ticket store" in resp

    def test_cmd_investigate_no_args(self):
        bot = TelegramBot()
        resp = bot.handle_command("/investigate")
        assert "Usage" in resp

    def test_cmd_investigate_no_store(self):
        bot = TelegramBot()
        resp = bot.handle_command("/investigate T-001")
        assert "No ticket store" in resp

    def test_handler_exception_returns_error_string(self):
        bot = TelegramBot()
        bot.register("/boom", lambda args: (_ for _ in ()).throw(RuntimeError("oops")))
        resp = bot.handle_command("/boom")
        assert "failed" in resp.lower() or "oops" in resp


# ---------------------------------------------------------------------------
# Tests: text_to_speech
# ---------------------------------------------------------------------------

class TestTextToSpeech(unittest.TestCase):
    def test_tts_missing_config_returns_none(self):
        with patch.dict(os.environ, {}, clear=False):
            for k in ("TTS_API_URL", "BASE_LLM_API_URL", "TTS_API_KEY", "BASE_LLM_API_KEY"):
                os.environ.pop(k, None)
            result = text_to_speech("hello")
        assert result is None

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_tts_success_returns_bytes(self, mock_urlopen):
        audio = b"\xff\xfb\x90d" * 100  # fake mp3 bytes
        mock_resp = MagicMock()
        mock_resp.read.return_value = audio
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        env = {
            "TTS_API_URL": "http://llm.example.com/v1",
            "TTS_API_KEY": "key123",
        }
        with patch.dict(os.environ, env):
            result = text_to_speech("hello world")
        assert result == audio

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_tts_empty_response_returns_none(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b""
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        env = {
            "TTS_API_URL": "http://llm.example.com/v1",
            "TTS_API_KEY": "key123",
        }
        with patch.dict(os.environ, env):
            result = text_to_speech("hello")
        assert result is None

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_tts_http_error_returns_none(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(500)
        env = {
            "TTS_API_URL": "http://llm.example.com/v1",
            "TTS_API_KEY": "key123",
        }
        with patch.dict(os.environ, env):
            result = text_to_speech("hello")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: speech_to_text
# ---------------------------------------------------------------------------

class TestSpeechToText(unittest.TestCase):
    def test_stt_empty_audio_returns_none(self):
        result = speech_to_text(b"")
        assert result is None

    def test_stt_missing_config_returns_none(self):
        with patch.dict(os.environ, {}, clear=False):
            for k in ("STT_API_URL", "BASE_LLM_API_URL", "STT_API_KEY", "BASE_LLM_API_KEY"):
                os.environ.pop(k, None)
            result = speech_to_text(b"audio data")
        assert result is None

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_stt_success_returns_text(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"text": "hello world"})
        env = {
            "STT_API_URL": "http://llm.example.com/v1",
            "STT_API_KEY": "key123",
        }
        with patch.dict(os.environ, env):
            result = speech_to_text(b"fake_audio_data")
        assert result == "hello world"

    @patch("src.swe_team.telegram.urllib.request.urlopen")
    def test_stt_empty_transcription_returns_none(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({"text": "  "})
        env = {
            "STT_API_URL": "http://llm.example.com/v1",
            "STT_API_KEY": "key123",
        }
        with patch.dict(os.environ, env):
            result = speech_to_text(b"audio")
        assert result is None


if __name__ == "__main__":
    unittest.main()
