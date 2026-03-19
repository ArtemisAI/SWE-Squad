"""
Rate limit detection and exponential backoff for Claude Code CLI calls.

Provides:
  - ``ExponentialBackoff``: retry wrapper with exponential backoff on 429 errors
  - ``RateLimitTracker``: observability tracker for rate limit events
  - ``RateLimitExhausted``: raised when all retries are exhausted
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class RateLimitExhausted(RuntimeError):
    """All retries exhausted on rate limit."""
    pass


class ExponentialBackoff:
    """Retry with exponential backoff on rate limit (429) errors.

    Parameters
    ----------
    max_retries:
        Maximum number of retry attempts before raising ``RateLimitExhausted``.
    initial_delay:
        Base delay in seconds before the first retry.
    max_delay:
        Upper bound on the backoff delay in seconds.
    tracker:
        Optional ``RateLimitTracker`` instance for recording events.
    """

    def __init__(
        self,
        max_retries: int = 3,
        initial_delay: float = 30,
        max_delay: float = 300,
        tracker: Optional["RateLimitTracker"] = None,
    ) -> None:
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.tracker = tracker

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """Return True if *exc* looks like a rate limit error."""
        msg = str(exc).lower()
        return "rate limit" in msg or "429" in msg

    def execute(self, func: Callable[[], Any], model: str = "", context: str = "") -> Any:
        """Call *func*, retrying with backoff on rate limit errors.

        Parameters
        ----------
        func:
            A zero-argument callable to invoke.
        context:
            Human-readable label for logging (e.g. model name or ticket ID).

        Returns
        -------
        Any
            Whatever *func* returns on success.

        Raises
        ------
        RateLimitExhausted
            If all retries are exhausted.
        Exception
            Any non-rate-limit exception is re-raised immediately.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                return func()
            except (RuntimeError, OSError) as exc:
                if not self._is_rate_limit_error(exc):
                    raise
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                # Calculate backoff: initial_delay * 2^attempt + jitter
                delay = min(
                    self.initial_delay * (2 ** attempt) + random.uniform(0, 5),
                    self.max_delay,
                )
                logger.warning(
                    "Rate limit hit (attempt %d/%d, context=%s). "
                    "Retrying in %.1fs: %s",
                    attempt + 1,
                    self.max_retries,
                    context or "unknown",
                    delay,
                    exc,
                )
                if self.tracker:
                    self.tracker.record(
                        model=model or context,
                        context=context,
                        attempt=attempt + 1,
                        wait_seconds=delay,
                    )
                time.sleep(delay)

        raise RateLimitExhausted(
            f"Rate limit exhausted after {self.max_retries} retries "
            f"(context={context}): {last_exc}"
        )


class RateLimitTracker:
    """Track rate limit events for observability.

    Records timestamped events and provides helpers for querying
    recent activity and cooldown status.
    """

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def record(
        self,
        model: str,
        context: str,
        attempt: int,
        wait_seconds: float,
    ) -> None:
        """Record a rate limit event."""
        self.events.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "context": context,
            "attempt": attempt,
            "wait_seconds": round(wait_seconds, 2),
        })

    def recent_events(self, hours: float = 1) -> List[Dict[str, Any]]:
        """Return events from the last *hours* hours."""
        now = datetime.now(timezone.utc)
        result: List[Dict[str, Any]] = []
        for event in self.events:
            try:
                ts = datetime.fromisoformat(event["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                elapsed_hours = (now - ts).total_seconds() / 3600
                if elapsed_hours <= hours:
                    result.append(event)
            except (ValueError, KeyError, TypeError):
                continue
        return result

    def is_cooling_down(self) -> bool:
        """True if we hit a rate limit in the last 5 minutes."""
        return len(self.recent_events(hours=5 / 60)) > 0
