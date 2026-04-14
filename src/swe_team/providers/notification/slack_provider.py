"""
SlackNotificationProvider — NotificationProvider implementation for Slack.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from src.swe_team.providers.notification.base import NotificationProvider

logger = logging.getLogger(__name__)


class SlackNotificationProvider:
    """NotificationProvider backed by Slack chat.postMessage API."""

    def __init__(self, *, token: str = "", channel: str = "") -> None:
        self._token = token
        self._channel = channel

    @property
    def name(self) -> str:
        return "slack"

    def send_alert(self, message: str, *, level: str = "info") -> bool:
        if not self.health_check():
            return False

        text = self._format_message(level=level, message=message)
        payload = {"channel": self._channel, "text": text}
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
                data = json.loads(resp.read().decode("utf-8") or "{}")
            return bool(data.get("ok"))
        except (urllib.error.URLError, TimeoutError, ValueError):
            logger.warning("SlackNotificationProvider.send_alert failed", exc_info=True)
            return False

    def send_daily_summary(self, summary: str) -> bool:
        return self.send_alert(summary, level="info")

    def send_hitl_escalation(self, ticket_id: str, message: str) -> bool:
        return self.send_alert(f"[HITL:{ticket_id}] {message}", level="critical")

    def health_check(self) -> bool:
        return bool(self._token and self._channel)

    @staticmethod
    def _format_message(*, level: str, message: str) -> str:
        prefix = {
            "info": ":information_source:",
            "warning": ":warning:",
            "critical": ":rotating_light:",
        }.get(level, ":speech_balloon:")
        return f"{prefix} {message}"


assert isinstance(SlackNotificationProvider(token="t", channel="c"), NotificationProvider)
