"""Unit tests for src/swe_team/embeddings.py — no real network calls."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

import src.swe_team.embeddings as emb_mod
from src.swe_team.embeddings import (
    _disable_base_llm,
    _is_auth_error,
    _is_base_llm_disabled,
    extract_edges_from_ticket,
    extract_memory_facts,
    get_base_llm_status,
    set_auth_provider,
)
from src.swe_team.models import EdgeType, SWETicket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_circuit_breaker() -> None:
    """Reset the module-level circuit breaker between tests."""
    emb_mod._BASE_LLM_DISABLED = False
    emb_mod._BASE_LLM_DISABLED_UNTIL = 0.0
    emb_mod._auth_provider = None


def _make_ticket(
    title: str = "Test ticket",
    investigation_report: str = "",
    source_module: str | None = None,
    error_log: str | None = None,
) -> SWETicket:
    t = SWETicket(title=title, description="desc")
    t.investigation_report = investigation_report or None
    t.source_module = source_module
    t.error_log = error_log
    return t


def _make_openai_embedding_mock(vector: list[float] | None = None) -> MagicMock:
    """Build a mock openai.OpenAI client that returns an embedding."""
    vector = vector or [0.1, 0.2, 0.3]
    mock_client = MagicMock()
    mock_data = MagicMock()
    mock_data.embedding = vector
    mock_resp = MagicMock()
    mock_resp.data = [mock_data]
    mock_client.embeddings.create.return_value = mock_resp
    return mock_client


def _make_openai_chat_mock(content: str = "Root cause: bug\nFix applied: patch") -> MagicMock:
    """Build a mock openai.OpenAI client that returns a chat completion."""
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = content
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_resp
    return mock_client


# ---------------------------------------------------------------------------
# 1. Circuit breaker — _is_base_llm_disabled / _disable_base_llm
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def setup_method(self) -> None:
        _reset_circuit_breaker()

    def teardown_method(self) -> None:
        _reset_circuit_breaker()

    def test_initially_enabled(self) -> None:
        assert _is_base_llm_disabled() is False

    def test_disable_sets_flag(self) -> None:
        _disable_base_llm()
        assert emb_mod._BASE_LLM_DISABLED is True

    def test_disabled_returns_true_within_ttl(self) -> None:
        _disable_base_llm()
        assert _is_base_llm_disabled() is True

    def test_circuit_breaker_resets_after_ttl(self) -> None:
        _disable_base_llm()
        # Manually wind back the deadline to "now - 1"
        emb_mod._BASE_LLM_DISABLED_UNTIL = time.monotonic() - 1
        assert _is_base_llm_disabled() is False
        assert emb_mod._BASE_LLM_DISABLED is False

    def test_auth_provider_called_on_disable(self) -> None:
        mock_provider = MagicMock()
        set_auth_provider(mock_provider)
        _disable_base_llm()
        mock_provider.record_auth_failure.assert_called_once()

    def test_auth_provider_error_does_not_propagate(self) -> None:
        mock_provider = MagicMock()
        mock_provider.record_auth_failure.side_effect = RuntimeError("boom")
        set_auth_provider(mock_provider)
        _disable_base_llm()  # Should not raise


# ---------------------------------------------------------------------------
# 2. _is_auth_error helper
# ---------------------------------------------------------------------------

class TestIsAuthError:
    def test_401_in_message(self) -> None:
        assert _is_auth_error(Exception("HTTP 401 Unauthorized")) is True

    def test_403_in_message(self) -> None:
        assert _is_auth_error(Exception("403 Forbidden")) is True

    def test_forbidden_keyword(self) -> None:
        assert _is_auth_error(Exception("forbidden access")) is True

    def test_unauthorized_keyword(self) -> None:
        assert _is_auth_error(Exception("unauthorized request")) is True

    def test_non_auth_error(self) -> None:
        assert _is_auth_error(Exception("connection timeout")) is False

    def test_invalid_api_key(self) -> None:
        assert _is_auth_error(Exception("invalid api key")) is True


# ---------------------------------------------------------------------------
# 3. embed_ticket
# ---------------------------------------------------------------------------

class TestEmbedTicket:
    def setup_method(self) -> None:
        _reset_circuit_breaker()

    def teardown_method(self) -> None:
        _reset_circuit_breaker()

    def test_returns_none_when_circuit_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _disable_base_llm()
        from src.swe_team.embeddings import embed_ticket
        ticket = _make_ticket()
        result = embed_ticket(ticket)
        assert result is None

    def test_returns_none_when_api_url_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EMBEDDING_API_URL", raising=False)
        monkeypatch.delenv("BASE_LLM_API_URL", raising=False)
        monkeypatch.setenv("EMBEDDING_API_KEY", "some-key")
        from src.swe_team.embeddings import embed_ticket
        ticket = _make_ticket()
        result = embed_ticket(ticket)
        assert result is None

    def test_returns_embedding_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BASE_LLM_API_URL", "http://proxy.example.com/v1")
        monkeypatch.setenv("BASE_LLM_API_KEY", "test-key")
        monkeypatch.setenv("EMBEDDING_MODEL", "bge-m3")
        mock_client = _make_openai_embedding_mock([0.1, 0.2, 0.3])
        ticket = _make_ticket(title="Crash in scraper")
        from src.swe_team.embeddings import embed_ticket
        with patch("openai.OpenAI", return_value=mock_client):
            result = embed_ticket(ticket)
        assert result == [0.1, 0.2, 0.3]

    def test_auth_error_disables_circuit_breaker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BASE_LLM_API_URL", "http://proxy.example.com/v1")
        monkeypatch.setenv("BASE_LLM_API_KEY", "bad-key")
        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = Exception("HTTP 403 Forbidden")
        ticket = _make_ticket()
        from src.swe_team.embeddings import embed_ticket
        with patch("openai.OpenAI", return_value=mock_client):
            result = embed_ticket(ticket)
        assert result is None
        assert emb_mod._BASE_LLM_DISABLED is True

    def test_non_auth_error_returns_none_no_disable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BASE_LLM_API_URL", "http://proxy.example.com/v1")
        monkeypatch.setenv("BASE_LLM_API_KEY", "key")
        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = Exception("connection timeout")
        ticket = _make_ticket()
        from src.swe_team.embeddings import embed_ticket
        with patch("openai.OpenAI", return_value=mock_client):
            result = embed_ticket(ticket)
        assert result is None
        assert emb_mod._BASE_LLM_DISABLED is False


# ---------------------------------------------------------------------------
# 4. extract_memory_facts
# ---------------------------------------------------------------------------

class TestExtractMemoryFacts:
    def setup_method(self) -> None:
        _reset_circuit_breaker()

    def teardown_method(self) -> None:
        _reset_circuit_breaker()

    def test_returns_ticket_text_when_no_investigation(self) -> None:
        ticket = _make_ticket(title="No report", investigation_report="")
        result = extract_memory_facts(ticket)
        assert "No report" in result

    def test_returns_ticket_text_when_circuit_open(self) -> None:
        _disable_base_llm()
        ticket = _make_ticket(
            title="Circuit open ticket",
            investigation_report="Some report text",
        )
        result = extract_memory_facts(ticket)
        assert "Circuit open ticket" in result

    def test_returns_ticket_text_when_api_url_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BASE_LLM_API_URL", raising=False)
        ticket = _make_ticket(
            title="No URL ticket",
            investigation_report="Investigation notes here",
        )
        result = extract_memory_facts(ticket)
        assert "No URL ticket" in result

    def test_returns_extracted_facts_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BASE_LLM_API_URL", "http://proxy.example.com/v1")
        monkeypatch.setenv("BASE_LLM_API_KEY", "test-key")
        facts = "Root cause: DB connection pool exhausted\nFix applied: increased pool size"
        mock_client = _make_openai_chat_mock(content=facts)
        ticket = _make_ticket(
            title="DB pool bug",
            investigation_report="Connection pool was full",
        )
        with patch("openai.OpenAI", return_value=mock_client):
            result = extract_memory_facts(ticket)
        assert "Root cause" in result
        assert ticket.metadata.get("memory_facts") == facts

    def test_auth_error_during_extraction_disables_proxy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BASE_LLM_API_URL", "http://proxy.example.com/v1")
        monkeypatch.setenv("BASE_LLM_API_KEY", "key")
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("401 Unauthorized")
        ticket = _make_ticket(
            title="Auth fail test",
            investigation_report="Some investigation",
        )
        with patch("openai.OpenAI", return_value=mock_client):
            result = extract_memory_facts(ticket)
        assert "Auth fail test" in result  # fallback to ticket text
        assert emb_mod._BASE_LLM_DISABLED is True

    def test_non_auth_failure_falls_back_to_ticket_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BASE_LLM_API_URL", "http://proxy.example.com/v1")
        monkeypatch.setenv("BASE_LLM_API_KEY", "key")
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("timeout")
        ticket = _make_ticket(
            title="Timeout test",
            investigation_report="Investigation present",
        )
        with patch("openai.OpenAI", return_value=mock_client):
            result = extract_memory_facts(ticket)
        assert "Timeout test" in result
        assert emb_mod._BASE_LLM_DISABLED is False


# ---------------------------------------------------------------------------
# 5. get_base_llm_status
# ---------------------------------------------------------------------------

class TestGetBaseLlmStatus:
    def setup_method(self) -> None:
        _reset_circuit_breaker()

    def teardown_method(self) -> None:
        _reset_circuit_breaker()

    def test_disabled_when_no_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BASE_LLM_API_URL", raising=False)
        assert get_base_llm_status() == "disabled"

    def test_degraded_when_circuit_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BASE_LLM_API_URL", "http://proxy.example.com/v1")
        _disable_base_llm()
        assert get_base_llm_status() == "degraded"

    def test_ok_when_url_set_and_circuit_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BASE_LLM_API_URL", "http://proxy.example.com/v1")
        assert get_base_llm_status() == "ok"


# ---------------------------------------------------------------------------
# 6. extract_edges_from_ticket
# ---------------------------------------------------------------------------

class TestExtractEdgesFromTicket:
    def test_similar_edge_created_above_threshold(self) -> None:
        ticket = _make_ticket(title="Base ticket")
        ticket.ticket_id = "base-001"
        similar = [
            {"ticket_id": "other-001", "raw_similarity": 0.92},
        ]
        edges = extract_edges_from_ticket(ticket, similar_tickets=similar)
        edge_types = [e.edge_type for e in edges]
        assert EdgeType.SIMILAR in edge_types

    def test_similar_edge_not_created_below_threshold(self) -> None:
        ticket = _make_ticket(title="Base ticket")
        ticket.ticket_id = "base-001"
        similar = [
            {"ticket_id": "other-001", "raw_similarity": 0.50},
        ]
        edges = extract_edges_from_ticket(ticket, similar_tickets=similar)
        edge_types = [e.edge_type for e in edges]
        assert EdgeType.SIMILAR not in edge_types

    def test_module_edge_created_from_source_module(self) -> None:
        ticket = _make_ticket(source_module="scraper.py")
        ticket.ticket_id = "base-001"
        edges = extract_edges_from_ticket(ticket)
        assert any(e.edge_type == EdgeType.TOUCHES_MODULE for e in edges)

    def test_no_self_similar_edge(self) -> None:
        ticket = _make_ticket(title="Self test")
        ticket.ticket_id = "self-001"
        similar = [{"ticket_id": "self-001", "raw_similarity": 0.99}]
        edges = extract_edges_from_ticket(ticket, similar_tickets=similar)
        assert not any(e.target_id == "self-001" for e in edges)

    def test_no_edges_when_no_module_and_no_similar(self) -> None:
        ticket = _make_ticket(title="Empty ticket")
        ticket.ticket_id = "empty-001"
        ticket.source_module = None
        edges = extract_edges_from_ticket(ticket, similar_tickets=[])
        assert edges == []
