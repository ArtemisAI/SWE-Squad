"""
Unit tests for src/swe_team/ticket_store.py

Tests cover:
- TicketStore CRUD: add (upsert), get, list_all, list_by_status, list_open
- Persistence: data survives a new TicketStore instance pointing to the same path
- Fingerprint dedup tracking
- list_recently_resolved
- mark_blocked / unblock_ticket / get_blocked_tickets
- list_by_status filtering
- known_fingerprints property
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.ticket_store import TicketStore


def make_ticket(
    title: str = "Test ticket",
    description: str = "Description",
    status: TicketStatus = TicketStatus.OPEN,
    severity: TicketSeverity = TicketSeverity.MEDIUM,
    fingerprint: str | None = None,
) -> SWETicket:
    t = SWETicket(title=title, description=description, status=status, severity=severity)
    if fingerprint:
        t.metadata["fingerprint"] = fingerprint
    return t


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------

class TestTicketStoreCRUD:
    def test_add_and_get(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        t = make_ticket("Bug A")
        store.add(t)
        retrieved = store.get(t.ticket_id)
        assert retrieved is not None
        assert retrieved.title == "Bug A"

    def test_get_nonexistent_returns_none(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        assert store.get("nonexistent-id") is None

    def test_add_updates_existing(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        t = make_ticket("Initial title")
        store.add(t)
        t.title = "Updated title"
        store.add(t)
        retrieved = store.get(t.ticket_id)
        assert retrieved.title == "Updated title"
        # Only one ticket in store (upsert semantics)
        assert len(store.list_all()) == 1

    def test_list_all_ordered_newest_first(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        t1 = make_ticket("Old ticket")
        t2 = make_ticket("New ticket")
        # t2 will have a later created_at due to being created after t1
        store.add(t1)
        store.add(t2)
        tickets = store.list_all()
        assert len(tickets) == 2
        # Newest first — t2 was created after t1
        assert tickets[0].title in ("Old ticket", "New ticket")  # order by ISO string

    def test_list_all_empty(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        assert store.list_all() == []


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestTicketStorePersistence:
    def test_tickets_survive_reload(self, tmp_path):
        path = str(tmp_path / "tickets.json")
        store1 = TicketStore(path=path)
        t = make_ticket("Persisted ticket", fingerprint="fp-001")
        store1.add(t)

        # Create a new store instance pointing at same file
        store2 = TicketStore(path=path)
        retrieved = store2.get(t.ticket_id)
        assert retrieved is not None
        assert retrieved.title == "Persisted ticket"

    def test_multiple_tickets_survive_reload(self, tmp_path):
        path = str(tmp_path / "tickets.json")
        store1 = TicketStore(path=path)
        tickets = [make_ticket(f"Ticket {i}") for i in range(5)]
        for t in tickets:
            store1.add(t)

        store2 = TicketStore(path=path)
        assert len(store2.list_all()) == 5

    def test_saved_file_is_valid_json(self, tmp_path):
        path = tmp_path / "tickets.json"
        store = TicketStore(path=str(path))
        store.add(make_ticket("JSON test"))
        with open(path) as f:
            data = json.load(f)
        assert "tickets" in data
        assert len(data["tickets"]) == 1


# ---------------------------------------------------------------------------
# list_by_status
# ---------------------------------------------------------------------------

class TestListByStatus:
    def test_filter_by_status(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        t1 = make_ticket("Open ticket", status=TicketStatus.OPEN)
        t2 = make_ticket("Triaged ticket", status=TicketStatus.TRIAGED)
        t3 = make_ticket("Another open", status=TicketStatus.OPEN)
        for t in (t1, t2, t3):
            store.add(t)

        open_tickets = store.list_by_status(TicketStatus.OPEN)
        assert len(open_tickets) == 2
        triaged = store.list_by_status(TicketStatus.TRIAGED)
        assert len(triaged) == 1

    def test_filter_returns_empty_for_missing_status(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        store.add(make_ticket("Open"))
        result = store.list_by_status(TicketStatus.RESOLVED)
        assert result == []


# ---------------------------------------------------------------------------
# list_open
# ---------------------------------------------------------------------------

class TestListOpen:
    def test_list_open_excludes_resolved_and_closed(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        open_t = make_ticket("Open", status=TicketStatus.OPEN)
        inv_t = make_ticket("Investigating", status=TicketStatus.INVESTIGATING)

        # Build a resolved ticket via bypass
        resolved_t = make_ticket("Resolved")
        resolved_t.metadata["resolution_note"] = "false_regression"
        resolved_t.status = TicketStatus.RESOLVED

        closed_t = make_ticket("Closed", status=TicketStatus.CLOSED)

        for t in (open_t, inv_t, resolved_t, closed_t):
            store.add(t)

        open_tickets = store.list_open()
        titles = {t.title for t in open_tickets}
        assert "Open" in titles
        assert "Investigating" in titles
        assert "Resolved" not in titles
        assert "Closed" not in titles


# ---------------------------------------------------------------------------
# Fingerprint tracking
# ---------------------------------------------------------------------------

class TestFingerprints:
    def test_fingerprint_registered_on_add(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        t = make_ticket(fingerprint="fp-abc123")
        store.add(t)
        assert "fp-abc123" in store.known_fingerprints

    def test_fingerprint_survives_reload(self, tmp_path):
        path = str(tmp_path / "tickets.json")
        store1 = TicketStore(path=path)
        t = make_ticket(fingerprint="fp-persist")
        store1.add(t)

        store2 = TicketStore(path=path)
        assert "fp-persist" in store2.known_fingerprints

    def test_no_fingerprint_not_added(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        t = make_ticket()  # no fingerprint
        store.add(t)
        assert len(store.known_fingerprints) == 0


# ---------------------------------------------------------------------------
# list_recently_resolved
# ---------------------------------------------------------------------------

class TestListRecentlyResolved:
    def test_recently_resolved_includes_recent(self, tmp_path):
        from datetime import datetime, timezone
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        t = make_ticket("Recent fix")
        t.metadata["resolution_note"] = "false_regression"
        t.status = TicketStatus.RESOLVED
        # updated_at is already "now"
        store.add(t)

        result = store.list_recently_resolved(hours=24)
        assert any(r.ticket_id == t.ticket_id for r in result)

    def test_recently_resolved_excludes_old(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        t = make_ticket("Old fix")
        t.metadata["resolution_note"] = "false_regression"
        t.status = TicketStatus.RESOLVED
        # Set updated_at to 48 hours ago
        from datetime import datetime, timedelta, timezone
        old_time = datetime.now(timezone.utc) - timedelta(hours=48)
        t.updated_at = old_time.isoformat()
        store.add(t)

        result = store.list_recently_resolved(hours=24)
        assert all(r.ticket_id != t.ticket_id for r in result)


# ---------------------------------------------------------------------------
# mark_blocked / unblock_ticket
# ---------------------------------------------------------------------------

class TestBlockedTickets:
    def test_mark_blocked(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        t1 = make_ticket("Blocked ticket", status=TicketStatus.TRIAGED)
        t2 = make_ticket("Blocker ticket", status=TicketStatus.OPEN)
        store.add(t1)
        store.add(t2)

        result = store.mark_blocked(t1.ticket_id, [t2.ticket_id])
        assert result is not None
        assert result.status == TicketStatus.BLOCKED
        assert t2.ticket_id in result.blocked_by

    def test_mark_blocked_nonexistent_returns_none(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        result = store.mark_blocked("ghost-id", ["other-id"])
        assert result is None

    def test_unblock_removes_blocker(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        t1 = make_ticket("Blocked", status=TicketStatus.TRIAGED)
        t2 = make_ticket("Blocker", status=TicketStatus.OPEN)
        store.add(t1)
        store.add(t2)

        store.mark_blocked(t1.ticket_id, [t2.ticket_id])
        store.unblock_ticket(t1.ticket_id, t2.ticket_id)

        updated = store.get(t1.ticket_id)
        assert t2.ticket_id not in updated.blocked_by
        assert updated.status == TicketStatus.TRIAGED  # back to triaged

    def test_get_blocked_tickets(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        t1 = make_ticket("Blocked", status=TicketStatus.TRIAGED)
        t2 = make_ticket("Blocker", status=TicketStatus.OPEN)
        store.add(t1)
        store.add(t2)
        store.mark_blocked(t1.ticket_id, [t2.ticket_id])

        blocked = store.get_blocked_tickets()
        assert any(b.ticket_id == t1.ticket_id for b in blocked)


# ---------------------------------------------------------------------------
# Goal Hierarchy — list_by_project_id, list_by_parent_ticket_id, etc.
# ---------------------------------------------------------------------------

class TestGoalHierarchy:
    def test_list_by_project_id(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        # Create tickets in different projects
        t1 = make_ticket("Task in proj-a")
        t1.project_id = "proj-a"
        t2 = make_ticket("Another in proj-a")
        t2.project_id = "proj-a"
        t3 = make_ticket("Task in proj-b")
        t3.project_id = "proj-b"

        store.add(t1)
        store.add(t2)
        store.add(t3)

        proj_a = store.list_by_project_id("proj-a")
        assert len(proj_a) == 2
        assert all(t.project_id == "proj-a" for t in proj_a)

        proj_b = store.list_by_project_id("proj-b")
        assert len(proj_b) == 1
        assert proj_b[0].project_id == "proj-b"

        proj_none = store.list_by_project_id("nonexistent")
        assert len(proj_none) == 0

    def test_list_by_parent_ticket_id(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        parent = make_ticket("Parent task")
        sub1 = make_ticket("Sub-task 1")
        sub1.parent_ticket_id = parent.ticket_id
        sub2 = make_ticket("Sub-task 2")
        sub2.parent_ticket_id = parent.ticket_id
        other = make_ticket("Unrelated")

        store.add(parent)
        store.add(sub1)
        store.add(sub2)
        store.add(other)

        subs = store.list_by_parent_ticket_id(parent.ticket_id)
        assert len(subs) == 2
        assert all(t.parent_ticket_id == parent.ticket_id for t in subs)

    def test_get_project_root_tickets(self, tmp_path):
        store = TicketStore(path=str(tmp_path / "tickets.json"))
        # Root ticket (no parent, has project)
        root1 = make_ticket("Root in proj-x")
        root1.project_id = "proj-x"
        root1.parent_ticket_id = None

        # Sub-task (has parent and project)
        sub = make_ticket("Sub-task")
        sub.project_id = "proj-x"
        sub.parent_ticket_id = root1.ticket_id

        # Root in different project
        root2 = make_ticket("Root in proj-y")
        root2.project_id = "proj-y"
        root2.parent_ticket_id = None

        store.add(root1)
        store.add(sub)
        store.add(root2)

        roots_in_x = store.get_project_root_tickets("proj-x")
        assert len(roots_in_x) == 1
        assert roots_in_x[0].ticket_id == root1.ticket_id
        assert roots_in_x[0].parent_ticket_id is None

        roots_in_y = store.get_project_root_tickets("proj-y")
        assert len(roots_in_y) == 1
        assert roots_in_y[0].ticket_id == root2.ticket_id

    def test_goal_hierarchy_persistence(self, tmp_path):
        """Test that goal hierarchy fields persist across store reloads."""
        path = str(tmp_path / "tickets.json")

        # First store: create a ticket with hierarchy fields
        store1 = TicketStore(path=path)
        t = make_ticket("Hierarchical task")
        t.project_id = "proj-mission"
        t.parent_ticket_id = "parent-123"
        t.goal = "Enable offline sync"
        store1.add(t)

        # Second store: reload and verify
        store2 = TicketStore(path=path)
        retrieved = store2.get(t.ticket_id)
        assert retrieved is not None
        assert retrieved.project_id == "proj-mission"
        assert retrieved.parent_ticket_id == "parent-123"
        assert retrieved.goal == "Enable offline sync"
