"""
JSON-based persistent ticket store for the Autonomous SWE Team.

Provides simple file-backed storage for ``SWETicket`` objects with
fingerprint dedup tracking.  Designed as a lightweight default;
production deployments should migrate to the Supabase PostgreSQL
backend via ``src/database/``.
"""

from __future__ import annotations

import json
import logging
import os
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

    def __init__(self, path: str = "data/swe_team/tickets.json") -> None:
        self._path = Path(path)
        self._tickets: Dict[str, SWETicket] = {}
        self._fingerprints: Set[str] = set()
        self._load()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(self, ticket: SWETicket) -> None:
        """Add or update a ticket."""
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
            for item in data.get("tickets", []):
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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"tickets": [t.to_dict() for t in self._tickets.values()]}
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=2)
            tmp.replace(self._path)
        except OSError as exc:
            logger.error("Failed to save tickets to %s: %s", self._path, exc)
