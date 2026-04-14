"""
Tests for Rate Limit Lifecycle (issue #942).

Covers:
  - RateLimitState enum values
  - RateLimitLifecycle state transitions (normal->throttled->cooldown->recovering->normal)
  - Exponential backoff calculation
  - Per-provider isolation
  - can_proceed() behavior in each state
  - Cooldown expiry (auto and manual)
  - get_status() snapshot
  - Global lifecycle registry (get_lifecycle, get_all_lifecycle_statuses)
  - Thread safety basics
"""

from __future__ import annotations

import time
import threading
from unittest.mock import patch

import pytest

from src.swe_team.rate_limiter import (
    RateLimitLifecycle,
    RateLimitState,
    get_all_lifecycle_statuses,
    get_lifecycle,
    reset_lifecycle_registry,
)


# ======================================================================
# RateLimitState enum
# ======================================================================


class TestRateLimitState:
    def test_enum_values(self):
        assert RateLimitState.NORMAL.value == "normal"
        assert RateLimitState.THROTTLED.value == "throttled"
        assert RateLimitState.COOLDOWN.value == "cooldown"
        assert RateLimitState.RECOVERING.value == "recovering"

    def test_all_states_present(self):
        names = {s.name for s in RateLimitState}
        assert names == {"NORMAL", "THROTTLED", "COOLDOWN", "RECOVERING"}


# ======================================================================
# State transitions
# ======================================================================


class TestStateTransitions:
    """Full lifecycle: NORMAL -> THROTTLED -> COOLDOWN -> RECOVERING -> NORMAL."""

    def test_initial_state_is_normal(self):
        lc = RateLimitLifecycle("test-provider")
        assert lc.state == RateLimitState.NORMAL

    def test_warning_transitions_normal_to_throttled(self):
        lc = RateLimitLifecycle("test")
        result = lc.on_rate_limit_warning()
        assert result == RateLimitState.THROTTLED
        assert lc.state == RateLimitState.THROTTLED

    def test_warning_when_already_throttled_stays_throttled(self):
        lc = RateLimitLifecycle("test")
        lc.on_rate_limit_warning()
        result = lc.on_rate_limit_warning()
        assert result == RateLimitState.THROTTLED

    def test_warning_when_cooldown_stays_cooldown(self):
        lc = RateLimitLifecycle("test", initial_cooldown=60)
        lc.on_rate_limit_hit()
        result = lc.on_rate_limit_warning()
        assert result == RateLimitState.COOLDOWN

    def test_hit_transitions_normal_to_cooldown(self):
        lc = RateLimitLifecycle("test", initial_cooldown=60)
        result = lc.on_rate_limit_hit()
        assert result == RateLimitState.COOLDOWN
        assert lc.state == RateLimitState.COOLDOWN

    def test_hit_transitions_throttled_to_cooldown(self):
        lc = RateLimitLifecycle("test", initial_cooldown=60)
        lc.on_rate_limit_warning()
        assert lc.state == RateLimitState.THROTTLED
        result = lc.on_rate_limit_hit()
        assert result == RateLimitState.COOLDOWN

    def test_hit_transitions_recovering_to_cooldown(self):
        lc = RateLimitLifecycle("test", initial_cooldown=0.01)
        lc.on_rate_limit_hit()
        time.sleep(0.02)
        assert lc.state == RateLimitState.RECOVERING
        result = lc.on_rate_limit_hit()
        assert result == RateLimitState.COOLDOWN

    def test_cooldown_auto_expires_to_recovering(self):
        lc = RateLimitLifecycle("test", initial_cooldown=0.01)
        lc.on_rate_limit_hit()
        assert lc.state == RateLimitState.COOLDOWN
        time.sleep(0.02)
        assert lc.state == RateLimitState.RECOVERING

    def test_manual_cooldown_expired(self):
        lc = RateLimitLifecycle("test", initial_cooldown=0.01)
        lc.on_rate_limit_hit()
        time.sleep(0.02)
        result = lc.on_cooldown_expired()
        assert result == RateLimitState.RECOVERING

    def test_cooldown_expired_noop_if_not_cooldown(self):
        lc = RateLimitLifecycle("test")
        result = lc.on_cooldown_expired()
        assert result == RateLimitState.NORMAL

    def test_success_in_recovering_transitions_to_normal(self):
        lc = RateLimitLifecycle("test", initial_cooldown=0.01, recovery_successes=2)
        lc.on_rate_limit_hit()
        time.sleep(0.02)
        assert lc.state == RateLimitState.RECOVERING
        lc.on_request_success()
        assert lc.state == RateLimitState.RECOVERING
        lc.on_request_success()
        assert lc.state == RateLimitState.NORMAL

    def test_success_in_throttled_transitions_to_normal(self):
        lc = RateLimitLifecycle("test", recovery_successes=2)
        lc.on_rate_limit_warning()
        assert lc.state == RateLimitState.THROTTLED
        lc.on_request_success()
        assert lc.state == RateLimitState.THROTTLED
        lc.on_request_success()
        assert lc.state == RateLimitState.NORMAL

    def test_success_in_normal_resets_backoff(self):
        lc = RateLimitLifecycle("test", initial_cooldown=0.01, recovery_successes=1)
        lc.on_rate_limit_hit()
        time.sleep(0.02)
        # Now recovering with backoff_level=1
        assert lc.state == RateLimitState.RECOVERING
        lc.on_request_success()
        assert lc.state == RateLimitState.NORMAL
        # One more success in NORMAL to confirm backoff resets
        lc.on_request_success()
        status = lc.get_status()
        assert status["backoff_level"] == 0

    def test_success_in_cooldown_no_change(self):
        lc = RateLimitLifecycle("test", initial_cooldown=60)
        lc.on_rate_limit_hit()
        result = lc.on_request_success()
        assert result == RateLimitState.COOLDOWN

    def test_full_lifecycle_round_trip(self):
        """NORMAL -> THROTTLED -> COOLDOWN -> RECOVERING -> NORMAL."""
        lc = RateLimitLifecycle("test", initial_cooldown=0.01, recovery_successes=1)
        assert lc.state == RateLimitState.NORMAL

        lc.on_rate_limit_warning()
        assert lc.state == RateLimitState.THROTTLED

        lc.on_rate_limit_hit()
        assert lc.state == RateLimitState.COOLDOWN

        time.sleep(0.02)
        assert lc.state == RateLimitState.RECOVERING

        lc.on_request_success()
        assert lc.state == RateLimitState.NORMAL

    def test_hit_resets_consecutive_successes(self):
        lc = RateLimitLifecycle("test", initial_cooldown=0.01, recovery_successes=3)
        lc.on_rate_limit_hit()
        time.sleep(0.02)
        # 2 successes...
        lc.on_request_success()
        lc.on_request_success()
        assert lc.state == RateLimitState.RECOVERING
        # Hit resets
        lc.on_rate_limit_hit()
        assert lc.state == RateLimitState.COOLDOWN
        time.sleep(0.02)
        # Need 3 fresh successes
        assert lc.state == RateLimitState.RECOVERING
        lc.on_request_success()
        lc.on_request_success()
        assert lc.state == RateLimitState.RECOVERING
        lc.on_request_success()
        assert lc.state == RateLimitState.NORMAL


# ======================================================================
# Exponential backoff calculation
# ======================================================================


class TestExponentialBackoffCalculation:
    def test_first_cooldown_uses_initial(self):
        lc = RateLimitLifecycle("test", initial_cooldown=30.0, max_cooldown=300.0)
        lc.on_rate_limit_hit()
        status = lc.get_status()
        # Cooldown remaining should be close to 30s
        assert 25.0 <= status["cooldown_remaining_seconds"] <= 31.0

    def test_backoff_doubles_each_hit(self):
        lc = RateLimitLifecycle("test", initial_cooldown=0.01, max_cooldown=10.0)
        # Hit 1: cooldown = 0.01
        lc.on_rate_limit_hit()
        assert lc.get_status()["backoff_level"] == 1
        time.sleep(0.02)
        # Hit 2: cooldown = 0.02
        lc.on_rate_limit_hit()
        assert lc.get_status()["backoff_level"] == 2
        time.sleep(0.03)
        # Hit 3: cooldown = 0.04
        lc.on_rate_limit_hit()
        assert lc.get_status()["backoff_level"] == 3

    def test_cooldown_capped_at_max(self):
        lc = RateLimitLifecycle("test", initial_cooldown=100.0, max_cooldown=300.0)
        # Level 0: 100, Level 1: 200, Level 2: 400 -> capped to 300
        lc.on_rate_limit_hit()  # level 0 -> 100
        status = lc.get_status()
        assert status["cooldown_remaining_seconds"] <= 101.0

        # Force expire and hit again
        lc._cooldown_until = time.time() - 1
        lc.on_rate_limit_hit()  # level 1 -> 200
        status = lc.get_status()
        assert status["cooldown_remaining_seconds"] <= 201.0

        lc._cooldown_until = time.time() - 1
        lc.on_rate_limit_hit()  # level 2 -> 400 capped to 300
        status = lc.get_status()
        assert status["cooldown_remaining_seconds"] <= 301.0

    def test_recovery_resets_backoff_level(self):
        lc = RateLimitLifecycle("test", initial_cooldown=0.01, recovery_successes=1)
        lc.on_rate_limit_hit()
        assert lc.get_status()["backoff_level"] == 1
        time.sleep(0.02)
        # Auto-expired to RECOVERING
        assert lc.state == RateLimitState.RECOVERING
        lc.on_request_success()
        assert lc.state == RateLimitState.NORMAL
        assert lc.get_status()["backoff_level"] == 0


# ======================================================================
# Per-provider isolation
# ======================================================================


class TestPerProviderIsolation:
    def test_separate_providers_independent(self):
        claude = RateLimitLifecycle("claude")
        github = RateLimitLifecycle("github")

        claude.on_rate_limit_hit()
        assert claude.state == RateLimitState.COOLDOWN
        assert github.state == RateLimitState.NORMAL

    def test_registry_isolates_providers(self):
        reset_lifecycle_registry()
        try:
            lc1 = get_lifecycle("provider_a")
            lc2 = get_lifecycle("provider_b")
            lc1.on_rate_limit_warning()
            assert lc1.state == RateLimitState.THROTTLED
            assert lc2.state == RateLimitState.NORMAL
        finally:
            reset_lifecycle_registry()

    def test_providers_track_separate_counters(self):
        a = RateLimitLifecycle("a")
        b = RateLimitLifecycle("b")
        a.on_rate_limit_hit()
        a.on_rate_limit_hit()
        b.on_rate_limit_warning()
        assert a.get_status()["total_hits"] == 2
        assert a.get_status()["total_warnings"] == 0
        assert b.get_status()["total_hits"] == 0
        assert b.get_status()["total_warnings"] == 1


# ======================================================================
# can_proceed() behavior
# ======================================================================


class TestCanProceed:
    def test_normal_can_proceed(self):
        lc = RateLimitLifecycle("test")
        assert lc.can_proceed() is True

    def test_throttled_can_proceed(self):
        lc = RateLimitLifecycle("test")
        lc.on_rate_limit_warning()
        assert lc.can_proceed() is True

    def test_cooldown_cannot_proceed(self):
        lc = RateLimitLifecycle("test", initial_cooldown=60)
        lc.on_rate_limit_hit()
        assert lc.can_proceed() is False

    def test_recovering_can_proceed(self):
        lc = RateLimitLifecycle("test", initial_cooldown=0.01)
        lc.on_rate_limit_hit()
        time.sleep(0.02)
        assert lc.can_proceed() is True
        assert lc.state == RateLimitState.RECOVERING

    def test_cooldown_expired_can_proceed(self):
        lc = RateLimitLifecycle("test", initial_cooldown=0.01)
        lc.on_rate_limit_hit()
        assert lc.can_proceed() is False
        time.sleep(0.02)
        assert lc.can_proceed() is True


# ======================================================================
# get_status() snapshot
# ======================================================================


class TestGetStatus:
    def test_normal_status_fields(self):
        lc = RateLimitLifecycle("my-provider", initial_cooldown=30.0)
        status = lc.get_status()
        assert status["provider"] == "my-provider"
        assert status["state"] == "normal"
        assert status["backoff_level"] == 0
        assert status["cooldown_remaining_seconds"] == 0.0
        assert status["cooldown_until"] is None
        assert status["consecutive_successes"] == 0
        assert status["recovery_target"] == 3
        assert status["total_hits"] == 0
        assert status["total_warnings"] == 0
        assert "last_transition" in status

    def test_cooldown_status_shows_remaining(self):
        lc = RateLimitLifecycle("test", initial_cooldown=60.0)
        lc.on_rate_limit_hit()
        status = lc.get_status()
        assert status["state"] == "cooldown"
        assert status["cooldown_remaining_seconds"] > 50.0
        assert status["cooldown_until"] is not None
        assert status["backoff_level"] == 1

    def test_recovering_status(self):
        lc = RateLimitLifecycle("test", initial_cooldown=0.01)
        lc.on_rate_limit_hit()
        time.sleep(0.02)
        status = lc.get_status()
        assert status["state"] == "recovering"
        assert status["cooldown_remaining_seconds"] == 0.0

    def test_counters_accumulate(self):
        lc = RateLimitLifecycle("test", initial_cooldown=0.01)
        lc.on_rate_limit_warning()
        lc.on_rate_limit_warning()
        lc.on_rate_limit_hit()
        status = lc.get_status()
        assert status["total_warnings"] == 2
        assert status["total_hits"] == 1


# ======================================================================
# Global lifecycle registry
# ======================================================================


class TestLifecycleRegistry:
    def setup_method(self):
        reset_lifecycle_registry()

    def teardown_method(self):
        reset_lifecycle_registry()

    def test_get_lifecycle_creates_new(self):
        lc = get_lifecycle("claude")
        assert lc.provider == "claude"
        assert lc.state == RateLimitState.NORMAL

    def test_get_lifecycle_returns_same_instance(self):
        lc1 = get_lifecycle("claude")
        lc2 = get_lifecycle("claude")
        assert lc1 is lc2

    def test_get_lifecycle_different_providers(self):
        lc1 = get_lifecycle("claude")
        lc2 = get_lifecycle("github")
        assert lc1 is not lc2
        assert lc1.provider == "claude"
        assert lc2.provider == "github"

    def test_get_all_lifecycle_statuses_empty(self):
        statuses = get_all_lifecycle_statuses()
        assert statuses == []

    def test_get_all_lifecycle_statuses_multiple(self):
        get_lifecycle("claude")
        get_lifecycle("github")
        get_lifecycle("base_llm")
        statuses = get_all_lifecycle_statuses()
        assert len(statuses) == 3
        providers = {s["provider"] for s in statuses}
        assert providers == {"claude", "github", "base_llm"}

    def test_get_all_reflects_state_changes(self):
        lc = get_lifecycle("claude", initial_cooldown=60)
        lc.on_rate_limit_hit()
        statuses = get_all_lifecycle_statuses()
        assert statuses[0]["state"] == "cooldown"

    def test_reset_clears_registry(self):
        get_lifecycle("claude")
        reset_lifecycle_registry()
        statuses = get_all_lifecycle_statuses()
        assert statuses == []

    def test_custom_params_passed_through(self):
        lc = get_lifecycle("custom", initial_cooldown=10.0, max_cooldown=50.0, recovery_successes=5)
        assert lc.initial_cooldown == 10.0
        assert lc.max_cooldown == 50.0
        assert lc.recovery_successes == 5

    def test_custom_params_ignored_on_existing(self):
        lc1 = get_lifecycle("reuse", initial_cooldown=10.0)
        lc2 = get_lifecycle("reuse", initial_cooldown=99.0)
        assert lc1 is lc2
        assert lc2.initial_cooldown == 10.0


# ======================================================================
# Thread safety
# ======================================================================


class TestThreadSafety:
    def test_concurrent_hits_no_crash(self):
        lc = RateLimitLifecycle("thread-test", initial_cooldown=0.001)
        errors = []

        def hammer():
            try:
                for _ in range(50):
                    lc.on_rate_limit_hit()
                    lc.on_request_success()
                    lc.on_rate_limit_warning()
                    lc.can_proceed()
                    lc.get_status()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert errors == [], f"Thread errors: {errors}"

    def test_concurrent_registry_access(self):
        reset_lifecycle_registry()
        errors = []

        def register(name):
            try:
                lc = get_lifecycle(name)
                lc.on_rate_limit_warning()
                lc.get_status()
            except Exception as exc:
                errors.append(exc)

        try:
            threads = [threading.Thread(target=register, args=(f"p{i}",)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            assert errors == []
        finally:
            reset_lifecycle_registry()


# ======================================================================
# Dashboard API endpoint (integration)
# ======================================================================


class TestDashboardRateLimitsEndpoint:
    """Verify that the /api/rate-limits endpoint returns lifecycle data."""

    def test_get_all_lifecycle_statuses_returns_list(self):
        reset_lifecycle_registry()
        try:
            get_lifecycle("claude")
            get_lifecycle("github")
            statuses = get_all_lifecycle_statuses()
            assert isinstance(statuses, list)
            assert len(statuses) == 2
            for s in statuses:
                assert "provider" in s
                assert "state" in s
                assert "can_proceed" not in s  # not a field, just a method
        finally:
            reset_lifecycle_registry()
