"""
Error-Monitoring Agent for the Autonomous SWE Team.

Scans configured log directories for error patterns, de-duplicates against
recently filed tickets, and creates ``SWETicket`` items for new issues.
Emits ``ISSUE_DETECTED`` events via the A2A event bus.

Designed to be invoked on a schedule (cron / APScheduler) or manually:

    from src.swe_team.monitor_agent import MonitorAgent
    agent = MonitorAgent(config)
    new_tickets = await agent.scan()
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.swe_team.config import MonitorConfig
from src.swe_team.events import SWEEvent, SWEEventType
from src.swe_team.models import SWETicket, TicketSeverity

logger = logging.getLogger(__name__)


def _severity_from_pattern(pattern: str) -> TicketSeverity:
    """Map a log pattern to a default severity."""
    mapping = {
        "CRITICAL": TicketSeverity.CRITICAL,
        "Traceback": TicketSeverity.HIGH,
        "ERROR": TicketSeverity.HIGH,
        "FAILED": TicketSeverity.MEDIUM,
    }
    return mapping.get(pattern, TicketSeverity.MEDIUM)


def _fingerprint(file_path: str, line: str) -> str:
    """Compute a stable fingerprint for a log line.

    Uses the first 120 chars (excluding timestamps) + file path so the
    same recurring error produces the same hash.
    """
    # Strip leading timestamps (e.g. 2025-01-01 12:00:00,000)
    cleaned = re.sub(r"^\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}[,.\d]*\s*", "", line)
    key = f"{file_path}::{cleaned[:120]}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class MonitorAgent:
    """Watches log files and produces tickets for new errors.

    Parameters
    ----------
    config:
        ``MonitorConfig`` controlling directories, patterns, and dedup.
    known_fingerprints:
        Set of fingerprints already filed — the caller is expected to
        persist these across runs (e.g. via the ticket store).
    """

    AGENT_NAME = "swe_monitor"

    def __init__(
        self,
        config: MonitorConfig,
        known_fingerprints: Optional[Set[str]] = None,
    ) -> None:
        self._config = config
        self._known: Set[str] = known_fingerprints or set()
        # Compile patterns once
        self._pattern_re = re.compile(
            "|".join(re.escape(p) for p in config.log_patterns)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> List[SWETicket]:
        """Scan configured directories and return newly-created tickets."""
        if not self._config.enabled:
            logger.debug("MonitorAgent disabled — skipping scan")
            return []

        tickets: List[SWETicket] = []
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self._config.dedup_window_hours
        )

        for log_dir in self._config.log_directories:
            dir_path = Path(log_dir)
            if not dir_path.is_dir():
                logger.debug("Log directory %s does not exist, skipping", log_dir)
                continue
            for log_file in sorted(dir_path.rglob("*.log")):
                new = self._scan_file(log_file, cutoff)
                tickets.extend(new)

        logger.info(
            "MonitorAgent scan complete: %d new ticket(s) from %s dir(s)",
            len(tickets),
            len(self._config.log_directories),
        )
        return tickets

    def build_events(self, tickets: List[SWETicket]) -> List[SWEEvent]:
        """Create ``ISSUE_DETECTED`` events for newly created tickets."""
        events: List[SWEEvent] = []
        for t in tickets:
            events.append(
                SWEEvent.issue_detected(
                    ticket_id=t.ticket_id,
                    source_agent=self.AGENT_NAME,
                    error_summary=t.title,
                    module=t.source_module or "",
                    severity=t.severity.value,
                )
            )
        return events

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _scan_file(
        self, log_file: Path, cutoff: datetime
    ) -> List[SWETicket]:
        """Scan a single log file for matching patterns."""
        tickets: List[SWETicket] = []

        # Skip files older than dedup window (mtime check)
        try:
            mtime = datetime.fromtimestamp(
                log_file.stat().st_mtime, tz=timezone.utc
            )
            if mtime < cutoff:
                return tickets
        except OSError:
            return tickets

        try:
            text = log_file.read_text(errors="replace")
        except OSError as exc:
            logger.warning("Cannot read %s: %s", log_file, exc)
            return tickets

        for line_no, line in enumerate(text.splitlines(), 1):
            match = self._pattern_re.search(line)
            if not match:
                continue
            fp = _fingerprint(str(log_file), line)
            if fp in self._known:
                continue
            self._known.add(fp)

            pattern = match.group()
            severity = _severity_from_pattern(pattern)
            module = _guess_module(str(log_file), line)

            ticket = SWETicket(
                title=f"[{pattern}] {line.strip()[:120]}",
                description=(
                    f"Detected in ``{log_file}`` at line {line_no}.\n\n"
                    f"```\n{line.strip()}\n```"
                ),
                severity=severity,
                source_module=module,
                error_log=line.strip()[:500],
                labels=["auto-detected", pattern.lower()],
                metadata={"file": str(log_file), "line_no": line_no, "fingerprint": fp},
            )
            tickets.append(ticket)

        return tickets


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _guess_module(file_path: str, line: str = "") -> str:
    """Best-effort guess of the source module from path AND log content."""
    # First check path components (original logic)
    parts = Path(file_path).parts
    for known in (
        "scraping", "evaluation", "cv_tailoring", "application",
        "easy_apply", "a2a", "database", "auth", "swe_team",
    ):
        if known in parts:
            return known

    # Then check log content for module signatures
    content = line.lower()
    if any(kw in content for kw in ("scraper", "scraping", "job_scraper", "playwright", "cdp", "selector")):
        return "scraping"
    if any(kw in content for kw in ("apply", "applicant", "recipe", "goose", "submission")):
        return "application"
    if any(kw in content for kw in ("auth", "session", "login", "cookie", "li_at", "chrome")):
        return "auth"
    if any(kw in content for kw in ("evaluat", "scoring", "ko_system", "sbert", "embedding")):
        return "evaluation"
    if any(kw in content for kw in ("enrich", "company_research", "google_jobs")):
        return "scraping"
    if any(kw in content for kw in ("database", "supabase", "asyncpg", "postgresql", "migration")):
        return "database"
    if any(kw in content for kw in ("a2a", "dispatch", "event_handler", "hub")):
        return "a2a"
    if any(kw in content for kw in ("telegram", "notification", "alert")):
        return "notifications"
    if any(kw in content for kw in ("health", "daemon", "monitor")):
        return "infrastructure"

    return "unknown"
