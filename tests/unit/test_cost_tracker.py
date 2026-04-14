"""
Tests for src/swe_team/cost_tracker.py and providers/cost/base.py

Covers:
- Cost computation for each model tier
- compute_cost_cents helper
- Daily/monthly aggregation
- Budget check (ok, warning, hard_stop)
- InMemoryCostTracker full lifecycle
- SupabaseCostTracker with mocked client
- BudgetStatus dataclass properties
- GuardrailsCoordinator budget gate integration
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.cost_tracker import (
    PRICING,
    BudgetPolicy,
    CostEvent,
    InMemoryCostTracker,
    SupabaseCostTracker,
    compute_cost_cents,
    make_cost_tracker,
    _resolve_model_key,
)
from src.swe_team.providers.cost.base import BudgetStatus, CostTrackerProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tracker(team_id: str = "t1") -> InMemoryCostTracker:
    tracker = InMemoryCostTracker()
    return tracker


# ---------------------------------------------------------------------------
# 1. compute_cost_cents — model pricing
# ---------------------------------------------------------------------------

class TestComputeCostCents:
    def test_haiku_input_only(self):
        cost = compute_cost_cents("haiku", 1_000_000, 0)
        assert cost == pytest.approx(PRICING["haiku"]["input"])

    def test_haiku_output_only(self):
        cost = compute_cost_cents("haiku", 0, 1_000_000)
        assert cost == pytest.approx(PRICING["haiku"]["output"])

    def test_sonnet_combined(self):
        cost = compute_cost_cents("sonnet", 1_000_000, 1_000_000)
        expected = PRICING["sonnet"]["input"] + PRICING["sonnet"]["output"]
        assert cost == pytest.approx(expected)

    def test_opus_combined(self):
        cost = compute_cost_cents("opus", 1_000_000, 1_000_000)
        expected = PRICING["opus"]["input"] + PRICING["opus"]["output"]
        assert cost == pytest.approx(expected)

    def test_unknown_model_falls_back_to_default(self):
        cost = compute_cost_cents("gpt-4o", 1_000_000, 0)
        assert cost == pytest.approx(PRICING["default"]["input"])

    def test_full_model_name_sonnet(self):
        cost = compute_cost_cents("claude-3-5-sonnet-20241022", 500_000, 250_000)
        expected = (500_000 / 1_000_000 * PRICING["sonnet"]["input"]) + (
            250_000 / 1_000_000 * PRICING["sonnet"]["output"]
        )
        assert cost == pytest.approx(expected)

    def test_full_model_name_opus(self):
        cost = compute_cost_cents("claude-opus-4", 100_000, 50_000)
        expected = (100_000 / 1_000_000 * PRICING["opus"]["input"]) + (
            50_000 / 1_000_000 * PRICING["opus"]["output"]
        )
        assert cost == pytest.approx(expected)

    def test_zero_tokens(self):
        assert compute_cost_cents("sonnet", 0, 0) == 0.0

    def test_model_key_resolution(self):
        assert _resolve_model_key("claude-3-haiku") == "haiku"
        assert _resolve_model_key("claude-sonnet") == "sonnet"
        assert _resolve_model_key("claude-opus") == "opus"
        assert _resolve_model_key("") == "default"
        assert _resolve_model_key("unknown-model") == "default"


# ---------------------------------------------------------------------------
# 2. InMemoryCostTracker — core recording and aggregation
# ---------------------------------------------------------------------------

class TestInMemoryCostTracker:
    def test_record_returns_cost_cents(self):
        tracker = _make_tracker()
        cost = tracker.record_cost("t1", "sonnet", 1_000_000, 0, "investigate")
        assert cost == pytest.approx(PRICING["sonnet"]["input"])

    def test_get_daily_spend_empty(self):
        tracker = _make_tracker()
        assert tracker.get_daily_spend("t1") == 0.0

    def test_get_daily_spend_accumulates(self):
        tracker = _make_tracker()
        tracker.record_cost("t1", "haiku", 1_000_000, 0, "triage")
        tracker.record_cost("t1", "haiku", 1_000_000, 0, "triage")
        expected = PRICING["haiku"]["input"] * 2
        assert tracker.get_daily_spend("t1") == pytest.approx(expected)

    def test_daily_spend_isolated_by_team(self):
        tracker = _make_tracker()
        tracker.record_cost("team-A", "sonnet", 1_000_000, 0, "investigate")
        tracker.record_cost("team-B", "sonnet", 1_000_000, 0, "investigate")
        assert tracker.get_daily_spend("team-A") == pytest.approx(PRICING["sonnet"]["input"])
        assert tracker.get_daily_spend("team-B") == pytest.approx(PRICING["sonnet"]["input"])

    def test_get_monthly_spend_accumulates(self):
        tracker = _make_tracker()
        tracker.record_cost("t1", "opus", 1_000_000, 0, "develop")
        assert tracker.get_monthly_spend("t1") == pytest.approx(PRICING["opus"]["input"])

    def test_spend_by_operation(self):
        tracker = _make_tracker()
        tracker.record_cost("t1", "haiku", 100_000, 0, "triage")
        tracker.record_cost("t1", "sonnet", 100_000, 0, "investigate")
        by_op = tracker.get_spend_by_operation("t1")
        assert "triage" in by_op
        assert "investigate" in by_op
        assert by_op["triage"] > 0
        assert by_op["investigate"] > 0

    def test_spend_by_ticket(self):
        tracker = _make_tracker()
        tracker.record_cost("t1", "sonnet", 100_000, 0, "investigate", ticket_id="ticket-1")
        tracker.record_cost("t1", "sonnet", 100_000, 0, "investigate", ticket_id="ticket-2")
        by_ticket = tracker.get_spend_by_ticket("t1")
        assert "ticket-1" in by_ticket
        assert "ticket-2" in by_ticket


# ---------------------------------------------------------------------------
# 3. Budget check (ok / warning / hard_stop)
# ---------------------------------------------------------------------------

class TestBudgetCheck:
    def test_status_ok_when_under_threshold(self):
        tracker = _make_tracker()
        # No spend → 0% → ok
        status = tracker.check_budget("t1")
        assert status.status == "ok"
        assert status.percent_used == 0.0

    def test_status_warning_when_over_alert_threshold(self):
        tracker = _make_tracker()
        # Set a tiny budget so we can trip the threshold cheaply
        tracker.set_budget_policy(BudgetPolicy(
            team_id="t1",
            daily_budget_cents=100,
            monthly_budget_cents=10_000,
            alert_threshold_percent=80,
            hard_stop_enabled=True,
        ))
        # 85 cents out of $1.00 daily = 85% → warning
        tracker._events.append(CostEvent(
            team_id="t1", model="sonnet", input_tokens=0, output_tokens=0,
            cost_cents=85.0, operation="investigate",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        status = tracker.check_budget("t1")
        assert status.status == "warning"
        assert status.percent_used >= 80.0

    def test_status_hard_stop_when_over_100_pct(self):
        tracker = _make_tracker()
        tracker.set_budget_policy(BudgetPolicy(
            team_id="t1",
            daily_budget_cents=100,
            monthly_budget_cents=10_000,
            alert_threshold_percent=80,
            hard_stop_enabled=True,
        ))
        # 110 cents out of $1.00 daily = 110% → hard_stop
        tracker._events.append(CostEvent(
            team_id="t1", model="sonnet", input_tokens=0, output_tokens=0,
            cost_cents=110.0, operation="investigate",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        status = tracker.check_budget("t1")
        assert status.status == "hard_stop"
        assert status.is_over_budget is True
        assert status.is_warning is False

    def test_hard_stop_disabled_returns_warning_not_stop(self):
        tracker = _make_tracker()
        tracker.set_budget_policy(BudgetPolicy(
            team_id="t1",
            daily_budget_cents=100,
            monthly_budget_cents=10_000,
            alert_threshold_percent=80,
            hard_stop_enabled=False,  # disabled
        ))
        tracker._events.append(CostEvent(
            team_id="t1", model="sonnet", input_tokens=0, output_tokens=0,
            cost_cents=200.0, operation="investigate",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        status = tracker.check_budget("t1")
        # Over budget but hard_stop disabled → warning
        assert status.status == "warning"
        assert status.is_over_budget is False

    def test_monthly_budget_triggers_warning(self):
        tracker = _make_tracker()
        tracker.set_budget_policy(BudgetPolicy(
            team_id="t1",
            daily_budget_cents=100_000,   # very high daily limit
            monthly_budget_cents=100,      # tiny monthly limit
            alert_threshold_percent=80,
            hard_stop_enabled=True,
        ))
        # 90 cents in a month with $1 limit = 90% → warning
        tracker._events.append(CostEvent(
            team_id="t1", model="haiku", input_tokens=0, output_tokens=0,
            cost_cents=90.0, operation="triage",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        status = tracker.check_budget("t1")
        assert status.status == "warning"

    def test_budget_status_dataclass_properties(self):
        s = BudgetStatus(
            status="hard_stop", daily_spent=110.0, daily_limit=100.0,
            monthly_spent=110.0, monthly_limit=10_000.0, percent_used=110.0,
        )
        assert s.is_over_budget is True
        assert s.is_warning is False

        s2 = BudgetStatus(
            status="warning", daily_spent=85.0, daily_limit=100.0,
            monthly_spent=85.0, monthly_limit=10_000.0, percent_used=85.0,
        )
        assert s2.is_warning is True
        assert s2.is_over_budget is False


# ---------------------------------------------------------------------------
# 4. SupabaseCostTracker (mocked client)
# ---------------------------------------------------------------------------

class TestSupabaseCostTracker:
    def _make_supabase_client(self, daily_cents=0.0, monthly_cents=0.0):
        """Build a minimal mock Supabase client."""
        client = MagicMock()

        # Mock table().insert().execute()
        insert_resp = MagicMock()
        insert_resp.data = [{}]
        client.table.return_value.insert.return_value.execute.return_value = insert_resp

        # Mock table().select().eq().limit().execute() for policy
        policy_resp = MagicMock()
        policy_resp.data = []  # no custom policy → defaults
        client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = policy_resp

        # Mock rpc for daily/monthly
        def _rpc_side_effect(fn_name, params):
            rpc_mock = MagicMock()
            if fn_name == "get_daily_spend_cents":
                rpc_mock.execute.return_value.data = daily_cents
            elif fn_name == "get_monthly_spend_cents":
                rpc_mock.execute.return_value.data = monthly_cents
            else:
                rpc_mock.execute.return_value.data = 0.0
            return rpc_mock

        client.rpc.side_effect = _rpc_side_effect
        return client

    def test_record_cost_returns_cents(self):
        client = self._make_supabase_client()
        tracker = SupabaseCostTracker(client)
        cost = tracker.record_cost("t1", "sonnet", 1_000_000, 0, "investigate")
        assert cost == pytest.approx(PRICING["sonnet"]["input"])

    def test_record_cost_inserts_row(self):
        client = self._make_supabase_client()
        tracker = SupabaseCostTracker(client)
        tracker.record_cost("t1", "haiku", 500_000, 250_000, "triage")
        # Should have called table("swe_cost_events").insert(...).execute()
        client.table.assert_called()

    def test_get_daily_spend_uses_rpc(self):
        client = self._make_supabase_client(daily_cents=1234.5)
        tracker = SupabaseCostTracker(client)
        assert tracker.get_daily_spend("t1") == pytest.approx(1234.5)

    def test_get_monthly_spend_uses_rpc(self):
        client = self._make_supabase_client(monthly_cents=9999.0)
        tracker = SupabaseCostTracker(client)
        assert tracker.get_monthly_spend("t1") == pytest.approx(9999.0)

    def test_check_budget_ok(self):
        client = self._make_supabase_client(daily_cents=100.0, monthly_cents=500.0)
        tracker = SupabaseCostTracker(client)
        status = tracker.check_budget("t1")
        # Default daily limit 5000 cents, 100 spent = 2% → ok
        assert status.status == "ok"

    def test_check_budget_hard_stop(self):
        # 6000 daily cents vs default 5000 limit → 120% → hard_stop
        client = self._make_supabase_client(daily_cents=6000.0, monthly_cents=6000.0)
        tracker = SupabaseCostTracker(client)
        status = tracker.check_budget("t1")
        assert status.status == "hard_stop"

    def test_fallback_on_rpc_error(self):
        client = MagicMock()
        client.table.return_value.insert.return_value.execute.return_value.data = [{}]
        # Policy lookup — no custom policy
        policy_resp = MagicMock()
        policy_resp.data = []
        client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = policy_resp
        # RPC raises
        client.rpc.side_effect = Exception("Supabase unavailable")
        tracker = SupabaseCostTracker(client)
        # Should fall back to in-memory (0 spend)
        assert tracker.get_daily_spend("t1") == 0.0


# ---------------------------------------------------------------------------
# 5. make_cost_tracker factory
# ---------------------------------------------------------------------------

class TestMakeFactory:
    def test_returns_in_memory_without_client(self):
        tracker = make_cost_tracker()
        assert isinstance(tracker, InMemoryCostTracker)

    def test_returns_supabase_with_client(self):
        mock_client = MagicMock()
        tracker = make_cost_tracker(supabase_client=mock_client)
        assert isinstance(tracker, SupabaseCostTracker)


# ---------------------------------------------------------------------------
# 6. GuardrailsCoordinator budget gate integration
# ---------------------------------------------------------------------------

class TestGuardrailsBudgetGate:
    def test_budget_ok_passes(self):
        from src.swe_team.guardrails import GuardrailsCoordinator
        tracker = InMemoryCostTracker()
        g = GuardrailsCoordinator()
        g.set_cost_tracker(tracker, team_id="t1")
        decision = g.can_proceed()
        assert decision.allowed is True

    def test_budget_hard_stop_blocks(self):
        from src.swe_team.guardrails import GuardrailsCoordinator
        tracker = InMemoryCostTracker()
        tracker.set_budget_policy(BudgetPolicy(
            team_id="t1",
            daily_budget_cents=100,
            monthly_budget_cents=10_000,
            alert_threshold_percent=80,
            hard_stop_enabled=True,
        ))
        tracker._events.append(CostEvent(
            team_id="t1", model="opus", input_tokens=0, output_tokens=0,
            cost_cents=200.0, operation="develop",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        g = GuardrailsCoordinator()
        g.set_cost_tracker(tracker, team_id="t1")
        decision = g.can_proceed()
        assert decision.allowed is False
        assert decision.gate == "budget_gate"
        assert "hard-stop" in decision.reason.lower()

    def test_no_cost_tracker_passes_through(self):
        from src.swe_team.guardrails import GuardrailsCoordinator
        g = GuardrailsCoordinator()
        # No cost tracker set — gate is skipped
        decision = g.can_proceed()
        assert decision.allowed is True

    def test_health_reports_budget_status(self):
        from src.swe_team.guardrails import GuardrailsCoordinator
        tracker = InMemoryCostTracker()
        g = GuardrailsCoordinator()
        g.set_cost_tracker(tracker, team_id="t1")
        h = g.health()
        assert h.budget_status == "ok"

    def test_health_unconfigured_without_tracker(self):
        from src.swe_team.guardrails import GuardrailsCoordinator
        g = GuardrailsCoordinator()
        h = g.health()
        assert h.budget_status == "unconfigured"


# ---------------------------------------------------------------------------
# 7. Thread-safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_record_and_read(self):
        tracker = InMemoryCostTracker()
        errors = []

        def writer():
            for _ in range(50):
                try:
                    tracker.record_cost("t1", "haiku", 1000, 500, "triage")
                except Exception as exc:
                    errors.append(exc)

        def reader():
            for _ in range(50):
                try:
                    tracker.get_daily_spend("t1")
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Thread errors: {errors}"
