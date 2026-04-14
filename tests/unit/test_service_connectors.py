from __future__ import annotations

import io
import json
from unittest.mock import patch

from src.swe_team.providers.deployment.vercel_provider import VercelDeploymentProvider
from src.swe_team.providers.notification.slack_provider import SlackNotificationProvider
from src.swe_team.providers.supabase_direct.provider import SupabaseRESTDirectProvider


class _FakeResponse:
    def __init__(self, payload: dict | list) -> None:
        self._payload = payload
        self.headers: dict[str, str] = {}

    def read(self, _size: int = -1) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class TestSlackNotificationProvider:
    def test_health_check(self):
        assert SlackNotificationProvider(token="t", channel="c").health_check() is True
        assert SlackNotificationProvider(token="", channel="c").health_check() is False

    def test_send_alert_success(self):
        provider = SlackNotificationProvider(token="t", channel="C123")
        with patch("urllib.request.urlopen", return_value=_FakeResponse({"ok": True})):
            assert provider.send_alert("hello", level="warning") is True

    def test_send_alert_missing_config(self):
        provider = SlackNotificationProvider(token="", channel="")
        assert provider.send_alert("hello") is False


class TestVercelDeploymentProvider:
    def test_create_deployment_success(self):
        provider = VercelDeploymentProvider(token="tok", team_id="team")
        with patch("urllib.request.urlopen", return_value=_FakeResponse({"id": "dep_123"})):
            result = provider.create_deployment(project="proj", branch="main")
        assert result == "dep_123"

    def test_get_deployment_success(self):
        provider = VercelDeploymentProvider(token="tok")
        with patch("urllib.request.urlopen", return_value=_FakeResponse({"id": "dep_123", "readyState": "READY"})):
            result = provider.get_deployment("dep_123")
        assert result is not None
        assert result["id"] == "dep_123"


class TestSupabaseRESTDirectProvider:
    def test_select_success(self):
        provider = SupabaseRESTDirectProvider(url="https://db.example", key="key")
        with patch("urllib.request.urlopen", return_value=_FakeResponse([{"id": 1}])):
            rows = provider.select("tickets", filters={"status": "open"}, limit=1)
        assert rows == [{"id": 1}]

    def test_insert_success(self):
        provider = SupabaseRESTDirectProvider(url="https://db.example", key="key")
        with patch("urllib.request.urlopen", return_value=_FakeResponse([{"id": 2}])):
            rows = provider.insert("tickets", [{"name": "test"}])
        assert rows == [{"id": 2}]

    def test_rpc_success(self):
        provider = SupabaseRESTDirectProvider(url="https://db.example", key="key")
        with patch("urllib.request.urlopen", return_value=_FakeResponse({"ok": True})):
            result = provider.rpc("do_work", {"x": 1})
        assert result == {"ok": True}
