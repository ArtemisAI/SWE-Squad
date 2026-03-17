"""
Standalone Telegram Bot API client using only stdlib.

Reads ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` from the environment.
Uses ``urllib`` for HTTP — zero external dependencies, consistent with the
rest of the SWE-Squad project.

All functions are best-effort: they return True/False and never raise.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"


def _get_credentials() -> tuple[Optional[str], Optional[str]]:
    """Return (bot_token, chat_id) from environment, or (None, None)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    return token, chat_id


def send_message(text: str, *, parse_mode: str = "HTML") -> bool:
    """Send a message via the Telegram Bot API.

    Parameters
    ----------
    text:
        Message body (may contain HTML if *parse_mode* is ``"HTML"``).
    parse_mode:
        Telegram parse mode — ``"HTML"`` (default) or ``"Markdown"``.

    Returns
    -------
    bool
        ``True`` if the message was sent successfully, ``False`` otherwise.
        Never raises — all errors are logged and swallowed.
    """
    token, chat_id = _get_credentials()
    if not token or not chat_id:
        logger.warning(
            "Telegram credentials missing — set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID environment variables"
        )
        return False

    url = f"{_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }

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
