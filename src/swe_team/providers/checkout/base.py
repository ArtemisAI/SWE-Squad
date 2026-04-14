"""
CheckoutProvider protocol — pluggable atomic checkout backend.

All checkout backends (Supabase RPC, in-memory, Redis, etc.) must
implement this protocol so the runner can swap implementations without
changing any core orchestration logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol, runtime_checkable
from uuid import UUID


@dataclass
class CheckoutLock:
    """Information about an active checkout lock."""

    run_id: UUID
    locked_by: str
    locked_at: datetime
    expires_at: datetime

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at

    def seconds_until_expiry(self) -> float:
        delta = self.expires_at - datetime.now(timezone.utc)
        return max(0.0, delta.total_seconds())


@dataclass
class CheckoutMetrics:
    """Aggregate metrics for checkout operations."""

    total_checkouts: int = 0
    successful_checkouts: int = 0
    failed_checkouts: int = 0
    expired_locks: int = 0
    avg_lock_duration_seconds: float = 0.0

    @property
    def contention_rate(self) -> float:
        """Fraction of checkout attempts that failed due to contention."""
        if self.total_checkouts == 0:
            return 0.0
        return self.failed_checkouts / self.total_checkouts


@runtime_checkable
class CheckoutProvider(Protocol):
    """Interface all checkout backends must implement."""

    @property
    def name(self) -> str:
        """Provider identifier (e.g. 'supabase', 'memory')."""
        ...

    def try_checkout(
        self,
        ticket_id: str,
        team_id: str,
        lock_minutes: int = 60,
    ) -> Optional[UUID]:
        """Atomically claim a ticket. Returns run_id on success, None if locked."""
        ...

    def release(self, ticket_id: str, run_id: UUID) -> bool:
        """Release a checkout lock. Only the holder (matching run_id) can release."""
        ...

    def heartbeat(self, ticket_id: str, run_id: UUID, extend_minutes: int = 60) -> bool:
        """Extend the lock duration. Returns False if run_id doesn't match."""
        ...

    def is_locked(self, ticket_id: str) -> bool:
        """Check if a ticket is currently locked (non-expired)."""
        ...

    def get_lock_info(self, ticket_id: str) -> Optional[CheckoutLock]:
        """Return lock details, or None if unlocked."""
        ...

    def cleanup_expired(self) -> int:
        """Release all expired locks. Returns count of locks released."""
        ...

    def force_release(self, ticket_id: str) -> bool:
        """Admin override: release regardless of run_id."""
        ...

    def metrics(self) -> CheckoutMetrics:
        """Return aggregate checkout metrics."""
        ...
