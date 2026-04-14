"""
In-memory CheckoutProvider — thread-safe, no external dependencies.

Suitable for single-process deployments and testing.  Does NOT provide
cross-process or cross-VM atomicity (use the Supabase provider for that).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional
from uuid import UUID, uuid4

from src.swe_team.providers.checkout.base import (
    CheckoutLock,
    CheckoutMetrics,
)

logger = logging.getLogger(__name__)


class InMemoryCheckoutProvider:
    """Thread-safe in-memory checkout with expiry-based lock release."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._locks: Dict[str, CheckoutLock] = {}
        # Metrics counters
        self._total: int = 0
        self._success: int = 0
        self._failed: int = 0
        self._expired: int = 0
        self._durations: list[float] = []

    @property
    def name(self) -> str:
        return "memory"

    def try_checkout(
        self,
        ticket_id: str,
        team_id: str,
        lock_minutes: int = 60,
    ) -> Optional[UUID]:
        now = datetime.now(timezone.utc)
        run_id = uuid4()
        with self._lock:
            self._total += 1
            existing = self._locks.get(ticket_id)
            if existing is not None and not existing.is_expired():
                self._failed += 1
                logger.info(
                    "Checkout contention: ticket %s locked by %s (expires in %.0fs)",
                    ticket_id,
                    existing.locked_by,
                    existing.seconds_until_expiry(),
                )
                return None
            self._locks[ticket_id] = CheckoutLock(
                run_id=run_id,
                locked_by=team_id,
                locked_at=now,
                expires_at=now + timedelta(minutes=lock_minutes),
            )
            self._success += 1
            logger.debug("Checkout acquired: ticket %s by %s (run=%s)", ticket_id, team_id, run_id)
            return run_id

    def release(self, ticket_id: str, run_id: UUID) -> bool:
        with self._lock:
            existing = self._locks.get(ticket_id)
            if existing is None or existing.run_id != run_id:
                return False
            duration = (datetime.now(timezone.utc) - existing.locked_at).total_seconds()
            self._durations.append(duration)
            del self._locks[ticket_id]
            logger.debug("Checkout released: ticket %s (run=%s, held %.0fs)", ticket_id, run_id, duration)
            return True

    def heartbeat(self, ticket_id: str, run_id: UUID, extend_minutes: int = 60) -> bool:
        with self._lock:
            existing = self._locks.get(ticket_id)
            if existing is None or existing.run_id != run_id:
                return False
            existing.expires_at = datetime.now(timezone.utc) + timedelta(minutes=extend_minutes)
            return True

    def is_locked(self, ticket_id: str) -> bool:
        with self._lock:
            existing = self._locks.get(ticket_id)
            return existing is not None and not existing.is_expired()

    def get_lock_info(self, ticket_id: str) -> Optional[CheckoutLock]:
        with self._lock:
            existing = self._locks.get(ticket_id)
            if existing is None or existing.is_expired():
                return None
            return existing

    def cleanup_expired(self) -> int:
        with self._lock:
            expired_ids = [
                tid for tid, lock in self._locks.items() if lock.is_expired()
            ]
            for tid in expired_ids:
                del self._locks[tid]
            self._expired += len(expired_ids)
            if expired_ids:
                logger.info("Cleaned up %d expired checkout lock(s)", len(expired_ids))
            return len(expired_ids)

    def force_release(self, ticket_id: str) -> bool:
        with self._lock:
            if ticket_id in self._locks:
                del self._locks[ticket_id]
                return True
            return False

    def metrics(self) -> CheckoutMetrics:
        with self._lock:
            avg_dur = (
                sum(self._durations) / len(self._durations)
                if self._durations
                else 0.0
            )
            return CheckoutMetrics(
                total_checkouts=self._total,
                successful_checkouts=self._success,
                failed_checkouts=self._failed,
                expired_locks=self._expired,
                avg_lock_duration_seconds=avg_dur,
            )
