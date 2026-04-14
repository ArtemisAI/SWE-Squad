"""
JSON-based persistent ticket store for the Autonomous SWE Team.

Provides simple file-backed storage for ``SWETicket`` objects with
fingerprint dedup tracking.  Designed as a lightweight default;
production deployments should migrate to the Supabase PostgreSQL
backend via ``src/database/``.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from src.swe_team.models import SWETicket, TicketStatus

logger = logging.getLogger(__name__)


class TicketStore:
    """File-backed ticket persistence.

    Parameters
    ----------
    path:
        JSON file path for ticket storage.
    """

    # Repo root is two levels above this file: src/swe_team/ticket_store.py
    _REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent

    def __init__(self, path: str = "data/swe_team/tickets.json") -> None:
        p = Path(path)
        # If the caller supplied a relative path, resolve it against the repo root
        # so the store works correctly even when CWD is a worktree subdirectory.
        if not p.is_absolute():
            p = self._REPO_ROOT / p
        self._path = p
        self._tickets: Dict[str, SWETicket] = {}
        self._fingerprints: Set[str] = set()
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(self, ticket: SWETicket) -> None:
        """Add or update a ticket."""
        with self._lock:
            self._tickets[ticket.ticket_id] = ticket
            fp = ticket.metadata.get("fingerprint")
            if fp:
                self._fingerprints.add(fp)
            self._save()

    def get(self, ticket_id: str) -> Optional[SWETicket]:
        """Return a ticket by ID, or ``None``."""
        return self._tickets.get(ticket_id)

    def list_all(self) -> List[SWETicket]:
        """Return all tickets ordered by creation time (newest first)."""
        return sorted(
            self._tickets.values(),
            key=lambda t: t.created_at,
            reverse=True,
        )

    def list_by_status(self, status: TicketStatus) -> List[SWETicket]:
        """Return tickets with the given status."""
        return [t for t in self._tickets.values() if t.status == status]

    def list_open(self) -> List[SWETicket]:
        """Return all tickets that are not resolved or closed."""
        closed = {
            TicketStatus.RESOLVED,
            TicketStatus.CLOSED,
            TicketStatus.ACKNOWLEDGED,
        }
        return [t for t in self._tickets.values() if t.status not in closed]

    def list_recently_resolved(self, hours: int = 24) -> List[SWETicket]:
        """Return tickets resolved within the last *hours* hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result: List[SWETicket] = []
        for t in self._tickets.values():
            if t.status != TicketStatus.RESOLVED:
                continue
            try:
                updated = datetime.fromisoformat(t.updated_at)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if updated >= cutoff:
                result.append(t)
        return sorted(result, key=lambda t: t.updated_at, reverse=True)

    def mark_blocked(self, ticket_id: str, blocked_by_ids: List[str]) -> Optional[SWETicket]:
        """Mark a ticket as blocked by the given ticket IDs."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if not ticket:
                return None
            for bid in blocked_by_ids:
                if bid not in ticket.blocked_by:
                    ticket.blocked_by.append(bid)
                blocker = self._tickets.get(bid)
                if blocker and ticket_id not in blocker.blocking:
                    blocker.blocking.append(ticket_id)
            ticket.transition(TicketStatus.BLOCKED)
            self._save()
        return ticket

    def unblock_ticket(self, ticket_id: str, resolved_id: str) -> Optional[SWETicket]:
        """Remove *resolved_id* from ticket's blocked_by list.

        If no blockers remain, transitions back to TRIAGED.
        """
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if not ticket:
                return None
            if resolved_id in ticket.blocked_by:
                ticket.blocked_by.remove(resolved_id)
            resolver = self._tickets.get(resolved_id)
            if resolver and ticket_id in resolver.blocking:
                resolver.blocking.remove(ticket_id)
            if not ticket.blocked_by and ticket.status == TicketStatus.BLOCKED:
                ticket.transition(TicketStatus.TRIAGED)
            self._save()
        return ticket

    def get_blocked_tickets(self) -> List[SWETicket]:
        """Return all tickets currently in BLOCKED status."""
        return [t for t in self._tickets.values() if t.status == TicketStatus.BLOCKED]

    def list_by_project_id(self, project_id: str) -> List[SWETicket]:
        """Return all tickets belonging to a project (goal hierarchy)."""
        return [
            t for t in self._tickets.values()
            if t.project_id == project_id
        ]

    def list_by_parent_ticket_id(self, parent_ticket_id: str) -> List[SWETicket]:
        """Return all sub-tasks (tickets with the given parent_ticket_id)."""
        return [
            t for t in self._tickets.values()
            if t.parent_ticket_id == parent_ticket_id
        ]

    def get_project_root_tickets(self, project_id: str) -> List[SWETicket]:
        """Return all root-level (no parent) tickets in a project."""
        return [
            t for t in self._tickets.values()
            if t.project_id == project_id and t.parent_ticket_id is None
        ]

    @property
    def known_fingerprints(self) -> Set[str]:
        """Fingerprints of all stored tickets (for dedup)."""
        return set(self._fingerprints)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            with open(self._path) as fh:
                data = json.load(fh)
            # Support both {"tickets": [...]} and bare [] formats
            items = data.get("tickets", []) if isinstance(data, dict) else data
            for item in items:
                t = SWETicket.from_dict(item)
                self._tickets[t.ticket_id] = t
                fp = t.metadata.get("fingerprint")
                if fp:
                    self._fingerprints.add(fp)
            logger.info(
                "Loaded %d ticket(s) from %s", len(self._tickets), self._path
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load tickets from %s: %s", self._path, exc)

    def _save(self) -> None:
        """Write tickets to disk atomically with a cross-process file lock.

        Uses an advisory lock file so concurrent processes/threads that each
        create their own TicketStore instance still serialise writes correctly.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(".lock")
        try:
            with open(lock_path, "w") as lock_fh:
                fcntl.flock(lock_fh, fcntl.LOCK_EX)
                try:
                    data = {"tickets": [t.to_dict() for t in self._tickets.values()]}
                    tmp = self._path.with_suffix(".tmp")
                    with open(tmp, "w") as fh:
                        json.dump(data, fh, indent=2)
                    tmp.replace(self._path)
                finally:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
        except OSError as exc:
            logger.error("Failed to save tickets to %s: %s", self._path, exc)
