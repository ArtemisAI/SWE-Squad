"""Tests for TokenTracker expansion: cache tokens, aggregation queries, subscription ROI."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.swe_team.token_tracker import TokenTracker, TokenUsage


@pytest.fixture
def tmp_tracker(tmp_path):
    """Create a TokenTracker backed by a temp JSONL file."""
    return TokenTracker(store_path=tmp_path / "usage.jsonl")


# ── Cache token fields ──────────────────────────────────────────


class TestCacheTokens:
    def test_token_usage_defaults(self):
        u = TokenUsage()
        assert u.cache_read_tokens == 0
        assert u.cache_creation_tokens == 0

    def test_record_with_cache_tokens(self, tmp_tracker):
        usage = tmp_tracker.record(
            model="sonnet",
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=30,
            cache_creation_tokens=10,
        )
        assert usage.cache_read_tokens == 30
        assert usage.cache_creation_tokens == 10

    def test_cache_tokens_persisted_in_jsonl(self, tmp_tracker):
        tmp_tracker.record(
            model="sonnet", input_tokens=100, output_tokens=50,
            cache_read_tokens=30, cache_creation_tokens=10,
        )
        with open(tmp_tracker._path) as f:
            data = json.loads(f.readline())
        assert data["cache_read_tokens"] == 30
        assert data["cache_creation_tokens"] == 10

    def test_backwards_compat_from_dict(self):
        """Old records without cache fields should still load fine."""
        old_data = {
            "session_id": "s1", "ticket_id": "t1", "model": "sonnet",
            "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001,
            "task": "dev", "agent": "claude-code",
            "timestamp": "2026-03-20T00:00:00+00:00", "metadata": {},
        }
        u = TokenUsage.from_dict(old_data)
        assert u.cache_read_tokens == 0
        assert u.cache_creation_tokens == 0

    def test_record_without_cache_tokens(self, tmp_tracker):
        """Calling record() without cache args should still work."""
        usage = tmp_tracker.record(model="haiku", input_tokens=10, output_tokens=5)
        assert usage.cache_read_tokens == 0
        assert usage.cache_creation_tokens == 0


# ── Aggregation helpers ──────────────────────────────────────────

def _seed_tracker(tracker: TokenTracker, hours_ago_list: list[float], **kwargs):
    """Seed records at various hours in the past."""
    for h in hours_ago_list:
        ts = (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()
        usage = TokenUsage(
            model=kwargs.get("model", "sonnet"),
            input_tokens=kwargs.get("input_tokens", 100),
            output_tokens=kwargs.get("output_tokens", 50),
            cache_read_tokens=kwargs.get("cache_read_tokens", 10),
            cache_creation_tokens=kwargs.get("cache_creation_tokens", 5),
            cost_usd=kwargs.get("cost_usd", 0.001),
            task=kwargs.get("task", "dev"),
            agent=kwargs.get("agent", "claude-code"),
            ticket_id=kwargs.get("ticket_id", "T-1"),
            timestamp=ts,
        )
        with open(tracker._path, "a") as f:
            f.write(json.dumps(usage.to_dict(), default=str) + "\n")


class TestByHour:
    def test_empty(self, tmp_tracker):
        assert tmp_tracker.by_hour() == []

    def test_groups_by_hour(self, tmp_tracker):
        _seed_tracker(tmp_tracker, [0.5, 0.7, 2.0])
        result = tmp_tracker.by_hour(since_hours=3)
        # 0.5h and 0.7h ago are usually in the same hour bucket, but near
        # the top of the hour they can split across two buckets → 2 or 3 groups.
        assert len(result) in (2, 3)
        total_count = sum(r["count"] for r in result)
        assert total_count == 3

    def test_respects_since_hours(self, tmp_tracker):
        _seed_tracker(tmp_tracker, [1, 5, 25])
        result = tmp_tracker.by_hour(since_hours=6)
        total_count = sum(r["count"] for r in result)
        assert total_count == 2  # excludes 25h ago


class TestByDay:
    def test_groups_by_day(self, tmp_tracker):
        _seed_tracker(tmp_tracker, [1, 25, 49])
        result = tmp_tracker.by_day(since_days=7)
        assert len(result) >= 2
        total_count = sum(r["count"] for r in result)
        assert total_count == 3


class TestByWeek:
    def test_groups_by_week(self, tmp_tracker):
        _seed_tracker(tmp_tracker, [1, 24 * 8])
        result = tmp_tracker.by_week(since_weeks=4)
        assert len(result) >= 1


class TestByMonth:
    def test_groups_by_month(self, tmp_tracker):
        _seed_tracker(tmp_tracker, [1, 24 * 35])
        result = tmp_tracker.by_month(since_months=3)
        assert len(result) >= 1


class TestByAgent:
    def test_groups_by_agent(self, tmp_tracker):
        _seed_tracker(tmp_tracker, [1], agent="agent-a")
        _seed_tracker(tmp_tracker, [1, 2], agent="agent-b")
        result = tmp_tracker.by_agent(since_hours=24)
        assert "agent-a" in result
        assert "agent-b" in result
        assert result["agent-a"]["count"] == 1
        assert result["agent-b"]["count"] == 2

    def test_includes_cache_fields(self, tmp_tracker):
        _seed_tracker(tmp_tracker, [1], agent="x", cache_read_tokens=20, cache_creation_tokens=7)
        result = tmp_tracker.by_agent(since_hours=24)
        assert result["x"]["cache_read_tokens"] == 20
        assert result["x"]["cache_creation_tokens"] == 7


class TestByTicket:
    def test_groups_by_ticket(self, tmp_tracker):
        _seed_tracker(tmp_tracker, [1], ticket_id="T-100")
        _seed_tracker(tmp_tracker, [1, 2], ticket_id="T-200")
        result = tmp_tracker.by_ticket(since_hours=24)
        assert result["T-100"]["count"] == 1
        assert result["T-200"]["count"] == 2


# ── Subscription ROI ────────────────────────────────────────────


class TestSubscriptionROI:
    def test_basic_roi(self, tmp_tracker):
        _seed_tracker(tmp_tracker, [1, 2, 3], cost_usd=10.0)
        result = tmp_tracker.subscription_roi(monthly_fee=20.0, since_days=1)
        assert result["api_equivalent_cost"] == 30.0
        assert result["subscription_fee"] == 20.0
        assert result["savings"] == 10.0
        assert result["roi_percent"] == 50.0

    def test_negative_roi(self, tmp_tracker):
        _seed_tracker(tmp_tracker, [1], cost_usd=5.0)
        result = tmp_tracker.subscription_roi(monthly_fee=100.0, since_days=1)
        assert result["savings"] < 0

    def test_zero_fee(self, tmp_tracker):
        _seed_tracker(tmp_tracker, [1], cost_usd=5.0)
        result = tmp_tracker.subscription_roi(monthly_fee=0.0)
        assert result["roi_percent"] == 0.0

    def test_empty_records(self, tmp_tracker):
        result = tmp_tracker.subscription_roi(monthly_fee=20.0)
        assert result["api_equivalent_cost"] == 0.0
        assert result["savings"] == -20.0


# ── Aggregation output schema ───────────────────────────────────


class TestAggregationSchema:
    def test_by_hour_has_all_fields(self, tmp_tracker):
        _seed_tracker(tmp_tracker, [1])
        result = tmp_tracker.by_hour(since_hours=2)
        assert len(result) == 1
        row = result[0]
        for key in ("period", "input_tokens", "output_tokens",
                     "cache_read_tokens", "cache_creation_tokens",
                     "cost_usd", "count"):
            assert key in row, f"Missing key: {key}"
