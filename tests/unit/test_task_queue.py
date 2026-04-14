"""Tests for the TaskQueue provider abstraction and InMemoryTaskQueue."""
from __future__ import annotations

import threading
import time
from typing import List
from unittest.mock import patch

import pytest

from src.swe_team.providers.task_queue.base import QueuedTask, TaskQueueProvider
from src.swe_team.providers.task_queue.memory import InMemoryTaskQueue, _LEASE_TIMEOUT_SECONDS
from src.swe_team.providers.task_queue import (
    create_task_queue,
    list_task_queues,
    register_task_queue,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_queue() -> InMemoryTaskQueue:
    return InMemoryTaskQueue()


def _enqueue(q: InMemoryTaskQueue, task_type: str = "investigate", priority: int = 50) -> QueuedTask:
    return q.enqueue(task_type, "ticket-001", {"log": "error"}, priority=priority)


# ---------------------------------------------------------------------------
# QueuedTask dataclass
# ---------------------------------------------------------------------------


class TestQueuedTask:
    def test_default_status_is_queued(self):
        task = QueuedTask(
            task_id="t1",
            ticket_id="ticket-1",
            task_type="investigate",
            priority=50,
            payload={},
            status="queued",
            created_at=time.time(),
        )
        assert task.status == "queued"

    def test_optional_fields_default_none(self):
        task = QueuedTask(
            task_id="t1",
            ticket_id="ticket-1",
            task_type="investigate",
            priority=50,
            payload={},
            status="queued",
            created_at=time.time(),
        )
        assert task.claimed_at is None
        assert task.claimed_by is None
        assert task.next_retry_at is None
        assert task.result is None
        assert task.error is None

    def test_default_attempts_zero(self):
        task = QueuedTask(
            task_id="t1",
            ticket_id="ticket-1",
            task_type="investigate",
            priority=50,
            payload={},
            status="queued",
            created_at=time.time(),
        )
        assert task.attempts == 0

    def test_default_max_retries(self):
        task = QueuedTask(
            task_id="t1",
            ticket_id="ticket-1",
            task_type="investigate",
            priority=50,
            payload={},
            status="queued",
            created_at=time.time(),
        )
        assert task.max_retries == 3

    def test_default_retry_delay(self):
        task = QueuedTask(
            task_id="t1",
            ticket_id="ticket-1",
            task_type="investigate",
            priority=50,
            payload={},
            status="queued",
            created_at=time.time(),
        )
        assert task.retry_delay_seconds == 30.0


# ---------------------------------------------------------------------------
# InMemoryTaskQueue.name
# ---------------------------------------------------------------------------


class TestInMemoryTaskQueueName:
    def test_name_is_memory(self):
        q = _make_queue()
        assert q.name == "memory"


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    def test_memory_queue_satisfies_protocol(self):
        q = _make_queue()
        assert isinstance(q, TaskQueueProvider)


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------


class TestEnqueue:
    def test_enqueue_returns_queued_task(self):
        q = _make_queue()
        task = q.enqueue("investigate", "ticket-1", {"key": "val"})
        assert isinstance(task, QueuedTask)

    def test_enqueue_status_is_queued(self):
        q = _make_queue()
        task = q.enqueue("investigate", "ticket-1", {})
        assert task.status == "queued"

    def test_enqueue_sets_correct_fields(self):
        q = _make_queue()
        payload = {"error": "NullPointerException"}
        task = q.enqueue("develop", "ticket-42", payload, priority=10)
        assert task.task_type == "develop"
        assert task.ticket_id == "ticket-42"
        assert task.payload == payload
        assert task.priority == 10

    def test_enqueue_assigns_unique_ids(self):
        q = _make_queue()
        t1 = q.enqueue("investigate", "ticket-1", {})
        t2 = q.enqueue("investigate", "ticket-1", {})
        assert t1.task_id != t2.task_id

    def test_enqueue_default_priority_is_50(self):
        q = _make_queue()
        task = q.enqueue("triage", "ticket-1", {})
        assert task.priority == 50

    def test_enqueue_increments_queue_depth(self):
        q = _make_queue()
        assert q.queue_depth() == 0
        q.enqueue("investigate", "t1", {})
        assert q.queue_depth() == 1
        q.enqueue("investigate", "t2", {})
        assert q.queue_depth() == 2

    def test_enqueue_stores_created_at(self):
        q = _make_queue()
        before = time.time()
        task = q.enqueue("investigate", "t1", {})
        after = time.time()
        assert before <= task.created_at <= after


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


class TestClaim:
    def test_claim_returns_task(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "worker-1")
        assert task is not None

    def test_claim_returns_none_when_empty(self):
        q = _make_queue()
        assert q.claim("investigate", "worker-1") is None

    def test_claim_sets_status_claimed(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "worker-1")
        assert task.status == "claimed"

    def test_claim_sets_claimed_by(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "worker-1")
        assert task.claimed_by == "worker-1"

    def test_claim_sets_claimed_at(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        before = time.time()
        task = q.claim("investigate", "worker-1")
        after = time.time()
        assert before <= task.claimed_at <= after

    def test_claim_increments_attempts(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "worker-1")
        assert task.attempts == 1

    def test_claim_removes_from_queue_depth(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        assert q.queue_depth() == 1
        q.claim("investigate", "worker-1")
        assert q.queue_depth() == 0

    def test_claim_filters_by_task_type(self):
        q = _make_queue()
        q.enqueue("develop", "t1", {})
        # No investigate tasks — should return None
        assert q.claim("investigate", "worker-1") is None

    def test_claim_only_returns_correct_type(self):
        q = _make_queue()
        q.enqueue("develop", "t1", {})
        q.enqueue("investigate", "t2", {})
        task = q.claim("investigate", "worker-1")
        assert task is not None
        assert task.task_type == "investigate"

    def test_second_claim_when_empty_returns_none(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        q.claim("investigate", "worker-1")
        assert q.claim("investigate", "worker-2") is None


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    def test_lower_priority_number_claimed_first(self):
        q = _make_queue()
        q.enqueue("investigate", "t-low", {}, priority=100)
        q.enqueue("investigate", "t-high", {}, priority=1)
        task = q.claim("investigate", "worker-1")
        assert task.priority == 1
        assert task.ticket_id == "t-high"

    def test_equal_priority_fifo(self):
        q = _make_queue()
        t1 = q.enqueue("investigate", "t1", {}, priority=50)
        time.sleep(0.01)  # ensure different created_at
        t2 = q.enqueue("investigate", "t2", {}, priority=50)
        claimed = q.claim("investigate", "worker-1")
        assert claimed.task_id == t1.task_id

    def test_three_tasks_correct_order(self):
        q = _make_queue()
        q.enqueue("investigate", "t-med",  {}, priority=50)
        q.enqueue("investigate", "t-high", {}, priority=10)
        q.enqueue("investigate", "t-low",  {}, priority=90)
        order = []
        while True:
            task = q.claim("investigate", "worker-1")
            if task is None:
                break
            order.append(task.priority)
            q.complete(task.task_id, {})
        assert order == [10, 50, 90]

    def test_mixed_types_priority_independent(self):
        q = _make_queue()
        q.enqueue("develop",     "t1", {}, priority=5)
        q.enqueue("investigate", "t2", {}, priority=99)
        # Claiming "investigate" must return the investigate task regardless of develop priority
        task = q.claim("investigate", "worker-1")
        assert task is not None
        assert task.task_type == "investigate"


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


class TestComplete:
    def test_complete_sets_status(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "worker-1")
        q.complete(task.task_id, {"summary": "done"})
        assert task.status == "completed"

    def test_complete_stores_result(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "worker-1")
        q.complete(task.task_id, {"root_cause": "null pointer"})
        assert task.result == {"root_cause": "null pointer"}

    def test_complete_unknown_task_raises(self):
        q = _make_queue()
        with pytest.raises(KeyError):
            q.complete("nonexistent-id", {})

    def test_complete_unclaimed_task_raises(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "worker-1")
        q.complete(task.task_id, {})
        # Trying to complete again should raise
        with pytest.raises(ValueError):
            q.complete(task.task_id, {})

    def test_completed_task_not_counted_in_queue_depth(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "worker-1")
        q.complete(task.task_id, {})
        assert q.queue_depth() == 0


# ---------------------------------------------------------------------------
# fail / auto-retry
# ---------------------------------------------------------------------------


class TestFail:
    def test_fail_sets_error(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "worker-1")
        q.fail(task.task_id, "connection refused")
        assert task.error == "connection refused"

    def test_fail_requeues_for_retry(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "worker-1")
        q.fail(task.task_id, "transient error")
        # After fail the task should be queued again (status="queued")
        assert task.status == "queued"

    def test_fail_sets_next_retry_at(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "worker-1")
        before = time.time()
        q.fail(task.task_id, "err")
        assert task.next_retry_at is not None
        assert task.next_retry_at > before

    def test_fail_exponential_backoff_30s_base(self):
        """First failure → retry_delay_seconds * 2^0 = 30 s delay."""
        q = _make_queue()
        q.enqueue("investigate", "t1", {}, priority=50)
        task = q.claim("investigate", "w")
        task.retry_delay_seconds = 30.0
        before = time.time()
        q.fail(task.task_id, "err")
        # delay should be 30 * 2^(1-1) = 30 s
        assert abs(task.next_retry_at - (before + 30.0)) < 1.0

    def test_fail_exponential_backoff_second_attempt(self):
        """Second failure → 60 s delay."""
        q = _make_queue()
        q.enqueue("investigate", "t1", {})

        # Simulate 2 attempts already
        with patch("time.time", return_value=1000.0):
            task = q.claim("investigate", "w")  # attempts = 1
        # Manually increment to simulate second attempt situation
        task.attempts = 2
        task.status = "claimed"
        task.retry_delay_seconds = 30.0

        with patch("time.time", return_value=2000.0):
            q.fail(task.task_id, "err")
        # delay = 30 * 2^(2-1) = 60 s → next_retry_at = 2000 + 60 = 2060
        assert abs(task.next_retry_at - 2060.0) < 1.0

    def test_fail_unknown_task_raises(self):
        q = _make_queue()
        with pytest.raises(KeyError):
            q.fail("nonexistent", "err")

    def test_fail_unclaimed_task_raises(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        # Not claimed — should raise
        task_id = q._tasks[next(iter(q._tasks))].task_id
        with pytest.raises(ValueError):
            q.fail(task_id, "err")

    def test_retry_not_claimable_before_next_retry_at(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "w")
        q.fail(task.task_id, "err")
        # next_retry_at is ~30 s in the future — should not be claimable now
        assert q.claim("investigate", "w") is None

    def test_retry_claimable_after_next_retry_at(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "w")
        q.fail(task.task_id, "err")
        # Override next_retry_at to the past
        task.next_retry_at = time.time() - 1.0
        retried = q.claim("investigate", "w2")
        assert retried is not None
        assert retried.task_id == task.task_id


# ---------------------------------------------------------------------------
# Dead-letter queue
# ---------------------------------------------------------------------------


class TestDeadLetter:
    def _exhaust_retries(self, q: InMemoryTaskQueue, task_type: str = "investigate") -> QueuedTask:
        """Drive a task through max_retries failures until it hits DLQ."""
        t = q.enqueue(task_type, "t1", {})
        max_r = t.max_retries
        for _ in range(max_r):
            claimed = q.claim(task_type, "w")
            assert claimed is not None, "Should be claimable"
            # Make retry eligible immediately
            claimed.next_retry_at = None
            q.fail(claimed.task_id, "persistent error")
            # Reset next_retry_at so next claim works
            if claimed.status == "queued":
                claimed.next_retry_at = time.time() - 1.0
        return t

    def test_dead_letter_after_max_retries(self):
        q = _make_queue()
        task = self._exhaust_retries(q)
        assert task.status == "dead_letter"

    def test_dead_letter_in_get_dead_letter(self):
        q = _make_queue()
        self._exhaust_retries(q)
        dlq = q.get_dead_letter()
        assert len(dlq) == 1

    def test_dead_letter_not_counted_in_queue_depth(self):
        q = _make_queue()
        self._exhaust_retries(q)
        assert q.queue_depth() == 0

    def test_dead_letter_not_claimable(self):
        q = _make_queue()
        self._exhaust_retries(q)
        assert q.claim("investigate", "w") is None

    def test_get_dead_letter_respects_limit(self):
        q = _make_queue()
        for i in range(5):
            t = q.enqueue("investigate", f"t{i}", {})
            for _ in range(t.max_retries):
                c = q.claim("investigate", "w")
                if c is None:
                    break
                c.next_retry_at = None
                q.fail(c.task_id, "err")
                if c.status == "queued":
                    c.next_retry_at = time.time() - 1.0
        dlq = q.get_dead_letter(limit=2)
        assert len(dlq) == 2

    def test_get_dead_letter_does_not_remove_items(self):
        q = _make_queue()
        self._exhaust_retries(q)
        q.get_dead_letter()
        q.get_dead_letter()  # second call should still return them
        assert len(q.get_dead_letter()) == 1

    def test_dead_letter_has_error_set(self):
        q = _make_queue()
        t = self._exhaust_retries(q)
        assert t.error == "persistent error"


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_renews_claimed_at(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "w")
        original_claimed_at = task.claimed_at
        time.sleep(0.05)
        q.heartbeat(task.task_id)
        assert task.claimed_at > original_claimed_at

    def test_heartbeat_unknown_task_raises(self):
        q = _make_queue()
        with pytest.raises(KeyError):
            q.heartbeat("nonexistent")

    def test_heartbeat_unclaimed_task_raises(self):
        q = _make_queue()
        t = q.enqueue("investigate", "t1", {})
        with pytest.raises(ValueError):
            q.heartbeat(t.task_id)

    def test_heartbeat_completed_task_raises(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "w")
        q.complete(task.task_id, {})
        with pytest.raises(ValueError):
            q.heartbeat(task.task_id)


# ---------------------------------------------------------------------------
# Stale task reclaim (lease timeout)
# ---------------------------------------------------------------------------


class TestStaleTaskReclaim:
    def test_stale_task_reclaimed_after_lease_timeout(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "w1")
        assert task is not None

        # Simulate lease expiry by backdating claimed_at
        task.claimed_at = time.time() - (_LEASE_TIMEOUT_SECONDS + 5)

        # A second worker should now be able to claim the task
        reclaimed = q.claim("investigate", "w2")
        assert reclaimed is not None
        assert reclaimed.task_id == task.task_id
        assert reclaimed.claimed_by == "w2"

    def test_fresh_task_not_reclaimed(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        q.claim("investigate", "w1")  # claimed recently

        # Should NOT be reclaimed — lease still fresh
        assert q.claim("investigate", "w2") is None

    def test_reclaimed_task_increments_attempts(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "w1")
        task.claimed_at = time.time() - (_LEASE_TIMEOUT_SECONDS + 5)

        reclaimed = q.claim("investigate", "w2")
        assert reclaimed.attempts == 2  # first claim was attempt 1


# ---------------------------------------------------------------------------
# queue_depth
# ---------------------------------------------------------------------------


class TestQueueDepth:
    def test_queue_depth_zero_initially(self):
        q = _make_queue()
        assert q.queue_depth() == 0

    def test_queue_depth_counts_only_queued(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        t2 = q.enqueue("investigate", "t2", {})
        q.claim("investigate", "w")  # t1 becomes claimed
        # Only t2 remains queued
        assert q.queue_depth() == 1

    def test_queue_depth_filter_by_type(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        q.enqueue("investigate", "t2", {})
        q.enqueue("develop",     "t3", {})
        assert q.queue_depth("investigate") == 2
        assert q.queue_depth("develop") == 1
        assert q.queue_depth("triage") == 0

    def test_queue_depth_all_types(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        q.enqueue("develop",     "t2", {})
        assert q.queue_depth() == 2

    def test_queue_depth_after_complete(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "w")
        q.complete(task.task_id, {})
        assert q.queue_depth() == 0


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def test_health_check_returns_true(self):
        q = _make_queue()
        assert q.health_check() is True


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_enqueue(self):
        q = _make_queue()
        errors: List[Exception] = []

        def enqueue_many() -> None:
            try:
                for i in range(50):
                    q.enqueue("investigate", f"t{i}", {})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=enqueue_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert q.queue_depth() == 200  # 4 threads × 50

    def test_concurrent_claim_no_double_claim(self):
        """Each task must be claimed by exactly one worker."""
        q = _make_queue()
        n_tasks = 20
        for i in range(n_tasks):
            q.enqueue("investigate", f"t{i}", {})

        claimed_ids: List[str] = []
        lock = threading.Lock()

        def claim_all() -> None:
            while True:
                task = q.claim("investigate", f"worker-{threading.get_ident()}")
                if task is None:
                    break
                with lock:
                    claimed_ids.append(task.task_id)
                q.complete(task.task_id, {})

        threads = [threading.Thread(target=claim_all) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every task should be claimed exactly once
        assert len(claimed_ids) == n_tasks
        assert len(set(claimed_ids)) == n_tasks  # no duplicates

    def test_concurrent_enqueue_and_claim(self):
        """Producers and consumers run simultaneously without corruption."""
        q = _make_queue()
        produced: List[str] = []
        consumed: List[str] = []
        p_lock = threading.Lock()
        c_lock = threading.Lock()
        stop_event = threading.Event()

        def producer() -> None:
            for i in range(30):
                task = q.enqueue("investigate", f"t{i}", {})
                with p_lock:
                    produced.append(task.task_id)
                time.sleep(0.001)

        def consumer() -> None:
            while not stop_event.is_set() or q.queue_depth("investigate") > 0:
                task = q.claim("investigate", "consumer")
                if task is None:
                    time.sleep(0.005)
                    continue
                q.complete(task.task_id, {})
                with c_lock:
                    consumed.append(task.task_id)

        producers = [threading.Thread(target=producer) for _ in range(2)]
        consumers = [threading.Thread(target=consumer, daemon=True) for _ in range(2)]

        for t in consumers:
            t.start()
        for t in producers:
            t.start()
        for t in producers:
            t.join()

        stop_event.set()
        for t in consumers:
            t.join(timeout=5.0)

        assert len(set(consumed)) == len(consumed)  # no duplicate consumptions
        assert len(produced) == 60  # 2 producers × 30


# ---------------------------------------------------------------------------
# Factory: create_task_queue
# ---------------------------------------------------------------------------


class TestFactory:
    def test_create_memory_queue(self):
        q = create_task_queue("memory")
        assert q.name == "memory"

    def test_create_memory_queue_with_config(self):
        q = create_task_queue("memory", {"some_option": True})
        assert q.name == "memory"

    def test_create_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown task queue provider"):
            create_task_queue("nonexistent")

    def test_error_message_lists_available(self):
        with pytest.raises(ValueError, match="memory"):
            create_task_queue("nonexistent")

    def test_list_task_queues_includes_memory(self):
        assert "memory" in list_task_queues()

    def test_list_task_queues_sorted(self):
        queues = list_task_queues()
        assert queues == sorted(queues)

    def test_register_custom_provider(self):
        class FakeQueue:
            @property
            def name(self) -> str:
                return "fake"

            def enqueue(self, *a, **kw):
                pass

            def claim(self, *a, **kw):
                return None

            def complete(self, *a, **kw):
                pass

            def fail(self, *a, **kw):
                pass

            def heartbeat(self, *a, **kw):
                pass

            def get_dead_letter(self, limit=10):
                return []

            def queue_depth(self, task_type=None):
                return 0

            def health_check(self):
                return True

        register_task_queue("fake", lambda cfg: FakeQueue())
        q = create_task_queue("fake")
        assert q.name == "fake"
        assert "fake" in list_task_queues()

    def test_create_returns_task_queue_provider(self):
        q = create_task_queue("memory")
        assert isinstance(q, TaskQueueProvider)


# ---------------------------------------------------------------------------
# Full flow integration
# ---------------------------------------------------------------------------


class TestFullFlow:
    def test_enqueue_claim_complete_flow(self):
        q = _make_queue()
        task = q.enqueue("investigate", "ticket-99", {"log": "crash"}, priority=10)
        assert task.status == "queued"

        claimed = q.claim("investigate", "worker-A")
        assert claimed is not None
        assert claimed.task_id == task.task_id
        assert claimed.status == "claimed"

        q.complete(claimed.task_id, {"report": "root cause found"})
        assert claimed.status == "completed"
        assert claimed.result == {"report": "root cause found"}

    def test_enqueue_claim_fail_retry_complete_flow(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {}, priority=5)

        # First attempt fails
        t = q.claim("investigate", "w1")
        assert t is not None
        q.fail(t.task_id, "transient")
        assert t.status == "queued"
        assert t.attempts == 1

        # Make retry immediately eligible
        t.next_retry_at = time.time() - 1.0

        # Second attempt succeeds
        t2 = q.claim("investigate", "w2")
        assert t2 is not None
        assert t2.task_id == t.task_id
        assert t2.attempts == 2
        q.complete(t2.task_id, {"ok": True})
        assert t2.status == "completed"

    def test_multiple_task_types_isolated(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        q.enqueue("develop",     "t2", {})
        q.enqueue("triage",      "t3", {})

        inv = q.claim("investigate", "w")
        dev = q.claim("develop",     "w")
        tri = q.claim("triage",      "w")

        assert inv.task_type == "investigate"
        assert dev.task_type == "develop"
        assert tri.task_type == "triage"

    def test_heartbeat_prevents_reclaim(self):
        q = _make_queue()
        q.enqueue("investigate", "t1", {})
        task = q.claim("investigate", "w1")

        # Backdate claimed_at to near-expiry
        task.claimed_at = time.time() - (_LEASE_TIMEOUT_SECONDS - 5)
        # Heartbeat should refresh the lease
        q.heartbeat(task.task_id)

        # Another worker tries to claim — should fail (lease is fresh again)
        assert q.claim("investigate", "w2") is None
