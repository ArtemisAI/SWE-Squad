"""
Atomic Checkout Manager — prevents duplicate work across multiple VMs.

Wraps a CheckoutProvider (Supabase or in-memory) with logging, metrics,
and a consistent API for the runner to use.

Usage in the runner::

    checkout = CheckoutManager(provider)
    run_id = checkout.try_checkout(ticket_id, team_id)
    if run_id is None:
        # Another VM has this ticket — skip
        continue
    try:
        # ... investigate / develop ...
        checkout.heartbeat(ticket_id, run_id)
    finally:
        checkout.release(ticket_id, run_id)
"""
from __future__ import annotations

import logging
import os
from typing import Optional
from uuid import UUID

from src.swe_team.providers.checkout.base import (
    CheckoutLock,
    CheckoutMetrics,
    CheckoutProvider,
)

logger = logging.getLogger(__name__)


class CheckoutManager:
    """High-level facade over a CheckoutProvider.

    Adds structured logging for contention events and exposes
    metrics for the dashboard.
    """

    def __init__(self, provider: CheckoutProvider) -> None:
        self._provider = provider

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def try_checkout(
        self,
        ticket_id: str,
        team_id: Optional[str] = None,
        lock_minutes: int = 60,
    ) -> Optional[UUID]:
        """Attempt to checkout a ticket. Returns run_id or None."""
        if team_id is None:
            team_id = os.environ.get("SWE_TEAM_ID", "default")
        run_id = self._provider.try_checkout(ticket_id, team_id, lock_minutes)
        if run_id is None:
            lock = self._provider.get_lock_info(ticket_id)
            if lock:
                logger.warning(
                    "Checkout DENIED for ticket %s: held by %s, expires in %.0fs",
                    ticket_id,
                    lock.locked_by,
                    lock.seconds_until_expiry(),
                )
            else:
                logger.warning(
                    "Checkout DENIED for ticket %s (lock info unavailable)",
                    ticket_id,
                )
        return run_id

    def release(self, ticket_id: str, run_id: UUID) -> bool:
        return self._provider.release(ticket_id, run_id)

    def heartbeat(self, ticket_id: str, run_id: UUID, extend_minutes: int = 60) -> bool:
        return self._provider.heartbeat(ticket_id, run_id, extend_minutes)

    def is_locked(self, ticket_id: str) -> bool:
        return self._provider.is_locked(ticket_id)

    def get_lock_info(self, ticket_id: str) -> Optional[CheckoutLock]:
        return self._provider.get_lock_info(ticket_id)

    def cleanup_expired(self) -> int:
        return self._provider.cleanup_expired()

    def force_release(self, ticket_id: str) -> bool:
        return self._provider.force_release(ticket_id)

    def metrics(self) -> CheckoutMetrics:
        return self._provider.metrics()
