"""
Tests for BASE_LLM proxy resilience: graceful 403/401 handling, circuit-breaker
disable flag, TTL reset, and no-retry on auth errors.

Covers:
- 403 response disables extraction (_BASE_LLM_DISABLED = True) and returns None
- Disabled flag causes subsequent calls to skip HTTP and return None/fallback
- After TTL expires the flag resets and calls resume
- 401 response is not retried — immediate disable
- _is_auth_error() recognises all auth error markers
- get_base_llm_status() returns correct status strings
- preflight warns when BASE_LLM_API_URL set but key missing
- extract_memory_facts falls back to _ticket_text when circuit-breaker is open
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers to reset module-level circuit-breaker state between tests
# ---------------------------------------------------------------------------

def _reset_circuit_breaker():
    """Reset the module-level circuit-breaker state to allow fresh tests."""
    import src.swe_team.embeddings as emb
    emb._BASE_LLM_DISABLED = False
    emb._BASE_LLM_DISABLED_UNTIL = 0.0


@pytest.fixture(autouse=True)
def _auto_reset_circuit_breaker():
    """Automatically reset circuit breaker before AND after every test in this module."""
    _reset_circuit_breaker()
    yield
    _reset_circuit_breaker()


def _make_ticket():
    """Return a minimal SWETicket for testing."""
    from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
    return SWETicket(
        ticket_id="TEST-001",
        title="Test ticket",
        description="A test ticket for BASE_LLM resilience tests",
        severity=TicketSeverity.HIGH,
        status=TicketStatus.OPEN,
        source_module="test_module",
        error_log="Something failed",
        investigation_report="Root cause is X. Fix is Y.",
    )


# ---------------------------------------------------------------------------
# Test: _is_auth_error detection
# ---------------------------------------------------------------------------

class TestIsAuthError:
    def setup_method(self):
        _reset_circuit_breaker()

    def test_detects_403_in_message(self):
        import src.swe_team.embeddings as emb
        exc = Exception("HTTP 403 Forbidden")
        assert emb._is_auth_error(exc) is True

    def test_detects_401_in_message(self):
        import src.swe_team.embeddings as emb
        exc = Exception("status code 401 Unauthorized")
        assert emb._is_auth_error(exc) is True

    def test_detects_forbidden_keyword(self):
        import src.swe_team.embeddings as emb
        exc = Exception("forbidden request")
        assert emb._is_auth_error(exc) is True

    def test_detects_unauthorized_keyword(self):
        import src.swe_team.embeddings as emb
        exc = Exception("Unauthorized access")
        assert emb._is_auth_error(exc) is True

    def test_detects_invalid_api_key(self):
        import src.swe_team.embeddings as emb
        exc = Exception("invalid_api_key supplied")
        assert emb._is_auth_error(exc) is True

    def test_does_not_flag_timeout(self):
        import src.swe_team.embeddings as emb
        exc = Exception("Connection timeout after 10s")
        assert emb._is_auth_error(exc) is False

    def test_does_not_flag_500(self):
        import src.swe_team.embeddings as emb
        exc = Exception("Internal Server Error 500")
        assert emb._is_auth_error(exc) is False


# ---------------------------------------------------------------------------
# Test: 403 disables extraction
# ---------------------------------------------------------------------------

class TestFourOhThreeDisablesExtraction:
    def setup_method(self):
        _reset_circuit_breaker()

    def test_403_disables_extraction(self):
        """HTTP 403 from proxy sets _BASE_LLM_DISABLED=True and returns None."""
        import src.swe_team.embeddings as emb
        ticket = _make_ticket()

        auth_exc = Exception("403 Forbidden — token expired")

        # Patch OpenAI client to raise a 403-style error
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = auth_exc

        with patch.dict(os.environ, {
            "BASE_LLM_API_URL": "http://proxy.example.com/v1",
            "BASE_LLM_API_KEY": "old-token",
        }):
            with patch("openai.OpenAI", return_value=mock_client):
                result = emb.extract_memory_facts(ticket)

        # Must fall back to plain ticket text (non-None), not raise
        assert result is not None
        assert isinstance(result, str)
        # Circuit-breaker must now be open
        assert emb._BASE_LLM_DISABLED is True

    def test_403_embed_ticket_returns_none(self):
        """embed_ticket returns None (not raises) on 403."""
        import src.swe_team.embeddings as emb
        ticket = _make_ticket()
        ticket.investigation_report = None  # Skip extraction path

        auth_exc = Exception("403 token invalid")
        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = auth_exc

        with patch.dict(os.environ, {
            "BASE_LLM_API_URL": "http://proxy.example.com/v1",
            "BASE_LLM_API_KEY": "bad-token",
            "EMBEDDING_API_URL": "",
            "EMBEDDING_API_KEY": "",
        }):
            with patch("openai.OpenAI", return_value=mock_client):
                result = emb.embed_ticket(ticket)

        assert result is None
        assert emb._BASE_LLM_DISABLED is True


# ---------------------------------------------------------------------------
# Test: disabled flag skips calls
# ---------------------------------------------------------------------------

class TestDisabledFlagSkipsCalls:
    def setup_method(self):
        _reset_circuit_breaker()

    def test_disabled_flag_skips_extract_calls(self):
        """When _BASE_LLM_DISABLED, extract_memory_facts makes no HTTP call."""
        import src.swe_team.embeddings as emb
        emb._BASE_LLM_DISABLED = True
        emb._BASE_LLM_DISABLED_UNTIL = time.monotonic() + 900

        ticket = _make_ticket()
        mock_openai = MagicMock()

        with patch.dict(os.environ, {
            "BASE_LLM_API_URL": "http://proxy.example.com/v1",
            "BASE_LLM_API_KEY": "some-key",
        }):
            with patch("openai.OpenAI", mock_openai):
                result = emb.extract_memory_facts(ticket)

        # OpenAI constructor should never be called
        mock_openai.assert_not_called()
        # Must fall back to plain text
        assert isinstance(result, str)
        assert "Test ticket" in result

    def test_disabled_flag_skips_embed_calls(self):
        """When _BASE_LLM_DISABLED, embed_ticket makes no HTTP call and returns None."""
        import src.swe_team.embeddings as emb
        emb._BASE_LLM_DISABLED = True
        emb._BASE_LLM_DISABLED_UNTIL = time.monotonic() + 900

        ticket = _make_ticket()
        mock_openai = MagicMock()

        with patch.dict(os.environ, {
            "BASE_LLM_API_URL": "http://proxy.example.com/v1",
            "BASE_LLM_API_KEY": "some-key",
        }):
            with patch("openai.OpenAI", mock_openai):
                result = emb.embed_ticket(ticket)

        mock_openai.assert_not_called()
        assert result is None


# ---------------------------------------------------------------------------
# Test: TTL reset
# ---------------------------------------------------------------------------

class TestFlagResetsAfterTTL:
    def setup_method(self):
        _reset_circuit_breaker()

    def test_flag_resets_after_ttl(self):
        """After _BASE_LLM_DISABLED_UNTIL passes, the flag resets and calls resume."""
        import src.swe_team.embeddings as emb

        # Simulate flag being set but already expired
        emb._BASE_LLM_DISABLED = True
        emb._BASE_LLM_DISABLED_UNTIL = time.monotonic() - 1  # already past

        # Checking _is_base_llm_disabled should reset the flag
        result = emb._is_base_llm_disabled()
        assert result is False
        assert emb._BASE_LLM_DISABLED is False
        assert emb._BASE_LLM_DISABLED_UNTIL == 0.0

    def test_flag_still_set_before_ttl(self):
        """While within TTL window, _is_base_llm_disabled returns True."""
        import src.swe_team.embeddings as emb

        emb._BASE_LLM_DISABLED = True
        emb._BASE_LLM_DISABLED_UNTIL = time.monotonic() + 900  # far future

        assert emb._is_base_llm_disabled() is True
        # Flag unchanged
        assert emb._BASE_LLM_DISABLED is True

    def test_extract_resumes_after_ttl(self):
        """extract_memory_facts calls the proxy again after TTL expiry."""
        import src.swe_team.embeddings as emb

        # Set flag as already expired
        emb._BASE_LLM_DISABLED = True
        emb._BASE_LLM_DISABLED_UNTIL = time.monotonic() - 1

        ticket = _make_ticket()
        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = "Root cause: X\nFix applied: Y"

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = fake_response

        with patch.dict(os.environ, {
            "BASE_LLM_API_URL": "http://proxy.example.com/v1",
            "BASE_LLM_API_KEY": "fresh-token",
        }):
            with patch("openai.OpenAI", return_value=mock_client):
                result = emb.extract_memory_facts(ticket)

        # Flag reset — call should have gone through
        assert emb._BASE_LLM_DISABLED is False
        mock_client.chat.completions.create.assert_called_once()
        assert "Root cause" in result


# ---------------------------------------------------------------------------
# Test: 401 is not retried
# ---------------------------------------------------------------------------

class TestFourOhOneNotRetried:
    def setup_method(self):
        _reset_circuit_breaker()

    def test_401_not_retried(self):
        """401 response immediately disables BASE_LLM without retrying."""
        import src.swe_team.embeddings as emb
        ticket = _make_ticket()

        call_count = 0

        def _raise_401(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise Exception("401 Unauthorized — token revoked")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = _raise_401

        with patch.dict(os.environ, {
            "BASE_LLM_API_URL": "http://proxy.example.com/v1",
            "BASE_LLM_API_KEY": "revoked-token",
        }):
            with patch("openai.OpenAI", return_value=mock_client):
                result = emb.extract_memory_facts(ticket)

        # Must not retry — exactly one call
        assert call_count == 1
        # Circuit-breaker must be open
        assert emb._BASE_LLM_DISABLED is True
        # Must return fallback, not raise
        assert result is not None

    def test_401_embed_not_retried(self):
        """401 on embed_ticket immediately disables and returns None without retry."""
        import src.swe_team.embeddings as emb

        # Give ticket no investigation_report to hit the embeddings path directly
        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
        ticket = SWETicket(
            ticket_id="TEST-002",
            title="Embed 401 test",
            description="Ticket for embed 401 test",
            severity=TicketSeverity.MEDIUM,
            status=TicketStatus.OPEN,
        )

        call_count = 0

        def _raise_401(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise Exception("401 Unauthorized")

        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = _raise_401

        with patch.dict(os.environ, {
            "BASE_LLM_API_URL": "http://proxy.example.com/v1",
            "BASE_LLM_API_KEY": "revoked-token",
            "EMBEDDING_API_URL": "",
            "EMBEDDING_API_KEY": "",
        }):
            with patch("openai.OpenAI", return_value=mock_client):
                result = emb.embed_ticket(ticket)

        assert call_count == 1
        assert result is None
        assert emb._BASE_LLM_DISABLED is True


# ---------------------------------------------------------------------------
# Test: get_base_llm_status()
# ---------------------------------------------------------------------------

class TestGetBaseLlmStatus:
    def setup_method(self):
        _reset_circuit_breaker()

    def test_status_ok_when_url_set_and_enabled(self):
        import src.swe_team.embeddings as emb
        with patch.dict(os.environ, {"BASE_LLM_API_URL": "http://proxy.example.com/v1"}):
            assert emb.get_base_llm_status() == "ok"

    def test_status_disabled_when_no_url(self):
        import src.swe_team.embeddings as emb
        with patch.dict(os.environ, {}, clear=True):
            env = {k: v for k, v in os.environ.items() if k != "BASE_LLM_API_URL"}
            with patch.dict(os.environ, env, clear=True):
                # Ensure BASE_LLM_API_URL is absent
                os.environ.pop("BASE_LLM_API_URL", None)
                assert emb.get_base_llm_status() == "disabled"

    def test_status_degraded_when_circuit_open(self):
        import src.swe_team.embeddings as emb
        emb._BASE_LLM_DISABLED = True
        emb._BASE_LLM_DISABLED_UNTIL = time.monotonic() + 900
        with patch.dict(os.environ, {"BASE_LLM_API_URL": "http://proxy.example.com/v1"}):
            assert emb.get_base_llm_status() == "degraded"


# ---------------------------------------------------------------------------
# Test: preflight warns on missing key
# ---------------------------------------------------------------------------

class TestPreflightBaseLlmWarning:
    def test_warns_when_url_set_but_key_missing(self, caplog):
        """PreflightCheck emits WARNING when BASE_LLM_API_URL is set but key is empty."""
        import logging
        from src.swe_team.preflight import PreflightCheck

        env = {
            "BASE_LLM_API_URL": "http://proxy.example.com/v1",
            "BASE_LLM_API_KEY": "",
            "EMBEDDING_API_KEY": "",
        }
        checker = PreflightCheck(required_env_vars=[])

        with patch.dict(os.environ, env):
            with caplog.at_level(logging.WARNING, logger="src.swe_team.preflight"):
                checker._warn_base_llm_config()

        assert any(
            "BASE_LLM_API_KEY" in msg and "empty" in msg
            for msg in caplog.messages
        )

    def test_no_warning_when_key_present(self, caplog):
        """No warning when both URL and key are set."""
        import logging
        from src.swe_team.preflight import PreflightCheck

        env = {
            "BASE_LLM_API_URL": "http://proxy.example.com/v1",
            "BASE_LLM_API_KEY": "valid-key",
        }
        checker = PreflightCheck(required_env_vars=[])

        with patch.dict(os.environ, env):
            with caplog.at_level(logging.WARNING, logger="src.swe_team.preflight"):
                checker._warn_base_llm_config()

        assert not any("BASE_LLM_API_KEY" in msg for msg in caplog.messages)

    def test_no_warning_when_url_not_set(self, caplog):
        """No warning when BASE_LLM_API_URL is absent (embeddings simply unused)."""
        import logging
        from src.swe_team.preflight import PreflightCheck

        checker = PreflightCheck(required_env_vars=[])

        env = {k: v for k, v in os.environ.items()}
        env.pop("BASE_LLM_API_URL", None)

        with patch.dict(os.environ, env, clear=True):
            with caplog.at_level(logging.WARNING, logger="src.swe_team.preflight"):
                checker._warn_base_llm_config()

        assert not any("BASE_LLM_API_KEY" in msg for msg in caplog.messages)
