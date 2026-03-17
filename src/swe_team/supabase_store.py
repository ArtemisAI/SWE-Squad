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
from datetime import datetime, timedelta, timezone
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
        # Track last successful API activity for keep-alive logic.
        # Supabase free tier pauses after 7 days of inactivity.
        self._last_activity: datetime = datetime.now(timezone.utc)

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

    def list_recently_resolved(self, hours: int = 24) -> List[SWETicket]:
        """Return tickets resolved within the last *hours* hours."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        params = {
            "team_id": f"eq.{self._team_id}",
            "status": "eq.resolved",
            "updated_at": f"gte.{cutoff}",
            "order": "updated_at.desc",
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    def store_embedding(self, ticket_id: str, embedding: List[float]) -> None:
        """Persist an embedding vector for an existing ticket."""
        params = {
            "ticket_id": f"eq.{ticket_id}",
            "team_id": f"eq.{self._team_id}",
        }
        self._request(
            "PATCH",
            "/swe_tickets",
            params=params,
            body={"embedding": self._vector_literal(embedding)},
        )

    def find_similar(
        self,
        embedding: List[float],
        *,
        top_k: int = 5,
        similarity_floor: float = 0.75,
        max_age_days: int = 180,
    ) -> List[Dict[str, Any]]:
        """Query the pgvector similarity RPC for resolved/closed matches."""
        payload = {
            "query_embedding": self._vector_literal(embedding),
            "team": self._team_id,
            "match_count": top_k,
            "similarity_floor": similarity_floor,
            "max_age_days": max_age_days,
        }
        rows = self._request("POST", "/rpc/match_similar_tickets", body=payload)
        return rows or []

    def store_embedding_with_dedup(
        self,
        ticket: SWETicket,
        embedding: List[float],
        *,
        dedup_threshold: float = 0.92,
    ) -> str:
        """Store embedding with semantic deduplication and memory merge behavior."""
        matches = self.find_similar(
            embedding,
            top_k=1,
            similarity_floor=dedup_threshold,
        )
        if not matches:
            self.store_embedding(ticket.ticket_id, embedding)
            return "stored"

        candidate_id = str(matches[0].get("ticket_id") or "")
        if not candidate_id or candidate_id == ticket.ticket_id:
            self.store_embedding(ticket.ticket_id, embedding)
            return "stored"

        existing = self.get(candidate_id)
        if not existing:
            self.store_embedding(ticket.ticket_id, embedding)
            return "stored"

        if self._memory_detail_score(existing) >= self._memory_detail_score(ticket):
            return "skipped"

        params = {
            "ticket_id": f"eq.{candidate_id}",
            "team_id": f"eq.{self._team_id}",
        }
        self._request(
            "PATCH",
            "/swe_tickets",
            params=params,
            body={
                "investigation_report": ticket.investigation_report,
                "proposed_fix": ticket.proposed_fix,
                "embedding": self._vector_literal(embedding),
            },
        )
        return "merged"

    def record_memory_hit(self, ticket_id: str, team_id: Optional[str] = None) -> None:
        """Increment confidence for a memory ticket that was used."""
        self._request(
            "POST",
            "/rpc/increment_memory_confidence",
            body={
                "p_ticket_id": ticket_id,
                "p_team": team_id or self._team_id,
            },
        )

    @property
    def known_fingerprints(self) -> Set[str]:
        """Fingerprints of all stored tickets for this team (for dedup)."""
        if self._fingerprint_cache is None:
            self._fingerprint_cache = self._load_fingerprints()
        return set(self._fingerprint_cache)

    # ------------------------------------------------------------------
    # Keep-alive (prevents Supabase free-tier pause after 7 days)
    # ------------------------------------------------------------------

    def keep_alive(self, threshold_days: int = 5) -> bool:
        """Ping Supabase if no organic activity within *threshold_days*.

        Supabase free-tier pauses the database after 7 consecutive days
        of inactivity.  This method checks the internal activity tracker
        and, only when needed, issues a lightweight ``SELECT`` to count
        as activity.

        Returns ``True`` if a keep-alive ping was sent, ``False`` if
        organic traffic was recent enough to skip it.
        """
        age = datetime.now(timezone.utc) - self._last_activity
        if age < timedelta(days=threshold_days):
            logger.info(
                "Supabase keep-alive: skipped (last activity %s ago)",
                age,
            )
            return False

        logger.info(
            "Supabase keep-alive: pinging (last activity %s ago, "
            "threshold=%d days)",
            age,
            threshold_days,
        )
        try:
            self._request(
                "GET",
                "/swe_tickets",
                params={"select": "ticket_id", "limit": "1"},
            )
            logger.info("Supabase keep-alive: ping successful")
            return True
        except Exception:
            logger.warning(
                "Supabase keep-alive: ping failed", exc_info=True,
            )
            return False

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
                self._last_activity = datetime.now(timezone.utc)
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

    @staticmethod
    def _vector_literal(embedding: List[float]) -> str:
        """Convert a list of floats into pgvector text literal format."""
        return "[" + ",".join(str(float(v)) for v in embedding) + "]"

    @staticmethod
    def _memory_detail_score(ticket: SWETicket) -> tuple[int, int]:
        """Rank memory richness by populated fields and text detail."""
        report = (ticket.investigation_report or "").strip()
        fix = (ticket.proposed_fix or "").strip()
        populated = int(bool(report)) + int(bool(fix))
        return populated, len(report) + len(fix)
