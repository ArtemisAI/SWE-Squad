"""Tests for LogQueryProvider integration in InvestigatorAgent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.investigator import InvestigatorAgent
from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus, TicketType
from src.swe_team.providers.log_query.base import LogEntry, LogQueryProvider


def _make_ticket(**overrides) -> SWETicket:
    defaults = dict(
        ticket_id="TEST-001",
        title="Test ticket",
        severity=TicketSeverity.HIGH,
        status=TicketStatus.TRIAGED,
        ticket_type=TicketType.BUG,
        description="Test description",
        source_module="browser",
        error_log="some error",
        metadata={},
    )
    defaults.update(overrides)
    return SWETicket(**defaults)


class FakeLogQueryProvider:
    """Concrete fake implementing the LogQueryProvider protocol."""

    def __init__(self, entries: List[LogEntry] | None = None, raise_on_query: bool = False):
        self._entries = entries or []
        self._raise_on_query = raise_on_query

    def query_logs(
        self,
        service: Optional[str] = None,
        level: Optional[str] = None,
        since_minutes: int = 60,
        limit: int = 500,
    ) -> List[LogEntry]:
        if self._raise_on_query:
            raise ConnectionError("backend unavailable")
        return self._entries

    def search_logs(
        self,
        pattern: str,
        service: Optional[str] = None,
        since_minutes: int = 60,
    ) -> List[LogEntry]:
        return []

    def health_check(self) -> bool:
        return not self._raise_on_query


class TestNoProvider:
    """When no LogQueryProvider is configured, existing behaviour is unchanged."""

    def test_query_logs_via_provider_returns_empty(self):
        agent = InvestigatorAgent()
        assert agent._query_logs_via_provider(service="x") == []

    def test_fetch_worker_logs_no_provider_no_workers(self):
        agent = InvestigatorAgent()
        ticket = _make_ticket(source_module="unknown_module", metadata={})
        result = agent._fetch_worker_logs(ticket)
        assert result is None


class TestProviderReturnsEntries:
    """Provider returns log entries; they appear in the output."""

    def test_provider_entries_formatted(self):
        entries = [
            LogEntry(timestamp="2026-03-22T10:00:00Z", level="ERROR", message="disk full", source="worker-1"),
            LogEntry(timestamp="2026-03-22T10:01:00Z", level="ERROR", message="oom killed", source="worker-1"),
        ]
        provider = FakeLogQueryProvider(entries=entries)
        agent = InvestigatorAgent(log_query_provider=provider)

        ticket = _make_ticket(source_module="unknown_module", metadata={})
        result = agent._fetch_worker_logs(ticket)
        assert result is not None
        assert "LogQueryProvider" in result
        assert "disk full" in result
        assert "oom killed" in result

    def test_query_logs_via_provider_delegates(self):
        entries = [LogEntry(timestamp="t", level="ERROR", message="x", source="s")]
        provider = FakeLogQueryProvider(entries=entries)
        agent = InvestigatorAgent(log_query_provider=provider)
        result = agent._query_logs_via_provider(service="svc", level="ERROR")
        assert len(result) == 1
        assert result[0].message == "x"


class TestProviderPlusSSHMerge:
    """Provider entries and SSH logs are merged with deduplication."""

    @patch("src.swe_team.investigator.fetch_worker_logs")
    def test_merge_dedup(self, mock_fetch):
        # SSH returns lines, one of which duplicates the provider entry
        mock_fetch.return_value = "disk full on /dev/sda1\nnew error from ssh"
        entries = [
            LogEntry(timestamp="t1", level="ERROR", message="disk full", source="worker"),
        ]
        provider = FakeLogQueryProvider(entries=entries)
        agent = InvestigatorAgent(
            log_query_provider=provider,
            worker_module_map={"browser": ["worker-browser-1"]},
        )

        ticket = _make_ticket(source_module="browser", metadata={})
        result = agent._fetch_worker_logs(ticket)
        assert result is not None
        # Provider section present
        assert "LogQueryProvider" in result
        assert "disk full" in result
        # SSH section present but the duplicate line removed
        assert "new error from ssh" in result
        # The SSH section should NOT contain the "disk full" line (deduped)
        ssh_section = result.split("### worker-browser-1")[1] if "### worker-browser-1" in result else ""
        assert "disk full" not in ssh_section


class TestProviderErrorFallback:
    """When the provider raises, SSH fallback still works."""

    @patch("src.swe_team.investigator.fetch_worker_logs")
    def test_provider_error_falls_back_to_ssh(self, mock_fetch):
        mock_fetch.return_value = "ssh log line here"
        provider = FakeLogQueryProvider(raise_on_query=True)
        agent = InvestigatorAgent(
            log_query_provider=provider,
            worker_module_map={"browser": ["worker-browser-1"]},
        )

        ticket = _make_ticket(source_module="browser", metadata={})
        result = agent._fetch_worker_logs(ticket)
        assert result is not None
        assert "ssh log line here" in result
        # No provider section since it failed
        assert "LogQueryProvider" not in result

    def test_query_logs_via_provider_error_returns_empty(self):
        provider = FakeLogQueryProvider(raise_on_query=True)
        agent = InvestigatorAgent(log_query_provider=provider)
        result = agent._query_logs_via_provider()
        assert result == []
