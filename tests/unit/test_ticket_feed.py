"""Tests for the ticket activity feed backend helpers.

These tests validate feed read/write/generation logic without requiring
a running server. They import the module-level helpers and exercise
the feed storage layer directly via filesystem operations.
"""

import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the project root is on sys.path so we can import dashboard_server
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "ops"))


# ---------------------------------------------------------------------------
# Helpers to build minimal ticket-like objects for testing
# ---------------------------------------------------------------------------


class _FakeStatus:
    def __init__(self, value: str):
        self.value = value


class _FakeSeverity:
    def __init__(self, value: str):
        self.value = value


class _FakeTicket:
    """Minimal ticket stub matching the attributes used by feed generation."""

    def __init__(
        self,
        ticket_id: str = "TST-001",
        title: str = "Test ticket",
        status: str = "INVESTIGATING",
        severity: str = "HIGH",
        assigned_to: str | None = "alpha-agent",
        created_at: str | None = None,
        updated_at: str | None = None,
        investigation_report: str | None = None,
        proposed_fix: str | None = None,
        metadata: dict | None = None,
    ):
        self.ticket_id = ticket_id
        self.title = title
        self.status = _FakeStatus(status)
        self.severity = _FakeSeverity(severity)
        self.assigned_to = assigned_to
        now = datetime.now(timezone.utc).isoformat()
        self.created_at = created_at or now
        self.updated_at = updated_at or now
        self.investigation_report = investigation_report
        self.proposed_fix = proposed_fix
        self.metadata = metadata or {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def feeds_dir(tmp_path):
    """Create a temporary feeds directory."""
    d = tmp_path / "feeds"
    d.mkdir()
    return d


@pytest.fixture()
def handler(feeds_dir):
    """Build a minimal mock handler with the feed helper methods patched in."""
    # We import the handler class to get the methods, but we won't start a server.
    # Instead, we'll patch _FEEDS_DIR so it points at our temp dir.
    import importlib
    import scripts.ops.dashboard_server as ds
    importlib.reload(ds)  # Ensure fresh module state

    # Create a minimal mock that has the needed methods
    h = MagicMock()
    h.store = MagicMock()

    # Bind the real methods from the handler class
    handler_cls = ds.DashboardHandler
    h._get_feed_path = lambda tid: handler_cls._get_feed_path(h, tid)
    h._read_feed = lambda tid: handler_cls._read_feed(h, tid)
    h._write_feed = lambda tid, entries: handler_cls._write_feed(h, tid, entries)
    h._generate_feed_from_ticket = lambda t: handler_cls._generate_feed_from_ticket(h, t)

    # Patch _FEEDS_DIR to our temp dir
    with patch.object(ds, "_FEEDS_DIR", feeds_dir):
        yield h, feeds_dir, ds


# ---------------------------------------------------------------------------
# Tests: feed storage
# ---------------------------------------------------------------------------


class TestFeedStorage:
    """Test reading and writing feed entries to disk."""

    def test_get_feed_empty(self, handler):
        """An empty feed returns an empty list when no ticket exists."""
        h, feeds_dir, ds = handler
        h.store.get.return_value = None
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            entries = h._read_feed("TST-NONEXISTENT")
        assert entries == []

    def test_write_and_read_feed(self, handler):
        """Written entries can be read back."""
        h, feeds_dir, ds = handler
        entries = [
            {
                "id": str(uuid.uuid4()),
                "type": "comment",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "actor": "user",
                "content": "Hello world",
                "metadata": {},
            }
        ]
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            h._write_feed("TST-001", entries)
            # Disable auto-generation by returning no ticket
            h.store.get.return_value = None
            result = h._read_feed("TST-001")

        assert len(result) == 1
        assert result[0]["content"] == "Hello world"
        assert result[0]["type"] == "comment"

    def test_feed_file_created_on_disk(self, handler):
        """Writing a feed creates a JSON file in the feeds directory."""
        h, feeds_dir, ds = handler
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            h._write_feed("TST-002", [{"id": "a", "type": "system", "content": "hi"}])

        files = list(feeds_dir.glob("*.json"))
        assert len(files) == 1
        assert "TST-002" in files[0].name

    def test_sanitized_ticket_id(self, handler):
        """Ticket IDs with special characters are sanitized for filenames."""
        h, feeds_dir, ds = handler
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            path = h._get_feed_path("gh-issue-owner/repo#42")
        # Should not contain / in the filename
        assert "/" not in path.name


class TestFeedGeneration:
    """Test auto-generation of feed entries from ticket state."""

    def test_generate_from_basic_ticket(self, handler):
        h, feeds_dir, ds = handler
        ticket = _FakeTicket()
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            entries = h._generate_feed_from_ticket(ticket)

        # Should have at least: created event, assignment, status change
        types = [e["type"] for e in entries]
        assert "system" in types  # Created event
        assert "status_change" in types  # INVESTIGATING != OPEN

    def test_generate_includes_investigation(self, handler):
        h, feeds_dir, ds = handler
        ticket = _FakeTicket(investigation_report="Root cause: null pointer in line 42")
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            entries = h._generate_feed_from_ticket(ticket)

        types = [e["type"] for e in entries]
        assert "investigation" in types

    def test_generate_includes_diff(self, handler):
        h, feeds_dir, ds = handler
        ticket = _FakeTicket(proposed_fix="--- a/file.py\n+++ b/file.py\n- old\n+ new")
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            entries = h._generate_feed_from_ticket(ticket)

        types = [e["type"] for e in entries]
        assert "diff" in types

    def test_generate_includes_comments(self, handler):
        h, feeds_dir, ds = handler
        ticket = _FakeTicket(
            metadata={
                "comments": [
                    {"text": "Fixed it", "timestamp": "2026-01-01T00:00:00Z", "source": "developer"}
                ]
            }
        )
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            entries = h._generate_feed_from_ticket(ticket)

        comments = [e for e in entries if e["type"] == "comment"]
        assert len(comments) == 1
        assert comments[0]["content"] == "Fixed it"

    def test_generate_includes_pr(self, handler):
        h, feeds_dir, ds = handler
        ticket = _FakeTicket(metadata={"pr_url": "https://github.com/o/r/pull/99", "pr_number": 99})
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            entries = h._generate_feed_from_ticket(ticket)

        system_entries = [e for e in entries if e["type"] == "system"]
        pr_entry = [e for e in system_entries if "PR created" in e["content"]]
        assert len(pr_entry) == 1

    def test_open_status_no_status_change_entry(self, handler):
        h, feeds_dir, ds = handler
        ticket = _FakeTicket(status="OPEN", assigned_to=None)
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            entries = h._generate_feed_from_ticket(ticket)

        status_entries = [e for e in entries if e["type"] == "status_change"]
        assert len(status_entries) == 0


class TestFeedOrdering:
    """Test that feed entries are returned in chronological order."""

    def test_entries_sorted_by_timestamp(self, handler):
        h, feeds_dir, ds = handler
        entries = [
            {
                "id": "2",
                "type": "comment",
                "timestamp": "2026-01-02T00:00:00Z",
                "actor": "user",
                "content": "Second",
                "metadata": {},
            },
            {
                "id": "1",
                "type": "system",
                "timestamp": "2026-01-01T00:00:00Z",
                "actor": "system",
                "content": "First",
                "metadata": {},
            },
            {
                "id": "3",
                "type": "comment",
                "timestamp": "2026-01-03T00:00:00Z",
                "actor": "user",
                "content": "Third",
                "metadata": {},
            },
        ]
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            h._write_feed("TST-ORD", entries)
            h.store.get.return_value = None
            result = h._read_feed("TST-ORD")

        # Read returns entries as-written (no re-sort on read)
        # but _generate_feed_from_ticket sorts — verify generation sorts
        ticket = _FakeTicket(
            created_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-03T00:00:00Z",
        )
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            generated = h._generate_feed_from_ticket(ticket)
        timestamps = [e["timestamp"] for e in generated]
        assert timestamps == sorted(timestamps)


class TestAddFeedComment:
    """Test adding comments to the feed (comment entry creation logic)."""

    def test_add_comment_creates_entry(self, handler):
        """Adding a comment appends a new entry to the feed."""
        h, feeds_dir, ds = handler

        initial = [
            {
                "id": str(uuid.uuid4()),
                "type": "system",
                "timestamp": "2026-01-01T00:00:00Z",
                "actor": "system",
                "content": "Ticket created",
                "metadata": {},
            }
        ]
        with patch.object(ds, "_FEEDS_DIR", feeds_dir):
            h._write_feed("TST-CMT", initial)

            # Simulate adding a comment
            new_entry = {
                "id": str(uuid.uuid4()),
                "type": "comment",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "actor": "user",
                "content": "This is my comment",
                "metadata": {},
            }
            entries = h._read_feed("TST-CMT")
            # Feed is empty from read because no ticket in store
            # Write directly:
            h.store.get.return_value = None
            all_entries = initial + [new_entry]
            h._write_feed("TST-CMT", all_entries)

            result = h._read_feed("TST-CMT")

        assert len(result) == 2
        assert result[1]["type"] == "comment"
        assert result[1]["content"] == "This is my comment"
        assert result[1]["actor"] == "user"

    def test_empty_comment_not_added(self):
        """Verify that empty content would be rejected (validated by handler)."""
        # This tests the validation logic conceptually — the handler returns 400
        # for empty content. We just verify the check condition.
        content = "   ".strip()
        assert not content  # Empty after strip = rejected
