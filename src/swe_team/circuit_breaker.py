"""
Circuit breaker for SWE-Squad agents.

Tracks rolling failure rates and provides a mechanism to pause processing
when failures exceed a defined threshold.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class CircuitBreaker:
    """Tracks failure rate and manages circuit state."""

    def __init__(
        self,
        state_path: str = "data/swe_team/circuit_breaker.json",
        window_size: int = 10,
        failure_threshold: float = 0.8,
        pause_duration_minutes: int = 30,
    ) -> None:
        self._path = Path(state_path)
        self._window_size = window_size
        self._threshold = failure_threshold
        self._pause_duration = pause_duration_minutes
        
        # Load state
        state = self._load()
        self._results: List[bool] = state.get("results", [])[-window_size:]
        self._paused_until: Optional[str] = state.get("paused_until")

    def _load(self) -> Dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "results": self._results,
                "paused_until": self._paused_until,
                "failure_rate": self.failure_rate,
                "is_paused": self.is_paused,
            }
            self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save circuit breaker state: %s", exc)

    @property
    def failure_rate(self) -> float:
        if not self._results:
            return 0.0
        failures = self._results.count(False)
        return failures / len(self._results)

    @property
    def is_paused(self) -> bool:
        if not self._paused_until:
            return False
        try:
            paused_until = datetime.fromisoformat(self._paused_until)
            if paused_until.tzinfo is None:
                paused_until = paused_until.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < paused_until
        except Exception:
            return False

    def record_result(self, success: bool) -> None:
        """Record a single result (True for success, False for failure)."""
        self._results.append(success)
        if len(self._results) > self._window_size:
            self._results.pop(0)

        # Check if threshold reached
        if len(self._results) >= 5 and self.failure_rate >= self._threshold:
            from datetime import timedelta
            until = datetime.now(timezone.utc) + timedelta(minutes=self._pause_duration)
            self._paused_until = until.isoformat()
            logger.error(
                "Circuit breaker tripped: failure rate %.1f%% (threshold %.1f%%). Pausing for %d min.",
                self.failure_rate * 100, self._threshold * 100, self._pause_duration
            )

        self._save()

    def record_skip(self) -> None:
        """Record a skip — a ticket that was bypassed without a genuine attempt.

        Skips do NOT affect the failure rate. They cover two scenarios:
        1. A ticket whose dev/investigation attempts are already exhausted (3/3 cap).
        2. A rate-limit pause where no attempt was made at all.

        Recording these as failures would inflate the failure rate, causing the
        circuit breaker to trip and creating a death spiral.
        """
        logger.debug("Circuit breaker: skip recorded (not counted toward failure rate).")
        # No change to _results — just log, no save needed unless we want a skip counter.

    def clear_pause(self) -> None:
        """Manually clear the pause state."""
        self._paused_until = None
        self._save()
