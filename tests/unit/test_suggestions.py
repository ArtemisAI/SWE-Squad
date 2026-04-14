"""
Tests for /api/suggestions endpoints — creative agent proposal cards.

Covers:
  - GET /api/suggestions — list suggestions (empty, populated, seeded)
  - POST /api/suggestions/<id>/accept — accept creates ticket
  - POST /api/suggestions/<id>/dismiss — dismiss updates status
  - Seed generation on first read
"""

from __future__ import annotations

import io
import json
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


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def tmp_store(tmp_path):
    path = tmp_path / "tickets.json"
    return TicketStore(path=str(path))


@pytest.fixture
def suggestions_path(tmp_path):
    return tmp_path / "suggestions.json"


@pytest.fixture
def sample_suggestions():
    return [
        {
            "id": "sug-001",
            "title": "Add retry logic",
            "description": "Improve reliability of API calls",
            "category": "reliability",
            "impact": "high",
            "created_at": "2026-04-08T00:00:00Z",
            "status": "pending",
        },
        {
            "id": "sug-002",
            "title": "Increase test coverage",
            "description": "Cover edge cases in rate limiter",
            "category": "testing",
            "impact": "medium",
            "created_at": "2026-04-08T00:00:00Z",
            "status": "pending",
        },
        {
            "id": "sug-003",
            "title": "Already dismissed",
            "description": "This was dismissed",
            "category": "performance",
            "impact": "low",
            "created_at": "2026-04-08T00:00:00Z",
            "status": "dismissed",
        },
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Helper: build a DashboardHandler with suggestions methods wired
# ══════════════════════════════════════════════════════════════════════════════


def _make_handler(store, suggestions_path):
    from scripts.ops.dashboard_server import DashboardHandler

    handler = MagicMock(spec=DashboardHandler)
    handler.store = store
    handler.auth_provider = None
    handler.headers = {"Content-Length": "0"}

    # Wire real methods
    handler._read_post_body = DashboardHandler._read_post_body.__get__(handler)
    handler._json_response = DashboardHandler._json_response.__get__(handler)
    handler._handle_list_suggestions = DashboardHandler._handle_list_suggestions.__get__(handler)
    handler._handle_suggestion_accept = DashboardHandler._handle_suggestion_accept.__get__(handler)
    handler._handle_suggestion_dismiss = DashboardHandler._handle_suggestion_dismiss.__get__(handler)

    return handler


def _set_body(handler, body_dict):
    raw = json.dumps(body_dict).encode()
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)


def _capture(handler):
    """Capture _json_response calls as (data, status) tuples."""
    results = []

    def _save(data, status=200, **kwargs):
        results.append((data, status))

    handler._json_response = _save
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Tests: helper functions
# ══════════════════════════════════════════════════════════════════════════════


class TestSuggestionsHelpers:
    def test_read_suggestions_seeds_defaults_when_missing(self, suggestions_path):
        """When suggestions.json doesn't exist, _read_suggestions seeds defaults."""
        from scripts.ops.dashboard_server import (
            _read_suggestions,
            _DEFAULT_SUGGESTIONS,
            _SUGGESTIONS_PATH,
        )
        with patch("scripts.ops.dashboard_server._SUGGESTIONS_PATH", suggestions_path):
            result = _read_suggestions()
            assert len(result) == len(_DEFAULT_SUGGESTIONS)
            assert result[0]["id"] == "seed-retry-github"
            # File should now exist
            assert suggestions_path.exists()
            stored = json.loads(suggestions_path.read_text())
            assert len(stored) == len(_DEFAULT_SUGGESTIONS)

    def test_read_suggestions_returns_existing(self, suggestions_path, sample_suggestions):
        """When suggestions.json exists, returns its contents."""
        from scripts.ops.dashboard_server import _read_suggestions
        suggestions_path.write_text(json.dumps(sample_suggestions))
        with patch("scripts.ops.dashboard_server._SUGGESTIONS_PATH", suggestions_path):
            result = _read_suggestions()
            assert len(result) == 3
            assert result[0]["id"] == "sug-001"

    def test_read_suggestions_returns_empty_for_corrupt_data(self, suggestions_path):
        """Non-list data in file returns empty list."""
        from scripts.ops.dashboard_server import _read_suggestions
        suggestions_path.write_text(json.dumps({"not": "a list"}))
        with patch("scripts.ops.dashboard_server._SUGGESTIONS_PATH", suggestions_path):
            result = _read_suggestions()
            assert result == []

    def test_find_suggestion(self, sample_suggestions):
        """_find_suggestion finds by ID."""
        from scripts.ops.dashboard_server import _find_suggestion
        assert _find_suggestion("sug-001", sample_suggestions)["title"] == "Add retry logic"
        assert _find_suggestion("nonexistent", sample_suggestions) is None


# ══════════════════════════════════════════════════════════════════════════════
# Tests: GET /api/suggestions
# ══════════════════════════════════════════════════════════════════════════════


class TestListSuggestions:
    def test_list_returns_all_suggestions(self, tmp_store, suggestions_path, sample_suggestions):
        suggestions_path.write_text(json.dumps(sample_suggestions))
        handler = _make_handler(tmp_store, suggestions_path)
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._SUGGESTIONS_PATH", suggestions_path):
            handler._handle_list_suggestions()

        assert len(results) == 1
        data, status = results[0]
        assert status == 200
        assert data["count"] == 3
        assert len(data["suggestions"]) == 3

    def test_list_empty_seeds_defaults(self, tmp_store, suggestions_path):
        handler = _make_handler(tmp_store, suggestions_path)
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._SUGGESTIONS_PATH", suggestions_path):
            handler._handle_list_suggestions()

        data, status = results[0]
        assert status == 200
        assert data["count"] == 3  # seeded defaults
        assert data["suggestions"][0]["id"] == "seed-retry-github"


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/suggestions/<id>/accept
# ══════════════════════════════════════════════════════════════════════════════


class TestAcceptSuggestion:
    def test_accept_creates_ticket(self, tmp_store, suggestions_path, sample_suggestions):
        suggestions_path.write_text(json.dumps(sample_suggestions))
        handler = _make_handler(tmp_store, suggestions_path)
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._SUGGESTIONS_PATH", suggestions_path):
            with patch("scripts.ops.dashboard_server._broadcast_sse_event"):
                handler._handle_suggestion_accept("sug-001")

        assert len(results) == 1
        data, status = results[0]
        assert status == 200
        assert data["status"] == "ok"
        assert data["suggestion_id"] == "sug-001"
        assert "ticket_id" in data

        # Verify ticket was created in store
        tickets = tmp_store.list_all()
        assert len(tickets) == 1
        assert tickets[0].title == "Add retry logic"
        assert "suggestion" in tickets[0].labels
        assert "reliability" in tickets[0].labels
        assert tickets[0].metadata["suggestion_id"] == "sug-001"

        # Verify suggestion status was updated on disk
        updated = json.loads(suggestions_path.read_text())
        sug = next(s for s in updated if s["id"] == "sug-001")
        assert sug["status"] == "accepted"
        assert "accepted_at" in sug
        assert "ticket_id" in sug

    def test_accept_nonexistent_returns_404(self, tmp_store, suggestions_path, sample_suggestions):
        suggestions_path.write_text(json.dumps(sample_suggestions))
        handler = _make_handler(tmp_store, suggestions_path)
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._SUGGESTIONS_PATH", suggestions_path):
            handler._handle_suggestion_accept("nonexistent")

        data, status = results[0]
        assert status == 404
        assert "error" in data

    def test_accept_already_dismissed_returns_400(self, tmp_store, suggestions_path, sample_suggestions):
        suggestions_path.write_text(json.dumps(sample_suggestions))
        handler = _make_handler(tmp_store, suggestions_path)
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._SUGGESTIONS_PATH", suggestions_path):
            handler._handle_suggestion_accept("sug-003")  # already dismissed

        data, status = results[0]
        assert status == 400
        assert "not pending" in data["error"]


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/suggestions/<id>/dismiss
# ══════════════════════════════════════════════════════════════════════════════


class TestDismissSuggestion:
    def test_dismiss_updates_status(self, tmp_store, suggestions_path, sample_suggestions):
        suggestions_path.write_text(json.dumps(sample_suggestions))
        handler = _make_handler(tmp_store, suggestions_path)
        _set_body(handler, {"reason": "Not needed"})
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._SUGGESTIONS_PATH", suggestions_path):
            with patch("scripts.ops.dashboard_server._broadcast_sse_event"):
                handler._handle_suggestion_dismiss("sug-002")

        data, status = results[0]
        assert status == 200
        assert data["status"] == "ok"
        assert data["suggestion_id"] == "sug-002"

        # Verify status on disk
        updated = json.loads(suggestions_path.read_text())
        sug = next(s for s in updated if s["id"] == "sug-002")
        assert sug["status"] == "dismissed"
        assert "dismissed_at" in sug
        assert sug["dismiss_reason"] == "Not needed"

    def test_dismiss_without_reason(self, tmp_store, suggestions_path, sample_suggestions):
        suggestions_path.write_text(json.dumps(sample_suggestions))
        handler = _make_handler(tmp_store, suggestions_path)
        _set_body(handler, {})
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._SUGGESTIONS_PATH", suggestions_path):
            with patch("scripts.ops.dashboard_server._broadcast_sse_event"):
                handler._handle_suggestion_dismiss("sug-001")

        data, status = results[0]
        assert status == 200

        updated = json.loads(suggestions_path.read_text())
        sug = next(s for s in updated if s["id"] == "sug-001")
        assert sug["status"] == "dismissed"
        assert "dismiss_reason" not in sug

    def test_dismiss_nonexistent_returns_404(self, tmp_store, suggestions_path, sample_suggestions):
        suggestions_path.write_text(json.dumps(sample_suggestions))
        handler = _make_handler(tmp_store, suggestions_path)
        _set_body(handler, {})
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._SUGGESTIONS_PATH", suggestions_path):
            handler._handle_suggestion_dismiss("nonexistent")

        data, status = results[0]
        assert status == 404

    def test_dismiss_already_accepted_returns_400(self, tmp_store, suggestions_path):
        accepted_suggestions = [
            {
                "id": "sug-accepted",
                "title": "Already accepted",
                "description": "This was accepted",
                "category": "testing",
                "impact": "low",
                "created_at": "2026-04-08T00:00:00Z",
                "status": "accepted",
            },
        ]
        suggestions_path.write_text(json.dumps(accepted_suggestions))
        handler = _make_handler(tmp_store, suggestions_path)
        _set_body(handler, {})
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._SUGGESTIONS_PATH", suggestions_path):
            handler._handle_suggestion_dismiss("sug-accepted")

        data, status = results[0]
        assert status == 400
        assert "not pending" in data["error"]
