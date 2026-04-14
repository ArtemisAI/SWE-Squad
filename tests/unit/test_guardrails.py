"""Tests for unified GuardrailsCoordinator."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from src.swe_team.cost_tracker import BudgetPolicy, CostEvent, InMemoryCostTracker
from src.swe_team.guardrails import (
    GuardrailDecision,
    GuardrailHealth,
    GuardrailsCoordinator,
)


@pytest.fixture
def coordinator():
    return GuardrailsCoordinator()


# ── No gates (all clear) ──────────────────────────────────────


class TestNoGates:
    def test_no_gates_allows_everything(self, coordinator):
        d = coordinator.can_proceed()
        assert d.allowed is True
        assert d.gate == "all_clear"

    def test_can_proceed_only_defaults(self, coordinator):
        """can_proceed with no severity, no agents — just defaults."""
        d = coordinator.can_proceed()
        assert d.allowed is True
        assert d.gate == "all_clear"
        assert d.reason == "All guardrails passed"
        assert "all_clear" in d.evaluated_gates

    def test_no_gates_health(self, coordinator):
        h = coordinator.health()
        assert h.circuit_breaker_paused is False
        assert h.governor_allow_new_work is True


# ── Circuit Breaker Gate ───────────────────────────────────────


class TestCircuitBreakerGate:
    def test_paused_blocks(self, coordinator):
        cb = MagicMock()
        cb.is_paused = True
        cb.failure_rate = 0.9
        cb._paused_until = "2026-12-31T00:00:00+00:00"
        coordinator.set_circuit_breaker(cb)

        d = coordinator.can_proceed()
        assert d.allowed is False
        assert d.gate == "circuit_breaker"
        assert "90%" in d.reason

    def test_not_paused_passes(self, coordinator):
        cb = MagicMock()
        cb.is_paused = False
        cb.failure_rate = 0.2
        coordinator.set_circuit_breaker(cb)

        d = coordinator.can_proceed()
        assert d.allowed is True

    def test_circuit_breaker_checked_first(self, coordinator):
        cb = MagicMock()
        cb.is_paused = True
        cb.failure_rate = 1.0
        coordinator.set_circuit_breaker(cb)

        # Even with governor allowing, circuit breaker should block
        gov = MagicMock()
        decision = MagicMock()
        decision.allow_new_work = True
        decision.max_agents = 10
        decision.priority_floor = 5
        gov.get_concurrency_decision.return_value = decision
        coordinator.set_usage_governor(gov)

        d = coordinator.can_proceed()
        assert d.gate == "circuit_breaker"
        assert "circuit_breaker" in d.evaluated_gates
        assert "usage_governor" not in d.evaluated_gates


# ── Usage Governor Gate ────────────────────────────────────────


class TestUsageGovernorGate:
    def _mock_governor(self, allow_new=True, max_agents=5, priority_floor=4):
        gov = MagicMock()
        decision = MagicMock()
        decision.allow_new_work = allow_new
        decision.max_agents = max_agents
        decision.priority_floor = priority_floor
        decision.audit_trail = "test"
        gov.get_concurrency_decision.return_value = decision
        return gov

    def test_new_work_blocked(self, coordinator):
        coordinator.set_usage_governor(self._mock_governor(allow_new=False))
        d = coordinator.can_proceed()
        assert d.allowed is False
        assert d.gate == "usage_governor"
        assert "blocked" in d.reason

    def test_severity_below_floor(self, coordinator):
        # priority_floor=1 means only CRITICAL (0) and HIGH (1) allowed
        coordinator.set_usage_governor(self._mock_governor(priority_floor=1))
        d = coordinator.can_proceed(ticket_severity="MEDIUM")
        assert d.allowed is False
        assert "priority floor" in d.reason

    def test_severity_meets_floor(self, coordinator):
        coordinator.set_usage_governor(self._mock_governor(priority_floor=2))
        d = coordinator.can_proceed(ticket_severity="MEDIUM")
        assert d.allowed is True

    def test_agent_count_exceeded(self, coordinator):
        coordinator.set_usage_governor(self._mock_governor(max_agents=3))
        d = coordinator.can_proceed(current_agents=3)
        assert d.allowed is False
        assert "3 agents running" in d.reason

    def test_agent_count_within_limit(self, coordinator):
        coordinator.set_usage_governor(self._mock_governor(max_agents=5))
        d = coordinator.can_proceed(current_agents=2)
        assert d.allowed is True

    def test_decision_missing_priority_floor(self, coordinator):
        """Governor decision without priority_floor attribute should not crash."""
        gov = MagicMock()
        decision = MagicMock(spec=["allow_new_work", "max_agents", "audit_trail"])
        decision.allow_new_work = True
        decision.max_agents = 5
        decision.audit_trail = "test"
        # priority_floor is missing from spec — accessing it raises AttributeError
        del decision.priority_floor
        gov.get_concurrency_decision.return_value = decision
        coordinator.set_usage_governor(gov)

        # The AttributeError on decision.priority_floor is caught by the
        # except Exception block, so it fails open.
        d = coordinator.can_proceed(ticket_severity="MEDIUM")
        assert d.allowed is True

    def test_empty_string_severity(self, coordinator):
        """ticket_severity='' should use default mapping (falls to priority 2)."""
        coordinator.set_usage_governor(self._mock_governor(priority_floor=4))
        d = coordinator.can_proceed(ticket_severity="")
        assert d.allowed is True  # empty string -> .get("", 2) -> 2, floor 4 -> passes

    def test_empty_string_severity_blocked_by_low_floor(self, coordinator):
        """ticket_severity='' with floor=1 should block (maps to priority 2)."""
        coordinator.set_usage_governor(self._mock_governor(priority_floor=1))
        d = coordinator.can_proceed(ticket_severity="")
        assert d.allowed is False
        assert "priority floor" in d.reason

    def test_unknown_severity_does_not_crash(self, coordinator):
        """ticket_severity='UNKNOWN' should still produce a decision."""
        coordinator.set_usage_governor(self._mock_governor(priority_floor=4))
        d = coordinator.can_proceed(ticket_severity="UNKNOWN")
        assert d.allowed is True  # UNKNOWN -> .get("UNKNOWN", 2) -> 2, floor 4 -> passes

    def test_unknown_severity_blocked_by_low_floor(self, coordinator):
        """ticket_severity='UNKNOWN' with floor=1 should block (maps to default 2)."""
        coordinator.set_usage_governor(self._mock_governor(priority_floor=1))
        d = coordinator.can_proceed(ticket_severity="UNKNOWN")
        assert d.allowed is False

    def test_governor_exception_fails_open(self, coordinator):
        gov = MagicMock()
        gov.get_concurrency_decision.side_effect = RuntimeError("oops")
        coordinator.set_usage_governor(gov)
        d = coordinator.can_proceed()
        assert d.allowed is True  # Fails open


# ── Stability Gate ─────────────────────────────────────────────


class TestStabilityGate:
    def test_block_on_deploy(self, coordinator):
        gate = MagicMock()
        report = MagicMock()
        report.verdict = "BLOCK"
        report.reason = "5 critical bugs open"
        report.open_critical = 5
        gate.evaluate.return_value = report
        coordinator.set_stability_gate(gate)

        d = coordinator.can_proceed(task_type="deploy")
        assert d.allowed is False
        assert d.gate == "stability_gate"

    def test_pass_on_deploy(self, coordinator):
        gate = MagicMock()
        report = MagicMock()
        report.verdict = "PASS"
        gate.evaluate.return_value = report
        coordinator.set_stability_gate(gate)

        d = coordinator.can_proceed(task_type="deploy")
        assert d.allowed is True

    def test_skipped_for_investigate(self, coordinator):
        """Stability gate only blocks deploy/creative, not investigate."""
        gate = MagicMock()
        report = MagicMock()
        report.verdict = "BLOCK"
        gate.evaluate.return_value = report
        coordinator.set_stability_gate(gate)

        d = coordinator.can_proceed(task_type="investigate")
        assert d.allowed is True
        gate.evaluate.assert_not_called()

    def test_blocks_creative_allows_develop(self, coordinator):
        """Stability gate blocks 'creative' but allows 'develop'."""
        gate = MagicMock()
        report = MagicMock()
        report.verdict = "BLOCK"
        report.reason = "3 critical bugs"
        report.open_critical = 3
        gate.evaluate.return_value = report
        coordinator.set_stability_gate(gate)

        d_creative = coordinator.can_proceed(task_type="creative")
        assert d_creative.allowed is False
        assert d_creative.gate == "stability_gate"

        d_develop = coordinator.can_proceed(task_type="develop")
        assert d_develop.allowed is True
        gate.evaluate.assert_called_once()  # only called for creative, not develop

    def test_set_gate_then_unset(self, coordinator):
        """Set a gate, verify it works, then set to None, verify it's skipped."""
        gate = MagicMock()
        report = MagicMock()
        report.verdict = "BLOCK"
        report.reason = "bugs"
        gate.evaluate.return_value = report
        coordinator.set_stability_gate(gate)

        d = coordinator.can_proceed(task_type="deploy")
        assert d.allowed is False
        assert d.gate == "stability_gate"

        # Unset the gate
        coordinator.set_stability_gate(None)
        d2 = coordinator.can_proceed(task_type="deploy")
        assert d2.allowed is True
        assert "stability_gate" not in d2.evaluated_gates

    def test_gate_exception_passes(self, coordinator):
        gate = MagicMock()
        gate.evaluate.side_effect = RuntimeError("gate error")
        coordinator.set_stability_gate(gate)

        d = coordinator.can_proceed(task_type="deploy")
        assert d.allowed is True  # Fails open


# ── Combined Gates ─────────────────────────────────────────────


class TestCombinedGates:
    def test_all_gates_pass(self, coordinator):
        cb = MagicMock(is_paused=False, failure_rate=0.1)
        coordinator.set_circuit_breaker(cb)

        gov = MagicMock()
        decision = MagicMock(allow_new_work=True, max_agents=5, priority_floor=4)
        gov.get_concurrency_decision.return_value = decision
        coordinator.set_usage_governor(gov)

        gate = MagicMock()
        report = MagicMock(verdict="PASS")
        gate.evaluate.return_value = report
        coordinator.set_stability_gate(gate)

        d = coordinator.can_proceed(task_type="deploy", current_agents=1)
        assert d.allowed is True
        assert "circuit_breaker" in d.evaluated_gates
        assert "usage_governor" in d.evaluated_gates
        assert "stability_gate" in d.evaluated_gates

    def test_concurrent_can_proceed(self, coordinator):
        """5 threads calling can_proceed simultaneously should not crash."""
        cb = MagicMock(is_paused=False, failure_rate=0.1)
        coordinator.set_circuit_breaker(cb)

        gov = MagicMock()
        decision = MagicMock(allow_new_work=True, max_agents=10, priority_floor=4)
        gov.get_concurrency_decision.return_value = decision
        coordinator.set_usage_governor(gov)

        results = []
        errors = []

        def call_can_proceed():
            try:
                d = coordinator.can_proceed(
                    task_type="investigate",
                    ticket_severity="HIGH",
                    current_agents=1,
                )
                results.append(d)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call_can_proceed) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Concurrent calls raised errors: {errors}"
        assert len(results) == 5
        assert all(d.allowed is True for d in results)

    def test_evaluated_gates_tracked(self, coordinator):
        cb = MagicMock(is_paused=False, failure_rate=0.0)
        coordinator.set_circuit_breaker(cb)

        d = coordinator.can_proceed()
        assert "circuit_breaker" in d.evaluated_gates
        assert "all_clear" in d.evaluated_gates


# ── Per-team overrides ────────────────────────────────────────────


class TestPerTeamOverrides:
    def _mock_governor(self, max_agents=5):
        gov = MagicMock()
        decision = MagicMock()
        decision.allow_new_work = True
        decision.max_agents = max_agents
        decision.priority_floor = 4
        decision.audit_trail = "test"
        gov.get_concurrency_decision.return_value = decision
        return gov

    def test_team_budget_isolated(self):
        tracker = InMemoryCostTracker()
        tracker.set_budget_policy(BudgetPolicy(team_id="alpha", daily_budget_cents=5000))
        tracker.set_budget_policy(BudgetPolicy(team_id="beta", daily_budget_cents=5000))
        tracker._events.append(CostEvent(
            team_id="alpha",
            model="sonnet",
            input_tokens=0,
            output_tokens=0,
            cost_cents=200.0,
            operation="develop",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        tracker._events.append(CostEvent(
            team_id="beta",
            model="sonnet",
            input_tokens=0,
            output_tokens=0,
            cost_cents=200.0,
            operation="develop",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))

        coordinator = GuardrailsCoordinator(
            config={"teams": {"alpha": {"budget_daily": 1.0}, "beta": {"budget_daily": 10.0}}}
        )
        coordinator.set_cost_tracker(tracker)

        alpha = coordinator.evaluate(team_id="alpha")
        beta = coordinator.evaluate(team_id="beta")

        assert alpha.allowed is False
        assert alpha.gate == "budget_gate"
        assert beta.allowed is True

    def test_team_circuit_breaker_state_isolated(self):
        coordinator = GuardrailsCoordinator()
        alpha_cb = MagicMock(is_paused=True, failure_rate=0.9, _paused_until="later")
        beta_cb = MagicMock(is_paused=False, failure_rate=0.1, _paused_until=None)
        coordinator.set_circuit_breaker(alpha_cb, team_id="alpha")
        coordinator.set_circuit_breaker(beta_cb, team_id="beta")

        alpha = coordinator.evaluate(team_id="alpha")
        beta = coordinator.evaluate(team_id="beta")

        assert alpha.allowed is False
        assert alpha.gate == "circuit_breaker"
        assert beta.allowed is True

    def test_global_fallback_used_when_team_missing_overrides(self):
        coordinator = GuardrailsCoordinator(config={"max_concurrent": 2})
        coordinator.set_usage_governor(self._mock_governor(max_agents=6))

        team_without_override = coordinator.evaluate(team_id="gamma", current_agents=2)
        assert team_without_override.allowed is False
        assert "max 2" in team_without_override.reason

    def test_team_circuit_breaker_threshold_override(self):
        coordinator = GuardrailsCoordinator(
            config={"circuit_breaker_threshold": 0.95, "teams": {"alpha": {"circuit_breaker_threshold": 0.5}}}
        )
        cb = MagicMock(is_paused=False, failure_rate=0.6)
        coordinator.set_circuit_breaker(cb)

        alpha = coordinator.evaluate(team_id="alpha")
        gamma = coordinator.evaluate(team_id="gamma")

        assert alpha.allowed is False
        assert alpha.gate == "circuit_breaker"
        assert gamma.allowed is True

# ── GuardrailDecision ──────────────────────────────────────────


class TestGuardrailDecision:
    def test_blocked_property(self):
        d = GuardrailDecision(allowed=False, reason="test", gate="test")
        assert d.blocked is True

    def test_not_blocked(self):
        d = GuardrailDecision(allowed=True, reason="ok", gate="all_clear")
        assert d.blocked is False

    def test_timestamp_set(self):
        d = GuardrailDecision(allowed=True, reason="ok", gate="all_clear")
        assert d.timestamp > 0


# ── Health ─────────────────────────────────────────────────────


class TestGuardrailHealth:
    def test_health_with_all_components(self, coordinator):
        cb = MagicMock(is_paused=False, failure_rate=0.1)
        coordinator.set_circuit_breaker(cb)

        gov = MagicMock()
        decision = MagicMock(allow_new_work=True, max_agents=5)
        gov.get_concurrency_decision.return_value = decision
        coordinator.set_usage_governor(gov)

        gate = MagicMock()
        report = MagicMock(verdict="PASS")
        gate.evaluate.return_value = report
        coordinator.set_stability_gate(gate)

        h = coordinator.health()
        assert h.circuit_breaker_paused is False
        assert h.circuit_breaker_failure_rate == 0.1
        assert h.governor_allow_new_work is True
        assert h.governor_max_agents == 5
        assert h.stability_verdict == "PASS"

    def test_health_with_queue(self, coordinator):
        dispatcher = MagicMock()
        dispatcher.health.return_value = {
            "investigate_depth": 3,
            "develop_depth": 2,
            "dead_letter_count": 1,
        }
        coordinator.set_queued_dispatcher(dispatcher)

        h = coordinator.health()
        assert h.queue_depth == 5
        assert h.dead_letter_count == 1

    def test_health_with_broken_governor(self, coordinator):
        """Governor.get_concurrency_decision raises; health returns partial data."""
        cb = MagicMock(is_paused=True, failure_rate=0.85)
        coordinator.set_circuit_breaker(cb)

        gov = MagicMock()
        gov.get_concurrency_decision.side_effect = RuntimeError("governor down")
        coordinator.set_usage_governor(gov)

        h = coordinator.health()
        # Circuit breaker data should still be present
        assert h.circuit_breaker_paused is True
        assert h.circuit_breaker_failure_rate == 0.85
        # Governor fields fall back to defaults
        assert h.governor_allow_new_work is True
        assert h.governor_max_agents == 5

    def test_health_with_broken_stability_gate(self, coordinator):
        """Stability gate.evaluate raises; health returns partial data."""
        cb = MagicMock(is_paused=False, failure_rate=0.05)
        coordinator.set_circuit_breaker(cb)

        gate = MagicMock()
        gate.evaluate.side_effect = RuntimeError("gate crashed")
        coordinator.set_stability_gate(gate)

        h = coordinator.health()
        # Circuit breaker data should still be present
        assert h.circuit_breaker_paused is False
        assert h.circuit_breaker_failure_rate == 0.05
        # Stability verdict falls back to default
        assert h.stability_verdict == "unknown"

    def test_health_defaults(self):
        h = GuardrailHealth()
        assert h.circuit_breaker_paused is False
        assert h.stability_verdict == "unknown"
        assert h.queue_depth == 0
