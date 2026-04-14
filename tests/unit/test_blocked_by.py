"""Tests for blocked_by dependency tracking on SWETicket and TicketStore."""

from __future__ import annotations

import tempfile
import os
from pathlib import Path

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.ticket_store import TicketStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(**kwargs) -> SWETicket:
    defaults = {"title": "test", "description": "desc"}
    defaults.update(kwargs)
    return SWETicket(**defaults)


def _tmp_store() -> TicketStore:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)  # start empty
    return TicketStore(path=path)


# ---------------------------------------------------------------------------
# SWETicket.is_blocked
# ---------------------------------------------------------------------------

class TestIsBlocked:
    def test_not_blocked_by_default(self):
        t = _make_ticket()
        assert t.is_blocked() is False

    def test_blocked_when_blocked_by_non_empty(self):
        t = _make_ticket(blocked_by=["abc123"])
        assert t.is_blocked() is True

    def test_not_blocked_after_clearing(self):
        t = _make_ticket(blocked_by=["abc123"])
        t.blocked_by.clear()
        assert t.is_blocked() is False


# ---------------------------------------------------------------------------
# TicketStatus.BLOCKED
# ---------------------------------------------------------------------------

class TestBlockedStatus:
    def test_blocked_enum_exists(self):
        assert TicketStatus.BLOCKED.value == "blocked"

    def test_transition_to_blocked(self):
        t = _make_ticket()
        t.transition(TicketStatus.BLOCKED)
        assert t.status == TicketStatus.BLOCKED


# ---------------------------------------------------------------------------
# TicketStore.mark_blocked / unblock_ticket / get_blocked_tickets
# ---------------------------------------------------------------------------

class TestTicketStoreBlocking:
    def test_mark_blocked_sets_status(self):
        store = _tmp_store()
        t1 = _make_ticket(ticket_id="blocker1")
        t2 = _make_ticket(ticket_id="blocked1")
        store.add(t1)
        store.add(t2)
        result = store.mark_blocked("blocked1", ["blocker1"])
        assert result is not None
        assert result.status == TicketStatus.BLOCKED
        assert "blocker1" in result.blocked_by

    def test_mark_blocked_sets_reverse_blocking(self):
        store = _tmp_store()
        t1 = _make_ticket(ticket_id="blocker1")
        t2 = _make_ticket(ticket_id="blocked1")
        store.add(t1)
        store.add(t2)
        store.mark_blocked("blocked1", ["blocker1"])
        blocker = store.get("blocker1")
        assert "blocked1" in blocker.blocking

    def test_unblock_removes_blocker(self):
        store = _tmp_store()
        t1 = _make_ticket(ticket_id="blocker1")
        t2 = _make_ticket(ticket_id="blocked1")
        store.add(t1)
        store.add(t2)
        store.mark_blocked("blocked1", ["blocker1"])
        result = store.unblock_ticket("blocked1", "blocker1")
        assert result is not None
        assert "blocker1" not in result.blocked_by
        assert result.status == TicketStatus.TRIAGED

    def test_unblock_keeps_blocked_if_other_blockers(self):
        store = _tmp_store()
        for tid in ["b1", "b2", "target"]:
            store.add(_make_ticket(ticket_id=tid))
        store.mark_blocked("target", ["b1", "b2"])
        result = store.unblock_ticket("target", "b1")
        assert result.status == TicketStatus.BLOCKED
        assert "b2" in result.blocked_by

    def test_get_blocked_tickets(self):
        store = _tmp_store()
        t1 = _make_ticket(ticket_id="a")
        t2 = _make_ticket(ticket_id="b")
        t3 = _make_ticket(ticket_id="c")
        store.add(t1)
        store.add(t2)
        store.add(t3)
        store.mark_blocked("b", ["a"])
        blocked = store.get_blocked_tickets()
        assert len(blocked) == 1
        assert blocked[0].ticket_id == "b"

    def test_mark_blocked_nonexistent_ticket(self):
        store = _tmp_store()
        result = store.mark_blocked("nope", ["x"])
        assert result is None

    def test_unblock_nonexistent_ticket(self):
        store = _tmp_store()
        result = store.unblock_ticket("nope", "x")
        assert result is None


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

class TestBlockedSerialization:
    def test_to_dict_includes_blocked_by(self):
        t = _make_ticket(blocked_by=["x"], blocking=["y"])
        d = t.to_dict()
        assert d["blocked_by"] == ["x"]
        assert d["blocking"] == ["y"]

    def test_from_dict_restores_blocked_by(self):
        t = _make_ticket(blocked_by=["x"], blocking=["y"])
        d = t.to_dict()
        t2 = SWETicket.from_dict(d)
        assert t2.blocked_by == ["x"]
        assert t2.blocking == ["y"]


# ---------------------------------------------------------------------------
# Triage dedup detection
# ---------------------------------------------------------------------------

class TestTriageDedup:
    def test_triage_blocks_on_matching_fingerprint(self):
        """Triage should block a new ticket if a similar one is in development."""
        from unittest.mock import MagicMock
        from src.swe_team.triage_agent import TriageAgent

        config = MagicMock()
        config.get_agents_by_role.return_value = [
            MagicMock(name="inv1")
        ]

        store = _tmp_store()
        existing = _make_ticket(ticket_id="existing1")
        existing.metadata["fingerprint"] = "abcdef1234567890"
        existing.transition(TicketStatus.IN_DEVELOPMENT)
        store.add(existing)

        agent = TriageAgent(config, ticket_store=store)
        new_ticket = _make_ticket(ticket_id="new1")
        new_ticket.metadata["fingerprint"] = "abcdef12xxxxxxxx"

        result = agent.triage(new_ticket)
        assert result.status == TicketStatus.BLOCKED
        assert "existing1" in result.blocked_by
