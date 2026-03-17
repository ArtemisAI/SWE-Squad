"""
Standalone Telegram Bot API client using only stdlib.

Reads ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` from the environment.
Uses ``urllib`` for HTTP — zero external dependencies, consistent with the
rest of the SWE-Squad project.

Includes STT (speech-to-text) and TTS (text-to-speech) via OpenAI-compatible
audio endpoints.  API URLs and keys fall back to ``BASE_LLM_API_URL`` /
``BASE_LLM_API_KEY`` when the provider-specific env vars are unset.

All functions are best-effort: they return True/False/None and never raise.

Enhanced with patterns inspired by the OpenClaw project
(https://github.com/openclaw/openclaw):
  - Interactive bot command registry (slash commands)
  - Rich message formatting with inline keyboards
  - Message threading (reply_to_message_id)
  - Photo/document sending
  - Message editing (live status updates)
  - Voice integration (STT/TTS via OpenAI-compatible endpoints)
"""

from __future__ import annotations

import io
import json
import logging
import os
import uuid
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"

# Default STT/TTS models — overridable via env vars
_DEFAULT_TTS_MODEL = "kokoro"
_DEFAULT_STT_MODEL = "whisper-1"


# ======================================================================
# Credentials
# ======================================================================


def _get_credentials() -> tuple[Optional[str], Optional[str]]:
    """Return (bot_token, chat_id) from environment, or (None, None)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    return token, chat_id


def _get_tts_config() -> tuple[Optional[str], Optional[str], str]:
    """Return (api_url, api_key, model) for the TTS endpoint."""
    api_url = os.environ.get("TTS_API_URL") or os.environ.get("BASE_LLM_API_URL")
    api_key = os.environ.get("TTS_API_KEY") or os.environ.get("BASE_LLM_API_KEY")
    model = os.environ.get("TTS_MODEL", _DEFAULT_TTS_MODEL)
    return api_url, api_key, model


def _get_stt_config() -> tuple[Optional[str], Optional[str], str]:
    """Return (api_url, api_key, model) for the STT endpoint."""
    api_url = os.environ.get("STT_API_URL") or os.environ.get("BASE_LLM_API_URL")
    api_key = os.environ.get("STT_API_KEY") or os.environ.get("BASE_LLM_API_KEY")
    model = os.environ.get("STT_MODEL", _DEFAULT_STT_MODEL)
    return api_url, api_key, model


# ======================================================================
# Low-level API helpers
# ======================================================================


def _api_request(
    method: str,
    payload: dict,
    *,
    token: Optional[str] = None,
) -> Optional[dict]:
    """Make a JSON POST request to the Telegram Bot API.

    Parameters
    ----------
    method:
        Telegram Bot API method name (e.g. ``"sendMessage"``).
    payload:
        JSON-serialisable request body.
    token:
        Bot token.  If *None*, read from ``TELEGRAM_BOT_TOKEN``.

    Returns
    -------
    dict or None
        The parsed ``result`` field on success, or *None* on failure.
        Never raises.
    """
    if token is None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.warning("Telegram bot token missing")
        return None

    url = f"{_API_BASE}/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if body.get("ok"):
                return body.get("result")
            logger.warning("Telegram API returned ok=false: %s", body)
            return None
    except urllib.error.HTTPError as exc:
        logger.warning(
            "Telegram HTTP error %d: %s",
            exc.code,
            exc.read().decode("utf-8", errors="replace")[:200],
        )
        return None
    except urllib.error.URLError as exc:
        logger.warning("Telegram connection error: %s", exc.reason)
        return None
    except (OSError, ValueError, TimeoutError) as exc:
        logger.warning("Telegram request failed: %s", exc)
        return None


def _multipart_request(
    method: str,
    fields: Dict[str, Any],
    *,
    token: Optional[str] = None,
) -> Optional[dict]:
    """Make a multipart/form-data POST to the Telegram Bot API.

    Used for sending binary data (photos, documents, voice messages).

    Parameters
    ----------
    method:
        Telegram Bot API method (e.g. ``"sendPhoto"``).
    fields:
        Dict of field names to values.  Values that are ``bytes`` or
        ``(filename, bytes, content_type)`` tuples are sent as file uploads.
        Everything else is sent as a string field.
    token:
        Bot token override.

    Returns
    -------
    dict or None
        The ``result`` on success, or *None* on failure.
    """
    if token is None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.warning("Telegram bot token missing")
        return None

    boundary = "----SWESquadBoundary9876543210"
    body_parts: list[bytes] = []

    for key, value in fields.items():
        if isinstance(value, tuple) and len(value) == 3:
            filename, data_bytes, content_type = value
            body_parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
            )
            body_parts.append(data_bytes)
            body_parts.append(b"\r\n")
        elif isinstance(value, bytes):
            body_parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"; '
                f'filename="{key}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
            )
            body_parts.append(value)
            body_parts.append(b"\r\n")
        else:
            body_parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8")
            )

    body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body_data = b"".join(body_parts)

    url = f"{_API_BASE}/bot{token}/{method}"
    req = urllib.request.Request(
        url,
        data=body_data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            response_body = json.loads(resp.read().decode("utf-8"))
            if response_body.get("ok"):
                return response_body.get("result")
            logger.warning("Telegram API returned ok=false: %s", response_body)
            return None
    except urllib.error.HTTPError as exc:
        logger.warning(
            "Telegram HTTP error %d: %s",
            exc.code,
            exc.read().decode("utf-8", errors="replace")[:200],
        )
        return None
    except urllib.error.URLError as exc:
        logger.warning("Telegram connection error: %s", exc.reason)
        return None
    except (OSError, ValueError, TimeoutError) as exc:
        logger.warning("Telegram multipart request failed: %s", exc)
        return None


# ======================================================================
# Core messaging functions
# ======================================================================


def send_message(
    text: str,
    *,
    parse_mode: str = "HTML",
    chat_id: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
    reply_markup: Optional[dict] = None,
) -> bool:
    """Send a text message via the Telegram Bot API.

    Parameters
    ----------
    text:
        Message body (may contain HTML if *parse_mode* is ``"HTML"``).
    parse_mode:
        Telegram parse mode — ``"HTML"`` (default) or ``"Markdown"``.
    chat_id:
        Override the default chat ID from the environment.
    reply_to_message_id:
        If set, the message is sent as a reply to this message ID,
        enabling conversation threading (inspired by OpenClaw's
        thread-aware session routing).
    reply_markup:
        Optional inline keyboard or other reply markup dict.
        Use :func:`build_inline_keyboard` to construct this.

    Returns
    -------
    bool
        ``True`` if the message was sent successfully, ``False`` otherwise.
        Never raises — all errors are logged and swallowed.
    """
    token, default_chat_id = _get_credentials()
    target_chat = chat_id or default_chat_id
    if not token or not target_chat:
        logger.warning(
            "Telegram credentials missing — set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID environment variables"
        )
        return False

    url = f"{_API_BASE}/bot{token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": parse_mode,
    }

    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id

    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if body.get("ok"):
                logger.debug("Telegram message sent successfully")
                return True
            logger.warning("Telegram API returned ok=false: %s", body)
            return False
    except urllib.error.HTTPError as exc:
        logger.warning(
            "Telegram HTTP error %d: %s",
            exc.code,
            exc.read().decode("utf-8", errors="replace")[:200],
        )
        return False
    except urllib.error.URLError as exc:
        logger.warning("Telegram connection error: %s", exc.reason)
        return False
    except (OSError, ValueError, TimeoutError) as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


def edit_message(
    chat_id: str,
    message_id: int,
    new_text: str,
    *,
    parse_mode: str = "HTML",
    reply_markup: Optional[dict] = None,
) -> bool:
    """Edit an existing message in-place.

    Inspired by OpenClaw's live stream preview pattern, which edits a
    preview message in real-time using ``editMessageText`` to show
    partial/streaming replies.

    Parameters
    ----------
    chat_id:
        The chat containing the message.
    message_id:
        ID of the message to edit.
    new_text:
        Replacement text.
    parse_mode:
        Telegram parse mode.
    reply_markup:
        Optional updated inline keyboard.

    Returns
    -------
    bool
        ``True`` on success, ``False`` otherwise.
    """
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    result = _api_request("editMessageText", payload)
    return result is not None


def send_photo(
    chat_id: str,
    photo_bytes: bytes,
    *,
    caption: Optional[str] = None,
    parse_mode: str = "HTML",
    filename: str = "photo.png",
) -> bool:
    """Send a photo (image bytes) to a Telegram chat.

    Parameters
    ----------
    chat_id:
        Target chat ID.
    photo_bytes:
        Raw image data (PNG, JPEG, etc.).
    caption:
        Optional caption text.
    parse_mode:
        Parse mode for the caption.
    filename:
        Filename hint for the upload.

    Returns
    -------
    bool
        ``True`` on success.
    """
    fields: Dict[str, Any] = {
        "chat_id": chat_id,
        "photo": (filename, photo_bytes, "image/png"),
    }
    if caption:
        fields["caption"] = caption
        fields["parse_mode"] = parse_mode

    result = _multipart_request("sendPhoto", fields)
    return result is not None


def send_document(
    chat_id: str,
    file_bytes: bytes,
    filename: str,
    *,
    caption: Optional[str] = None,
    parse_mode: str = "HTML",
) -> bool:
    """Send a document (file bytes) to a Telegram chat.

    Parameters
    ----------
    chat_id:
        Target chat ID.
    file_bytes:
        Raw file data.
    filename:
        Name for the uploaded file.
    caption:
        Optional caption.
    parse_mode:
        Parse mode for the caption.

    Returns
    -------
    bool
        ``True`` on success.
    """
    content_type = "application/octet-stream"
    if filename.endswith(".pdf"):
        content_type = "application/pdf"
    elif filename.endswith(".json"):
        content_type = "application/json"
    elif filename.endswith(".csv"):
        content_type = "text/csv"
    elif filename.endswith(".txt"):
        content_type = "text/plain"

    fields: Dict[str, Any] = {
        "chat_id": chat_id,
        "document": (filename, file_bytes, content_type),
    }
    if caption:
        fields["caption"] = caption
        fields["parse_mode"] = parse_mode

    result = _multipart_request("sendDocument", fields)
    return result is not None


# ======================================================================
# Rich message formatting — inline keyboards
# ======================================================================


def build_inline_keyboard(
    rows: List[List[Dict[str, str]]],
) -> dict:
    """Build an inline keyboard markup dict for Telegram.

    Inspired by OpenClaw's inline keyboard support
    (``channels.telegram.capabilities.inlineButtons``).

    Parameters
    ----------
    rows:
        A list of rows, where each row is a list of button dicts.
        Each button dict should have ``"text"`` and one action key:
        ``"callback_data"``, ``"url"``, or ``"switch_inline_query"``.

    Returns
    -------
    dict
        A ``{"inline_keyboard": [...]}`` dict suitable for
        ``reply_markup`` in :func:`send_message`.

    Examples
    --------
    >>> build_inline_keyboard([
    ...     [{"text": "Investigate", "callback_data": "investigate:T-001"}],
    ...     [{"text": "Acknowledge", "callback_data": "ack:T-001"},
    ...      {"text": "Dismiss", "callback_data": "dismiss:T-001"}],
    ... ])
    {'inline_keyboard': [[{'text': 'Investigate', 'callback_data': 'investigate:T-001'}], ...]}
    """
    return {"inline_keyboard": rows}


def build_alert_keyboard(ticket_id: str) -> dict:
    """Build a standard alert action keyboard for a ticket.

    Provides Investigate / Acknowledge / Dismiss buttons.

    Parameters
    ----------
    ticket_id:
        The ticket identifier to embed in callback data.

    Returns
    -------
    dict
        Inline keyboard markup.
    """
    return build_inline_keyboard([
        [{"text": "Investigate", "callback_data": f"investigate:{ticket_id}"}],
        [
            {"text": "Acknowledge", "callback_data": f"ack:{ticket_id}"},
            {"text": "Dismiss", "callback_data": f"dismiss:{ticket_id}"},
        ],
    ])


# ======================================================================
# TTS — Text-to-Speech via OpenAI-compatible /v1/audio/speech
# ======================================================================


def text_to_speech(text: str, model: Optional[str] = None) -> Optional[bytes]:
    """Convert *text* to speech audio bytes (mp3) via an OpenAI-compatible endpoint.

    Parameters
    ----------
    text:
        The text to synthesise.
    model:
        TTS model name.  Falls back to ``TTS_MODEL`` env var, then ``kokoro``.

    Returns
    -------
    bytes or None
        Raw mp3 audio bytes on success, ``None`` on any failure.
    """
    api_url, api_key, default_model = _get_tts_config()
    if not api_url or not api_key:
        logger.warning("TTS API URL/key missing — set TTS_API_URL or BASE_LLM_API_URL")
        return None

    model = model or default_model
    # Ensure the base URL ends without a trailing slash before appending path
    base = api_url.rstrip("/")
    url = f"{base}/audio/speech"

    payload = {
        "model": model,
        "input": text,
        "voice": "alloy",
        "response_format": "mp3",
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio_bytes = resp.read()
            if len(audio_bytes) == 0:
                logger.warning("TTS returned empty audio response")
                return None
            logger.debug("TTS generated %d bytes of audio", len(audio_bytes))
            return audio_bytes
    except urllib.error.HTTPError as exc:
        logger.warning(
            "TTS HTTP error %d: %s",
            exc.code,
            exc.read().decode("utf-8", errors="replace")[:200],
        )
        return None
    except urllib.error.URLError as exc:
        logger.warning("TTS connection error: %s", exc.reason)
        return None
    except (OSError, ValueError, TimeoutError) as exc:
        logger.warning("TTS failed: %s", exc)
        return None


# ======================================================================
# STT — Speech-to-Text via OpenAI-compatible /v1/audio/transcriptions
# ======================================================================


def _build_multipart_body(
    audio_bytes: bytes,
    model: str,
    filename: str = "audio.ogg",
) -> tuple[bytes, str]:
    """Build a multipart/form-data body for the transcription endpoint.

    Returns (body_bytes, content_type_header).
    """
    boundary = f"----SWESquadBoundary{uuid.uuid4().hex[:16]}"
    parts: list[bytes] = []

    # model field
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="model"\r\n\r\n')
    parts.append(f"{model}\r\n".encode())

    # audio file field
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    )
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.append(audio_bytes)
    parts.append(b"\r\n")

    # closing boundary
    parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def speech_to_text(
    audio_bytes: bytes, model: Optional[str] = None
) -> Optional[str]:
    """Transcribe *audio_bytes* to text via an OpenAI-compatible endpoint.

    Parameters
    ----------
    audio_bytes:
        Raw audio data (ogg/opus, mp3, wav, etc.).
    model:
        STT model name.  Falls back to ``STT_MODEL`` env var, then ``whisper-1``.

    Returns
    -------
    str or None
        The transcribed text on success, ``None`` on any failure.
    """
    if not audio_bytes:
        logger.warning("speech_to_text called with empty audio")
        return None

    api_url, api_key, default_model = _get_stt_config()
    if not api_url or not api_key:
        logger.warning("STT API URL/key missing — set STT_API_URL or BASE_LLM_API_URL")
        return None

    model = model or default_model
    base = api_url.rstrip("/")
    url = f"{base}/audio/transcriptions"

    body, content_type = _build_multipart_body(audio_bytes, model)
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": content_type,
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = json.loads(resp.read().decode("utf-8"))
            text = resp_body.get("text", "").strip()
            if not text:
                logger.warning("STT returned empty transcription")
                return None
            logger.debug("STT transcribed %d chars", len(text))
            return text
    except urllib.error.HTTPError as exc:
        logger.warning(
            "STT HTTP error %d: %s",
            exc.code,
            exc.read().decode("utf-8", errors="replace")[:200],
        )
        return None
    except urllib.error.URLError as exc:
        logger.warning("STT connection error: %s", exc.reason)
        return None
    except (OSError, ValueError, TimeoutError) as exc:
        logger.warning("STT failed: %s", exc)
        return None


# ======================================================================
# Telegram Bot API — voice messaging (TTS + sendVoice)
# ======================================================================


def send_voice_message(
    chat_id: Optional[str] = None, text: str = ""
) -> bool:
    """Synthesise *text* to speech and send as a Telegram voice message.

    Steps:
      1. Convert *text* to mp3 via :func:`text_to_speech`.
      2. Upload via Telegram ``sendVoice`` (multipart/form-data).

    Falls back gracefully: returns ``False`` if TTS fails or Telegram
    credentials are missing. Never raises.

    Parameters
    ----------
    chat_id:
        Target Telegram chat.  Defaults to ``TELEGRAM_CHAT_ID`` env var.
    text:
        The text to speak.

    Returns
    -------
    bool
        ``True`` if the voice message was sent successfully.
    """
    if not text:
        logger.warning("send_voice_message called with empty text")
        return False

    token, default_chat_id = _get_credentials()
    chat_id = chat_id or default_chat_id
    if not token or not chat_id:
        logger.warning(
            "Telegram credentials missing — cannot send voice message"
        )
        return False

    # Step 1: TTS
    audio_bytes = text_to_speech(text)
    if audio_bytes is None:
        logger.warning("TTS failed — falling back; voice message not sent")
        return False

    # Step 2: Upload via sendVoice (multipart/form-data with OGG expected,
    # but Telegram also accepts mp3 for voice messages).
    url = f"{_API_BASE}/bot{token}/sendVoice"
    boundary = f"----SWESquadVoice{uuid.uuid4().hex[:16]}"
    parts: list[bytes] = []

    # chat_id field
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
    parts.append(f"{chat_id}\r\n".encode())

    # voice file field
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        b'Content-Disposition: form-data; name="voice"; filename="voice.mp3"\r\n'
    )
    parts.append(b"Content-Type: audio/mpeg\r\n\r\n")
    parts.append(audio_bytes)
    parts.append(b"\r\n")

    # closing boundary
    parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_json = json.loads(resp.read().decode("utf-8"))
            if resp_json.get("ok"):
                logger.debug("Telegram voice message sent successfully")
                return True
            logger.warning("Telegram sendVoice returned ok=false: %s", resp_json)
            return False
    except urllib.error.HTTPError as exc:
        logger.warning(
            "Telegram sendVoice HTTP error %d: %s",
            exc.code,
            exc.read().decode("utf-8", errors="replace")[:200],
        )
        return False
    except urllib.error.URLError as exc:
        logger.warning("Telegram sendVoice connection error: %s", exc.reason)
        return False
    except (OSError, ValueError, TimeoutError) as exc:
        logger.warning("Telegram sendVoice failed: %s", exc)
        return False


def transcribe_voice(
    file_id: str,
    *,
    stt_provider: str = "whisper",
) -> Optional[str]:
    """Download a Telegram voice message and transcribe it (STT).

    This is a stub for future integration.  When implemented, it will:
    1. Download the voice file using ``getFile`` + file_id.
    2. Transcribe the audio using the configured STT provider.
    3. Return the transcript text.

    Parameters
    ----------
    file_id:
        Telegram file ID of the voice message.
    stt_provider:
        STT backend to use.

    Returns
    -------
    str or None
        Always ``None`` — not yet implemented.
    """
    logger.info(
        "transcribe_voice: STT not implemented (provider=%s, file_id=%s)",
        stt_provider,
        file_id,
    )
    return None


# ======================================================================
# Interactive bot command registry
#
# Inspired by OpenClaw's command/directive system and Telegram custom
# command menu support.  Commands are registered as handler functions
# that accept arguments and return a formatted response string.
#
# The actual webhook/polling loop is NOT included — this is purely
# the command dispatch layer.  A future integration can call
# ``TelegramBot.handle_command()`` from a webhook endpoint or
# polling loop.
# ======================================================================


# Type alias for command handlers
CommandHandler = Callable[[str], str]


class TelegramBot:
    """Interactive Telegram bot with a slash-command registry.

    Each command is a function ``(args: str) -> str`` that processes
    the user's input and returns a formatted response message.

    Usage
    -----
    >>> bot = TelegramBot()
    >>> bot.register("/ping", lambda args: "pong!")
    >>> bot.handle_command("/ping")
    'pong!'

    The class ships with built-in handlers for common SWE-Squad
    commands: ``/status``, ``/tickets``, ``/investigate``,
    ``/summary``, and ``/help``.

    Parameters
    ----------
    ticket_store:
        Optional ``TicketStore`` instance for data-driven commands.
    status_provider:
        Optional callable ``() -> dict`` that returns current system
        status (e.g. contents of ``status.json``).
    """

    def __init__(
        self,
        *,
        ticket_store: Any = None,
        status_provider: Optional[Callable[[], dict]] = None,
    ) -> None:
        self._commands: Dict[str, CommandHandler] = {}
        self._ticket_store = ticket_store
        self._status_provider = status_provider

        # Register built-in commands
        self.register("/help", self._cmd_help)
        self.register("/status", self._cmd_status)
        self.register("/tickets", self._cmd_tickets)
        self.register("/investigate", self._cmd_investigate)
        self.register("/summary", self._cmd_summary)

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------

    def register(self, command: str, handler: CommandHandler) -> None:
        """Register a command handler.

        Parameters
        ----------
        command:
            The slash command (e.g. ``"/status"``).  Must start with ``/``.
        handler:
            A callable ``(args: str) -> str``.
        """
        if not command.startswith("/"):
            raise ValueError(f"Command must start with '/': {command!r}")
        self._commands[command] = handler

    def list_commands(self) -> List[str]:
        """Return sorted list of registered command names."""
        return sorted(self._commands.keys())

    def handle_command(self, text: str) -> Optional[str]:
        """Parse and dispatch a command message.

        Parameters
        ----------
        text:
            The raw message text (e.g. ``"/status"`` or
            ``"/investigate T-001"``).

        Returns
        -------
        str or None
            The response string if the command is recognised,
            or ``None`` for unknown commands.
        """
        text = text.strip()
        if not text.startswith("/"):
            return None

        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Strip @botname suffix (e.g. "/status@mybot")
        if "@" in command:
            command = command.split("@")[0]

        handler = self._commands.get(command)
        if handler is None:
            return None

        try:
            return handler(args)
        except Exception as exc:
            logger.warning("Command %s failed: %s", command, exc)
            return f"Command {command} failed: {exc}"

    # ------------------------------------------------------------------
    # Built-in command handlers
    # ------------------------------------------------------------------

    def _cmd_help(self, args: str) -> str:
        """List all available commands with descriptions."""
        lines = [
            "<b>SWE-Squad Bot Commands</b>",
            "",
            "/status - Current system status (tickets, gate, last cycle)",
            "/tickets - List open tickets with severity",
            "/investigate &lt;ticket_id&gt; - Trigger investigation of a ticket",
            "/summary - Daily summary on demand",
            "/help - Show this help message",
        ]
        # Add any custom-registered commands
        builtins = {"/help", "/status", "/tickets", "/investigate", "/summary"}
        custom = sorted(set(self._commands.keys()) - builtins)
        if custom:
            lines.append("")
            lines.append("<b>Custom commands:</b>")
            for cmd in custom:
                lines.append(f"{cmd}")

        return "\n".join(lines)

    def _cmd_status(self, args: str) -> str:
        """Return current system status."""
        if self._status_provider:
            try:
                data = self._status_provider()
                lines = ["<b>System Status</b>", ""]
                for key, value in sorted(data.items()):
                    lines.append(f"<b>{_esc(str(key))}:</b> {_esc(str(value))}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Failed to fetch status: {exc}"

        return "No status provider configured."

    def _cmd_tickets(self, args: str) -> str:
        """List open tickets with severity."""
        if not self._ticket_store:
            return "No ticket store configured."

        try:
            open_tickets = self._ticket_store.list_open()
        except Exception as exc:
            return f"Failed to list tickets: {exc}"

        if not open_tickets:
            return "No open tickets."

        severity_emoji = {
            "critical": "\U0001f534",
            "high": "\U0001f7e0",
            "medium": "\U0001f7e1",
            "low": "\u26aa",
        }

        lines = [f"<b>Open Tickets ({len(open_tickets)})</b>", ""]
        for t in open_tickets[:20]:  # Limit to 20 to stay within message limits
            sev = t.severity.value.lower()
            emoji = severity_emoji.get(sev, "")
            assignee = t.assigned_to or "unassigned"
            lines.append(
                f"{emoji} <code>{_esc(t.ticket_id[:12])}</code> "
                f"[{sev.upper()}] {_esc(t.title[:60])}\n"
                f"    Assigned: {_esc(assignee)} | Status: {t.status.value}"
            )

        if len(open_tickets) > 20:
            lines.append(f"\n... and {len(open_tickets) - 20} more")

        return "\n".join(lines)

    def _cmd_investigate(self, args: str) -> str:
        """Trigger investigation of a specific ticket."""
        ticket_id = args.strip()
        if not ticket_id:
            return "Usage: /investigate &lt;ticket_id&gt;"

        if not self._ticket_store:
            return "No ticket store configured."

        try:
            ticket = self._ticket_store.get(ticket_id)
        except Exception:
            ticket = None

        if ticket is None:
            return f"Ticket not found: <code>{_esc(ticket_id)}</code>"

        sev = ticket.severity.value.upper()
        return (
            f"Investigation queued for "
            f"<code>{_esc(ticket_id)}</code>\n"
            f"[{sev}] {_esc(ticket.title[:80])}"
        )

    def _cmd_summary(self, args: str) -> str:
        """On-demand daily summary."""
        if not self._ticket_store:
            return "No ticket store configured."

        try:
            open_tickets = self._ticket_store.list_open()
        except Exception as exc:
            return f"Failed to generate summary: {exc}"

        if not open_tickets:
            return "<b>Daily Summary</b>\n\nNo open tickets."

        from collections import Counter

        sev_counts = Counter(t.severity.value for t in open_tickets)
        status_counts = Counter(t.status.value for t in open_tickets)

        severity_emoji = {
            "critical": "\U0001f534",
            "high": "\U0001f7e0",
            "medium": "\U0001f7e1",
            "low": "\u26aa",
        }

        lines = [
            f"<b>Daily Summary — {len(open_tickets)} open ticket(s)</b>",
            "",
            "<b>By severity:</b>",
        ]
        for sev in ("critical", "high", "medium", "low"):
            count = sev_counts.get(sev, 0)
            if count:
                emoji = severity_emoji.get(sev, "")
                lines.append(f"  {emoji} {sev.upper()}: {count}")

        lines.append("")
        lines.append("<b>By status:</b>")
        for status, count in sorted(status_counts.items()):
            lines.append(f"  {status}: {count}")

        return "\n".join(lines)


# ======================================================================
# HTML escaping utility
# ======================================================================


def _esc(text: str) -> str:
    """Escape HTML for Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
