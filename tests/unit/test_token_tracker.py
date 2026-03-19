"""Tests for token_tracker module."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.swe_team.token_tracker import (
    DEFAULT_PRICING,
    TokenUsage,
    TokenTracker,
    calculate_cost,
)


# ---------------------------------------------------------------------------
# calculate_cost
# ---------------------------------------------------------------------------

class TestCalculateCost:
    def test_haiku_pricing(self):
        cost = calculate_cost("haiku", 1000, 1000)
        expected = 1 * 0.00025 + 1 * 0.00125
        assert cost == pytest.approx(expected)

    def test_sonnet_pricing(self):
        cost = calculate_cost("sonnet", 1000, 1000)
        expected = 1 * 0.003 + 1 * 0.015
        assert cost == pytest.approx(expected)

    def test_opus_pricing(self):
        cost = calculate_cost("opus", 1000, 1000)
        expected = 1 * 0.015 + 1 * 0.075
        assert cost == pytest.approx(expected)

    def test_unknown_model_uses_default(self):
        cost = calculate_cost("gpt-4o", 1000, 1000)
        expected = 1 * 0.003 + 1 * 0.015
        assert cost == pytest.approx(expected)

    def test_empty_model_uses_default(self):
        cost = calculate_cost("", 1000, 1000)
        expected = 1 * 0.003 + 1 * 0.015
        assert cost == pytest.approx(expected)

    def test_zero_tokens(self):
        assert calculate_cost("sonnet", 0, 0) == 0.0

    def test_custom_pricing(self):
        custom = {"sonnet": {"input": 0.01, "output": 0.02}, "default": {"input": 0.001, "output": 0.002}}
        cost = calculate_cost("claude-sonnet-4", 2000, 3000)
        # Without custom pricing, uses default sonnet rates
        assert cost > 0
        cost_custom = calculate_cost("claude-sonnet-4", 2000, 3000, pricing=custom)
        expected = 2 * 0.01 + 3 * 0.02
        assert cost_custom == pytest.approx(expected)


# ---------------------------------------------------------------------------
# TokenUsage
# ---------------------------------------------------------------------------

class TestTokenUsage:
    def test_serialization_round_trip(self):
        usage = TokenUsage(
            session_id="s1",
            ticket_id="t1",
            model="sonnet",
            input_tokens=500,
            output_tokens=200,
            cost_usd=0.0045,
            task="investigate",
            agent="claude-code",
            metadata={"attempt": 1},
        )
        d = usage.to_dict()
        restored = TokenUsage.from_dict(d)
        assert restored.session_id == "s1"
        assert restored.ticket_id == "t1"
        assert restored.model == "sonnet"
        assert restored.input_tokens == 500
        assert restored.output_tokens == 200
        assert restored.cost_usd == 0.0045
        assert restored.task == "investigate"
        assert restored.metadata == {"attempt": 1}

    def test_from_dict_ignores_unknown_keys(self):
        data = {"model": "haiku", "input_tokens": 10, "bogus_field": 42}
        usage = TokenUsage.from_dict(data)
        assert usage.model == "haiku"
        assert usage.input_tokens == 10

    def test_defaults(self):
        usage = TokenUsage()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cost_usd == 0.0
        assert usage.agent == "claude-code"


# ---------------------------------------------------------------------------
# TokenTracker
# ---------------------------------------------------------------------------

def _make_tracker(tmp_path: Path) -> TokenTracker:
    return TokenTracker(store_path=tmp_path / "usage.jsonl")


class TestTokenTracker:
    def test_record_writes_jsonl(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        tracker.record("sonnet", 100, 50, task="investigate", ticket_id="T-1")
        lines = (tmp_path / "usage.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["model"] == "sonnet"
        assert rec["input_tokens"] == 100
        assert rec["output_tokens"] == 50
        assert rec["ticket_id"] == "T-1"
        assert rec["cost_usd"] > 0

    def test_record_calculates_cost(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        usage = tracker.record("haiku", 2000, 1000)
        expected = calculate_cost("haiku", 2000, 1000)
        assert usage.cost_usd == pytest.approx(expected, abs=1e-5)

    def test_multiple_records(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        tracker.record("haiku", 100, 50, ticket_id="A")
        tracker.record("sonnet", 200, 100, ticket_id="A")
        tracker.record("opus", 300, 150, ticket_id="B")
        lines = (tmp_path / "usage.jsonl").read_text().strip().splitlines()
        assert len(lines) == 3

    def test_get_ticket_cost_multiple_stages(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        tracker.record("sonnet", 1000, 500, task="investigate", ticket_id="T-1")
        tracker.record("sonnet", 800, 400, task="develop", ticket_id="T-1")
        tracker.record("haiku", 500, 100, task="triage", ticket_id="T-2")

        result = tracker.get_ticket_cost("T-1")
        assert result["total_input_tokens"] == 1800
        assert result["total_output_tokens"] == 900
        assert result["total_usd"] > 0
        assert "investigate" in result["stages"]
        assert "develop" in result["stages"]

    def test_get_ticket_cost_empty(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        result = tracker.get_ticket_cost("nonexistent")
        assert result["total_usd"] == 0
        assert result["total_input_tokens"] == 0
        assert result["stages"] == {}

    def test_get_daily_spend_filters_by_date(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        # Record something today
        tracker.record("sonnet", 1000, 500, task="investigate")
        today_spend = tracker.get_daily_spend()
        assert today_spend > 0

        # Check a different day returns 0
        other_day = datetime(2020, 1, 1, tzinfo=timezone.utc)
        assert tracker.get_daily_spend(date=other_day) == 0.0

    def test_check_budget_daily_cap_exceeded(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        # Record enough to exceed a $0.01 cap
        tracker.record("opus", 10000, 5000, task="investigate")
        has_budget, remaining = tracker.check_budget(daily_cap=0.01)
        assert has_budget is False
        assert remaining == 0.0

    def test_check_budget_daily_cap_ok(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        tracker.record("haiku", 100, 50, task="triage")
        has_budget, remaining = tracker.check_budget(daily_cap=100.0)
        assert has_budget is True
        assert remaining > 0

    def test_check_budget_per_ticket_cap(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        tracker.record("opus", 10000, 5000, task="investigate", ticket_id="T-1")
        has_budget, _ = tracker.check_budget(per_ticket_cap=0.01, ticket_id="T-1")
        assert has_budget is False

    def test_check_budget_no_caps(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        has_budget, remaining = tracker.check_budget()
        assert has_budget is True
        assert remaining == float("inf")

    def test_summary_aggregates_by_model(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        tracker.record("haiku", 100, 50, task="triage")
        tracker.record("sonnet", 200, 100, task="investigate")
        tracker.record("sonnet", 300, 150, task="develop")

        s = tracker.summary()
        assert s["total_records"] == 3
        assert s["total_cost_usd"] > 0
        assert "haiku" in s["by_model"]
        assert "sonnet" in s["by_model"]
        assert s["by_model"]["sonnet"]["calls"] == 2

    def test_empty_store_returns_zeros(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        s = tracker.summary()
        assert s["total_records"] == 0
        assert s["total_cost_usd"] == 0.0
        assert s["by_model"] == {}

    def test_session_totals_updated(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        tracker.record("sonnet", 100, 50, ticket_id="T-1")
        tracker.record("sonnet", 200, 100, ticket_id="T-1")
        assert tracker._session_totals["T-1"]["input_tokens"] == 300
        assert tracker._session_totals["T-1"]["output_tokens"] == 150

    def test_record_with_metadata(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        usage = tracker.record("haiku", 50, 25, metadata={"attempt": 3, "retried": True})
        assert usage.metadata == {"attempt": 3, "retried": True}
        # Verify persisted
        lines = (tmp_path / "usage.jsonl").read_text().strip().splitlines()
        rec = json.loads(lines[0])
        assert rec["metadata"]["attempt"] == 3

    def test_hourly_spend(self, tmp_path):
        tracker = _make_tracker(tmp_path)
        tracker.record("sonnet", 1000, 500)
        spend = tracker.get_hourly_spend()
        assert spend > 0

    def test_custom_pricing_in_tracker(self, tmp_path):
        custom = {
            "haiku": {"input": 0.001, "output": 0.005},
            "default": {"input": 0.001, "output": 0.005},
        }
        tracker = TokenTracker(store_path=tmp_path / "usage.jsonl", pricing=custom)
        usage = tracker.record("haiku", 1000, 1000)
        expected = 1 * 0.001 + 1 * 0.005
        assert usage.cost_usd == pytest.approx(expected, abs=1e-5)
