"""Unit tests for src/swe_team/goals_api.py — no real network calls."""
from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.swe_team.goals_api import (
    Goal,
    GoalDetail,
    _error_response,
    _get_goal_detail,
    _json_response,
    _list_goals,
    _read_json_body,
    handle_delete,
    handle_get,
    handle_post,
    handle_put,
)
from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus, TicketType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket_store(tmp_path: Path) -> MagicMock:
    """Create a mock ticket store with some sample tickets."""
    from src.swe_team.ticket_store import TicketStore

    store = TicketStore(path=str(tmp_path / "tickets.json"))

    # Add some tickets with goal hierarchy
    root1 = SWETicket(
        title="Root task 1",
        description="Main task",
        severity=TicketSeverity.HIGH,
        ticket_type=TicketType.FEATURE,
        project_id="mobile-app",
        goal="Build mobile app v1",
    )
    store.add(root1)

    sub1 = SWETicket(
        title="Sub task 1",
        description="Sub-task",
        severity=TicketSeverity.MEDIUM,
        ticket_type=TicketType.FEATURE,
        project_id="mobile-app",
        goal="Build mobile app v1",
        parent_ticket_id=root1.ticket_id,
    )
    store.add(sub1)

    resolved = SWETicket(
        title="Completed task",
        description="Already done",
        severity=TicketSeverity.LOW,
        ticket_type=TicketType.BUG,
        project_id="mobile-app",
        goal="Build mobile app v1",
        status=TicketStatus.RESOLVED,
    )
    store.add(resolved)

    # Another project
    web_root = SWETicket(
        title="Web dashboard",
        description="Build web UI",
        severity=TicketSeverity.HIGH,
        ticket_type=TicketType.FEATURE,
        project_id="web-dashboard",
        goal="Create admin dashboard",
    )
    store.add(web_root)

    # Ticket without project
    orphan = SWETicket(
        title="Orphan task",
        description="No project",
        severity=TicketSeverity.LOW,
    )
    store.add(orphan)

    return store


def _make_handler(
    method: str = "GET",
    path: str = "/",
    body: bytes = b"",
    headers: dict | None = None,
) -> MagicMock:
    """Build a minimal fake BaseHTTPRequestHandler."""
    handler = MagicMock()
    handler.path = path
    handler.command = method

    _headers = {"Content-Length": str(len(body))}
    if headers:
        _headers.update(headers)
    handler.headers = MagicMock()
    handler.headers.get = lambda k, d=None: _headers.get(k, d)

    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    return handler


def _json_body(data: Any) -> bytes:
    return json.dumps(data).encode("utf-8")


def _read_wfile(handler: MagicMock) -> Any:
    """Read and decode the JSON written to handler.wfile."""
    handler.wfile.seek(0)
    return json.loads(handler.wfile.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 1. Goal dataclass
# ---------------------------------------------------------------------------

class TestGoal:
    def test_goal_to_dict(self) -> None:
        goal = Goal(
            project_id="test-project",
            goal="Test goal",
            ticket_count=10,
            open_count=5,
            resolved_count=3,
            created_at="2024-01-01T00:00:00Z",
        )
        data = goal.to_dict()
        assert data["project_id"] == "test-project"
        assert data["goal"] == "Test goal"
        assert data["ticket_count"] == 10
        assert data["open_count"] == 5
        assert data["resolved_count"] == 3
        assert data["created_at"] == "2024-01-01T00:00:00Z"

    def test_goal_from_tickets(self) -> None:
        tickets = [
            SWETicket(
                title="Task 1",
                description="First task",
                project_id="proj-1",
                goal="Main goal",
            ),
            SWETicket(
                title="Task 2",
                description="Second task",
                project_id="proj-1",
                goal="Main goal",
                status=TicketStatus.RESOLVED,
            ),
        ]
        goal = Goal.from_tickets("proj-1", tickets)
        assert goal.project_id == "proj-1"
        assert goal.goal == "Main goal"
        assert goal.ticket_count == 2
        assert goal.open_count == 1
        assert goal.resolved_count == 1

    def test_goal_from_empty_tickets(self) -> None:
        goal = Goal.from_tickets("empty-proj", [])
        assert goal.project_id == "empty-proj"
        assert goal.goal is None
        assert goal.ticket_count == 0
        assert goal.open_count == 0
        assert goal.resolved_count == 0


# ---------------------------------------------------------------------------
# 2. GoalDetail dataclass
# ---------------------------------------------------------------------------

class TestGoalDetail:
    def test_goal_detail_to_dict(self) -> None:
        goal = Goal(project_id="test", goal="Test")
        detail = GoalDetail(
            goal=goal,
            root_tickets=[{"id": "1"}],
            all_tickets=[{"id": "1"}, {"id": "2"}],
            sub_tasks={"1": [{"id": "2"}]},
        )
        data = detail.to_dict()
        assert data["goal"]["project_id"] == "test"
        assert len(data["root_tickets"]) == 1
        assert len(data["all_tickets"]) == 2
        assert "1" in data["sub_tasks"]


# ---------------------------------------------------------------------------
# 3. Response helpers
# ---------------------------------------------------------------------------

class TestResponseHelpers:
    def test_json_response_sends_200_by_default(self) -> None:
        handler = _make_handler()
        handler.wfile = BytesIO()
        _json_response(handler, {"ok": True})
        handler.send_response.assert_called_with(200)

    def test_json_response_sends_custom_status(self) -> None:
        handler = _make_handler()
        handler.wfile = BytesIO()
        _json_response(handler, {"created": True}, status=201)
        handler.send_response.assert_called_with(201)

    def test_error_response_sends_400_by_default(self) -> None:
        handler = _make_handler()
        handler.wfile = BytesIO()
        _error_response(handler, "bad request")
        handler.send_response.assert_called_with(400)

    def test_error_response_wraps_in_error_key(self) -> None:
        handler = _make_handler()
        handler.wfile = BytesIO()
        body_written = []
        handler.wfile.write = lambda b: body_written.append(b)
        _error_response(handler, "not found", 404)
        combined = b"".join(body_written)
        data = json.loads(combined.decode())
        assert data["error"] == "not found"

    def test_read_json_body(self) -> None:
        payload = {"project_id": "test", "goal": "Test goal"}
        handler = _make_handler(body=_json_body(payload))
        result = _read_json_body(handler)
        assert result == payload

    def test_read_json_empty_body(self) -> None:
        handler = _make_handler(body=b"", headers={"Content-Length": "0"})
        result = _read_json_body(handler)
        assert result == {}


# ---------------------------------------------------------------------------
# 4. handle_get
# ---------------------------------------------------------------------------

class TestHandleGet:
    def test_list_goals(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        handler = _make_handler(path="/api/goals")
        handler.wfile = BytesIO()
        result = handle_get(handler, store)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert isinstance(data, list)
        assert len(data) >= 2  # mobile-app and web-dashboard
        project_ids = {g["project_id"] for g in data}
        assert "mobile-app" in project_ids
        assert "web-dashboard" in project_ids

    def test_get_goal_detail_found(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        handler = _make_handler(path="/api/goals/mobile-app")
        handler.wfile = BytesIO()
        result = handle_get(handler, store)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert data["goal"]["project_id"] == "mobile-app"
        assert data["goal"]["goal"] == "Build mobile app v1"
        assert data["goal"]["ticket_count"] == 3  # root1, sub1, resolved
        assert data["goal"]["open_count"] == 2
        assert data["goal"]["resolved_count"] == 1
        assert len(data["root_tickets"]) == 2  # root1 and resolved

    def test_get_goal_detail_not_found(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        handler = _make_handler(path="/api/goals/nonexistent")
        handler.wfile = BytesIO()
        result = handle_get(handler, store)
        assert result is True
        handler.send_response.assert_called_with(404)

    def test_get_unrecognised_path_returns_false(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        handler = _make_handler(path="/api/unknown/goals")
        result = handle_get(handler, store)
        assert result is False


# ---------------------------------------------------------------------------
# 5. handle_post
# ---------------------------------------------------------------------------

class TestHandlePost:
    def test_create_goal_success(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        payload = {"project_id": "new-project", "goal": "New project goal"}
        handler = _make_handler(
            path="/api/goals",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        result = handle_post(handler, store)
        assert result is True
        handler.send_response.assert_called_with(201)

        data = _read_wfile(handler)
        assert data["project_id"] == "new-project"
        assert data["goal"] == "New project goal"
        assert "ticket_id" in data
        assert data["status"] == "created"

        # Verify goal exists
        goals = _list_goals(store)
        project_ids = {g.project_id for g in goals}
        assert "new-project" in project_ids

    def test_create_goal_missing_project_id(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        payload = {"goal": "Just a goal"}
        handler = _make_handler(
            path="/api/goals",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        result = handle_post(handler, store)
        assert result is True
        handler.send_response.assert_called_with(400)

    def test_create_goal_already_exists(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        payload = {"project_id": "mobile-app", "goal": "Duplicate"}
        handler = _make_handler(
            path="/api/goals",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        result = handle_post(handler, store)
        assert result is True
        handler.send_response.assert_called_with(409)

    def test_create_goal_invalid_json(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        handler = _make_handler(
            path="/api/goals",
            body=b"not json",
        )
        handler.wfile = BytesIO()
        result = handle_post(handler, store)
        assert result is True
        handler.send_response.assert_called_with(400)

    def test_post_unrecognised_path_returns_false(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        handler = _make_handler(path="/api/goals/unknown")
        result = handle_post(handler, store)
        assert result is False


# ---------------------------------------------------------------------------
# 6. handle_put
# ---------------------------------------------------------------------------

class TestHandlePut:
    def test_update_goal_success(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        payload = {"goal": "Updated goal description"}
        handler = _make_handler(
            path="/api/goals/mobile-app",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        result = handle_put(handler, store)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert data["project_id"] == "mobile-app"
        assert data["goal"] == "Updated goal description"
        assert data["updated_count"] == 3  # All mobile-app tickets

        # Verify all tickets have updated goal
        tickets = store.list_by_project_id("mobile-app")
        for t in tickets:
            assert t.goal == "Updated goal description"

    def test_update_goal_not_found(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        payload = {"goal": "New goal"}
        handler = _make_handler(
            path="/api/goals/nonexistent",
            body=_json_body(payload),
        )
        handler.wfile = BytesIO()
        result = handle_put(handler, store)
        assert result is True
        handler.send_response.assert_called_with(404)

    def test_update_goal_invalid_json(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        handler = _make_handler(
            path="/api/goals/mobile-app",
            body=b"bad json",
        )
        handler.wfile = BytesIO()
        result = handle_put(handler, store)
        assert result is True
        handler.send_response.assert_called_with(400)

    def test_put_unrecognised_path_returns_false(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        handler = _make_handler(path="/api/unknown/something")
        result = handle_put(handler, store)
        assert result is False


# ---------------------------------------------------------------------------
# 7. handle_delete
# ---------------------------------------------------------------------------

class TestHandleDelete:
    def test_delete_goal_success(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        handler = _make_handler(path="/api/goals/mobile-app")
        handler.wfile = BytesIO()
        result = handle_delete(handler, store)
        assert result is True
        handler.send_response.assert_called_with(200)

        data = _read_wfile(handler)
        assert data["project_id"] == "mobile-app"
        assert data["cleared_tickets"] == 3
        assert data["status"] == "deleted"

        # Verify tickets no longer have project_id
        tickets = store.list_by_project_id("mobile-app")
        assert len(tickets) == 0

        # But tickets still exist (just without project_id)
        all_tickets = store.list_all()
        assert len(all_tickets) == 5  # Original 5 tickets still exist

    def test_delete_goal_not_found(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        handler = _make_handler(path="/api/goals/nonexistent")
        handler.wfile = BytesIO()
        result = handle_delete(handler, store)
        assert result is True
        handler.send_response.assert_called_with(404)

    def test_delete_unrecognised_path_returns_false(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        handler = _make_handler(path="/api/goals")
        result = handle_delete(handler, store)
        assert result is False


# ---------------------------------------------------------------------------
# 8. Helper functions
# ---------------------------------------------------------------------------

class TestHelperFunctions:
    def test_list_goals_groups_by_project_id(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        goals = _list_goals(store)
        assert len(goals) == 2  # mobile-app and web-dashboard

        # Verify goal data
        mobile_app = next((g for g in goals if g.project_id == "mobile-app"), None)
        assert mobile_app is not None
        assert mobile_app.goal == "Build mobile app v1"
        assert mobile_app.ticket_count == 3
        assert mobile_app.open_count == 2
        assert mobile_app.resolved_count == 1

        web_dash = next((g for g in goals if g.project_id == "web-dashboard"), None)
        assert web_dash is not None
        assert web_dash.goal == "Create admin dashboard"
        assert web_dash.ticket_count == 1

    def test_get_goal_detail_builds_hierarchy(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        detail = _get_goal_detail(store, "mobile-app")
        assert detail is not None
        assert detail.goal.project_id == "mobile-app"
        assert len(detail.root_tickets) == 2  # root1 and resolved
        assert len(detail.all_tickets) == 3
        assert "sub_tasks" in detail.to_dict()

        # Find the root ticket that has sub-tasks
        root_with_subs = None
        for root in detail.root_tickets:
            if root["ticket_id"] in detail.sub_tasks:
                root_with_subs = root
                break
        assert root_with_subs is not None
        assert len(detail.sub_tasks[root_with_subs["ticket_id"]]) == 1

    def test_get_goal_detail_not_found(self, tmp_path: Path) -> None:
        store = _make_ticket_store(tmp_path)
        detail = _get_goal_detail(store, "nonexistent")
        assert detail is None
