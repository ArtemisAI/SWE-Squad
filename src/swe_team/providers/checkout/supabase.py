"""
Supabase CheckoutProvider — atomic checkout via PostgreSQL RPCs.

Uses server-side functions (atomic_checkout, release_checkout, etc.)
to guarantee exactly-once ticket claiming across multiple VMs.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from src.swe_team.providers.checkout.base import (
    CheckoutLock,
    CheckoutMetrics,
)

logger = logging.getLogger(__name__)


class SupabaseCheckoutProvider:
    """Atomic checkout via Supabase PostgREST RPCs.

    Requires migration ``002_atomic_checkout.sql`` to be applied.
    """

    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
    ) -> None:
        self._url = supabase_url.rstrip("/")
        self._key = supabase_key
        self._rest = f"{self._url}/rest/v1"
        self._headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        # Metrics counters (in-process; reset on restart)
        self._total: int = 0
        self._success: int = 0
        self._failed: int = 0
        self._expired: int = 0
        self._durations: list[float] = []

    @property
    def name(self) -> str:
        return "supabase"

    def try_checkout(
        self,
        ticket_id: str,
        team_id: str,
        lock_minutes: int = 60,
    ) -> Optional[UUID]:
        run_id = uuid4()
        self._total += 1
        try:
            result = self._rpc("atomic_checkout", {
                "p_ticket_id": ticket_id,
                "p_run_id": str(run_id),
                "p_locked_by": team_id,
                "p_lock_duration_minutes": lock_minutes,
            })
            if result:
                self._success += 1
                logger.debug("Checkout acquired via Supabase: ticket %s by %s", ticket_id, team_id)
                return run_id
            else:
                self._failed += 1
                logger.info("Checkout contention via Supabase: ticket %s already locked", ticket_id)
                return None
        except Exception as exc:
            self._failed += 1
            logger.warning("atomic_checkout RPC failed (fail-closed): %s", exc)
            return None

    def release(self, ticket_id: str, run_id: UUID) -> bool:
        try:
            result = self._rpc("release_checkout", {
                "p_ticket_id": ticket_id,
                "p_run_id": str(run_id),
            })
            if result:
                logger.debug("Checkout released via Supabase: ticket %s", ticket_id)
            return bool(result)
        except Exception as exc:
            logger.warning("release_checkout RPC failed: %s", exc)
            return False

    def heartbeat(self, ticket_id: str, run_id: UUID, extend_minutes: int = 60) -> bool:
        try:
            result = self._rpc("checkout_heartbeat", {
                "p_ticket_id": ticket_id,
                "p_run_id": str(run_id),
                "p_extend_minutes": extend_minutes,
            })
            return bool(result)
        except Exception as exc:
            logger.warning("checkout_heartbeat RPC failed: %s", exc)
            return False

    def is_locked(self, ticket_id: str) -> bool:
        lock = self.get_lock_info(ticket_id)
        return lock is not None

    def get_lock_info(self, ticket_id: str) -> Optional[CheckoutLock]:
        try:
            params = {
                "id": f"eq.{ticket_id}",
                "select": "checkout_run_id,checkout_locked_by,checkout_locked_at,checkout_expires_at",
            }
            rows = self._get("/swe_tickets", params)
            if not rows:
                return None
            row = rows[0]
            run_id_str = row.get("checkout_run_id")
            if not run_id_str:
                return None
            expires_at = datetime.fromisoformat(row["checkout_expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at < datetime.now(timezone.utc):
                return None  # expired
            return CheckoutLock(
                run_id=UUID(run_id_str),
                locked_by=row.get("checkout_locked_by", ""),
                locked_at=datetime.fromisoformat(row.get("checkout_locked_at", "")),
                expires_at=expires_at,
            )
        except Exception as exc:
            logger.warning("get_lock_info failed: %s", exc)
            return None

    def cleanup_expired(self) -> int:
        try:
            result = self._rpc("cleanup_expired_checkouts", {})
            count = int(result) if result else 0
            self._expired += count
            if count:
                logger.info("Cleaned up %d expired checkout lock(s) via Supabase", count)
            return count
        except Exception as exc:
            logger.warning("cleanup_expired_checkouts RPC failed: %s", exc)
            return 0

    def force_release(self, ticket_id: str) -> bool:
        try:
            result = self._rpc("force_release_checkout", {
                "p_ticket_id": ticket_id,
            })
            return bool(result)
        except Exception as exc:
            logger.warning("force_release_checkout RPC failed: %s", exc)
            return False

    def metrics(self) -> CheckoutMetrics:
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

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _rpc(self, fn_name: str, body: Dict[str, Any]) -> Any:
        url = f"{self._rest}/rpc/{fn_name}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=self._headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else None

    def _get(self, path: str, params: Dict[str, str]) -> Any:
        url = f"{self._rest}{path}?" + urllib.parse.urlencode(params, safe=".,()!")
        req = urllib.request.Request(url, headers=self._headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else None
