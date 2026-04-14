"""
Tests for /api/approvals endpoints — HITL escalation approval API.

Covers:
  - GET /api/approvals — list pending approvals
  - GET /api/approvals/<id> — get single approval
  - POST /api/approvals/<id>/approve — approve ticket
  - POST /api/approvals/<id>/reject — reject ticket
  - POST /api/approvals/<id>/request-revision — request revision
  - GET /api/approvals/<id>/comments — list comments
  - POST /api/approvals/<id>/comments — add comment
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.ticket_store import TicketStore

logging.logAsyncioTasks = False


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def tmp_store(tmp_path):
    path = tmp_path / "tickets.json"
    return TicketStore(path=str(path))


@pytest.fixture
def hitl_ticket():
    """A ticket flagged with needs_hitl=True."""
    return SWETicket(
        ticket_id="hitl-aaa111",
        title="HITL ticket",
        description="Needs human review",
        severity=TicketSeverity.HIGH,
        status=TicketStatus.INVESTIGATING,
        metadata={"needs_hitl": True, "fingerprint": "fp-hitl"},
    )


@pytest.fixture
def review_ticket():
    """A ticket in IN_REVIEW status."""
    t = SWETicket(
        ticket_id="review-bbb222",
        title="Review ticket",
        description="In review status",
        severity=TicketSeverity.MEDIUM,
        status=TicketStatus.IN_REVIEW,
        metadata={"fingerprint": "fp-review"},
    )
    return t


@pytest.fixture
def rework_ticket():
    """A ticket in REWORK_REQUESTED status."""
    return SWETicket(
        ticket_id="rework-ccc333",
        title="Rework ticket",
        description="Rework requested",
        severity=TicketSeverity.LOW,
        status=TicketStatus.REWORK_REQUESTED,
        metadata={"fingerprint": "fp-rework"},
    )


@pytest.fixture
def normal_ticket():
    """A ticket not pending approval."""
    return SWETicket(
        ticket_id="normal-ddd444",
        title="Normal open ticket",
        description="No approval needed",
        severity=TicketSeverity.MEDIUM,
        status=TicketStatus.OPEN,
        metadata={},
    )


@pytest.fixture
def store_with_approvals(tmp_store, hitl_ticket, review_ticket, rework_ticket, normal_ticket):
    tmp_store.add(hitl_ticket)
    tmp_store.add(review_ticket)
    tmp_store.add(rework_ticket)
    tmp_store.add(normal_ticket)
    return tmp_store


# ══════════════════════════════════════════════════════════════════════════════
# Helper: build a DashboardHandler with approvals methods wired
# ══════════════════════════════════════════════════════════════════════════════


def _make_handler(store):
    from scripts.ops.dashboard_server import DashboardHandler

    handler = MagicMock(spec=DashboardHandler)
    handler.store = store
    handler.auth_provider = None
    handler.headers = {"Content-Length": "0"}

    # Wire real approvals methods
    handler._read_post_body = DashboardHandler._read_post_body.__get__(handler)
    handler._json_response = DashboardHandler._json_response.__get__(handler)
    handler._approval_is_pending = DashboardHandler._approval_is_pending.__get__(handler)
    handler._handle_list_approvals = DashboardHandler._handle_list_approvals.__get__(handler)
    handler._handle_get_approval = DashboardHandler._handle_get_approval.__get__(handler)
    handler._handle_approval_approve = DashboardHandler._handle_approval_approve.__get__(handler)
    handler._handle_approval_reject = DashboardHandler._handle_approval_reject.__get__(handler)
    handler._handle_approval_request_revision = (
        DashboardHandler._handle_approval_request_revision.__get__(handler)
    )
    handler._handle_get_approval_comments = (
        DashboardHandler._handle_get_approval_comments.__get__(handler)
    )
    handler._handle_add_approval_comment = (
        DashboardHandler._handle_add_approval_comment.__get__(handler)
    )
    handler._gh_comment_async = MagicMock()

    return handler


def _set_body(handler, body_dict):
    raw = json.dumps(body_dict).encode()
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)


def _capture(handler):
    """Capture _json_response calls as (data, status) tuples."""
    results = []

    original = type(handler)._json_response if hasattr(type(handler), "_json_response") else None

    def _save(data, status=200, **kwargs):
        results.append((data, status))

    handler._json_response = _save
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Tests: GET /api/approvals
# ══════════════════════════════════════════════════════════════════════════════


class TestListApprovals:
    def test_returns_only_pending_tickets(self, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        results = _capture(handler)
        handler._handle_list_approvals()

        assert len(results) == 1
        data, status = results[0]
        assert status == 200
        assert "approvals" in data
        assert data["count"] == 3  # hitl + review + rework
        ids = {a["ticket_id"] for a in data["approvals"]}
        assert "hitl-aaa111" in ids
        assert "review-bbb222" in ids
        assert "rework-ccc333" in ids
        assert "normal-ddd444" not in ids

    def test_all_approvals_have_is_pending_flag(self, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        results = _capture(handler)
        handler._handle_list_approvals()

        data, _ = results[0]
        for a in data["approvals"]:
            assert a.get("is_pending_approval") is True

    def test_empty_store_returns_empty_list(self, tmp_store):
        handler = _make_handler(tmp_store)
        results = _capture(handler)
        handler._handle_list_approvals()

        data, status = results[0]
        assert status == 200
        assert data["approvals"] == []
        assert data["count"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Tests: GET /api/approvals/<id>
# ══════════════════════════════════════════════════════════════════════════════


class TestGetApproval:
    def test_returns_hitl_ticket(self, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        results = _capture(handler)
        handler._handle_get_approval("hitl-aaa111")

        data, status = results[0]
        assert status == 200
        assert data["ticket_id"] == "hitl-aaa111"
        assert data["is_pending_approval"] is True

    def test_returns_in_review_ticket(self, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        results = _capture(handler)
        handler._handle_get_approval("review-bbb222")

        data, status = results[0]
        assert status == 200
        assert data["ticket_id"] == "review-bbb222"

    def test_nonexistent_ticket_returns_404(self, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        results = _capture(handler)
        handler._handle_get_approval("does-not-exist")

        _, status = results[0]
        assert status == 404

    def test_non_pending_ticket_returns_404(self, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        results = _capture(handler)
        handler._handle_get_approval("normal-ddd444")

        _, status = results[0]
        assert status == 404


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/approvals/<id>/approve
# ══════════════════════════════════════════════════════════════════════════════


class TestApprove:
    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_approve_hitl_ticket_without_fix(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {})
        results = _capture(handler)
        handler._handle_approval_approve("hitl-aaa111")

        data, status = results[0]
        assert status == 200
        assert data["status"] == "ok"
        assert data["ticket_id"] == "hitl-aaa111"
        # Without a proposed_fix, ticket should move to IN_DEVELOPMENT
        assert data["new_status"] == TicketStatus.IN_DEVELOPMENT.value

        # Verify store was updated
        updated = store_with_approvals.get("hitl-aaa111")
        assert updated.metadata.get("needs_hitl") is False
        assert updated.metadata.get("approved_at") is not None

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_approve_with_proposed_fix_resolves(self, mock_sse, store_with_approvals, hitl_ticket):
        # Add proposed_fix so it should resolve
        hitl_ticket.proposed_fix = "Fix the thing"
        store_with_approvals.add(hitl_ticket)

        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"note": "LGTM"})
        results = _capture(handler)
        handler._handle_approval_approve("hitl-aaa111")

        data, status = results[0]
        assert status == 200
        assert data["new_status"] == TicketStatus.RESOLVED.value

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_approve_with_note(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"note": "Looks good!"})
        results = _capture(handler)
        handler._handle_approval_approve("hitl-aaa111")

        updated = store_with_approvals.get("hitl-aaa111")
        assert updated.metadata.get("approval_note") == "Looks good!"

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_approve_nonexistent_ticket_returns_404(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {})
        results = _capture(handler)
        handler._handle_approval_approve("does-not-exist")

        _, status = results[0]
        assert status == 404

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_approve_non_pending_ticket_returns_400(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {})
        results = _capture(handler)
        handler._handle_approval_approve("normal-ddd444")

        _, status = results[0]
        assert status == 400

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_sse_broadcast_on_approve(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {})
        _capture(handler)
        handler._handle_approval_approve("hitl-aaa111")

        mock_sse.assert_called_once()
        call_kwargs = mock_sse.call_args
        assert call_kwargs[0][0] == "action"
        assert call_kwargs[0][1]["event"] == "approval_approved"
        assert call_kwargs[0][1]["ticket_id"] == "hitl-aaa111"


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/approvals/<id>/reject
# ══════════════════════════════════════════════════════════════════════════════


class TestReject:
    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_reject_closes_ticket(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"reason": "Not ready for production"})
        results = _capture(handler)
        handler._handle_approval_reject("hitl-aaa111")

        data, status = results[0]
        assert status == 200
        assert data["status"] == "ok"
        assert data["new_status"] == TicketStatus.CLOSED.value

        updated = store_with_approvals.get("hitl-aaa111")
        assert updated.status == TicketStatus.CLOSED
        assert updated.metadata["rejection_reason"] == "Not ready for production"
        assert updated.metadata.get("rejected_at") is not None
        assert updated.metadata.get("needs_hitl") is False

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_reject_requires_reason(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {})
        results = _capture(handler)
        handler._handle_approval_reject("hitl-aaa111")

        _, status = results[0]
        assert status == 400

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_reject_empty_reason_rejected(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"reason": ""})
        results = _capture(handler)
        handler._handle_approval_reject("hitl-aaa111")

        _, status = results[0]
        assert status == 400

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_reject_nonexistent_ticket_returns_404(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"reason": "No"})
        results = _capture(handler)
        handler._handle_approval_reject("does-not-exist")

        _, status = results[0]
        assert status == 404

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_reject_non_pending_returns_400(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"reason": "No"})
        results = _capture(handler)
        handler._handle_approval_reject("normal-ddd444")

        _, status = results[0]
        assert status == 400

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_sse_broadcast_on_reject(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"reason": "Not good enough"})
        _capture(handler)
        handler._handle_approval_reject("hitl-aaa111")

        mock_sse.assert_called_once()
        assert mock_sse.call_args[0][1]["event"] == "approval_rejected"


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/approvals/<id>/request-revision
# ══════════════════════════════════════════════════════════════════════════════


class TestRequestRevision:
    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_revision_moves_to_rework_requested(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"feedback": "Please fix the edge case"})
        results = _capture(handler)
        handler._handle_approval_request_revision("hitl-aaa111")

        data, status = results[0]
        assert status == 200
        assert data["status"] == "ok"
        assert data["new_status"] == TicketStatus.REWORK_REQUESTED.value

        updated = store_with_approvals.get("hitl-aaa111")
        assert updated.status == TicketStatus.REWORK_REQUESTED
        assert updated.metadata["rework_feedback"] == "Please fix the edge case"
        assert updated.metadata.get("rework_requested_at") is not None

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_revision_requires_feedback(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {})
        results = _capture(handler)
        handler._handle_approval_request_revision("hitl-aaa111")

        _, status = results[0]
        assert status == 400

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_revision_empty_feedback_rejected(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"feedback": ""})
        results = _capture(handler)
        handler._handle_approval_request_revision("hitl-aaa111")

        _, status = results[0]
        assert status == 400

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_revision_nonexistent_ticket_returns_404(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"feedback": "Fix it"})
        results = _capture(handler)
        handler._handle_approval_request_revision("does-not-exist")

        _, status = results[0]
        assert status == 404

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_revision_non_pending_returns_400(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"feedback": "Fix it"})
        results = _capture(handler)
        handler._handle_approval_request_revision("normal-ddd444")

        _, status = results[0]
        assert status == 400

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_sse_broadcast_on_revision(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"feedback": "Needs work"})
        _capture(handler)
        handler._handle_approval_request_revision("hitl-aaa111")

        mock_sse.assert_called_once()
        assert mock_sse.call_args[0][1]["event"] == "approval_revision_requested"


# ══════════════════════════════════════════════════════════════════════════════
# Tests: GET /api/approvals/<id>/comments
# ══════════════════════════════════════════════════════════════════════════════


class TestGetApprovalComments:
    def test_returns_empty_comments_list(self, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        results = _capture(handler)
        handler._handle_get_approval_comments("hitl-aaa111")

        data, status = results[0]
        assert status == 200
        assert data["comments"] == []
        assert data["count"] == 0

    def test_returns_existing_comments(self, tmp_store):
        ticket = SWETicket(
            ticket_id="cmnt-eee555",
            title="Ticket with comments",
            description="Has comments",
            status=TicketStatus.IN_REVIEW,
            metadata={
                "needs_hitl": False,
                "comments": [
                    {"text": "First comment", "timestamp": "2026-01-01T00:00:00Z", "source": "dashboard"},
                    {"text": "Second comment", "timestamp": "2026-01-02T00:00:00Z", "source": "dashboard"},
                ],
            },
        )
        tmp_store.add(ticket)

        handler = _make_handler(tmp_store)
        results = _capture(handler)
        handler._handle_get_approval_comments("cmnt-eee555")

        data, status = results[0]
        assert status == 200
        assert data["count"] == 2
        assert data["comments"][0]["text"] == "First comment"

    def test_nonexistent_ticket_returns_404(self, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        results = _capture(handler)
        handler._handle_get_approval_comments("does-not-exist")

        _, status = results[0]
        assert status == 404

    def test_non_pending_ticket_returns_404(self, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        results = _capture(handler)
        handler._handle_get_approval_comments("normal-ddd444")

        _, status = results[0]
        assert status == 404


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/approvals/<id>/comments
# ══════════════════════════════════════════════════════════════════════════════


class TestAddApprovalComment:
    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_add_comment_success(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"comment": "This looks good to me"})
        results = _capture(handler)
        handler._handle_add_approval_comment("hitl-aaa111")

        data, status = results[0]
        assert status == 200
        assert data["status"] == "ok"
        assert data["ticket_id"] == "hitl-aaa111"
        assert data["comment_count"] == 1

        updated = store_with_approvals.get("hitl-aaa111")
        comments = updated.metadata.get("comments", [])
        assert len(comments) == 1
        assert comments[0]["text"] == "This looks good to me"
        assert comments[0]["source"] == "dashboard"

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_add_multiple_comments(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)

        _set_body(handler, {"comment": "Comment 1"})
        _capture(handler)
        handler._handle_add_approval_comment("review-bbb222")

        _set_body(handler, {"comment": "Comment 2"})
        results2 = _capture(handler)
        handler._handle_add_approval_comment("review-bbb222")

        data, _ = results2[0]
        assert data["comment_count"] == 2

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_add_comment_requires_text(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {})
        results = _capture(handler)
        handler._handle_add_approval_comment("hitl-aaa111")

        _, status = results[0]
        assert status == 400

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_add_empty_comment_rejected(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"comment": ""})
        results = _capture(handler)
        handler._handle_add_approval_comment("hitl-aaa111")

        _, status = results[0]
        assert status == 400

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_add_comment_nonexistent_ticket(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"comment": "Hello"})
        results = _capture(handler)
        handler._handle_add_approval_comment("does-not-exist")

        _, status = results[0]
        assert status == 404

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_add_comment_non_pending_ticket(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"comment": "Hello"})
        results = _capture(handler)
        handler._handle_add_approval_comment("normal-ddd444")

        _, status = results[0]
        assert status == 404

    @patch("scripts.ops.dashboard_server._broadcast_sse_event")
    def test_sse_broadcast_on_comment(self, mock_sse, store_with_approvals):
        handler = _make_handler(store_with_approvals)
        _set_body(handler, {"comment": "Looks good"})
        _capture(handler)
        handler._handle_add_approval_comment("hitl-aaa111")

        mock_sse.assert_called_once()
        assert mock_sse.call_args[0][1]["event"] == "approval_comment_added"
        assert mock_sse.call_args[0][1]["ticket_id"] == "hitl-aaa111"


# ══════════════════════════════════════════════════════════════════════════════
# Tests: _approval_is_pending helper
# ══════════════════════════════════════════════════════════════════════════════


class TestApprovalIsPending:
    def test_needs_hitl_flag(self, tmp_store):
        ticket = SWETicket(
            ticket_id="t1",
            title="T",
            description="D",
            status=TicketStatus.OPEN,
            metadata={"needs_hitl": True},
        )
        tmp_store.add(ticket)
        handler = _make_handler(tmp_store)
        assert handler._approval_is_pending(ticket) is True

    def test_in_review_status(self, tmp_store):
        ticket = SWETicket(
            ticket_id="t2",
            title="T",
            description="D",
            status=TicketStatus.IN_REVIEW,
            metadata={},
        )
        tmp_store.add(ticket)
        handler = _make_handler(tmp_store)
        assert handler._approval_is_pending(ticket) is True

    def test_rework_requested_status(self, tmp_store):
        ticket = SWETicket(
            ticket_id="t3",
            title="T",
            description="D",
            status=TicketStatus.REWORK_REQUESTED,
            metadata={},
        )
        tmp_store.add(ticket)
        handler = _make_handler(tmp_store)
        assert handler._approval_is_pending(ticket) is True

    def test_open_ticket_not_pending(self, tmp_store):
        ticket = SWETicket(
            ticket_id="t4",
            title="T",
            description="D",
            status=TicketStatus.OPEN,
            metadata={},
        )
        tmp_store.add(ticket)
        handler = _make_handler(tmp_store)
        assert handler._approval_is_pending(ticket) is False

    def test_needs_hitl_false_not_pending(self, tmp_store):
        ticket = SWETicket(
            ticket_id="t5",
            title="T",
            description="D",
            status=TicketStatus.OPEN,
            metadata={"needs_hitl": False},
        )
        tmp_store.add(ticket)
        handler = _make_handler(tmp_store)
        assert handler._approval_is_pending(ticket) is False
