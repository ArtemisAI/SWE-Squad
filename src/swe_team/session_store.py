"""
Session lifecycle store for Claude Code sessions.

Persists session records to data/swe_team/sessions.json so that
investigations and fix attempts can be resumed after crashes or
rate-limit interruptions.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from src.swe_team.models import SWETicket

logger = logging.getLogger(__name__)

# Valid session statuses
_VALID_STATUSES = {"active", "suspended", "completed", "failed", "escalated"}


@dataclass
class SessionRecord:
    """A single Claude Code session tied to a ticket."""

    session_id: str
    ticket_id: str
    agent_type: str  # "investigator" | "developer"
    created_at: float
    last_active: float
    status: str  # "active" | "suspended" | "completed"
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SessionRecord:
        return cls(
            session_id=data["session_id"],
            ticket_id=data["ticket_id"],
            agent_type=data["agent_type"],
            created_at=data["created_at"],
            last_active=data["last_active"],
            status=data["status"],
            metadata=data.get("metadata", {}),
        )


class SessionStore:
    """Persists session records to data/swe_team/sessions.json."""

    _REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent

    def __init__(self, path: str = "data/swe_team/sessions.json") -> None:
        p = Path(path)
        if not p.is_absolute():
            p = self._REPO_ROOT / p
        self._path = p
        self._lock = threading.Lock()
        self._sessions: Dict[str, SessionRecord] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, ticket_id: str, agent_type: str, metadata: Optional[dict] = None) -> SessionRecord:
        """Create a new session record with a UUID4 session ID.

        Claude CLI requires session IDs to be valid UUIDs for ``--session-id``
        and ``--resume``.  We generate a UUID4 up front so the ID can be passed
        directly to the CLI without conversion.
        """
        now = time.time()
        session_id = str(uuid.uuid4())

        merged_metadata: dict = dict(metadata or {})
        if "display_name" not in merged_metadata:
            merged_metadata["display_name"] = self.generate_session_name(ticket_id, agent_type)

        record = SessionRecord(
            session_id=session_id,
            ticket_id=ticket_id,
            agent_type=agent_type,
            created_at=now,
            last_active=now,
            status="active",
            metadata=merged_metadata,
        )
        self._sessions[session_id] = record
        self._save()
        return record

    # ------------------------------------------------------------------
    # #264 — Session naming helpers
    # ------------------------------------------------------------------

    @staticmethod
    def generate_session_name(ticket_id: str, agent_type: str) -> str:
        """Return a human-readable session name.

        Format: ``SWE-<agent_type>-<ticket_id>-<YYYY-MM-DD>``

        Example: ``SWE-investigate-ticket123-2026-03-27``
        """
        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        # Normalise agent_type for readability: "investigator" → "investigate"
        readable_type = agent_type.rstrip("or").rstrip("er") if agent_type else agent_type
        # Simple suffix-strip heuristic; keep the full name if stripping yields empty.
        if not readable_type:
            readable_type = agent_type
        return f"SWE-{readable_type}-{ticket_id}-{date_str}"

    def rename(self, session_id: str, new_name: str) -> None:
        """Update the ``display_name`` in a session's metadata.

        Args:
            session_id: The ID of the session to rename.
            new_name: The new human-readable name to store.

        Raises:
            KeyError: If *session_id* is not found.
        """
        record = self._sessions.get(session_id)
        if record is None:
            raise KeyError(f"Session not found: {session_id}")
        record.metadata["display_name"] = new_name
        record.last_active = time.time()
        self._save()

    def get(self, session_id: str) -> Optional[SessionRecord]:
        """Return a session by ID, or None."""
        return self._sessions.get(session_id)

    def get_by_ticket(self, ticket_id: str) -> List[SessionRecord]:
        """Return all sessions for a given ticket, newest first."""
        results = [
            s for s in self._sessions.values()
            if s.ticket_id == ticket_id
        ]
        results.sort(key=lambda s: s.last_active, reverse=True)
        return results

    def update_status(self, session_id: str, status: str) -> None:
        """Update the status of a session."""
        if status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'; must be one of {_VALID_STATUSES}")
        record = self._sessions.get(session_id)
        if record is None:
            logger.warning("Session not found for status update (may have been created by another process): %s", session_id)
            return
        record.status = status
        record.last_active = time.time()
        self._save()

    def touch(self, session_id: str) -> None:
        """Update last_active timestamp for a session."""
        record = self._sessions.get(session_id)
        if record is None:
            raise KeyError(f"Session not found: {session_id}")
        record.last_active = time.time()
        self._save()

    def update_session_id(self, old_id: str, new_id: str) -> SessionRecord:
        """Replace *old_id* with *new_id* (e.g. the actual Claude CLI session UUID).

        This is used after a Claude CLI run returns the real session UUID in its
        JSON output.  The record is re-keyed under *new_id* so that future
        ``resume`` calls use the correct UUID.
        """
        record = self._sessions.pop(old_id, None)
        if record is None:
            raise KeyError(f"Session not found: {old_id}")
        record.session_id = new_id
        record.last_active = time.time()
        self._sessions[new_id] = record
        self._save()
        return record

    def cleanup_stale(self, max_age_hours: float = 24.0) -> int:
        """Remove sessions older than *max_age_hours*. Returns count removed."""
        cutoff = time.time() - (max_age_hours * 3600)
        stale_ids = [
            sid for sid, rec in self._sessions.items()
            if rec.last_active < cutoff
        ]
        for sid in stale_ids:
            del self._sessions[sid]
        if stale_ids:
            self._save()
        return len(stale_ids)

    def list_active(self) -> List[SessionRecord]:
        """Return all sessions with status 'active', newest first."""
        results = [
            s for s in self._sessions.values()
            if s.status == "active"
        ]
        results.sort(key=lambda s: s.last_active, reverse=True)
        return results

    def list_all(self) -> List[SessionRecord]:
        """Return all sessions, newest first."""
        results = list(self._sessions.values())
        results.sort(key=lambda s: s.last_active, reverse=True)
        return results

    # ------------------------------------------------------------------
    # #265 — Session ID collector helpers
    # ------------------------------------------------------------------

    def find_resumable(self, ticket_id: str) -> Optional[SessionRecord]:
        """Return the most recent *active* or *suspended* session for *ticket_id*.

        Used to resume an interrupted investigation or development attempt
        without creating a duplicate session.

        Returns:
            The most-recently-active resumable session, or ``None`` if there
            is no active/suspended session for this ticket.
        """
        candidates = [
            s for s in self._sessions.values()
            if s.ticket_id == ticket_id and s.status in ("active", "suspended")
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.last_active, reverse=True)
        return candidates[0]

    def find_by_status(self, status: str) -> List[SessionRecord]:
        """Return all sessions with the given *status*, newest first.

        Args:
            status: One of ``active``, ``suspended``, ``completed``,
                ``failed``, or ``escalated``.

        Raises:
            ValueError: If *status* is not a recognised value.

        Returns:
            List of matching :class:`SessionRecord` objects, ordered by
            ``last_active`` descending.
        """
        if status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'; must be one of {_VALID_STATUSES}")
        results = [s for s in self._sessions.values() if s.status == status]
        results.sort(key=lambda s: s.last_active, reverse=True)
        return results

    def mark_for_escalation(self, session_id: str, reason: str) -> None:
        """Mark a session as *escalated* and record the escalation reason.

        Sets ``status`` to ``"escalated"`` and stores *reason* in
        ``metadata["escalation_reason"]``.

        Args:
            session_id: The ID of the session to escalate.
            reason: Human-readable explanation of why it was escalated.

        Raises:
            KeyError: If *session_id* is not found.
        """
        record = self._sessions.get(session_id)
        if record is None:
            raise KeyError(f"Session not found: {session_id}")
        record.status = "escalated"
        record.last_active = time.time()
        record.metadata["escalation_reason"] = reason
        self._save()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            lock_path = self._path.with_suffix(".lock")
            lock_fd = open(lock_path, "w")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_SH)
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for item in data:
                    rec = SessionRecord.from_dict(item)
                    self._sessions[rec.session_id] = rec
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to load sessions from %s: %s", self._path, exc)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [rec.to_dict() for rec in list(self._sessions.values())]
        lock_path = self._path.with_suffix(".lock")
        with self._lock:
            try:
                lock_fd = open(lock_path, "w")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                tmp.replace(self._path)
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# #266 — Structured session header builder
# ---------------------------------------------------------------------------


def build_session_header(session_record: SessionRecord, ticket: "SWETicket") -> str:
    """Build a structured prompt header for a Claude Code session.

    Produces two tagged lines that provide unambiguous context at the start
    of every investigation or development prompt::

        [SESSION] id=swe-investigate-abc123 ticket=TICKET-001 agent=investigator attempt=1
        [CONTEXT] severity=HIGH module=src/swe_team/developer.py

    Args:
        session_record: The :class:`SessionRecord` for the current session.
        ticket: The :class:`~src.swe_team.models.SWETicket` being worked on.

    Returns:
        A two-line string ready to be prepended to the prompt body.
    """
    attempt = session_record.metadata.get("attempt", 1)
    session_line = (
        f"[SESSION] id={session_record.session_id}"
        f" ticket={ticket.ticket_id}"
        f" agent={session_record.agent_type}"
        f" attempt={attempt}"
    )

    # Severity may be an enum or a plain string.
    severity_val = ticket.severity
    if hasattr(severity_val, "value"):
        severity_str = str(severity_val.value).upper()
    else:
        severity_str = str(severity_val).upper()

    module = getattr(ticket, "source_module", "") or ""
    context_line = f"[CONTEXT] severity={severity_str} module={module}"

    return f"{session_line}\n{context_line}"
