"""
Integration tests for new architectural components.

Tests the interaction between components end-to-end, not just individual units.
Covers: TaskQueue + QueuedDispatcher + Executor, GuardrailsCoordinator + CircuitBreaker,
RBAC middleware + agent classes, provider factory round-trips, and full pipeline simulation.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from src.swe_team.providers.task_queue.base import QueuedTask
from src.swe_team.providers.task_queue.memory import InMemoryTaskQueue
from src.swe_team.queued_dispatcher import QueuedDispatcher, SEVERITY_PRIORITY
from src.swe_team.guardrails import GuardrailsCoordinator, GuardrailDecision
from src.swe_team.circuit_breaker import CircuitBreaker
from src.swe_team.rbac_middleware import (
    require_permission,
    require_sandbox,
    SandboxViolationError,
)
from src.swe_team.agent_rbac import PermissionDeniedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class FakeTicket:
    """Minimal ticket-like object for integration tests."""
    ticket_id: str
    title: str = "Test ticket"
    severity: Any = None
    fingerprint: str = "fp-test"
    fix_plan: str = ""


class FakeSeverity:
    """Enum-like severity with .name attribute."""
    def __init__(self, name: str):
        self.name = name

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# 1. Queue -> Dispatcher -> Executor flow
# ---------------------------------------------------------------------------

class TestQueueDispatcherExecutorFlow:
    """Verify InMemoryTaskQueue + QueuedDispatcher + ThreadPoolExecutor work together."""

    def test_priority_ordering_critical_first(self):
        """Enqueue 3 tickets with different severities; CRITICAL claimed first."""
        queue = InMemoryTaskQueue()
        dispatcher = QueuedDispatcher(queue, worker_id="test-worker")

        tickets = {
            "t-low": FakeTicket("t-low", severity=FakeSeverity("LOW")),
            "t-critical": FakeTicket("t-critical", severity=FakeSeverity("CRITICAL")),
            "t-high": FakeTicket("t-high", severity=FakeSeverity("HIGH")),
        }

        # Enqueue in non-priority order: LOW, CRITICAL, HIGH
        dispatcher.enqueue_investigation(tickets["t-low"])
        dispatcher.enqueue_investigation(tickets["t-critical"])
        dispatcher.enqueue_investigation(tickets["t-high"])

        assert queue.queue_depth("investigate") == 3

        # Claims should come back in priority order: CRITICAL, HIGH, LOW
        claim1 = queue.claim("investigate", "w1")
        claim2 = queue.claim("investigate", "w1")
        claim3 = queue.claim("investigate", "w1")

        assert claim1 is not None and claim1.ticket_id == "t-critical"
        assert claim2 is not None and claim2.ticket_id == "t-high"
        assert claim3 is not None and claim3.ticket_id == "t-low"

    def test_completed_tasks_leave_queue(self):
        """After dispatch_one succeeds, the task is completed and queue is empty."""
        queue = InMemoryTaskQueue()
        dispatcher = QueuedDispatcher(queue, worker_id="test-worker")

        ticket = FakeTicket("t-1", severity=FakeSeverity("HIGH"))
        dispatcher.enqueue_investigation(ticket)

        def worker_fn(t):
            return True

        def lookup(tid):
            return ticket if tid == "t-1" else None

        result = dispatcher.dispatch_one("investigate", worker_fn, lookup)

        assert result is not None
        assert result.success is True
        assert result.ticket_id == "t-1"
        # Queue should be empty now
        assert queue.queue_depth("investigate") == 0
        # No dead-letter entries
        assert len(queue.get_dead_letter()) == 0

    def test_failed_tasks_go_to_dead_letter(self):
        """After max_retries failures, task ends up in dead-letter queue."""
        queue = InMemoryTaskQueue()
        dispatcher = QueuedDispatcher(queue, worker_id="test-worker")

        ticket = FakeTicket("t-fail", severity=FakeSeverity("MEDIUM"))
        task = dispatcher.enqueue_investigation(ticket)

        def worker_fn(t):
            return False

        def lookup(tid):
            return ticket

        # The default max_retries is 3, so we need 3 failed attempts.
        # Each dispatch_one will claim, execute (fail), and re-queue with delay.
        # To bypass the retry delay, we manipulate next_retry_at after each failure.
        for i in range(3):
            # Clear retry delay so the task is claimable immediately
            for t in queue._tasks.values():
                if t.status == "queued" and t.next_retry_at is not None:
                    t.next_retry_at = 0  # make immediately eligible

            result = dispatcher.dispatch_one("investigate", worker_fn, lookup)
            assert result is not None
            assert result.success is False

        # After 3 failures the task should be in dead-letter
        dlq = queue.get_dead_letter()
        assert len(dlq) == 1
        assert dlq[0].ticket_id == "t-fail"
        assert dlq[0].status == "dead_letter"

    def test_parallel_dispatch_with_thread_pool(self):
        """dispatch_parallel sends tasks to a real ThreadPoolExecutor."""
        queue = InMemoryTaskQueue()
        dispatcher = QueuedDispatcher(queue, worker_id="test-worker")

        tickets = {}
        for sev in ["CRITICAL", "HIGH", "MEDIUM"]:
            tid = f"t-{sev.lower()}"
            tickets[tid] = FakeTicket(tid, severity=FakeSeverity(sev))
            dispatcher.enqueue_investigation(tickets[tid])

        execution_order: List[str] = []
        order_lock = threading.Lock()

        def worker_fn(t):
            with order_lock:
                execution_order.append(t.ticket_id)
            return True

        def lookup(tid):
            return tickets.get(tid)

        with ThreadPoolExecutor(max_workers=1) as pool:
            # Using max_workers=1 to ensure serial execution for ordering test
            results = dispatcher.dispatch_parallel(
                "investigate", worker_fn, lookup, pool, max_tasks=3
            )

        assert len(results) == 3
        assert all(r.success for r in results)
        # With 1 worker, tasks claimed in priority order should execute in order
        assert execution_order == ["t-critical", "t-high", "t-medium"]
        assert queue.queue_depth("investigate") == 0


# ---------------------------------------------------------------------------
# 2. GuardrailsCoordinator with real CircuitBreaker
# ---------------------------------------------------------------------------

class TestGuardrailsWithCircuitBreaker:
    """Verify GuardrailsCoordinator integrates correctly with a real CircuitBreaker."""

    def _make_circuit_breaker(self, tmp_path: Path) -> CircuitBreaker:
        state_file = str(tmp_path / "cb_state.json")
        return CircuitBreaker(
            state_path=state_file,
            window_size=5,
            failure_threshold=0.8,
            pause_duration_minutes=30,
        )

    def test_tripped_breaker_blocks_guardrails(self, tmp_path):
        """Record enough failures to trip the breaker, then guardrails should block."""
        cb = self._make_circuit_breaker(tmp_path)
        guardrails = GuardrailsCoordinator()
        guardrails.set_circuit_breaker(cb)

        # Initially should allow
        decision = guardrails.can_proceed(task_type="investigate", ticket_severity="HIGH")
        assert decision.allowed is True

        # Trip the circuit breaker: 5 failures with window_size=5 => 100% failure rate
        for _ in range(5):
            cb.record_result(False)

        assert cb.is_paused is True
        assert cb.failure_rate >= 0.8

        # Now guardrails should block
        decision = guardrails.can_proceed(task_type="investigate", ticket_severity="CRITICAL")
        assert decision.allowed is False
        assert decision.gate == "circuit_breaker"
        assert "Circuit breaker paused" in decision.reason

    def test_cleared_breaker_allows_guardrails(self, tmp_path):
        """After clearing the pause, guardrails should allow work again."""
        cb = self._make_circuit_breaker(tmp_path)
        guardrails = GuardrailsCoordinator()
        guardrails.set_circuit_breaker(cb)

        # Trip it
        for _ in range(5):
            cb.record_result(False)
        assert cb.is_paused is True

        # Clear the pause
        cb.clear_pause()
        assert cb.is_paused is False

        # Guardrails should allow again
        decision = guardrails.can_proceed(task_type="investigate", ticket_severity="HIGH")
        assert decision.allowed is True
        assert decision.gate == "all_clear"

    def test_guardrails_health_reflects_breaker_state(self, tmp_path):
        """GuardrailHealth correctly reports circuit breaker state."""
        cb = self._make_circuit_breaker(tmp_path)
        guardrails = GuardrailsCoordinator()
        guardrails.set_circuit_breaker(cb)

        health = guardrails.health()
        assert health.circuit_breaker_paused is False
        assert health.circuit_breaker_failure_rate == 0.0

        # Trip it
        for _ in range(5):
            cb.record_result(False)

        health = guardrails.health()
        assert health.circuit_breaker_paused is True
        assert health.circuit_breaker_failure_rate >= 0.8

    def test_breaker_state_persists_to_disk(self, tmp_path):
        """CircuitBreaker saves and reloads state from disk."""
        state_file = str(tmp_path / "cb_persist.json")
        cb1 = CircuitBreaker(
            state_path=state_file,
            window_size=5,
            failure_threshold=0.8,
            pause_duration_minutes=30,
        )
        # Record 3 failures (not enough to trip)
        for _ in range(3):
            cb1.record_result(False)
        cb1.record_result(True)

        # Create a new instance from the same file
        cb2 = CircuitBreaker(
            state_path=state_file,
            window_size=5,
            failure_threshold=0.8,
            pause_duration_minutes=30,
        )
        # Should have loaded the persisted results
        assert len(cb2._results) == 4
        assert cb2.failure_rate == 0.75  # 3/4


# ---------------------------------------------------------------------------
# 3. RBAC + Agent integration
# ---------------------------------------------------------------------------

class TestRBACAgentIntegration:
    """Test require_permission and require_sandbox decorators with real-ish objects."""

    def test_require_permission_allowed(self):
        """Method executes when RBAC engine grants permission."""

        class FakeEngine:
            def check_permission(self, agent, task, context=None):
                return (True, "granted")

        class MyAgent:
            def __init__(self):
                self._rbac_engine = FakeEngine()
                self._agent_name = "swe_developer"

            @require_permission("code_generation")
            def do_work(self):
                return "work_done"

        agent = MyAgent()
        assert agent.do_work() == "work_done"

    def test_require_permission_denied_raises(self):
        """Method raises PermissionDeniedError when RBAC engine denies permission."""

        class FakeEngine:
            def check_permission(self, agent, task, context=None):
                return (False, "no permission")

        class MyAgent:
            def __init__(self):
                self._rbac_engine = FakeEngine()
                self._agent_name = "rogue_agent"

            @require_permission("code_generation")
            def do_work(self):
                return "should_not_reach"

        agent = MyAgent()
        with pytest.raises(PermissionDeniedError, match="RBAC denied"):
            agent.do_work()

    def test_require_permission_no_engine_skips(self):
        """Without an RBAC engine, the decorator is a no-op (backward compat)."""

        class MyAgent:
            @require_permission("code_generation")
            def do_work(self):
                return "work_done"

        agent = MyAgent()
        assert agent.do_work() == "work_done"

    def test_require_permission_return_none_on_deny(self):
        """With fail_action='return_none', denied permission returns None."""

        class FakeEngine:
            def check_permission(self, agent, task, context=None):
                return (False, "denied")

        class MyAgent:
            def __init__(self):
                self._rbac_engine = FakeEngine()
                self._agent_name = "limited_agent"

            @require_permission("deploy", fail_action="return_none")
            def deploy(self):
                return "deployed"

        agent = MyAgent()
        assert agent.deploy() is None

    def test_require_sandbox_inside_allowed_path(self, tmp_path):
        """Method executes when cwd is inside a sandbox path."""
        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()

        class MyAgent:
            def __init__(self):
                self._sandbox_paths = [sandbox_dir]
                self._repo_root = sandbox_dir

            @require_sandbox
            def do_work(self):
                return "sandboxed_work"

        agent = MyAgent()
        assert agent.do_work() == "sandboxed_work"

    def test_require_sandbox_outside_raises(self, tmp_path):
        """Method raises SandboxViolationError when cwd is outside sandbox paths."""
        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()

        class MyAgent:
            def __init__(self):
                self._sandbox_paths = [sandbox_dir]
                self._repo_root = outside_dir

            @require_sandbox
            def do_work(self):
                return "should_not_reach"

        agent = MyAgent()
        with pytest.raises(SandboxViolationError, match="outside all sandbox paths"):
            agent.do_work()

    def test_require_sandbox_no_paths_skips(self):
        """Without sandbox_paths, the decorator is a no-op."""

        class MyAgent:
            @require_sandbox
            def do_work(self):
                return "no_sandbox_check"

        agent = MyAgent()
        assert agent.do_work() == "no_sandbox_check"

    def test_require_sandbox_subdirectory_allowed(self, tmp_path):
        """A subdirectory of a sandbox path is allowed."""
        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()
        sub_dir = sandbox_dir / "project" / "src"
        sub_dir.mkdir(parents=True)

        class MyAgent:
            def __init__(self):
                self._sandbox_paths = [sandbox_dir]
                self._repo_root = sub_dir

            @require_sandbox
            def do_work(self):
                return "sub_ok"

        agent = MyAgent()
        assert agent.do_work() == "sub_ok"


# ---------------------------------------------------------------------------
# 4. Provider factory round-trips
# ---------------------------------------------------------------------------

class TestProviderFactoryRoundTrips:
    """Verify each provider factory resolves defaults and rejects unknowns."""

    def test_coding_engine_resolve_claude(self):
        """resolve_engine('claude') returns a CodingEngine instance."""
        from src.swe_team.providers.coding_engine import resolve_engine
        from src.swe_team.providers.coding_engine.base import CodingEngine

        engine = resolve_engine("claude", {})
        assert isinstance(engine, CodingEngine)

    def test_coding_engine_unknown_raises(self):
        from src.swe_team.providers.coding_engine import resolve_engine

        with pytest.raises(ValueError, match="Unknown coding engine provider"):
            resolve_engine("nonexistent_engine", {})

    def test_notification_create_telegram(self):
        """create_notification_provider('telegram') returns a NotificationProvider."""
        from src.swe_team.providers.notification import create_notification_provider
        from src.swe_team.providers.notification.base import NotificationProvider

        provider = create_notification_provider("telegram", {"token": "t", "chat_id": "c"})
        assert isinstance(provider, NotificationProvider)

    def test_notification_unknown_raises(self):
        from src.swe_team.providers.notification import create_notification_provider

        with pytest.raises(ValueError, match="Unknown notification provider"):
            create_notification_provider("carrier_pigeon", {})

    def test_issue_tracker_create_github(self):
        """create_issue_tracker('github') returns an IssueTracker."""
        from src.swe_team.providers.issue_tracker import create_issue_tracker
        from src.swe_team.providers.issue_tracker.base import IssueTracker

        tracker = create_issue_tracker("github", {"repo": "test/repo", "token": "tok"})
        assert isinstance(tracker, IssueTracker)

    def test_issue_tracker_unknown_raises(self):
        from src.swe_team.providers.issue_tracker import create_issue_tracker

        with pytest.raises(ValueError, match="Unknown issue tracker provider"):
            create_issue_tracker("jira_dream", {})

    def test_workspace_create_git_worktree(self):
        """create_workspace_provider('git-worktree') returns a WorkspaceProvider."""
        from src.swe_team.providers.workspace import create_workspace_provider
        from src.swe_team.providers.workspace.base import WorkspaceProvider

        provider = create_workspace_provider("git-worktree", {})
        assert isinstance(provider, WorkspaceProvider)

    def test_workspace_unknown_raises(self):
        from src.swe_team.providers.workspace import create_workspace_provider

        with pytest.raises(ValueError, match="Unknown workspace provider"):
            create_workspace_provider("magic_fs", {})

    def test_sandbox_create_local(self):
        """create_sandbox_provider('local') returns a SandboxProvider."""
        from src.swe_team.providers.sandbox import create_sandbox_provider
        from src.swe_team.providers.sandbox.base import SandboxProvider

        provider = create_sandbox_provider("local", {})
        assert isinstance(provider, SandboxProvider)

    def test_sandbox_unknown_raises(self):
        from src.swe_team.providers.sandbox import create_sandbox_provider

        with pytest.raises(ValueError, match="Unknown sandbox provider"):
            create_sandbox_provider("teleporter", {})

    def test_repomap_create_ctags(self):
        """create_repomap_provider('ctags') returns a RepoMapProvider."""
        from src.swe_team.providers.repomap import create_repomap_provider
        from src.swe_team.providers.repomap.base import RepoMapProvider

        provider = create_repomap_provider("ctags", {})
        assert isinstance(provider, RepoMapProvider)

    def test_repomap_unknown_raises(self):
        from src.swe_team.providers.repomap import create_repomap_provider

        with pytest.raises(ValueError, match="Unknown repo map provider"):
            create_repomap_provider("ast_magic", {})


# ---------------------------------------------------------------------------
# 5. Full pipeline simulation
# ---------------------------------------------------------------------------

class TestFullPipelineSimulation:
    """End-to-end: enqueue -> guardrails -> dispatch -> complete/block."""

    def test_happy_path_enqueue_check_dispatch_complete(self, tmp_path):
        """Ticket flows: enqueue -> guardrails pass -> dispatch -> worker succeeds -> done."""
        # Set up components
        queue = InMemoryTaskQueue()
        dispatcher = QueuedDispatcher(queue, worker_id="pipeline-worker")
        cb = CircuitBreaker(
            state_path=str(tmp_path / "cb.json"),
            window_size=5,
            failure_threshold=0.8,
            pause_duration_minutes=30,
        )
        guardrails = GuardrailsCoordinator()
        guardrails.set_circuit_breaker(cb)

        # Enqueue a ticket
        ticket = FakeTicket("pipeline-1", severity=FakeSeverity("HIGH"))
        dispatcher.enqueue_investigation(ticket)
        assert queue.queue_depth("investigate") == 1

        # Guardrails check
        decision = guardrails.can_proceed(task_type="investigate", ticket_severity="HIGH")
        assert decision.allowed is True

        # Dispatch
        def worker_fn(t):
            return True

        def lookup(tid):
            return ticket if tid == "pipeline-1" else None

        result = dispatcher.dispatch_one("investigate", worker_fn, lookup)
        assert result is not None
        assert result.success is True

        # Record success in circuit breaker
        cb.record_result(True)
        assert cb.failure_rate == 0.0

        # Queue is empty, no dead-letter
        assert queue.queue_depth("investigate") == 0
        assert len(queue.get_dead_letter()) == 0

    def test_circuit_breaker_blocks_pipeline(self, tmp_path):
        """When circuit breaker trips, guardrails block even with queued tasks."""
        queue = InMemoryTaskQueue()
        dispatcher = QueuedDispatcher(queue, worker_id="pipeline-worker")
        cb = CircuitBreaker(
            state_path=str(tmp_path / "cb_block.json"),
            window_size=5,
            failure_threshold=0.8,
            pause_duration_minutes=30,
        )
        guardrails = GuardrailsCoordinator()
        guardrails.set_circuit_breaker(cb)

        # Enqueue a ticket
        ticket = FakeTicket("blocked-1", severity=FakeSeverity("CRITICAL"))
        dispatcher.enqueue_investigation(ticket)

        # Trip the circuit breaker
        for _ in range(5):
            cb.record_result(False)
        assert cb.is_paused is True

        # Guardrails should block
        decision = guardrails.can_proceed(task_type="investigate", ticket_severity="CRITICAL")
        assert decision.allowed is False
        assert decision.gate == "circuit_breaker"

        # The task is still in the queue (not lost)
        assert queue.queue_depth("investigate") == 1

    def test_pipeline_failure_to_dead_letter_trips_breaker(self, tmp_path):
        """Failed tasks accumulate, eventually tripping the circuit breaker."""
        queue = InMemoryTaskQueue()
        dispatcher = QueuedDispatcher(queue, worker_id="pipeline-worker")
        cb = CircuitBreaker(
            state_path=str(tmp_path / "cb_dlq.json"),
            window_size=5,
            failure_threshold=0.8,
            pause_duration_minutes=30,
        )
        guardrails = GuardrailsCoordinator()
        guardrails.set_circuit_breaker(cb)

        tickets = {}
        for i in range(5):
            tid = f"fail-{i}"
            tickets[tid] = FakeTicket(tid, severity=FakeSeverity("MEDIUM"))

        def worker_fn(t):
            return False

        def lookup(tid):
            return tickets.get(tid)

        # Process 5 tickets, each failing through all retries
        for tid, ticket in tickets.items():
            dispatcher.enqueue_investigation(ticket)

            # Exhaust retries for this ticket
            for attempt in range(3):
                # Clear retry delays
                for t in queue._tasks.values():
                    if t.status == "queued" and t.next_retry_at is not None:
                        t.next_retry_at = 0

                result = dispatcher.dispatch_one("investigate", worker_fn, lookup)
                if result:
                    cb.record_result(result.success)

        # Circuit breaker should be tripped
        assert cb.is_paused is True

        # Dead-letter queue should have entries
        dlq = queue.get_dead_letter()
        assert len(dlq) == 5

        # Guardrails should block new work
        decision = guardrails.can_proceed(task_type="investigate", ticket_severity="HIGH")
        assert decision.allowed is False

    def test_pipeline_recovery_after_clear(self, tmp_path):
        """After clearing breaker, the pipeline can resume processing."""
        queue = InMemoryTaskQueue()
        dispatcher = QueuedDispatcher(queue, worker_id="pipeline-worker")
        cb = CircuitBreaker(
            state_path=str(tmp_path / "cb_recover.json"),
            window_size=5,
            failure_threshold=0.8,
            pause_duration_minutes=30,
        )
        guardrails = GuardrailsCoordinator()
        guardrails.set_circuit_breaker(cb)

        # Trip the breaker
        for _ in range(5):
            cb.record_result(False)
        assert cb.is_paused is True

        decision = guardrails.can_proceed(task_type="investigate", ticket_severity="HIGH")
        assert decision.allowed is False

        # Clear and enqueue new work
        cb.clear_pause()
        ticket = FakeTicket("recover-1", severity=FakeSeverity("HIGH"))
        dispatcher.enqueue_investigation(ticket)

        decision = guardrails.can_proceed(task_type="investigate", ticket_severity="HIGH")
        assert decision.allowed is True

        # Dispatch succeeds
        result = dispatcher.dispatch_one(
            "investigate",
            lambda t: True,
            lambda tid: ticket if tid == "recover-1" else None,
        )
        assert result is not None
        assert result.success is True

    def test_guardrails_health_with_dispatcher(self, tmp_path):
        """GuardrailsCoordinator health() reports queue depth and dead-letter count."""
        queue = InMemoryTaskQueue()
        dispatcher = QueuedDispatcher(queue, worker_id="pipeline-worker")
        cb = CircuitBreaker(
            state_path=str(tmp_path / "cb_health.json"),
            window_size=5,
            failure_threshold=0.8,
            pause_duration_minutes=30,
        )
        guardrails = GuardrailsCoordinator()
        guardrails.set_circuit_breaker(cb)
        guardrails.set_queued_dispatcher(dispatcher)

        # Enqueue some tasks
        for sev in ["CRITICAL", "HIGH", "MEDIUM"]:
            ticket = FakeTicket(f"h-{sev.lower()}", severity=FakeSeverity(sev))
            dispatcher.enqueue_investigation(ticket)

        health = guardrails.health()
        assert health.circuit_breaker_paused is False
        assert health.queue_depth == 3
        assert health.dead_letter_count == 0
