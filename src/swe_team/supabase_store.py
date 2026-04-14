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
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.swe_team.models import EngineHandover, SWETicket, TicketStatus
from src.swe_team.providers.notification.base import NotificationProvider
from src.swe_team.workflow.models import WorkflowDefinition

logger = logging.getLogger(__name__)

# Threshold for consecutive keepalive failures before a Telegram alert is sent.
_KEEPALIVE_ALERT_THRESHOLD = 3

# Statuses considered "closed" for list_open() filtering
_CLOSED_STATUSES = frozenset({
    TicketStatus.RESOLVED.value,
    TicketStatus.CLOSED.value,
    TicketStatus.ACKNOWLEDGED.value,
    TicketStatus.FAILED.value,   # exhausted all dev attempts
    TicketStatus.BLOCKED.value,  # no files changed on all attempts
})

# File-based activity tracking for keepalive persistence across processes.
# The in-memory _last_activity timestamp resets to now() on every process
# start, which means keepalive checks always see "0 seconds ago" and skip.
# This file persists the last successful API call timestamp to disk.
_ACTIVITY_FILE_DEFAULT = Path("data/swe_team/supabase_last_activity.json")


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
    activity_file:
        Path to the file used to persist the last API activity timestamp
        across process restarts.  Defaults to
        ``data/swe_team/supabase_last_activity.json``.
    """

    def __init__(
        self,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
        team_id: str = "default",
        activity_file: Optional[Path] = None,
        notifier: Optional[NotificationProvider] = None,
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
        # Write-failure alerting counters (in-memory; reset on process restart)
        self._consecutive_write_failures: int = 0
        self._consecutive_keepalive_failures: int = 0
        self._write_errors_today: int = 0
        self._last_successful_write: Optional[datetime] = None
        # File-based activity tracking — persists across process restarts.
        # Falls back to datetime.min if the file cannot be read, which
        # guarantees the keepalive will fire on the first invocation.
        self._activity_file: Path = activity_file or _ACTIVITY_FILE_DEFAULT
        self._last_activity: datetime = self._load_last_activity()
        self._notifier: Optional[NotificationProvider] = notifier

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

    def get_workflow_definition(self, team_id: Optional[str] = None) -> Optional[WorkflowDefinition]:
        """Return the active workflow definition for a team, if configured."""
        scoped_team = team_id or self._team_id
        params = {
            "team_id": f"eq.{scoped_team}",
            "is_active": "eq.true",
            "order": "updated_at.desc",
            "limit": "1",
        }
        try:
            rows = self._request("GET", "/swe_workflows", params=params)
        except Exception:
            return None
        if not rows:
            return None
        row = rows[0]
        payload: Dict[str, Any]
        definition = row.get("definition")
        if isinstance(definition, dict):
            payload = dict(definition)
        else:
            payload = {
                "id": row.get("id"),
                "name": row.get("name", "Workflow"),
                "team_id": row.get("team_id", scoped_team),
                "nodes": row.get("nodes", []),
                "connections": row.get("connections", []),
                "settings": row.get("settings", {}),
                "is_active": row.get("is_active", True),
            }
        if not payload.get("team_id"):
            payload["team_id"] = scoped_team
        return WorkflowDefinition.from_dict(payload)

    def list_all(self, limit: int = 500) -> List[SWETicket]:
        """Return all tickets for this team, newest first.

        Parameters
        ----------
        limit:
            Maximum number of tickets to return (default 500).  Pass a larger
            value only when you genuinely need the full history.
        """
        params = {
            "team_id": f"eq.{self._team_id}",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    def list_by_status(self, status: TicketStatus, limit: int = 500) -> List[SWETicket]:
        """Return tickets with the given status."""
        params = {
            "team_id": f"eq.{self._team_id}",
            "status": f"eq.{status.value}",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    def list_open(self, limit: int = 500) -> List[SWETicket]:
        """Return all tickets that are not resolved, closed, acknowledged, failed, or blocked."""
        params = {
            "team_id": f"eq.{self._team_id}",
            "status": "not.in.(resolved,closed,acknowledged,failed,blocked)",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    def list_all_teams(self, limit: int = 500) -> List[SWETicket]:
        """Return tickets across ALL teams (for dashboard aggregation)."""
        params = {
            "order": "created_at.desc",
            "limit": str(limit),
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    def list_open_all_teams(self, limit: int = 500) -> List[SWETicket]:
        """Return open tickets across ALL teams."""
        params = {
            "status": "not.in.(resolved,closed,acknowledged,failed,blocked)",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    def list_recently_resolved(self, hours: int = 24, limit: int = 500) -> List[SWETicket]:
        """Return tickets resolved within the last *hours* hours."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        params = {
            "team_id": f"eq.{self._team_id}",
            "status": "eq.resolved",
            "updated_at": f"gte.{cutoff}",
            "order": "updated_at.desc",
            "limit": str(limit),
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    def list_by_project_id(self, project_id: str, limit: int = 500) -> List[SWETicket]:
        """Return all tickets belonging to a project (goal hierarchy)."""
        params = {
            "team_id": f"eq.{self._team_id}",
            "project_id": f"eq.{project_id}",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    def list_by_parent_ticket_id(self, parent_ticket_id: str, limit: int = 500) -> List[SWETicket]:
        """Return all sub-tasks (tickets with the given parent_ticket_id)."""
        params = {
            "team_id": f"eq.{self._team_id}",
            "parent_ticket_id": f"eq.{parent_ticket_id}",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    def get_project_root_tickets(self, project_id: str, limit: int = 500) -> List[SWETicket]:
        """Return all root-level (no parent) tickets in a project."""
        params = {
            "team_id": f"eq.{self._team_id}",
            "project_id": f"eq.{project_id}",
            "parent_ticket_id": "is.null",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        rows = self._request("GET", "/swe_tickets", params=params)
        return [self._row_to_ticket(r) for r in (rows or [])]

    def get_engine_cooldown(self, engine_name: str) -> Optional[Dict[str, Any]]:
        """Return cooldown row for a specific engine in this team."""
        params = {
            "team_id": f"eq.{self._team_id}",
            "engine_name": f"eq.{engine_name}",
            "limit": "1",
        }
        rows = self._request("GET", "/engine_cooldowns", params=params)
        if rows:
            return rows[0]
        return None

    def list_engine_cooldowns(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Return cooldown rows for all engines in this team."""
        params = {
            "team_id": f"eq.{self._team_id}",
            "order": "updated_at.desc",
            "limit": str(limit),
        }
        rows = self._request("GET", "/engine_cooldowns", params=params)
        return [r for r in (rows or []) if isinstance(r, dict)]

    def upsert_engine_cooldown(
        self,
        *,
        team_id: str,
        engine_name: str,
        status: str,
        cooldown_until: Optional[str] = None,
        reset_at: Optional[str] = None,
        next_probe_at: Optional[str] = None,
        last_error: str = "",
        fallback_engine: str = "",
        updated_at: Optional[str] = None,
    ) -> None:
        """Upsert per-engine cooldown state for cross-VM coordination."""
        body = {
            "team_id": team_id,
            "engine_name": engine_name,
            "status": status,
            "cooldown_until": cooldown_until,
            "reset_at": reset_at,
            "next_probe_at": next_probe_at,
            "last_error": last_error,
            "fallback_engine": fallback_engine,
            "updated_at": updated_at,
        }
        headers = dict(self._headers)
        headers["Prefer"] = "resolution=merge-duplicates,return=representation"
        self._request("POST", "/engine_cooldowns", body=body, extra_headers=headers)

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
        """Increment confidence for a memory ticket that was used.

        Failures (DNS errors, network blips) are logged at DEBUG level and
        do NOT trigger the write-failure Telegram alert — a missed confidence
        increment is not a data-loss event.
        """
        try:
            self._request(
                "POST",
                "/rpc/increment_memory_confidence",
                body={
                    "p_ticket_id": ticket_id,
                    "p_team": team_id or self._team_id,
                },
            )
        except Exception as exc:
            logger.debug(
                "record_memory_hit: failed to increment confidence for %s (non-fatal): %s",
                ticket_id, exc,
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
        of inactivity.  This method reads the persisted activity timestamp
        from disk (not an in-memory variable that resets each process) and,
        only when needed, issues a lightweight ``SELECT`` to count as
        activity.

        Returns ``True`` if a keep-alive ping was sent, ``False`` if
        organic traffic was recent enough to skip it.
        """
        # Re-read from disk to get the true last activity across all
        # processes (cron invocations, daemon restarts, etc.).
        last = self._load_last_activity()
        age = datetime.now(timezone.utc) - last
        if age < timedelta(days=threshold_days):
            logger.info(
                "Supabase keep-alive: skipped (last activity %s ago, "
                "from %s)",
                age, self._activity_file,
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
            self._consecutive_keepalive_failures = 0
            return True
        except Exception:
            self._consecutive_keepalive_failures += 1
            logger.warning(
                "Supabase keep-alive: ping failed (consecutive failures: %d)",
                self._consecutive_keepalive_failures,
                exc_info=True,
            )
            if self._consecutive_keepalive_failures >= _KEEPALIVE_ALERT_THRESHOLD:
                self._alert(
                    f"\U0001f534 <b>Supabase keepalive failing</b>\n"
                    f"Consecutive ping failures: {self._consecutive_keepalive_failures}\n"
                    f"Supabase may be paused or unreachable. "
                    f"Ticket writes are at risk of data loss."
                )
            return False

    # ------------------------------------------------------------------
    # Persistent activity tracking (file-based)
    # ------------------------------------------------------------------

    def _load_last_activity(self) -> datetime:
        """Load the last activity timestamp from disk.

        Returns ``datetime.min`` (UTC) if the file does not exist or is
        unreadable, which guarantees the keepalive will fire on the first
        invocation after a fresh install or data wipe.
        """
        try:
            if self._activity_file.exists():
                data = json.loads(self._activity_file.read_text())
                ts = data.get("last_activity")
                if ts:
                    return datetime.fromisoformat(ts)
        except Exception:
            logger.debug(
                "Could not read activity file %s, treating as stale",
                self._activity_file,
                exc_info=True,
            )
        return datetime.min.replace(tzinfo=timezone.utc)

    def _save_last_activity(self, ts: datetime) -> None:
        """Persist the activity timestamp to disk."""
        try:
            self._activity_file.parent.mkdir(parents=True, exist_ok=True)
            self._activity_file.write_text(json.dumps({
                "last_activity": ts.isoformat(),
                "team_id": self._team_id,
            }))
        except Exception:
            logger.debug(
                "Could not write activity file %s",
                self._activity_file,
                exc_info=True,
            )

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

    def log_handover(self, handover: EngineHandover) -> None:
        """Write an engine-handover envelope to the audit trail."""
        note = f"engine_handover:{handover.to_json()}"
        self._log_event(
            ticket_id=handover.task_id,
            from_status=f"phase:{handover.phase}",
            to_status=f"handover:{handover.phase}",
            agent=handover.source_engine,
            note=note,
        )

    # ------------------------------------------------------------------
    # Internal alerting helpers
    # ------------------------------------------------------------------

    def _alert(self, message: str) -> None:
        """Send an alert via the injected NotificationProvider (or legacy fallback).

        Never raises.
        """
        if self._notifier:
            try:
                self._notifier.send_alert(message, level="warning")
            except Exception:  # noqa: BLE001
                logger.debug("NotificationProvider.send_alert failed", exc_info=True)
        else:
            try:
                from src.swe_team.notifier import _send  # noqa: PLC0415
                _send(message)
            except Exception:  # noqa: BLE001
                logger.debug("Could not send Telegram alert", exc_info=True)

    def health_stats(self) -> Dict[str, Any]:
        """Return a dict of Supabase health counters for inclusion in status.json."""
        return {
            "supabase_write_errors_today": self._write_errors_today,
            "supabase_last_successful_write": (
                self._last_successful_write.isoformat()
                if self._last_successful_write
                else None
            ),
            "supabase_consecutive_write_failures": self._consecutive_write_failures,
            "supabase_consecutive_keepalive_failures": self._consecutive_keepalive_failures,
        }

    # ------------------------------------------------------------------
    # HTTP helpers (stdlib urllib — zero external deps)
    # ------------------------------------------------------------------

    # Paths where write failures should NOT send the high-priority Telegram
    # alert. Write counters are still incremented for observability, but the
    # 🚨 alert is suppressed for non-critical advisory RPC calls.
    _NO_WRITE_ALERT_PATHS: frozenset = frozenset({
        "/rpc/increment_memory_confidence",
        "/rpc/match_similar_tickets",  # semantic search is advisory, not data-critical
    })

    # Minimum consecutive write failures before a Telegram alert fires.
    # Parallel workers each have their own SupabaseStore instance, so a single
    # Supabase blip can trigger N alerts (one per worker). Only alert after
    # this many back-to-back failures on the same instance to reduce noise.
    _ALERT_THRESHOLD: int = 3

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
                now = datetime.now(timezone.utc)
                self._last_activity = now
                self._save_last_activity(now)
                # Reset write-failure counter on any successful write request
                if method in ("POST", "PATCH", "PUT", "DELETE"):
                    self._consecutive_write_failures = 0
                    self._last_successful_write = now
                if raw:
                    return json.loads(raw)
                return None
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode() if exc.fp else ""
            logger.error(
                "Supabase %s %s failed (%d): %s",
                method, path, exc.code, error_body[:500],
            )
            # Alert on write failures (4xx/5xx) — these risk data loss
            if method in ("POST", "PATCH", "PUT", "DELETE"):
                self._consecutive_write_failures += 1
                self._write_errors_today += 1
                logger.warning(
                    "Supabase write failure #%d (HTTP %d) on %s %s",
                    self._consecutive_write_failures, exc.code, method, path,
                )
                if (
                    path not in self._NO_WRITE_ALERT_PATHS
                    and self._consecutive_write_failures >= self._ALERT_THRESHOLD
                ):
                    self._alert(
                        f"\U0001f6a8 <b>Supabase write failure</b> (data loss risk)\n"
                        f"Method: {method} {path}\n"
                        f"HTTP status: {exc.code}\n"
                        f"Consecutive failures: {self._consecutive_write_failures}\n"
                        f"Errors today: {self._write_errors_today}\n"
                        f"Detail: {error_body[:300]}"
                    )
            raise
        except urllib.error.URLError as exc:
            if path in self._NO_WRITE_ALERT_PATHS:
                logger.debug(
                    "Supabase connection error on non-critical path %s (non-fatal): %s",
                    path, exc.reason,
                )
            else:
                logger.error("Supabase connection error: %s", exc.reason)
            # Connection errors on writes also count as write failures
            if method in ("POST", "PATCH", "PUT", "DELETE"):
                self._consecutive_write_failures += 1
                self._write_errors_today += 1
                logger.warning(
                    "Supabase write connection error #%d on %s %s: %s",
                    self._consecutive_write_failures, method, path, exc.reason,
                )
                if (
                    path not in self._NO_WRITE_ALERT_PATHS
                    and self._consecutive_write_failures >= self._ALERT_THRESHOLD
                ):
                    self._alert(
                        f"\U0001f6a8 <b>Supabase write connection error</b> (data loss risk)\n"
                        f"Method: {method} {path}\n"
                        f"Reason: {exc.reason}\n"
                        f"Consecutive failures: {self._consecutive_write_failures}\n"
                        f"Errors today: {self._write_errors_today}"
                    )
            raise

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def _ticket_to_row(self, ticket: SWETicket) -> Dict[str, Any]:
        """Convert a SWETicket to a Supabase row dict."""
        d = ticket.to_dict()
        d["team_id"] = self._team_id
        # Strip fields not in Supabase DDL schema to avoid PGRST204 400 errors
        for key in ("ticket_type", "blocked_by", "blocking"):
            d.pop(key, None)
        # Map statuses not in the Supabase check constraint to valid equivalents.
        # 'failed' and 'blocked' are Python-side states; Supabase schema uses
        # 'closed' and 'acknowledged' respectively.
        _status_map = {"failed": "closed", "blocked": "acknowledged"}
        if d.get("status") in _status_map:
            d["status"] = _status_map[d["status"]]
        # Ensure JSONB fields are actual dicts/lists, not strings
        for key in ("labels", "related_tickets", "metadata", "test_results"):
            val = d.get(key)
            if isinstance(val, str):
                try:
                    d[key] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
        # Session ID fields — only include if populated AND columns exist.
        # Gracefully handles Supabase deployments that haven't run the
        # ALTER TABLE migration yet (avoids PGRST204 400 errors).
        if ticket.investigation_session_id is not None:
            d["investigation_session_id"] = ticket.investigation_session_id
        else:
            d.pop("investigation_session_id", None)
        if ticket.development_session_id is not None:
            d["development_session_id"] = ticket.development_session_id
        else:
            d.pop("development_session_id", None)
        return d

    @staticmethod
    def _row_to_ticket(row: Dict[str, Any]) -> SWETicket:
        """Convert a Supabase row back to a SWETicket."""
        # PostgREST returns JSONB as dicts already, but some rows may have
        # metadata stored as a JSON string (e.g. from manual PATCH calls).
        # Normalise to dict before passing to from_dict().
        if isinstance(row.get("metadata"), str):
            try:
                row = dict(row)
                row["metadata"] = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                row = dict(row)
                row["metadata"] = {}
        # Ensure session ID fields are present (graceful for rows predating these columns)
        row = dict(row)
        row.setdefault("investigation_session_id", None)
        row.setdefault("development_session_id", None)
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

    def claim_ticket(self, ticket_id: str, agent_id: str) -> bool:
        """Atomically claim a ticket using Postgres advisory lock via RPC.

        Returns True if claimed, False if another agent holds it.
        Falls back to True on any RPC error to not block local mode.
        """
        try:
            result = self._request(
                "POST",
                "/rpc/claim_ticket",
                body={"p_ticket_id": ticket_id, "p_agent_id": agent_id},
            )
            return bool(result)
        except Exception as exc:
            logger.warning("claim_ticket RPC failed — rejecting claim (fail-closed): %s", exc)
            return False

    def release_ticket(self, ticket_id: str, reset_status: str = "OPEN") -> None:
        """Release a ticket claim. Used by watchdog for stale ticket recovery."""
        try:
            self._request(
                "POST",
                "/rpc/release_ticket",
                body={"p_ticket_id": ticket_id, "p_reset_status": reset_status},
            )
        except Exception as exc:
            logger.warning("release_ticket RPC failed (non-fatal): %s", exc)

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
