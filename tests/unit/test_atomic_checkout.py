"""Tests for atomic checkout — prevents duplicate work across VMs."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from src.swe_team.providers.checkout.base import CheckoutLock, CheckoutMetrics
from src.swe_team.providers.checkout.memory import InMemoryCheckoutProvider
from src.swe_team.atomic_checkout import CheckoutManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    return InMemoryCheckoutProvider()


@pytest.fixture
def manager(provider):
    return CheckoutManager(provider)


# ---------------------------------------------------------------------------
# Basic checkout / release cycle
# ---------------------------------------------------------------------------

class TestBasicCheckout:

    def test_checkout_returns_uuid(self, manager):
        run_id = manager.try_checkout("t1", "agent-1")
        assert isinstance(run_id, UUID)

    def test_release_after_checkout(self, manager):
        run_id = manager.try_checkout("t1", "agent-1")
        assert manager.release("t1", run_id)

    def test_double_checkout_same_ticket_fails(self, manager):
        run_id1 = manager.try_checkout("t1", "agent-1")
        assert run_id1 is not None
        run_id2 = manager.try_checkout("t1", "agent-2")
        assert run_id2 is None

    def test_checkout_after_release_succeeds(self, manager):
        run_id1 = manager.try_checkout("t1", "agent-1")
        manager.release("t1", run_id1)
        run_id2 = manager.try_checkout("t1", "agent-2")
        assert run_id2 is not None

    def test_different_tickets_independent(self, manager):
        run1 = manager.try_checkout("t1", "agent-1")
        run2 = manager.try_checkout("t2", "agent-1")
        assert run1 is not None
        assert run2 is not None

    def test_release_wrong_run_id_fails(self, manager):
        manager.try_checkout("t1", "agent-1")
        assert not manager.release("t1", uuid4())

    def test_release_nonexistent_ticket(self, manager):
        assert not manager.release("nonexistent", uuid4())


# ---------------------------------------------------------------------------
# Lock expiry
# ---------------------------------------------------------------------------

class TestLockExpiry:

    def test_expired_lock_allows_recheckout(self, provider):
        mgr = CheckoutManager(provider)
        run1 = mgr.try_checkout("t1", "agent-1", lock_minutes=0)
        # lock_minutes=0 means it expires immediately
        # But the lock is set with timedelta(minutes=0), so it's at NOW
        # We need to simulate expiry — set expires_at to the past
        with provider._lock:
            provider._locks["t1"].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        run2 = mgr.try_checkout("t1", "agent-2")
        assert run2 is not None

    def test_cleanup_expired_returns_count(self, provider):
        mgr = CheckoutManager(provider)
        mgr.try_checkout("t1", "agent-1")
        mgr.try_checkout("t2", "agent-1")
        # Expire both
        with provider._lock:
            for lock in provider._locks.values():
                lock.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        count = mgr.cleanup_expired()
        assert count == 2

    def test_cleanup_does_not_remove_active(self, provider):
        mgr = CheckoutManager(provider)
        mgr.try_checkout("t1", "agent-1", lock_minutes=60)
        count = mgr.cleanup_expired()
        assert count == 0
        assert mgr.is_locked("t1")

    def test_is_locked_false_after_expiry(self, provider):
        mgr = CheckoutManager(provider)
        mgr.try_checkout("t1", "agent-1")
        with provider._lock:
            provider._locks["t1"].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert not mgr.is_locked("t1")


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeat:

    def test_heartbeat_extends_lock(self, provider):
        mgr = CheckoutManager(provider)
        run_id = mgr.try_checkout("t1", "agent-1", lock_minutes=1)
        original_expiry = provider._locks["t1"].expires_at
        assert mgr.heartbeat("t1", run_id, extend_minutes=120)
        new_expiry = provider._locks["t1"].expires_at
        assert new_expiry > original_expiry

    def test_heartbeat_wrong_run_id(self, manager):
        manager.try_checkout("t1", "agent-1")
        assert not manager.heartbeat("t1", uuid4())

    def test_heartbeat_nonexistent_ticket(self, manager):
        assert not manager.heartbeat("nonexistent", uuid4())


# ---------------------------------------------------------------------------
# Force release
# ---------------------------------------------------------------------------

class TestForceRelease:

    def test_force_release_ignores_run_id(self, manager):
        manager.try_checkout("t1", "agent-1")
        assert manager.force_release("t1")
        assert not manager.is_locked("t1")

    def test_force_release_nonexistent(self, manager):
        assert not manager.force_release("nonexistent")


# ---------------------------------------------------------------------------
# Lock info
# ---------------------------------------------------------------------------

class TestLockInfo:

    def test_get_lock_info(self, manager):
        run_id = manager.try_checkout("t1", "agent-1")
        info = manager.get_lock_info("t1")
        assert info is not None
        assert info.run_id == run_id
        assert info.locked_by == "agent-1"

    def test_lock_info_none_when_unlocked(self, manager):
        assert manager.get_lock_info("t1") is None

    def test_lock_info_none_after_expiry(self, provider):
        mgr = CheckoutManager(provider)
        mgr.try_checkout("t1", "agent-1")
        with provider._lock:
            provider._locks["t1"].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert mgr.get_lock_info("t1") is None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class TestMetrics:

    def test_metrics_initial(self, manager):
        m = manager.metrics()
        assert m.total_checkouts == 0
        assert m.contention_rate == 0.0

    def test_metrics_after_success(self, manager):
        manager.try_checkout("t1", "agent-1")
        m = manager.metrics()
        assert m.total_checkouts == 1
        assert m.successful_checkouts == 1
        assert m.failed_checkouts == 0

    def test_metrics_contention(self, manager):
        manager.try_checkout("t1", "agent-1")
        manager.try_checkout("t1", "agent-2")  # fails
        m = manager.metrics()
        assert m.total_checkouts == 2
        assert m.failed_checkouts == 1
        assert m.contention_rate == 0.5

    def test_metrics_avg_duration(self, manager):
        run_id = manager.try_checkout("t1", "agent-1")
        manager.release("t1", run_id)
        m = manager.metrics()
        assert m.avg_lock_duration_seconds >= 0.0

    def test_metrics_expired_counter(self, provider):
        mgr = CheckoutManager(provider)
        mgr.try_checkout("t1", "agent-1")
        with provider._lock:
            provider._locks["t1"].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        mgr.cleanup_expired()
        m = mgr.metrics()
        assert m.expired_locks == 1


# ---------------------------------------------------------------------------
# Concurrent checkout (two threads, only one wins)
# ---------------------------------------------------------------------------

class TestConcurrency:

    def test_concurrent_checkout_only_one_wins(self, provider):
        results = []
        barrier = threading.Barrier(2)

        def attempt(team_id):
            barrier.wait()
            run_id = provider.try_checkout("t1", team_id)
            results.append((team_id, run_id))

        threads = [
            threading.Thread(target=attempt, args=("agent-1",)),
            threading.Thread(target=attempt, args=("agent-2",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r[1] is not None]
        losers = [r for r in results if r[1] is None]
        assert len(winners) == 1
        assert len(losers) == 1

    def test_concurrent_checkout_different_tickets(self, provider):
        results = []
        barrier = threading.Barrier(2)

        def attempt(ticket_id, team_id):
            barrier.wait()
            run_id = provider.try_checkout(ticket_id, team_id)
            results.append((ticket_id, run_id))

        threads = [
            threading.Thread(target=attempt, args=("t1", "agent-1")),
            threading.Thread(target=attempt, args=("t2", "agent-2")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r[1] is not None for r in results)


# ---------------------------------------------------------------------------
# CheckoutLock dataclass
# ---------------------------------------------------------------------------

class TestCheckoutLock:

    def test_is_expired_true(self):
        lock = CheckoutLock(
            run_id=uuid4(),
            locked_by="test",
            locked_at=datetime.now(timezone.utc) - timedelta(hours=2),
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert lock.is_expired()

    def test_is_expired_false(self):
        lock = CheckoutLock(
            run_id=uuid4(),
            locked_by="test",
            locked_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert not lock.is_expired()

    def test_seconds_until_expiry(self):
        lock = CheckoutLock(
            run_id=uuid4(),
            locked_by="test",
            locked_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        assert 25 < lock.seconds_until_expiry() <= 30

    def test_seconds_until_expiry_expired(self):
        lock = CheckoutLock(
            run_id=uuid4(),
            locked_by="test",
            locked_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        assert lock.seconds_until_expiry() == 0.0


# ---------------------------------------------------------------------------
# CheckoutMetrics dataclass
# ---------------------------------------------------------------------------

class TestCheckoutMetrics:

    def test_contention_rate_zero_division(self):
        m = CheckoutMetrics()
        assert m.contention_rate == 0.0

    def test_contention_rate_calculation(self):
        m = CheckoutMetrics(total_checkouts=10, failed_checkouts=3)
        assert m.contention_rate == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Provider name
# ---------------------------------------------------------------------------

class TestProviderName:

    def test_memory_provider_name(self, provider):
        assert provider.name == "memory"

    def test_manager_provider_name(self, manager):
        assert manager.provider_name == "memory"


# ---------------------------------------------------------------------------
# Default team_id from env
# ---------------------------------------------------------------------------

class TestDefaultTeamId:

    def test_default_team_id(self, manager, monkeypatch):
        monkeypatch.setenv("SWE_TEAM_ID", "swe-test-99")
        run_id = manager.try_checkout("t1")
        info = manager.get_lock_info("t1")
        assert info.locked_by == "swe-test-99"
