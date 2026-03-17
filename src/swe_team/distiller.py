"""
Trajectory distiller for the Autonomous SWE Team.

Stores deterministic fix automations keyed by error fingerprint so repeated
issues can be resolved without re-running LLM investigations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.swe_team.models import SWETicket, TicketStatus

logger = logging.getLogger(__name__)

_DEFAULT_AUTOMATIONS_DIR = Path("data/swe_team/automations")
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass
class AutomationRecord:
    """A deterministic fix automation stored on disk."""

    fingerprint: str
    steps: List[List[str]]
    success_count: int = 0
    failure_count: int = 0
    success_rate: float = 0.0
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "steps": self.steps,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": self.success_rate,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AutomationRecord":
        return cls(
            fingerprint=data["fingerprint"],
            steps=[list(step) for step in data.get("steps", [])],
            success_count=int(data.get("success_count", 0)),
            failure_count=int(data.get("failure_count", 0)),
            success_rate=float(data.get("success_rate", 0.0)),
            updated_at=data.get(
                "updated_at", datetime.now(timezone.utc).isoformat()
            ),
        )


class TrajectoryDistiller:
    """Persist and replay deterministic fixes keyed by fingerprint."""

    def __init__(
        self,
        *,
        automations_dir: Path | str = _DEFAULT_AUTOMATIONS_DIR,
        success_threshold: float = 0.8,
        repo_root: Path | str = ".",
        step_timeout: int = 60,
    ) -> None:
        self._automations_dir = Path(automations_dir)
        self._success_threshold = success_threshold
        self._repo_root = Path(repo_root)
        self._step_timeout = step_timeout
        # File-backed automations are stored locally (not in the DB migration flow).
        self._automations_dir.mkdir(parents=True, exist_ok=True)

    def automation_path(self, fingerprint: str) -> Path:
        """Return the on-disk path for the automation record."""
        safe = self._safe_filename(fingerprint)
        return self._automations_dir / f"{safe}.json"

    def get_automation(self, fingerprint: str) -> Optional[AutomationRecord]:
        """Load an automation record if it exists."""
        path = self.automation_path(fingerprint)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return AutomationRecord.from_dict(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load automation %s: %s", fingerprint, exc)
            return None

    def record_success(
        self, ticket: SWETicket, steps: List[List[str]]
    ) -> Optional[AutomationRecord]:
        """Record a successful fix with deterministic *steps*."""
        fingerprint = ticket.metadata.get("fingerprint")
        if not fingerprint:
            ticket_id = ticket.ticket_id
            logger.info("Skipping automation record for ticket %s (no fingerprint)", ticket_id)
            return None
        record = self.get_automation(fingerprint) or AutomationRecord(
            fingerprint=fingerprint, steps=steps
        )
        record.steps = steps
        record.success_count += 1
        record.updated_at = datetime.now(timezone.utc).isoformat()
        record.success_rate = self._success_rate(record)
        self._save_record(record)
        return record

    def record_patch(
        self, ticket: SWETicket, patch_text: str
    ) -> Optional[AutomationRecord]:
        """Persist a patch and record an automation pointing to it."""
        fingerprint = ticket.metadata.get("fingerprint")
        if not fingerprint or not patch_text.strip():
            return None
        self._automations_dir.mkdir(parents=True, exist_ok=True)
        patch_path = self._automations_dir / f"{self._safe_filename(fingerprint)}.patch"
        patch_path.write_text(patch_text, encoding="utf-8")
        steps = [["git", "apply", str(patch_path)]]
        return self.record_success(ticket, steps)

    def run_automation(self, ticket: SWETicket) -> bool:
        """Run a stored automation if the success threshold is met."""
        fingerprint = ticket.metadata.get("fingerprint")
        if not fingerprint:
            return False
        record = self.get_automation(fingerprint)
        if record is None:
            return False
        if record.success_rate < self._success_threshold:
            return False
        if not record.steps:
            return False

        for step in record.steps:
            try:
                result = subprocess.run(
                    step,
                    cwd=self._repo_root,
                    capture_output=True,
                    text=True,
                    timeout=self._step_timeout,
                )
            except subprocess.TimeoutExpired:
                self._record_failure(record)
                ticket.metadata["automation"] = {
                    "status": "timeout",
                    "fingerprint": fingerprint,
                    "success_rate": record.success_rate,
                }
                return False
            if result.returncode != 0:
                self._record_failure(record)
                ticket.metadata["automation"] = {
                    "status": "failed",
                    "fingerprint": fingerprint,
                    "success_rate": record.success_rate,
                }
                return False

        self._record_success(record)
        ticket.transition(TicketStatus.IN_REVIEW)
        ticket.metadata["automation"] = {
            "status": "applied",
            "fingerprint": fingerprint,
            "success_rate": record.success_rate,
        }
        return True

    def _record_success(self, record: AutomationRecord) -> None:
        record.success_count += 1
        record.updated_at = datetime.now(timezone.utc).isoformat()
        record.success_rate = self._success_rate(record)
        self._save_record(record)

    def _record_failure(self, record: AutomationRecord) -> None:
        record.failure_count += 1
        record.updated_at = datetime.now(timezone.utc).isoformat()
        record.success_rate = self._success_rate(record)
        self._save_record(record)

    def _save_record(self, record: AutomationRecord) -> None:
        self._automations_dir.mkdir(parents=True, exist_ok=True)
        path = self.automation_path(record.fingerprint)
        try:
            path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to save automation %s: %s", record.fingerprint, exc)

    @staticmethod
    def _success_rate(record: AutomationRecord) -> float:
        total = record.success_count + record.failure_count
        if total == 0:
            return 0.0
        return round(record.success_count / total, 2)

    @staticmethod
    def _safe_filename(fingerprint: str) -> str:
        safe = _SAFE_NAME_RE.sub("_", fingerprint)
        if safe and safe.strip("_"):
            return safe
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:8]
        return f"unknown_{digest}"
