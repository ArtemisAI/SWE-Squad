"""
Goals API module for SWE-Squad WebUI.

Provides CRUD endpoints for managing project goals, which are stored
as goal hierarchy fields on tickets (project_id, parent_ticket_id, goal).

Endpoints:
    GET  /api/goals              — List all goals (projects)
    GET  /api/goals/<project_id> — Get tickets for a specific goal
    POST /api/goals              — Create a new goal
    PUT  /api/goals/<project_id> — Update goal description
    DELETE /api/goals/<project_id> — Delete a goal
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus, TicketType
from src.swe_team.ticket_store import TicketStore

logger = logging.getLogger(__name__)

# Pattern: /api/goals/<project_id>
_GOAL_ID_RE = re.compile(r"^/api/goals/([a-zA-Z0-9_-]+)$")


@dataclass
class Goal:
    """Represents a project goal with ticket hierarchy information."""

    project_id: str
    goal: Optional[str] = None
    ticket_count: int = 0
    open_count: int = 0
    resolved_count: int = 0
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "goal": self.goal,
            "ticket_count": self.ticket_count,
            "open_count": self.open_count,
            "resolved_count": self.resolved_count,
            "created_at": self.created_at,
        }

    @classmethod
    def from_tickets(cls, project_id: str, tickets: List[SWETicket]) -> "Goal":
        """Create a Goal from a list of tickets with the same project_id."""
        if not tickets:
            return cls(project_id=project_id)

        # Get goal description from the first ticket that has it
        goal = None
        earliest_created = None
        for t in tickets:
            if t.goal:
                goal = t.goal
                break
            if earliest_created is None or t.created_at < earliest_created:
                earliest_created = t.created_at

        # Count tickets by status
        ticket_count = len(tickets)
        open_count = sum(
            1 for t in tickets
            if t.status not in (TicketStatus.RESOLVED, TicketStatus.CLOSED)
        )
        resolved_count = sum(
            1 for t in tickets
            if t.status == TicketStatus.RESOLVED
        )

        return cls(
            project_id=project_id,
            goal=goal,
            ticket_count=ticket_count,
            open_count=open_count,
            resolved_count=resolved_count,
            created_at=earliest_created,
        )


@dataclass
class GoalDetail:
    """Detailed view of a goal including its tickets and hierarchy."""

    goal: Goal
    root_tickets: List[Dict[str, Any]] = field(default_factory=list)
    all_tickets: List[Dict[str, Any]] = field(default_factory=list)
    sub_tasks: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal.to_dict(),
            "root_tickets": self.root_tickets,
            "all_tickets": self.all_tickets,
            "sub_tasks": self.sub_tasks,
        }


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _json_response(
    handler: BaseHTTPRequestHandler,
    data: Any,
    status: int = 200,
) -> None:
    """Send a JSON response."""
    try:
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        logger.debug("Client disconnected during response")


def _error_response(
    handler: BaseHTTPRequestHandler,
    message: str,
    status: int = 400,
) -> None:
    """Send a JSON error response."""
    _json_response(handler, {"error": message}, status=status)


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    """Read and parse JSON from the request body."""
    content_length = int(handler.headers.get("Content-Length", 0))
    if content_length == 0:
        return {}
    body = handler.rfile.read(content_length)
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def handle_get(
    handler: BaseHTTPRequestHandler,
    ticket_store: TicketStore,
) -> bool:
    """Handle GET requests for goals. Returns True if handled."""
    path = handler.path

    if path == "/api/goals":
        # List all goals (projects)
        goals = _list_goals(ticket_store)
        _json_response(handler, [g.to_dict() for g in goals])
        return True

    match = _GOAL_ID_RE.match(path)
    if match:
        project_id = match.group(1)
        goal_detail = _get_goal_detail(ticket_store, project_id)
        if goal_detail is None:
            _error_response(handler, f"Goal not found: {project_id}", 404)
        else:
            _json_response(handler, goal_detail.to_dict())
        return True

    return False


def handle_post(
    handler: BaseHTTPRequestHandler,
    ticket_store: TicketStore,
) -> bool:
    """Handle POST requests for goals. Returns True if handled."""
    path = handler.path

    if path == "/api/goals":
        try:
            payload = _read_json_body(handler)
            project_id = payload.get("project_id")
            if not project_id:
                _error_response(handler, "Missing required field: project_id")
                return True

            # Check if goal already exists
            existing_tickets = ticket_store.list_by_project_id(project_id)
            if existing_tickets:
                _error_response(
                    handler, f"Goal already exists: {project_id}", 409
                )
                return True

            # Create a placeholder ticket to establish the goal
            goal_text = payload.get("goal", "")
            placeholder = SWETicket(
                title=f"[Goal] {project_id}",
                description=f"Project goal placeholder: {goal_text}",
                severity=TicketSeverity.LOW,
                ticket_type=TicketType.DOCUMENTATION,
                project_id=project_id,
                goal=goal_text,
            )
            ticket_store.add(placeholder)

            _json_response(handler, {
                "project_id": project_id,
                "goal": goal_text,
                "ticket_id": placeholder.ticket_id,
                "status": "created",
            }, status=201)
        except json.JSONDecodeError:
            _error_response(handler, "Invalid JSON body")
        except Exception as exc:
            logger.exception("Error creating goal")
            _error_response(handler, str(exc), 500)
        return True

    return False


def handle_put(
    handler: BaseHTTPRequestHandler,
    ticket_store: TicketStore,
) -> bool:
    """Handle PUT requests for goals. Returns True if handled."""
    path = handler.path

    match = _GOAL_ID_RE.match(path)
    if match:
        project_id = match.group(1)
        try:
            payload = _read_json_body(handler)
            tickets = ticket_store.list_by_project_id(project_id)

            if not tickets:
                _error_response(handler, f"Goal not found: {project_id}", 404)
                return True

            # Update the goal field on all tickets with this project_id
            goal_text = payload.get("goal")
            if goal_text is not None:
                for ticket in tickets:
                    ticket.goal = goal_text
                    ticket_store.add(ticket)

            _json_response(handler, {
                "project_id": project_id,
                "goal": goal_text,
                "updated_count": len(tickets),
                "status": "updated",
            })
        except json.JSONDecodeError:
            _error_response(handler, "Invalid JSON body")
        except Exception as exc:
            logger.exception("Error updating goal")
            _error_response(handler, str(exc), 500)
        return True

    return False


def handle_delete(
    handler: BaseHTTPRequestHandler,
    ticket_store: TicketStore,
) -> bool:
    """Handle DELETE requests for goals. Returns True if handled."""
    path = handler.path

    match = _GOAL_ID_RE.match(path)
    if match:
        project_id = match.group(1)
        tickets = ticket_store.list_by_project_id(project_id)

        if not tickets:
            _error_response(handler, f"Goal not found: {project_id}", 404)
            return True

        # Clear project_id and goal from all tickets
        for ticket in tickets:
            ticket.project_id = None
            ticket.parent_ticket_id = None
            ticket.goal = None
            ticket_store.add(ticket)

        _json_response(handler, {
            "project_id": project_id,
            "cleared_tickets": len(tickets),
            "status": "deleted",
        })
        return True

    return False


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _list_goals(ticket_store: TicketStore) -> List[Goal]:
    """List all unique goals (projects) from the ticket store."""
    # Get all tickets and group by project_id
    all_tickets = ticket_store.list_all()
    tickets_by_project: Dict[str, List[SWETicket]] = {}

    for ticket in all_tickets:
        if ticket.project_id:
            if ticket.project_id not in tickets_by_project:
                tickets_by_project[ticket.project_id] = []
            tickets_by_project[ticket.project_id].append(ticket)

    # Create Goal objects for each project
    goals = []
    for project_id, tickets in tickets_by_project.items():
        goal = Goal.from_tickets(project_id, tickets)
        goals.append(goal)

    # Sort by created_at (newest first)
    goals.sort(key=lambda g: g.created_at or "", reverse=True)
    return goals


def _get_goal_detail(ticket_store: TicketStore, project_id: str) -> Optional[GoalDetail]:
    """Get detailed information about a specific goal."""
    tickets = ticket_store.list_by_project_id(project_id)
    if not tickets:
        return None

    goal = Goal.from_tickets(project_id, tickets)

    # Get root tickets (no parent)
    root_tickets = ticket_store.get_project_root_tickets(project_id)

    # Build sub-task hierarchy
    sub_tasks: Dict[str, List[Dict[str, Any]]] = {}
    for root in root_tickets:
        sub_tasks[root.ticket_id] = [
            t.to_dict()
            for t in ticket_store.list_by_parent_ticket_id(root.ticket_id)
        ]

    return GoalDetail(
        goal=goal,
        root_tickets=[t.to_dict() for t in root_tickets],
        all_tickets=[t.to_dict() for t in tickets],
        sub_tasks=sub_tasks,
    )
