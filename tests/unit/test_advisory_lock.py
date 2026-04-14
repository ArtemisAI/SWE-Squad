"""Tests for the advisory lock (claim_ticket / release_ticket) in SupabaseTicketStore."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# We test the methods on SupabaseTicketStore which use self._request for RPC calls.
from src.swe_team.supabase_store import SupabaseTicketStore


def _make_store(**kwargs) -> SupabaseTicketStore:
    """Create a SupabaseTicketStore with mocked HTTP so it never hits the network."""
    with patch.dict("os.environ", {"SUPABASE_URL": "https://fake.supabase.co", "SUPABASE_ANON_KEY": "fake-key"}):
        store = SupabaseTicketStore(**kwargs)
    return store


class TestClaimTicket:
    @patch.object(SupabaseTicketStore, "_request")
    def test_claim_returns_true_on_success(self, mock_req):
        mock_req.return_value = True
        store = _make_store()
        assert store.claim_ticket("ticket-1", "agent-a") is True

    @patch.object(SupabaseTicketStore, "_request")
    def test_claim_returns_false_when_locked(self, mock_req):
        mock_req.return_value = False
        store = _make_store()
        assert store.claim_ticket("ticket-1", "agent-a") is False

    @patch.object(SupabaseTicketStore, "_request")
    def test_claim_returns_true_on_none(self, mock_req):
        """None from RPC is treated as falsy -> False."""
        mock_req.return_value = None
        store = _make_store()
        assert store.claim_ticket("ticket-1", "agent-a") is False

    @patch.object(SupabaseTicketStore, "_request")
    def test_claim_calls_rpc_with_correct_params(self, mock_req):
        mock_req.return_value = True
        store = _make_store()
        store.claim_ticket("ticket-42", "agent-x")
        mock_req.assert_called_once_with(
            "POST",
            "/rpc/claim_ticket",
            body={"p_ticket_id": "ticket-42", "p_agent_id": "agent-x"},
        )

    @patch.object(SupabaseTicketStore, "_request")
    def test_claim_rejects_on_rpc_error(self, mock_req):
        mock_req.side_effect = Exception("RPC unavailable")
        store = _make_store()
        # Fail-closed: returns False to prevent duplicate work
        assert store.claim_ticket("ticket-1", "agent-a") is False

    @patch.object(SupabaseTicketStore, "_request")
    def test_claim_rejects_on_connection_error(self, mock_req):
        mock_req.side_effect = ConnectionError("no network")
        store = _make_store()
        assert store.claim_ticket("ticket-1", "agent-a") is False

    @patch.object(SupabaseTicketStore, "_request")
    def test_claim_returns_false_for_zero(self, mock_req):
        """Numeric 0 is falsy."""
        mock_req.return_value = 0
        store = _make_store()
        assert store.claim_ticket("ticket-1", "agent-a") is False


class TestReleaseTicket:
    @patch.object(SupabaseTicketStore, "_request")
    def test_release_calls_rpc(self, mock_req):
        mock_req.return_value = None
        store = _make_store()
        store.release_ticket("ticket-1")
        mock_req.assert_called_once_with(
            "POST",
            "/rpc/release_ticket",
            body={"p_ticket_id": "ticket-1", "p_reset_status": "OPEN"},
        )

    @patch.object(SupabaseTicketStore, "_request")
    def test_release_custom_status(self, mock_req):
        mock_req.return_value = None
        store = _make_store()
        store.release_ticket("ticket-1", reset_status="TRIAGED")
        mock_req.assert_called_once_with(
            "POST",
            "/rpc/release_ticket",
            body={"p_ticket_id": "ticket-1", "p_reset_status": "TRIAGED"},
        )

    @patch.object(SupabaseTicketStore, "_request")
    def test_release_fallback_on_error(self, mock_req):
        mock_req.side_effect = Exception("RPC failed")
        store = _make_store()
        # Should not raise
        store.release_ticket("ticket-1")

    @patch.object(SupabaseTicketStore, "_request")
    def test_release_no_return_value(self, mock_req):
        """release_ticket returns None (void)."""
        mock_req.return_value = None
        store = _make_store()
        result = store.release_ticket("ticket-1")
        assert result is None
