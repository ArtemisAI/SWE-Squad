"""
Supabase-backed ticket store for the Autonomous SWE Team.

Drop-in replacement for ``TicketStore`` that persists tickets to
Supabase PostgreSQL via the PostgREST API (zero extra dependencies —
uses stdlib ``urllib``).

Each team instance is scoped by ``team_id`` so multiple SWE teams
can share the same Supabase project without overlap.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set

from src.swe_team.models import SWETicket, TicketStatus

logger = logging.getLogger(__name__)

# Statuses considered "closed" for list_open() filtering
_CLOSED_STATUSES = frozenset({
    TicketStatus.RESOLVED.value,
    TicketStatus.CLOSED.value,
    TicketStatus.ACKNOWLEDGED.value,
})


class SupabaseTicketStore:
    """Supabase PostgREST-backed ticket persistence.

    Implements the same public interface as ``TicketStore`` so the
    runner can swap backends transparently.

    Parameters
    ----------
    supabase_url:
        Project URL, e.g. ``https://xyz.supabase.co``.
    supabase_key:
        Anon or service-role key for the project.
    team_id:
        Scoping identifier for this SWE team instance.
    """

    def __init__(
        self,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
        team_id: str = "default",
    ) -> None:
        self._url = (supabase_url or os.environ["SUPABASE_URL"]).rstrip("/")
        self._key = supabase_key or os.environ.get(
            "SUPABASE_ANON_KEY",
            os.environ.get("SUPABASE_KEY", ""),
        )
        self._team_id = team_id
        self._rest = f"{self._url}/rest/v1"
        self._headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        # Fingerprint cache — loaded lazily on first access
        self._fingerprint_cache: Optional[Set[str]] = None

    # ------------------------------------------------------------------
    # Public interface (mirrors TicketStore)
    # ------------------------------------------------------------------

    def add(self, ticket: SWETicket) -> None:
        """Upsert a ticket (insert or update on conflict)."""
        row = self._ticket_to_row(ticket)
        # Capture previous status for audit trail
        existing = self.get(ticket.ticket_id)
        old_status = existing.status.value if existing else None

        headers = dict(self._headers)
        headers["Prefer"] = "resolution=merge-duplicates,return=representation"
        self._request("POST", "/swe_tickets", body=row, extra_headers=headers)

        # Audit trail: log status transition
        new_status = ticket.status.value
        if old_status != new_status:
            self._log_event(
                ticket_id=ticket.ticket_id,
                from_status=old_status,
                to_status=new_status,
                agent=ticket.assigned_to,
            )

        # Update fingerprint cache
        fp = ticket.metadata.get("fingerprint")
        if fp and self._fingerprint_cache is not None:
            self._fingerprint_cache.add(fp)

    def get(self, ticket_id: str) -> Optional[SWETicket]:
        """Return a ticket by ID, or ``None``."""
        params = {
            "ticket_id": f"eq.{ticket_id}",
            "team_id": f"eq.{self._team_id}",
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        if rows:
            return self._row_to_ticket(rows[0])
        return None

    def list_all(self) -> List[SWETicket]:
        """Return all tickets for this team, newest first."""
        params = {
            "team_id": f"eq.{self._team_id}",
            "order": "created_at.desc",
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    def list_by_status(self, status: TicketStatus) -> List[SWETicket]:
        """Return tickets with the given status."""
        params = {
            "team_id": f"eq.{self._team_id}",
            "status": f"eq.{status.value}",
            "order": "created_at.desc",
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    def list_open(self) -> List[SWETicket]:
        """Return all tickets that are not resolved, closed, or acknowledged."""
        params = {
            "team_id": f"eq.{self._team_id}",
            "status": "not.in.(resolved,closed,acknowledged)",
            "order": "created_at.desc",
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    @property
    def known_fingerprints(self) -> Set[str]:
        """Fingerprints of all stored tickets for this team (for dedup)."""
        if self._fingerprint_cache is None:
            self._fingerprint_cache = self._load_fingerprints()
        return set(self._fingerprint_cache)

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    def _log_event(
        self,
        ticket_id: str,
        from_status: Optional[str],
        to_status: str,
        agent: Optional[str] = None,
        note: str = "",
    ) -> None:
        """Insert an audit event for a status transition."""
        row = {
            "ticket_id": ticket_id,
            "team_id": self._team_id,
            "from_status": from_status,
            "to_status": to_status,
            "agent": agent,
            "note": note,
        }
        try:
            self._request("POST", "/swe_ticket_events", body=row)
        except Exception:
            logger.warning(
                "Failed to log audit event for %s: %s -> %s",
                ticket_id, from_status, to_status,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # HTTP helpers (stdlib urllib — zero external deps)
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, str]] = None,
        body: Optional[Any] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Execute a request against the Supabase PostgREST API."""
        url = f"{self._rest}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, safe=".,()!")

        headers = dict(self._headers)
        if extra_headers:
            headers.update(extra_headers)

        data = json.dumps(body).encode() if body else None

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode()
                if raw:
                    return json.loads(raw)
                return None
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode() if exc.fp else ""
            logger.error(
                "Supabase %s %s failed (%d): %s",
                method, path, exc.code, error_body[:500],
            )
            raise
        except urllib.error.URLError as exc:
            logger.error("Supabase connection error: %s", exc.reason)
            raise

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def _ticket_to_row(self, ticket: SWETicket) -> Dict[str, Any]:
        """Convert a SWETicket to a Supabase row dict."""
        d = ticket.to_dict()
        d["team_id"] = self._team_id
        # Ensure JSONB fields are actual dicts/lists, not strings
        for key in ("labels", "related_tickets", "metadata", "test_results"):
            val = d.get(key)
            if isinstance(val, str):
                try:
                    d[key] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    @staticmethod
    def _row_to_ticket(row: Dict[str, Any]) -> SWETicket:
        """Convert a Supabase row back to a SWETicket."""
        # PostgREST returns JSONB as dicts already, but ensure strings
        # round-trip safely for the SWETicket.from_dict() contract.
        return SWETicket.from_dict(row)

    def _load_fingerprints(self) -> Set[str]:
        """Load all known fingerprints from Supabase for this team."""
        params = {
            "team_id": f"eq.{self._team_id}",
            "select": "metadata",
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        fps: Set[str] = set()
        for row in (rows or []):
            meta = row.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    continue
            fp = meta.get("fingerprint")
            if fp:
                fps.add(fp)
        logger.info(
            "Loaded %d fingerprint(s) from Supabase (team=%s)",
            len(fps), self._team_id,
        )
        return fps
