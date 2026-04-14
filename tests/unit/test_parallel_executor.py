"""Tests for the parallel execution engine."""

import threading
import time
from concurrent.futures import Future
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.parallel_executor import (
    AdaptiveConfig,
    AdaptiveScheduleEntry,
    ExecutionConfig,
    ExecutionProfile,
    ParallelExecutor,
    TaskResult,
    ThroughputMetrics,
)


# ---------------------------------------------------------------------------
# ExecutionProfile
# ---------------------------------------------------------------------------

class TestExecutionProfile:
    def test_defaults(self):
        p = ExecutionProfile()
        assert p.max_concurrent_investigations == 2
        assert p.max_concurrent_developments == 1
        assert p.cycle_interval_seconds == 900

    def test_from_dict(self):
        p = ExecutionProfile.from_dict({
            "max_concurrent_investigations": 5,
            "max_concurrent_developments": 3,
            "cycle_interval_seconds": 300,
        })
        assert p.max_concurrent_investigations == 5
        assert p.max_concurrent_developments == 3
        assert p.cycle_interval_seconds == 300

    def test_to_dict(self):
        p = ExecutionProfile(max_concurrent_investigations=8, max_concurrent_developments=5)
        d = p.to_dict()
        assert d["max_concurrent_investigations"] == 8
        assert d["max_concurrent_developments"] == 5


# ---------------------------------------------------------------------------
# ExecutionConfig
# ---------------------------------------------------------------------------

class TestExecutionConfig:
    def test_defaults(self):
        c = ExecutionConfig()
        assert c.mode == "sequential"
        assert "base" in c.profiles
        assert "burst" in c.profiles
        assert "max" in c.profiles

    def test_from_dict_empty(self):
        c = ExecutionConfig.from_dict({})
        assert c.mode == "sequential"
        assert "base" in c.profiles

    def test_from_dict_full(self):
        c = ExecutionConfig.from_dict({
            "mode": "adaptive",
            "profiles": {
                "base": {"max_concurrent_investigations": 3},
                "burst": {"max_concurrent_investigations": 6},
            },
            "adaptive": {
                "backlog_burst_threshold": 20,
                "backlog_max_threshold": 50,
                "schedule": [
                    {"hours": "0-8", "profile": "burst"},
                ],
            },
        })
        assert c.mode == "adaptive"
        assert c.profiles["base"].max_concurrent_investigations == 3
        assert c.adaptive.backlog_burst_threshold == 20
        assert len(c.adaptive.schedule) == 1

    def test_to_dict_roundtrip(self):
        c = ExecutionConfig()
        d = c.to_dict()
        c2 = ExecutionConfig.from_dict(d)
        assert c2.mode == c.mode
        assert c2.profiles["base"].max_concurrent_investigations == c.profiles["base"].max_concurrent_investigations


# ---------------------------------------------------------------------------
# ThroughputMetrics
# ---------------------------------------------------------------------------

class TestThroughputMetrics:
    def test_record_investigation(self):
        m = ThroughputMetrics()
        m.record_investigation(10.0, True)
        m.record_investigation(5.0, False)
        snap = m.snapshot()
        assert snap["investigations_completed"] == 1
        assert snap["investigations_failed"] == 1
        assert snap["avg_investigation_time_s"] == 7.5

    def test_record_development(self):
        m = ThroughputMetrics()
        m.record_development(20.0, True)
        snap = m.snapshot()
        assert snap["developments_completed"] == 1
        assert snap["avg_development_time_s"] == 20.0

    def test_empty_snapshot(self):
        m = ThroughputMetrics()
        snap = m.snapshot()
        assert snap["investigations_completed"] == 0
        assert snap["avg_investigation_time_s"] == 0.0

    def test_thread_safety(self):
        m = ThroughputMetrics()
        errors = []

        def record_many():
            try:
                for _ in range(100):
                    m.record_investigation(0.1, True)
                    m.record_development(0.1, True)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        snap = m.snapshot()
        assert snap["investigations_completed"] == 400
        assert snap["developments_completed"] == 400


# ---------------------------------------------------------------------------
# ParallelExecutor
# ---------------------------------------------------------------------------

def _make_executor(mode="parallel", profile="base"):
    config = ExecutionConfig(mode=mode)
    return ParallelExecutor(execution_config=config, active_profile=profile)


class TestParallelExecutor:
    def test_init(self):
        executor = _make_executor()
        assert executor.active_profile_name == "base"
        assert executor.active_count() == 0

    def test_scale_to(self):
        executor = _make_executor()
        executor.scale_to("burst")
        assert executor.active_profile_name == "burst"
        assert executor.active_profile.max_concurrent_investigations == 5

    def test_scale_to_unknown_raises(self):
        executor = _make_executor()
        with pytest.raises(ValueError, match="Unknown profile"):
            executor.scale_to("nonexistent")

    def test_scale_to_same_noop(self):
        executor = _make_executor()
        executor.scale_to("base")  # should not raise
        assert executor.active_profile_name == "base"

    def test_submit_investigation_success(self):
        executor = _make_executor()
        ticket = MagicMock()
        ticket.ticket_id = "test-123"
        investigator = MagicMock()
        investigator.investigate.return_value = True

        future = executor.submit_investigation(ticket, investigator)
        result = future.result(timeout=5)

        assert isinstance(result, TaskResult)
        assert result.success is True
        assert result.ticket_id == "test-123"
        assert result.task_type == "investigation"
        investigator.investigate.assert_called_once_with(ticket)
        executor.shutdown(wait=True)

    def test_submit_investigation_failure(self):
        executor = _make_executor()
        ticket = MagicMock()
        ticket.ticket_id = "test-456"
        investigator = MagicMock()
        investigator.investigate.side_effect = RuntimeError("Claude CLI timeout")

        future = executor.submit_investigation(ticket, investigator)
        result = future.result(timeout=5)

        assert result.success is False
        assert "Claude CLI timeout" in result.error
        executor.shutdown(wait=True)

    def test_submit_development_success(self):
        executor = _make_executor()
        ticket = MagicMock()
        ticket.ticket_id = "dev-789"
        developer = MagicMock()
        developer.attempt_fix.return_value = True

        future = executor.submit_development(ticket, developer)
        result = future.result(timeout=5)

        assert result.success is True
        assert result.task_type == "development"
        developer.attempt_fix.assert_called_once_with(ticket)
        executor.shutdown(wait=True)

    def test_submit_development_failure(self):
        executor = _make_executor()
        ticket = MagicMock()
        ticket.ticket_id = "dev-fail"
        developer = MagicMock()
        developer.attempt_fix.return_value = False

        future = executor.submit_development(ticket, developer)
        result = future.result(timeout=5)

        assert result.success is False
        assert result.task_type == "development"
        executor.shutdown(wait=True)

    def test_collect_results(self):
        executor = _make_executor()
        tickets = [MagicMock(ticket_id=f"t-{i}") for i in range(3)]
        investigator = MagicMock()
        investigator.investigate.return_value = True

        futures = [executor.submit_investigation(t, investigator) for t in tickets]
        results = executor.collect_results(futures, timeout=10)

        assert len(results) == 3
        assert all(r.success for r in results)
        executor.shutdown(wait=True)

    def test_concurrent_execution(self):
        """Verify tasks actually run concurrently."""
        executor = _make_executor(profile="burst")  # 5 investigation slots
        active_counts = []
        lock = threading.Lock()

        def slow_investigate(ticket):
            with lock:
                active_counts.append(executor.active_investigation_count())
            time.sleep(0.1)
            return True

        tickets = [MagicMock(ticket_id=f"c-{i}") for i in range(3)]
        investigator = MagicMock()
        investigator.investigate.side_effect = slow_investigate

        futures = [executor.submit_investigation(t, investigator) for t in tickets]
        executor.collect_results(futures, timeout=10)

        # At least some should have seen >1 concurrent
        assert max(active_counts) >= 2 or len(tickets) < 2
        executor.shutdown(wait=True)

    def test_on_ticket_complete_callback(self):
        callback = MagicMock()
        config = ExecutionConfig(mode="parallel")
        executor = ParallelExecutor(
            execution_config=config,
            on_ticket_complete=callback,
        )

        ticket = MagicMock()
        ticket.ticket_id = "cb-test"
        investigator = MagicMock()
        investigator.investigate.return_value = True

        future = executor.submit_investigation(ticket, investigator)
        future.result(timeout=5)

        callback.assert_called_once_with(ticket, "investigation", True)
        executor.shutdown(wait=True)

    def test_shutdown_prevents_new_submissions(self):
        executor = _make_executor()
        executor.shutdown(wait=True)

        ticket = MagicMock()
        investigator = MagicMock()

        with pytest.raises(RuntimeError, match="shut down"):
            executor.submit_investigation(ticket, investigator)

    def test_status(self):
        executor = _make_executor()
        status = executor.status()

        assert status["mode"] == "parallel"
        assert status["active_profile"] == "base"
        assert status["active_investigations"] == 0
        assert status["active_developments"] == 0
        assert "metrics" in status
        assert "utilization" in status
        executor.shutdown(wait=True)

    def test_metrics_tracking(self):
        executor = _make_executor()
        ticket = MagicMock(ticket_id="m-1")
        investigator = MagicMock()
        investigator.investigate.return_value = True

        future = executor.submit_investigation(ticket, investigator)
        future.result(timeout=5)

        snap = executor.metrics.snapshot()
        assert snap["investigations_completed"] == 1
        assert snap["investigations_per_hour"] > 0
        executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Adaptive profile resolution
# ---------------------------------------------------------------------------

class TestAdaptiveProfileResolution:
    def test_backlog_override_max(self):
        config = ExecutionConfig(
            mode="adaptive",
            adaptive=AdaptiveConfig(
                backlog_burst_threshold=30,
                backlog_max_threshold=80,
            ),
        )
        executor = ParallelExecutor(execution_config=config)
        profile = executor.resolve_adaptive_profile(backlog_size=100)
        assert profile == "max"
        executor.shutdown(wait=False)

    def test_backlog_override_burst(self):
        config = ExecutionConfig(
            mode="adaptive",
            adaptive=AdaptiveConfig(
                backlog_burst_threshold=30,
                backlog_max_threshold=80,
            ),
        )
        executor = ParallelExecutor(execution_config=config)
        profile = executor.resolve_adaptive_profile(backlog_size=50)
        assert profile == "burst"
        executor.shutdown(wait=False)

    def test_time_schedule(self):
        config = ExecutionConfig(
            mode="adaptive",
            adaptive=AdaptiveConfig(
                schedule=[
                    AdaptiveScheduleEntry(hours="0-8", profile="burst"),
                    AdaptiveScheduleEntry(hours="8-17", profile="base"),
                    AdaptiveScheduleEntry(hours="17-24", profile="burst"),
                ],
            ),
        )
        executor = ParallelExecutor(execution_config=config)

        # 3 AM UTC → overnight → burst
        night = datetime(2026, 3, 19, 3, 0, tzinfo=timezone.utc)
        assert executor.resolve_adaptive_profile(backlog_size=0, now_utc=night) == "burst"

        # 10 AM UTC → business → base
        day = datetime(2026, 3, 19, 10, 0, tzinfo=timezone.utc)
        assert executor.resolve_adaptive_profile(backlog_size=0, now_utc=day) == "base"

        # 20 PM UTC → evening → burst
        eve = datetime(2026, 3, 19, 20, 0, tzinfo=timezone.utc)
        assert executor.resolve_adaptive_profile(backlog_size=0, now_utc=eve) == "burst"

        executor.shutdown(wait=False)

    def test_backlog_overrides_schedule(self):
        """Backlog thresholds take priority over time-based schedule."""
        config = ExecutionConfig(
            mode="adaptive",
            adaptive=AdaptiveConfig(
                backlog_burst_threshold=30,
                backlog_max_threshold=80,
                schedule=[
                    AdaptiveScheduleEntry(hours="0-24", profile="base"),
                ],
            ),
        )
        executor = ParallelExecutor(execution_config=config)

        # Even during "base" hours, high backlog forces max
        profile = executor.resolve_adaptive_profile(backlog_size=100)
        assert profile == "max"
        executor.shutdown(wait=False)

    def test_fallback_to_base(self):
        """When no schedule matches, fall back to base."""
        config = ExecutionConfig(
            mode="adaptive",
            adaptive=AdaptiveConfig(schedule=[]),
        )
        executor = ParallelExecutor(execution_config=config)
        profile = executor.resolve_adaptive_profile(backlog_size=0)
        assert profile == "base"
        executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# TaskResult
# ---------------------------------------------------------------------------

class TestTaskResult:
    def test_basic(self):
        r = TaskResult(
            ticket_id="t-1",
            task_type="investigation",
            success=True,
            duration_s=10.5,
        )
        assert r.ticket_id == "t-1"
        assert r.success is True
        assert r.error is None

    def test_with_error(self):
        r = TaskResult(
            ticket_id="t-2",
            task_type="development",
            success=False,
            duration_s=5.0,
            error="timeout",
        )
        assert r.success is False
        assert r.error == "timeout"


# ---------------------------------------------------------------------------
# RateLimitCooldown propagation through parallel executor
# ---------------------------------------------------------------------------

class TestRateLimitCooldownPropagation:
    """Verify RateLimitCooldown escapes the thread pool instead of being swallowed."""

    def test_investigation_propagates_cooldown(self):
        from src.swe_team.rate_limiter import RateLimitCooldown

        executor = _make_executor()
        ticket = MagicMock(ticket_id="rl-inv-1")
        investigator = MagicMock()
        investigator.investigate.side_effect = RateLimitCooldown(
            "rate limited", cooldown_seconds=600
        )

        future = executor.submit_investigation(ticket, investigator)
        with pytest.raises(RateLimitCooldown):
            future.result(timeout=5)
        executor.shutdown(wait=True)

    def test_development_propagates_cooldown(self):
        from src.swe_team.rate_limiter import RateLimitCooldown

        executor = _make_executor()
        ticket = MagicMock(ticket_id="rl-worker-1")
        developer = MagicMock()
        developer.attempt_fix.side_effect = RateLimitCooldown(
            "rate limited", cooldown_seconds=600
        )

        future = executor.submit_development(ticket, developer)
        with pytest.raises(RateLimitCooldown):
            future.result(timeout=5)
        executor.shutdown(wait=True)

    def test_collect_results_propagates_cooldown(self):
        from src.swe_team.rate_limiter import RateLimitCooldown

        executor = _make_executor()
        ticket = MagicMock(ticket_id="rl-collect-1")
        investigator = MagicMock()
        investigator.investigate.side_effect = RateLimitCooldown(
            "rate limited", cooldown_seconds=600
        )

        futures = [executor.submit_investigation(ticket, investigator)]
        with pytest.raises(RateLimitCooldown):
            executor.collect_results(futures, timeout=5)
        executor.shutdown(wait=True)

    def test_non_cooldown_exceptions_still_caught(self):
        """Regular exceptions should still be caught and turned into TaskResult."""
        executor = _make_executor()
        ticket = MagicMock(ticket_id="rl-other-1")
        investigator = MagicMock()
        investigator.investigate.side_effect = RuntimeError("some other error")

        future = executor.submit_investigation(ticket, investigator)
        result = future.result(timeout=5)

        assert result.success is False
        assert "some other error" in result.error
        executor.shutdown(wait=True)
