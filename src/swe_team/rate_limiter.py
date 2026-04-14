"""
Rate limit detection and exponential backoff for Claude Code CLI calls.

Provides:
  - ``RateLimitState``: enum for lifecycle states (NORMAL, THROTTLED, COOLDOWN, RECOVERING)
  - ``RateLimitLifecycle``: per-provider rate limit state machine with exponential backoff
  - ``ExponentialBackoff``: retry wrapper with exponential backoff on 429 errors
  - ``RateLimitTracker``: observability tracker for rate limit events
  - ``RateLimitExhausted``: raised when all retries are exhausted
  - ``RateLimitCooldown``: raised to signal an extended cooldown is needed (pause all work)
"""

from __future__ import annotations

import enum
import logging
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Rate Limit Lifecycle State Machine ────────────────────────────────────────


class RateLimitState(enum.Enum):
    """Lifecycle states for a rate-limited API provider.

    Transitions::

        NORMAL ──on_rate_limit_warning()──▶ THROTTLED
        NORMAL ──on_rate_limit_hit()──────▶ COOLDOWN
        THROTTLED ──on_rate_limit_hit()───▶ COOLDOWN
        THROTTLED ──on_request_success()──▶ NORMAL  (after N consecutive successes)
        COOLDOWN ──on_cooldown_expired()──▶ RECOVERING
        RECOVERING ──on_request_success()─▶ NORMAL  (after N consecutive successes)
        RECOVERING ──on_rate_limit_hit()──▶ COOLDOWN  (backoff level increases)
    """

    NORMAL = "normal"
    THROTTLED = "throttled"
    COOLDOWN = "cooldown"
    RECOVERING = "recovering"


class RateLimitLifecycle:
    """Per-provider rate limit lifecycle with exponential backoff.

    Thread-safe state machine that tracks whether a given API provider
    (e.g. ``claude``, ``github``, ``base_llm``) is currently rate-limited
    and manages the cooldown/recovery cycle.

    Parameters
    ----------
    provider:
        Human-readable provider name (used as the key).
    initial_cooldown:
        Base cooldown duration in seconds on first rate limit hit.
    max_cooldown:
        Upper bound on exponential cooldown in seconds.
    recovery_successes:
        Number of consecutive successes required to transition from
        RECOVERING/THROTTLED back to NORMAL.
    """

    def __init__(
        self,
        provider: str,
        *,
        initial_cooldown: float = 30.0,
        max_cooldown: float = 300.0,
        recovery_successes: int = 3,
    ) -> None:
        self.provider = provider
        self.initial_cooldown = initial_cooldown
        self.max_cooldown = max_cooldown
        self.recovery_successes = recovery_successes

        self._lock = threading.Lock()
        self._state = RateLimitState.NORMAL
        self._backoff_level: int = 0
        self._cooldown_until: float = 0.0
        self._consecutive_successes: int = 0
        self._last_transition: float = time.time()
        self._total_hits: int = 0
        self._total_warnings: int = 0

    # ── Public query methods ──────────────────────────────────────────

    @property
    def state(self) -> RateLimitState:
        with self._lock:
            # Auto-expire cooldown
            if self._state == RateLimitState.COOLDOWN and time.time() >= self._cooldown_until:
                self._transition(RateLimitState.RECOVERING)
            return self._state

    def can_proceed(self) -> bool:
        """Return True if requests are currently allowed.

        - NORMAL / THROTTLED / RECOVERING → True
        - COOLDOWN → False (until cooldown expires)
        """
        current = self.state  # triggers auto-expire
        return current != RateLimitState.COOLDOWN

    def get_status(self) -> Dict[str, Any]:
        """Return a JSON-serialisable snapshot of the current lifecycle state."""
        with self._lock:
            now = time.time()
            # Auto-expire inside lock for consistency
            if self._state == RateLimitState.COOLDOWN and now >= self._cooldown_until:
                self._transition(RateLimitState.RECOVERING)
            cooldown_remaining = max(0.0, self._cooldown_until - now) if self._state == RateLimitState.COOLDOWN else 0.0
            return {
                "provider": self.provider,
                "state": self._state.value,
                "backoff_level": self._backoff_level,
                "cooldown_remaining_seconds": round(cooldown_remaining, 1),
                "cooldown_until": (
                    datetime.fromtimestamp(self._cooldown_until, tz=timezone.utc).isoformat()
                    if self._state == RateLimitState.COOLDOWN and self._cooldown_until > 0
                    else None
                ),
                "consecutive_successes": self._consecutive_successes,
                "recovery_target": self.recovery_successes,
                "total_hits": self._total_hits,
                "total_warnings": self._total_warnings,
                "last_transition": datetime.fromtimestamp(self._last_transition, tz=timezone.utc).isoformat(),
            }

    # ── State transition methods ──────────────────────────────────────

    def on_rate_limit_warning(self) -> RateLimitState:
        """A rate limit warning was received (e.g. ``Retry-After`` header, 429 soft).

        NORMAL → THROTTLED.  Other states unchanged (already escalated).
        """
        with self._lock:
            self._total_warnings += 1
            self._consecutive_successes = 0
            if self._state == RateLimitState.NORMAL:
                self._transition(RateLimitState.THROTTLED)
            return self._state

    def on_rate_limit_hit(self) -> RateLimitState:
        """A hard rate limit was hit (429 error, retries exhausted).

        Any state → COOLDOWN with exponential backoff.
        """
        with self._lock:
            self._total_hits += 1
            self._consecutive_successes = 0
            cooldown_seconds = self._calculate_cooldown()
            self._cooldown_until = time.time() + cooldown_seconds
            self._backoff_level += 1
            self._transition(RateLimitState.COOLDOWN)
            logger.warning(
                "Rate limit hit for provider=%s. Entering COOLDOWN for %.1fs "
                "(backoff_level=%d)",
                self.provider,
                cooldown_seconds,
                self._backoff_level,
            )
            return self._state

    def on_request_success(self) -> RateLimitState:
        """A request completed successfully.

        - THROTTLED/RECOVERING: increment consecutive successes;
          transition to NORMAL after ``recovery_successes`` in a row.
        - NORMAL: reset backoff level.
        - COOLDOWN: no change (must wait for expiry).
        """
        with self._lock:
            if self._state == RateLimitState.NORMAL:
                self._backoff_level = 0
                return self._state

            if self._state in (RateLimitState.THROTTLED, RateLimitState.RECOVERING):
                self._consecutive_successes += 1
                if self._consecutive_successes >= self.recovery_successes:
                    self._backoff_level = 0
                    self._consecutive_successes = 0
                    self._transition(RateLimitState.NORMAL)
                    logger.info(
                        "Provider=%s recovered to NORMAL after %d consecutive successes",
                        self.provider,
                        self.recovery_successes,
                    )
            return self._state

    def on_cooldown_expired(self) -> RateLimitState:
        """Manually signal that cooldown has expired.

        Normally cooldown auto-expires via ``state`` / ``can_proceed()``,
        but this method allows explicit transition.

        COOLDOWN → RECOVERING.  No-op if not in COOLDOWN.
        """
        with self._lock:
            if self._state == RateLimitState.COOLDOWN:
                self._transition(RateLimitState.RECOVERING)
            return self._state

    # ── Internal helpers ──────────────────────────────────────────────

    def _calculate_cooldown(self) -> float:
        """Exponential backoff: initial * 2^level, capped at max_cooldown."""
        raw = self.initial_cooldown * (2 ** self._backoff_level)
        return min(raw, self.max_cooldown)

    def _transition(self, new_state: RateLimitState) -> None:
        """Transition to *new_state* (caller must hold ``_lock``)."""
        old = self._state
        self._state = new_state
        self._last_transition = time.time()
        if old != new_state:
            logger.debug(
                "Provider=%s state transition: %s → %s",
                self.provider,
                old.value,
                new_state.value,
            )


# ── Global lifecycle registry ─────────────────────────────────────────────────

_lifecycle_registry: Dict[str, RateLimitLifecycle] = {}
_registry_lock = threading.Lock()


def get_lifecycle(
    provider: str,
    *,
    initial_cooldown: float = 30.0,
    max_cooldown: float = 300.0,
    recovery_successes: int = 3,
) -> RateLimitLifecycle:
    """Get or create a ``RateLimitLifecycle`` for the named provider.

    Thread-safe singleton per provider name.
    """
    with _registry_lock:
        if provider not in _lifecycle_registry:
            _lifecycle_registry[provider] = RateLimitLifecycle(
                provider,
                initial_cooldown=initial_cooldown,
                max_cooldown=max_cooldown,
                recovery_successes=recovery_successes,
            )
        return _lifecycle_registry[provider]


def get_all_lifecycle_statuses() -> List[Dict[str, Any]]:
    """Return the current status of all tracked providers."""
    with _registry_lock:
        providers = list(_lifecycle_registry.values())
    return [p.get_status() for p in providers]


def reset_lifecycle_registry() -> None:
    """Clear all registered lifecycles (primarily for testing)."""
    with _registry_lock:
        _lifecycle_registry.clear()


class RateLimitExhausted(RuntimeError):
    """All retries exhausted on rate limit."""
    pass


class MonthlyLimitExhausted(RateLimitExhausted):
    """Weekly/monthly quota exhausted — retrying is pointless until reset.

    This is distinct from transient 429s: the proxy won't serve ANY requests
    until the billing period resets (could be days). The agent should immediately
    fall back to an alternate engine, not retry with backoff.
    """

    def __init__(self, message: str = "", reset_at: str = ""):
        super().__init__(message)
        self.reset_at = reset_at


class RateLimitCooldown(RuntimeError):
    """Signal that an extended global cooldown is needed.

    Distinct from ``RateLimitExhausted``: this exception tells the runner to
    pause *all* work (not just the current ticket) until the cooldown period
    expires.  Typically raised after ``RateLimitExhausted`` is caught at the
    developer/investigator level.

    Attributes
    ----------
    cooldown_seconds:
        Suggested cooldown duration in seconds.
    cooldown_until:
        Unix timestamp after which work may resume.
    """

    def __init__(
        self,
        message: str = "",
        cooldown_seconds: float = 600,
        *,
        global_pause: bool = True,
        engine_name: str = "",
        status: str = "",
        reset_at: str = "",
        fallback_engine: str = "",
    ) -> None:
        super().__init__(message)
        self.cooldown_seconds = cooldown_seconds
        self.cooldown_until: float = time.time() + cooldown_seconds
        self.global_pause = global_pause
        self.engine_name = engine_name
        self.status = status
        self.reset_at = reset_at
        self.fallback_engine = fallback_engine


class EngineCooldownManager:
    """Manage per-engine cooldown lifecycle, optionally persisted in Supabase."""

    _RATE_LIMIT_STATUSES = frozenset({"rate_limited", "monthly_exhausted", "down", "recovering"})

    def __init__(self, *, store: Optional[object] = None, team_id: str = "default") -> None:
        self._store = store
        self._team_id = team_id

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _parse_ts(value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @staticmethod
    def _to_iso(value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat()

    def _store_available(self) -> bool:
        return hasattr(self._store, "upsert_engine_cooldown") and hasattr(self._store, "get_engine_cooldown")

    def get_status(self, engine_name: str) -> Dict[str, Any]:
        if not engine_name or not self._store_available():
            return {}
        try:
            row = self._store.get_engine_cooldown(engine_name)  # type: ignore[attr-defined]
            return row if isinstance(row, dict) else {}
        except Exception:
            logger.debug("engine cooldown get_status failed for %s", engine_name, exc_info=True)
            return {}

    def list_statuses(self) -> List[Dict[str, Any]]:
        if not hasattr(self._store, "list_engine_cooldowns"):
            return []
        try:
            rows = self._store.list_engine_cooldowns()  # type: ignore[attr-defined]
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
        except Exception:
            logger.debug("engine cooldown list_statuses failed", exc_info=True)
        return []

    def _upsert(
        self,
        *,
        engine_name: str,
        status: str,
        cooldown_until: Optional[datetime] = None,
        reset_at: Optional[datetime] = None,
        next_probe_at: Optional[datetime] = None,
        last_error: str = "",
        fallback_engine: str = "",
    ) -> Dict[str, Any]:
        row = {
            "team_id": self._team_id,
            "engine_name": engine_name,
            "status": status,
            "cooldown_until": self._to_iso(cooldown_until),
            "reset_at": self._to_iso(reset_at),
            "next_probe_at": self._to_iso(next_probe_at),
            "last_error": last_error[:2000] if last_error else "",
            "fallback_engine": fallback_engine,
            "updated_at": self._to_iso(self._now()),
        }
        if self._store_available():
            try:
                self._store.upsert_engine_cooldown(**row)  # type: ignore[attr-defined]
            except Exception:
                logger.debug("engine cooldown upsert failed for %s", engine_name, exc_info=True)
        return row

    def _status_defaults(self, status: str) -> Tuple[int, int]:
        if status == "monthly_exhausted":
            return 3600, 3600
        if status == "down":
            return 300, 300
        if status == "rate_limited":
            return 600, 120
        if status == "recovering":
            return 60, 60
        return 0, 0

    def mark_healthy(self, engine_name: str) -> Dict[str, Any]:
        return self._upsert(engine_name=engine_name, status="healthy")

    def mark_failure(
        self,
        engine_name: str,
        exc: Exception,
        *,
        fallback_engine: str = "",
    ) -> Dict[str, Any]:
        message = str(exc)
        now = self._now()
        status = "rate_limited"
        reset_at: Optional[datetime] = None
        if ExponentialBackoff._is_monthly_exhaustion(exc):
            status = "monthly_exhausted"
            reset_match = re.search(r"reset at (\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})", message, re.IGNORECASE)
            if reset_match:
                parsed = self._parse_ts(reset_match.group(1).replace(" ", "T"))
                if parsed is not None:
                    reset_at = parsed
        elif ExponentialBackoff._is_down_error(exc):
            status = "down"
        cooldown_s, probe_s = self._status_defaults(status)
        cooldown_until = now + timedelta(seconds=cooldown_s)
        if status == "monthly_exhausted" and reset_at and reset_at > now:
            cooldown_until = reset_at
        return self._upsert(
            engine_name=engine_name,
            status=status,
            cooldown_until=cooldown_until,
            reset_at=reset_at,
            next_probe_at=now + timedelta(seconds=probe_s),
            last_error=message,
            fallback_engine=fallback_engine,
        )

    def probe_if_due(self, engine_name: str, probe_fn: Optional[Callable[[], bool]]) -> Dict[str, Any]:
        row = self.get_status(engine_name)
        if not row:
            return {}
        status = str(row.get("status") or "")
        if status not in self._RATE_LIMIT_STATUSES or status == "healthy":
            return row
        now = self._now()
        next_probe_at = self._parse_ts(row.get("next_probe_at"))
        if next_probe_at and now < next_probe_at:
            return row
        if probe_fn is None:
            return row
        try:
            healthy = bool(probe_fn())
        except Exception as exc:
            healthy = False
            row["last_error"] = str(exc)
        if healthy:
            return self._upsert(
                engine_name=engine_name,
                status="recovering",
                cooldown_until=now + timedelta(seconds=60),
                next_probe_at=None,
                last_error="probe_ok",
                fallback_engine=str(row.get("fallback_engine") or ""),
            )
        _, probe_s = self._status_defaults(status if status in self._RATE_LIMIT_STATUSES else "down")
        return self._upsert(
            engine_name=engine_name,
            status=status or "down",
            cooldown_until=now + timedelta(seconds=probe_s),
            reset_at=self._parse_ts(row.get("reset_at")),
            next_probe_at=now + timedelta(seconds=probe_s),
            last_error=str(row.get("last_error") or "probe_failed"),
            fallback_engine=str(row.get("fallback_engine") or ""),
        )

    def should_use_engine(self, engine_name: str, *, probe_fn: Optional[Callable[[], bool]] = None) -> bool:
        row = self.get_status(engine_name)
        if not row:
            return True
        status = str(row.get("status") or "healthy")
        if status == "healthy":
            return True
        if status == "recovering":
            return True
        now = self._now()
        cooldown_until = self._parse_ts(row.get("cooldown_until"))
        if cooldown_until and now >= cooldown_until:
            row = self.probe_if_due(engine_name, probe_fn)
            status = str(row.get("status") or status)
            cooldown_until = self._parse_ts(row.get("cooldown_until"))
            if status in {"healthy", "recovering"}:
                return True
        if cooldown_until and now < cooldown_until:
            self.probe_if_due(engine_name, probe_fn)
            return False
        return status in {"healthy", "recovering"}

    def remaining_cooldown_seconds(self, engine_name: str) -> float:
        row = self.get_status(engine_name)
        if not row:
            return 0.0
        cooldown_until = self._parse_ts(row.get("cooldown_until"))
        if not cooldown_until:
            return 0.0
        return max(0.0, (cooldown_until - self._now()).total_seconds())


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
        max_retries: int = 5,
        initial_delay: float = 60,
        max_delay: float = 900,
        tracker: Optional["RateLimitTracker"] = None,
        engine_name: str = "",
        cooldown_manager: Optional[EngineCooldownManager] = None,
    ) -> None:
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.tracker = tracker
        self.engine_name = engine_name
        self.cooldown_manager = cooldown_manager

    @staticmethod
    def _is_monthly_exhaustion(exc: Exception) -> bool:
        """Return True if the error indicates weekly/monthly quota exhaustion.

        These are permanent until the billing period resets — retrying is
        pointless and wastes resources. The agent should immediately fall
        back to an alternate engine.
        """
        msg = str(exc)
        return any(kw in msg for kw in (
            "Monthly Limit Exhausted",
            "Weekly Limit Exhausted",
            "Weekly/Monthly Limit Exhausted",
            "quota exceeded",
            "billing",
            '"code":"1310"',
        ))

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """Return True if *exc* looks like a rate limit (429) error."""
        msg = str(exc).lower()
        return "rate limit" in msg or "429" in msg

    @staticmethod
    def _is_overloaded_error(exc: Exception) -> bool:
        """Return True if *exc* looks like an overloaded / 529 error."""
        msg = str(exc).lower()
        return "overloaded" in msg or "529" in msg

    @staticmethod
    def _is_server_error(exc: Exception) -> bool:
        """Return True if *exc* looks like a transient 500-class server error.

        Auth errors (401/403) and model-not-found (404) are NOT included here
        because they are non-transient and should be re-raised immediately.
        """
        msg = str(exc).lower()
        return "500" in msg or "server error" in msg or "internal server error" in msg

    @staticmethod
    def _is_down_error(exc: Exception) -> bool:
        """Return True if *exc* looks like API/network unavailability."""
        msg = str(exc).lower()
        return any(kw in msg for kw in (
            "connection refused",
            "timed out",
            "timeout",
            "temporary failure in name resolution",
            "network is unreachable",
            "connection reset",
            "max retries exceeded",
            "service unavailable",
        ))

    def execute(self, func: Callable[[], Any], context: str = "") -> Any:
        """Call *func*, retrying with backoff on transient errors.

        Backs off on:
        - Rate limit / 429 — uses configured ``initial_delay`` / ``max_delay``.
        - Overloaded / 529 — same backoff as rate limit.
        - Server error / 500 — shorter backoff (initial 15 s, max 120 s).

        Full-jitter strategy is used to prevent thundering herd: jitter is
        proportional to the computed backoff (up to 20% of the base delay).

        Non-transient errors (auth, model not found, unknown) are re-raised
        immediately without consuming a retry.

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
            Any non-transient exception is re-raised immediately.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            if self.cooldown_manager and self.engine_name:
                if not self.cooldown_manager.should_use_engine(
                    self.engine_name,
                    probe_fn=None,
                ):
                    remaining = self.cooldown_manager.remaining_cooldown_seconds(self.engine_name)
                    raise RateLimitCooldown(
                        f"Engine {self.engine_name} is in cooldown for {remaining:.0f}s",
                        cooldown_seconds=max(1.0, remaining),
                        global_pause=False,
                        engine_name=self.engine_name,
                    )
            try:
                return func()
            except (RuntimeError, OSError) as exc:
                # Monthly/weekly exhaustion: fail IMMEDIATELY — no retry
                if self._is_monthly_exhaustion(exc):
                    reset_match = re.search(r"reset at (\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})", str(exc))
                    reset_at = reset_match.group(1) if reset_match else "unknown"
                    if self.cooldown_manager and self.engine_name:
                        self.cooldown_manager.mark_failure(self.engine_name, exc)
                    logger.error(
                        "MONTHLY LIMIT EXHAUSTED (context=%s). "
                        "Proxy quota depleted until %s. Failing immediately to trigger fallback.",
                        context or "unknown", reset_at,
                    )
                    raise MonthlyLimitExhausted(
                        f"Monthly/weekly limit exhausted (context={context}). "
                        f"Reset at {reset_at}. Use fallback engine.",
                        reset_at=reset_at,
                    ) from exc
                if self._is_rate_limit_error(exc) or self._is_overloaded_error(exc):
                    # Standard (long) backoff — same treatment as 429
                    last_exc = exc
                    if attempt >= self.max_retries:
                        break
                    base = self.initial_delay * (2 ** attempt)
                    jitter = random.uniform(0, base * 0.2)
                    delay = min(base + jitter, self.max_delay)
                    error_label = "overloaded/529" if self._is_overloaded_error(exc) else "rate limit"
                    logger.warning(
                        "%s hit (attempt %d/%d, context=%s). "
                        "Retrying in %.1fs: %s",
                        error_label,
                        attempt + 1,
                        self.max_retries,
                        context or "unknown",
                        delay,
                        exc,
                    )
                    if self.tracker:
                        self.tracker.record(
                            model=context,
                            context=context,
                            attempt=attempt + 1,
                            wait_seconds=delay,
                        )
                    time.sleep(delay)
                elif self._is_down_error(exc):
                    # API endpoint down/unreachable: short cooldown and probe cadence.
                    last_exc = exc
                    if attempt >= self.max_retries:
                        break
                    base = 30 * (2 ** attempt)
                    jitter = random.uniform(0, base * 0.2)
                    delay = min(base + jitter, 300)
                    logger.warning(
                        "Engine/API down (attempt %d/%d, context=%s). Retrying in %.1fs: %s",
                        attempt + 1,
                        self.max_retries,
                        context or "unknown",
                        delay,
                        exc,
                    )
                    if self.tracker:
                        self.tracker.record(
                            model=context,
                            context=context,
                            attempt=attempt + 1,
                            wait_seconds=delay,
                        )
                    time.sleep(delay)
                elif self._is_server_error(exc):
                    # Shorter backoff for transient 500-class errors
                    last_exc = exc
                    if attempt >= self.max_retries:
                        break
                    base = 15 * (2 ** attempt)
                    jitter = random.uniform(0, base * 0.2)
                    delay = min(base + jitter, 120)
                    logger.warning(
                        "Server error hit (attempt %d/%d, context=%s). "
                        "Retrying in %.1fs: %s",
                        attempt + 1,
                        self.max_retries,
                        context or "unknown",
                        delay,
                        exc,
                    )
                    if self.tracker:
                        self.tracker.record(
                            model=context,
                            context=context,
                            attempt=attempt + 1,
                            wait_seconds=delay,
                        )
                    time.sleep(delay)
                else:
                    # Non-transient error (auth, model not found, unknown) — re-raise immediately
                    raise
        if self.cooldown_manager and self.engine_name and last_exc is not None:
            self.cooldown_manager.mark_failure(self.engine_name, last_exc)

        raise RateLimitExhausted(
            f"Retries exhausted after {self.max_retries} attempts "
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
            except (ValueError, KeyError):
                continue
        return result

    def is_cooling_down(self) -> bool:
        """True if we hit a rate limit in the last 5 minutes."""
        return len(self.recent_events(hours=5 / 60)) > 0


# ── Persistent cooldown lockfile (survives across cron-spawned processes) ────

_COOLDOWN_FILE = Path("data/swe_team/cooldown.lock")


def write_cooldown_lockfile(cooldown_seconds: float) -> None:
    """Write a cooldown lockfile so cron-spawned processes respect cooldown."""
    expiry = time.time() + cooldown_seconds
    _COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _COOLDOWN_FILE.write_text(str(expiry))
    logger.info(
        "Cooldown lockfile written: pausing all work for %.0fs (until %s)",
        cooldown_seconds,
        datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat(),
    )


def check_cooldown_lockfile() -> Optional[float]:
    """Check if a cooldown lockfile exists and hasn't expired.

    Returns remaining seconds if cooldown is active, None otherwise.
    Expired lockfiles are cleaned up automatically.
    """
    if not _COOLDOWN_FILE.exists():
        return None
    try:
        expiry = float(_COOLDOWN_FILE.read_text().strip())
        remaining = expiry - time.time()
        if remaining > 0:
            return remaining
        _COOLDOWN_FILE.unlink(missing_ok=True)
    except (ValueError, OSError):
        _COOLDOWN_FILE.unlink(missing_ok=True)
    return None
