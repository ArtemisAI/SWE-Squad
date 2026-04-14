"""Tests for notification, auth, and env providers."""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Notification provider
# ---------------------------------------------------------------------------
from src.swe_team.providers.notification.base import NotificationProvider
from src.swe_team.providers.notification.telegram_provider import TelegramNotificationProvider

# ---------------------------------------------------------------------------
# Auth provider
# ---------------------------------------------------------------------------
from src.swe_team.providers.auth.base import AuthProvider, AuthState
from src.swe_team.providers.auth.inmemory_provider import InMemoryAuthProvider

# ---------------------------------------------------------------------------
# Env provider
# ---------------------------------------------------------------------------
from src.swe_team.providers.env.base import (
    BLOCKED_ENV_VARS,
    DEFAULT_ALLOWLISTS,
    EnvProvider,
    EnvSpec,
)
from src.swe_team.providers.env.dotenv_provider import DotenvEnvProvider


# ===================================================================
# NOTIFICATION PROVIDER TESTS
# ===================================================================

class TestNotificationProtocol:
    """NotificationProvider protocol interface tests."""

    def test_telegram_satisfies_protocol(self) -> None:
        provider = TelegramNotificationProvider(token="tok", chat_id="123")
        assert isinstance(provider, NotificationProvider)

    def test_protocol_is_runtime_checkable(self) -> None:
        """A plain object without the right methods must NOT satisfy the protocol."""
        assert not isinstance(object(), NotificationProvider)

    def test_custom_impl_satisfies_protocol(self) -> None:
        """An ad-hoc class that defines all required methods satisfies the protocol."""

        class StubNotifier:
            @property
            def name(self) -> str:
                return "stub"

            def send_alert(self, message: str, *, level: str = "info") -> bool:
                return True

            def send_daily_summary(self, summary: str) -> bool:
                return True

            def send_hitl_escalation(self, ticket_id: str, message: str) -> bool:
                return True

            def health_check(self) -> bool:
                return True

        assert isinstance(StubNotifier(), NotificationProvider)


class TestTelegramNotificationProvider:
    """Concrete Telegram provider tests (mocked network)."""

    def test_name_property(self) -> None:
        p = TelegramNotificationProvider(token="t", chat_id="c")
        assert p.name == "telegram"

    def test_send_alert_success(self) -> None:
        """Patching the lazy-imported _send ensures no real network call."""
        p = TelegramNotificationProvider(token="t", chat_id="c")
        with patch("src.swe_team.notifier._send", return_value=True):
            result = p.send_alert("test message", level="critical")
        assert result is True

    def test_send_alert_handles_import_error(self) -> None:
        """If the notifier module raises, send_alert returns False."""
        p = TelegramNotificationProvider(token="t", chat_id="c")
        with patch("src.swe_team.notifier._send", side_effect=RuntimeError("boom")):
            result = p.send_alert("hello")
        assert result is False

    def test_send_daily_summary_delegates_to_send_alert(self) -> None:
        p = TelegramNotificationProvider(token="t", chat_id="c")
        with patch.object(p, "send_alert", return_value=True) as mock_alert:
            result = p.send_daily_summary("daily report")
        assert result is True
        mock_alert.assert_called_once_with("daily report", level="info")

    def test_send_hitl_escalation_delegates_to_send_alert(self) -> None:
        p = TelegramNotificationProvider(token="t", chat_id="c")
        with patch.object(p, "send_alert", return_value=True) as mock_alert:
            result = p.send_hitl_escalation("TKT-1", "need human")
        assert result is True
        mock_alert.assert_called_once_with("need human", level="critical")

    def test_health_check_configured(self) -> None:
        assert TelegramNotificationProvider(token="t", chat_id="c").health_check() is True

    def test_health_check_missing_token(self) -> None:
        assert TelegramNotificationProvider(token="", chat_id="c").health_check() is False

    def test_health_check_missing_chat_id(self) -> None:
        assert TelegramNotificationProvider(token="t", chat_id="").health_check() is False

    def test_health_check_both_missing(self) -> None:
        assert TelegramNotificationProvider().health_check() is False


# ===================================================================
# AUTH PROVIDER TESTS
# ===================================================================

class TestAuthState:
    """Unit tests for the AuthState dataclass."""

    def test_defaults(self) -> None:
        s = AuthState(provider_name="test")
        assert s.is_authenticated is False
        assert s.auth_method == "api_key"
        assert s.consecutive_auth_failures == 0
        assert s.is_expired() is False
        assert s.is_revoked() is False

    def test_is_expired_when_past(self) -> None:
        s = AuthState(provider_name="x", key_expires_at=time.monotonic() - 10)
        assert s.is_expired() is True

    def test_is_expired_when_future(self) -> None:
        s = AuthState(provider_name="x", key_expires_at=time.monotonic() + 3600)
        assert s.is_expired() is False

    def test_is_revoked(self) -> None:
        s = AuthState(provider_name="x", revoked_at=time.monotonic())
        assert s.is_revoked() is True

    def test_is_healthy_true(self) -> None:
        s = AuthState(provider_name="x", is_authenticated=True)
        assert s.is_healthy() is True

    def test_is_healthy_false_unauthenticated(self) -> None:
        s = AuthState(provider_name="x", is_authenticated=False)
        assert s.is_healthy() is False

    def test_is_healthy_false_expired(self) -> None:
        s = AuthState(
            provider_name="x",
            is_authenticated=True,
            key_expires_at=time.monotonic() - 1,
        )
        assert s.is_healthy() is False

    def test_is_healthy_false_too_many_failures(self) -> None:
        s = AuthState(
            provider_name="x",
            is_authenticated=True,
            consecutive_auth_failures=3,
        )
        assert s.is_healthy() is False

    def test_is_healthy_false_revoked(self) -> None:
        s = AuthState(
            provider_name="x",
            is_authenticated=True,
            revoked_at=time.monotonic(),
        )
        assert s.is_healthy() is False


class TestAuthProtocol:
    """AuthProvider protocol interface tests."""

    def test_inmemory_satisfies_protocol(self) -> None:
        provider = InMemoryAuthProvider()
        assert isinstance(provider, AuthProvider)

    def test_protocol_is_runtime_checkable(self) -> None:
        assert not isinstance(object(), AuthProvider)

    def test_custom_impl_satisfies_protocol(self) -> None:

        class StubAuth:
            def get_state(self, provider_name: str) -> AuthState:
                return AuthState(provider_name=provider_name)

            def record_auth_success(self, provider_name: str) -> None:
                pass

            def record_auth_failure(self, provider_name: str, error: str) -> None:
                pass

            def rotate_key(self, provider_name: str, new_key: str) -> None:
                pass

            def list_states(self) -> list[AuthState]:
                return []

            def health_check(self) -> bool:
                return True

            def revoke(self, provider_name: str, reason: str) -> None:
                pass

            def unrevoke(self, provider_name: str) -> None:
                pass

            def is_revoked(self, provider_name: str) -> bool:
                return False

        assert isinstance(StubAuth(), AuthProvider)


class TestInMemoryAuthProvider:
    """Concrete InMemoryAuthProvider tests."""

    def test_get_state_auto_creates(self) -> None:
        p = InMemoryAuthProvider()
        state = p.get_state("claude")
        assert state.provider_name == "claude"
        assert state.is_authenticated is False

    def test_record_auth_success(self) -> None:
        p = InMemoryAuthProvider(provider_names=["claude"])
        p.record_auth_success("claude")
        state = p.get_state("claude")
        assert state.is_authenticated is True
        assert state.consecutive_auth_failures == 0
        assert state.last_auth_error is None

    def test_record_auth_failure_increments(self) -> None:
        p = InMemoryAuthProvider()
        p.record_auth_success("x")
        p.record_auth_failure("x", "401")
        state = p.get_state("x")
        assert state.consecutive_auth_failures == 1
        assert state.last_auth_error == "401"
        # Still authenticated after 1 failure (threshold is 3)
        assert state.is_authenticated is True

    def test_record_auth_failure_threshold_deauths(self) -> None:
        p = InMemoryAuthProvider()
        p.record_auth_success("x")
        for i in range(3):
            p.record_auth_failure("x", f"err-{i}")
        state = p.get_state("x")
        assert state.is_authenticated is False
        assert state.consecutive_auth_failures == 3

    def test_rotate_key_resets_state(self) -> None:
        p = InMemoryAuthProvider()
        p.record_auth_failure("x", "bad")
        p.record_auth_failure("x", "bad")
        p.rotate_key("x", "new-secret")
        state = p.get_state("x")
        assert state.is_authenticated is True
        assert state.consecutive_auth_failures == 0
        assert state.key_last_rotated is not None

    def test_revoke_marks_provider(self) -> None:
        p = InMemoryAuthProvider(provider_names=["claude"])
        p.record_auth_success("claude")
        p.revoke("claude", "compromised key")
        state = p.get_state("claude")
        assert state.is_authenticated is False
        assert state.is_revoked() is True
        assert state.revocation_reason == "compromised key"
        assert state.revoked_at is not None
        assert p.is_revoked("claude") is True

    def test_revoked_provider_ignores_auth_success(self) -> None:
        p = InMemoryAuthProvider()
        p.revoke("x", "bad actor")
        p.record_auth_success("x")
        state = p.get_state("x")
        # Still revoked
        assert state.is_authenticated is False
        assert state.is_revoked() is True

    def test_unrevoke_clears_revocation(self) -> None:
        p = InMemoryAuthProvider()
        p.revoke("x", "temporary ban")
        p.unrevoke("x")
        state = p.get_state("x")
        assert state.is_revoked() is False
        assert state.revocation_reason is None
        assert state.consecutive_auth_failures == 0
        # Not authenticated yet -- must go through normal auth
        assert state.is_authenticated is False

    def test_unrevoke_unknown_provider_noop(self) -> None:
        p = InMemoryAuthProvider()
        p.unrevoke("nonexistent")  # should not raise

    def test_is_revoked_unknown_provider(self) -> None:
        p = InMemoryAuthProvider()
        assert p.is_revoked("nonexistent") is False

    def test_list_states_sorted(self) -> None:
        p = InMemoryAuthProvider(provider_names=["z_provider", "a_provider", "m_provider"])
        states = p.list_states()
        names = [s.provider_name for s in states]
        assert names == ["a_provider", "m_provider", "z_provider"]

    def test_list_states_empty(self) -> None:
        p = InMemoryAuthProvider()
        assert p.list_states() == []

    def test_health_check_all_healthy(self) -> None:
        p = InMemoryAuthProvider(provider_names=["a", "b"])
        p.record_auth_success("a")
        p.record_auth_success("b")
        assert p.health_check() is True

    def test_health_check_empty_is_healthy(self) -> None:
        p = InMemoryAuthProvider()
        assert p.health_check() is True

    def test_health_check_unhealthy_when_failure(self) -> None:
        p = InMemoryAuthProvider(provider_names=["a"])
        # a is not authenticated -> not healthy
        assert p.health_check() is False

    def test_health_check_unhealthy_when_revoked(self) -> None:
        p = InMemoryAuthProvider(provider_names=["a"])
        p.record_auth_success("a")
        p.revoke("a", "test")
        assert p.health_check() is False


# ===================================================================
# ENV PROVIDER TESTS
# ===================================================================

class TestEnvProtocol:
    """EnvProvider protocol interface tests."""

    def test_dotenv_satisfies_protocol(self) -> None:
        provider = DotenvEnvProvider()
        assert isinstance(provider, EnvProvider)

    def test_protocol_is_runtime_checkable(self) -> None:
        assert not isinstance(object(), EnvProvider)

    def test_custom_impl_satisfies_protocol(self) -> None:

        class StubEnv:
            def build_env(self, spec: EnvSpec) -> dict[str, str]:
                return {}

            def allowed_keys(self, role: str) -> list[str]:
                return []

            def is_blocked(self, key: str) -> bool:
                return False

            def health_check(self) -> bool:
                return True

        assert isinstance(StubEnv(), EnvProvider)


class TestDotenvEnvProvider:
    """Concrete DotenvEnvProvider tests."""

    def test_get_env_var_from_build_env(self) -> None:
        """build_env includes allowed env vars that are present in os.environ."""
        p = DotenvEnvProvider()
        spec = EnvSpec(role="investigator")
        env = p.build_env(spec)
        # PATH should always be present
        assert "PATH" in env

    def test_build_env_overrides(self) -> None:
        p = DotenvEnvProvider()
        spec = EnvSpec(role="investigator", overrides={"MY_CUSTOM": "value123"})
        env = p.build_env(spec)
        assert env["MY_CUSTOM"] == "value123"

    def test_build_env_strips_blocked_vars(self) -> None:
        """Blocked vars injected via overrides are stripped unless the role allowlists them."""
        p = DotenvEnvProvider()
        spec = EnvSpec(
            role="investigator",
            overrides={"ANTHROPIC_API_KEY": "sk-secret"},
        )
        env = p.build_env(spec)
        # investigator does NOT allowlist ANTHROPIC_API_KEY
        assert "ANTHROPIC_API_KEY" not in env

    def test_build_env_allows_blocked_var_for_claude_cli_role(self) -> None:
        """The claude_cli role explicitly allowlists ANTHROPIC_API_KEY."""
        p = DotenvEnvProvider()
        spec = EnvSpec(
            role="claude_cli",
            overrides={"ANTHROPIC_API_KEY": "sk-allowed"},
        )
        env = p.build_env(spec)
        assert env.get("ANTHROPIC_API_KEY") == "sk-allowed"

    def test_build_env_unknown_role_gets_minimal(self) -> None:
        """Unknown roles get PATH + HOME only."""
        p = DotenvEnvProvider()
        spec = EnvSpec(role="unknown_role")
        env = p.build_env(spec)
        assert "PATH" in env
        assert "HOME" in env

    def test_build_env_strip_blocked_false(self) -> None:
        """When strip_blocked=False, blocked vars from overrides are kept."""
        p = DotenvEnvProvider()
        spec = EnvSpec(
            role="investigator",
            overrides={"TELEGRAM_BOT_TOKEN": "secret-token"},
            strip_blocked=False,
        )
        env = p.build_env(spec)
        assert env.get("TELEGRAM_BOT_TOKEN") == "secret-token"

    def test_allowed_keys_known_role(self) -> None:
        p = DotenvEnvProvider()
        keys = p.allowed_keys("investigator")
        assert "PATH" in keys
        assert "SWE_TEAM_ID" in keys

    def test_allowed_keys_unknown_role(self) -> None:
        p = DotenvEnvProvider()
        keys = p.allowed_keys("nonexistent")
        assert keys == ["PATH", "HOME"]

    def test_is_blocked(self) -> None:
        p = DotenvEnvProvider()
        assert p.is_blocked("SUPABASE_ANON_KEY") is True
        assert p.is_blocked("BASE_LLM_API_KEY") is True
        assert p.is_blocked("PATH") is False
        assert p.is_blocked("HOME") is False

    def test_health_check(self) -> None:
        p = DotenvEnvProvider()
        assert p.health_check() is True

    def test_config_allowlists_extend_defaults(self) -> None:
        """Custom config allowlists extend (don't replace) defaults."""
        p = DotenvEnvProvider(config_allowlists={"investigator": ["EXTRA_VAR"]})
        keys = p.allowed_keys("investigator")
        # Has both the default keys and the new one
        assert "PATH" in keys
        assert "EXTRA_VAR" in keys

    def test_config_allowlists_new_role(self) -> None:
        """Config can define allowlists for entirely new roles."""
        p = DotenvEnvProvider(config_allowlists={"deployer": ["PATH", "HOME", "DEPLOY_KEY"]})
        keys = p.allowed_keys("deployer")
        assert "DEPLOY_KEY" in keys

    def test_missing_var_not_in_env(self) -> None:
        """Allowed keys not present in os.environ are simply absent from the result."""
        p = DotenvEnvProvider()
        # Use a key that is in the investigator allowlist but almost certainly not in os.environ
        spec = EnvSpec(role="investigator")
        env = p.build_env(spec)
        # SWE_REPO_PATH is allowlisted for investigator but unlikely to be set in test env
        if "SWE_REPO_PATH" not in os.environ:
            assert "SWE_REPO_PATH" not in env

    def test_build_env_always_has_path_and_home(self) -> None:
        """Even if PATH/HOME are missing from os.environ, build_env provides fallbacks."""
        p = DotenvEnvProvider()
        spec = EnvSpec(role="unknown_role")
        with patch.dict(os.environ, {}, clear=True):
            env = p.build_env(spec)
        assert "PATH" in env
        assert "HOME" in env


class TestEnvSpec:
    """Tests for the EnvSpec dataclass."""

    def test_defaults(self) -> None:
        spec = EnvSpec(role="investigator")
        assert spec.role == "investigator"
        assert spec.overrides == {}
        assert spec.strip_blocked is True

    def test_custom_values(self) -> None:
        spec = EnvSpec(role="dev", overrides={"A": "B"}, strip_blocked=False)
        assert spec.role == "dev"
        assert spec.overrides == {"A": "B"}
        assert spec.strip_blocked is False


class TestBlockedEnvVars:
    """Tests for the BLOCKED_ENV_VARS constant."""

    def test_known_blocked_vars_present(self) -> None:
        expected = {
            "SUPABASE_ANON_KEY",
            "BASE_LLM_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            "WEBHOOK_SECRET",
            "ANTHROPIC_API_KEY",
            "PROXMOXAI_API_KEY",
        }
        assert expected.issubset(BLOCKED_ENV_VARS)

    def test_blocked_vars_is_frozenset(self) -> None:
        assert isinstance(BLOCKED_ENV_VARS, frozenset)
