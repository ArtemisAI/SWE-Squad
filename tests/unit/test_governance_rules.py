"""Tests for hierarchical governance rule engine."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.swe_team.providers.usage_governor.rules import (
    GovernanceRule,
    RuleEngine,
    RuleResult,
)
from src.swe_team.providers.usage_governor.adaptive import AdaptiveUsageGovernor
from src.swe_team.providers.usage_governor.schedule import UsageScheduler, TimeWindow
from src.swe_team.providers.usage_governor.bonus_detector import BonusDetector
from src.swe_team.providers.usage_governor import create_usage_governor
from unittest.mock import MagicMock


def _empty_tracker():
    """Return a mock tracker reporting zero usage (full quota available)."""
    tracker = MagicMock()
    tracker.by_hour.return_value = []
    return tracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rule(name: str = "test", multiplier: float = 1.0, precedence: int = 5,
          active: bool = True, **kwargs) -> GovernanceRule:
    return GovernanceRule(
        name=name,
        description=f"Test rule {name}",
        multiplier=multiplier,
        precedence=precedence,
        active=active,
        source="config",
        **kwargs,
    )


# A Wednesday 10:00 AM in America/Toronto (EDT = UTC-4 => 14:00 UTC)
WED_10AM_ET = datetime(2026, 3, 25, 14, 0, 0, tzinfo=timezone.utc)
# A Wednesday 3:00 PM in America/Toronto (EDT = UTC-4 => 19:00 UTC)
WED_3PM_ET = datetime(2026, 3, 25, 19, 0, 0, tzinfo=timezone.utc)
# A Saturday 10:00 AM in America/Toronto
SAT_10AM_ET = datetime(2026, 3, 28, 14, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Rule creation and sorting
# ---------------------------------------------------------------------------

class TestRuleCreation:
    def test_rule_dataclass(self):
        r = _rule("boost", 2.0, 2)
        assert r.name == "boost"
        assert r.multiplier == 2.0
        assert r.precedence == 2
        assert r.active is True
        assert r.schedule_days is None

    def test_rules_sorted_by_precedence(self):
        rules = [_rule("a", precedence=5), _rule("b", precedence=1), _rule("c", precedence=3)]
        engine = RuleEngine(rules, {})
        assert [r.precedence for r in engine._rules] == [1, 3, 5]


# ---------------------------------------------------------------------------
# Single rule application
# ---------------------------------------------------------------------------

class TestSingleRule:
    def test_boost_rule(self):
        engine = RuleEngine([_rule("boost", multiplier=2.0, precedence=2)], {})
        result = engine.evaluate(5, WED_10AM_ET)
        assert result.effective_agents == 10
        assert result.combined_multiplier == 2.0

    def test_reduce_rule(self):
        engine = RuleEngine([_rule("reduce", multiplier=0.5, precedence=2)], {})
        result = engine.evaluate(6, WED_10AM_ET)
        assert result.effective_agents == 3
        assert result.combined_multiplier == 0.5

    def test_inactive_rule_ignored(self):
        engine = RuleEngine([_rule("off", multiplier=0.1, active=False)], {})
        result = engine.evaluate(5, WED_10AM_ET)
        assert result.effective_agents == 5
        assert result.combined_multiplier == 1.0
        assert len(result.applied_rules) == 0


# ---------------------------------------------------------------------------
# Multiple rules — multiplicative composition
# ---------------------------------------------------------------------------

class TestMultipleRules:
    def test_two_rules_multiply(self):
        rules = [
            _rule("a", multiplier=2.0, precedence=2),
            _rule("b", multiplier=0.5, precedence=4),
        ]
        engine = RuleEngine(rules, {"max_agents_absolute": 20})
        result = engine.evaluate(5, WED_10AM_ET)
        # 5 * 2.0 * 0.5 = 5
        assert result.effective_agents == 5
        assert result.combined_multiplier == 1.0

    def test_three_rules_multiply(self):
        rules = [
            _rule("a", multiplier=2.0, precedence=2),
            _rule("b", multiplier=3.0, precedence=3),
            _rule("c", multiplier=0.5, precedence=4),
        ]
        engine = RuleEngine(rules, {"max_agents_absolute": 50})
        result = engine.evaluate(4, WED_10AM_ET)
        # 4 * 2.0 * 3.0 * 0.5 = 12
        assert result.effective_agents == 12


# ---------------------------------------------------------------------------
# Conflict resolution at same precedence
# ---------------------------------------------------------------------------

class TestConflictResolution:
    def test_same_precedence_conflict_takes_conservative(self):
        rules = [
            _rule("boost", multiplier=2.0, precedence=2),
            _rule("reduce", multiplier=0.1, precedence=2),
        ]
        engine = RuleEngine(rules, {})
        result = engine.evaluate(5, WED_10AM_ET)
        # Conflict at P2: boost vs reduce => take 0.1 (conservative)
        # 5 * 0.1 = 0.5 => round = 0, clamped to 1
        assert result.effective_agents == 1
        assert len(result.applied_rules) == 1
        assert result.applied_rules[0].name == "reduce"

    def test_same_precedence_no_conflict_all_boosts(self):
        rules = [
            _rule("a", multiplier=2.0, precedence=2),
            _rule("b", multiplier=3.0, precedence=2),
        ]
        engine = RuleEngine(rules, {"max_agents_absolute": 50})
        result = engine.evaluate(2, WED_10AM_ET)
        # No conflict (both > 1.0): 2 * 2.0 * 3.0 = 12
        assert result.effective_agents == 12

    def test_same_precedence_no_conflict_all_reduces(self):
        rules = [
            _rule("a", multiplier=0.5, precedence=2),
            _rule("b", multiplier=0.8, precedence=2),
        ]
        engine = RuleEngine(rules, {})
        result = engine.evaluate(10, WED_10AM_ET)
        # No conflict (both < 1.0): 10 * 0.5 * 0.8 = 4
        assert result.effective_agents == 4


# ---------------------------------------------------------------------------
# Operator override schedule matching
# ---------------------------------------------------------------------------

class TestScheduleMatching:
    def test_normal_window_match(self):
        """8-14 ET on weekdays should match at 10 AM ET."""
        r = _rule("peak", multiplier=0.1, schedule_days=["wed"],
                   schedule_start_hour=8, schedule_end_hour=14,
                   schedule_timezone="America/Toronto")
        assert r.matches_time(WED_10AM_ET) is True

    def test_normal_window_no_match(self):
        """8-14 ET on weekdays should NOT match at 3 PM ET."""
        r = _rule("peak", multiplier=0.1, schedule_days=["wed"],
                   schedule_start_hour=8, schedule_end_hour=14,
                   schedule_timezone="America/Toronto")
        assert r.matches_time(WED_3PM_ET) is False

    def test_overnight_window_match_evening(self):
        """14-8 ET (overnight) should match at 3 PM ET."""
        r = _rule("night", multiplier=2.0, schedule_days=["wed"],
                   schedule_start_hour=14, schedule_end_hour=8,
                   schedule_timezone="America/Toronto")
        assert r.matches_time(WED_3PM_ET) is True

    def test_overnight_window_match_early_morning(self):
        """14-8 ET (overnight) should match at 3 AM ET (7 UTC)."""
        early_morning = datetime(2026, 3, 26, 7, 0, 0, tzinfo=timezone.utc)  # Thu 3AM ET
        r = _rule("night", multiplier=2.0, schedule_days=["thu"],
                   schedule_start_hour=14, schedule_end_hour=8,
                   schedule_timezone="America/Toronto")
        assert r.matches_time(early_morning) is True

    def test_all_day_window(self):
        """0-24 should match any hour."""
        r = _rule("allday", multiplier=1.5, schedule_days=["sat"],
                   schedule_start_hour=0, schedule_end_hour=24,
                   schedule_timezone="America/Toronto")
        assert r.matches_time(SAT_10AM_ET) is True

    def test_wrong_day_no_match(self):
        """Rule for mon should not match on wed."""
        r = _rule("mon_only", multiplier=2.0, schedule_days=["mon"],
                   schedule_start_hour=0, schedule_end_hour=24,
                   schedule_timezone="America/Toronto")
        assert r.matches_time(WED_10AM_ET) is False

    def test_no_schedule_always_matches(self):
        """Rule without schedule_days always matches."""
        r = _rule("always")
        assert r.matches_time(WED_10AM_ET) is True


# ---------------------------------------------------------------------------
# Timezone-aware matching
# ---------------------------------------------------------------------------

class TestTimezoneAware:
    def test_utc_vs_toronto(self):
        """A rule set for 10-11 UTC should NOT match 10 AM ET (which is 14 UTC)."""
        r = _rule("utc_window", multiplier=0.5, schedule_days=["wed"],
                   schedule_start_hour=10, schedule_end_hour=11,
                   schedule_timezone="UTC")
        # WED_10AM_ET is 14:00 UTC
        assert r.matches_time(WED_10AM_ET) is False

    def test_toronto_timezone_conversion(self):
        """Rule for 13-15 ET should match at 14:00 ET (18:00 UTC)."""
        dt_18utc = datetime(2026, 3, 25, 18, 0, 0, tzinfo=timezone.utc)  # Wed 2PM ET
        r = _rule("et_afternoon", multiplier=0.5, schedule_days=["wed"],
                   schedule_start_hour=13, schedule_end_hour=15,
                   schedule_timezone="America/Toronto")
        assert r.matches_time(dt_18utc) is True


# ---------------------------------------------------------------------------
# Hard limit clamping
# ---------------------------------------------------------------------------

class TestHardLimits:
    def test_clamp_max(self):
        engine = RuleEngine(
            [_rule("big_boost", multiplier=10.0)],
            {"max_agents_absolute": 8, "min_agents_absolute": 1},
        )
        result = engine.evaluate(5, WED_10AM_ET)
        assert result.effective_agents == 8

    def test_clamp_min(self):
        engine = RuleEngine(
            [_rule("big_reduce", multiplier=0.01)],
            {"max_agents_absolute": 10, "min_agents_absolute": 2},
        )
        result = engine.evaluate(5, WED_10AM_ET)
        assert result.effective_agents == 2

    def test_default_limits(self):
        engine = RuleEngine([_rule("big_boost", multiplier=100.0)], {})
        result = engine.evaluate(5, WED_10AM_ET)
        # Default max is 10
        assert result.effective_agents == 10

    def test_default_min_is_1(self):
        engine = RuleEngine([_rule("big_reduce", multiplier=0.001)], {})
        result = engine.evaluate(5, WED_10AM_ET)
        assert result.effective_agents == 1


# ---------------------------------------------------------------------------
# Audit trail format
# ---------------------------------------------------------------------------

class TestAuditTrail:
    def test_audit_trail_contains_rule_names(self):
        rules = [_rule("peak_reduction", multiplier=0.1, precedence=2)]
        engine = RuleEngine(rules, {})
        result = engine.evaluate(5, WED_10AM_ET)
        assert "peak_reduction" in result.audit_trail
        assert "0.1x" in result.audit_trail
        assert "P2" in result.audit_trail

    def test_audit_trail_contains_effective_and_base(self):
        rules = [_rule("boost", multiplier=2.0, precedence=3)]
        engine = RuleEngine(rules, {"max_agents_absolute": 20})
        result = engine.evaluate(5, WED_10AM_ET)
        assert "effective=10" in result.audit_trail
        assert "base=5" in result.audit_trail

    def test_audit_trail_clamped_range(self):
        engine = RuleEngine([_rule("x", multiplier=1.0)], {"max_agents_absolute": 8, "min_agents_absolute": 2})
        result = engine.evaluate(5, WED_10AM_ET)
        assert "clamped=[2,8]" in result.audit_trail

    def test_no_rules_audit_trail(self):
        engine = RuleEngine([], {})
        result = engine.evaluate(5, WED_10AM_ET)
        assert "no rules" in result.audit_trail


# ---------------------------------------------------------------------------
# Empty rules = base unchanged
# ---------------------------------------------------------------------------

class TestEmptyRules:
    def test_no_rules(self):
        engine = RuleEngine([], {})
        result = engine.evaluate(5, WED_10AM_ET)
        assert result.effective_agents == 5
        assert result.combined_multiplier == 1.0
        assert result.applied_rules == []

    def test_all_inactive_rules(self):
        rules = [_rule("off1", active=False), _rule("off2", active=False)]
        engine = RuleEngine(rules, {})
        result = engine.evaluate(5, WED_10AM_ET)
        assert result.effective_agents == 5


# ---------------------------------------------------------------------------
# Integration: AdaptiveUsageGovernor uses RuleEngine
# ---------------------------------------------------------------------------

class TestAdaptiveIntegration:
    def test_governor_with_no_overrides_backwards_compatible(self):
        """No operator_overrides => same behavior as before (with tracker attached)."""
        gov = AdaptiveUsageGovernor(token_tracker=_empty_tracker())
        decision = gov.get_concurrency_decision()
        # Default: 100% remaining => tier 70% => 5 agents, no rules
        assert decision.max_parallel_agents == 5
        assert isinstance(decision.applied_rules, list)

    def test_governor_with_operator_override(self):
        """Operator override should affect concurrency."""
        overrides = [
            {
                "name": "boost",
                "description": "test boost",
                "multiplier": 2.0,
                "active": True,
                # No schedule = always active
            }
        ]
        gov = AdaptiveUsageGovernor(
            operator_overrides=overrides,
            hard_limits={"max_agents_absolute": 20},
            token_tracker=_empty_tracker(),
        )
        decision = gov.get_concurrency_decision()
        # Default tier gives 5 agents, * 2.0 = 10
        assert decision.max_parallel_agents == 10
        assert "boost" in decision.applied_rules

    def test_governor_audit_trail_in_decision(self):
        overrides = [{"name": "x", "multiplier": 0.5, "active": True}]
        gov = AdaptiveUsageGovernor(operator_overrides=overrides, token_tracker=_empty_tracker())
        decision = gov.get_concurrency_decision()
        assert decision.audit_trail != ""
        assert "x" in decision.audit_trail


# ---------------------------------------------------------------------------
# Config parsing of operator_overrides
# ---------------------------------------------------------------------------

class TestConfigParsing:
    def test_create_usage_governor_with_overrides(self):
        config = {
            "quota": {"tokens_per_day": 2400000},
            "concurrency": {"max_agents": 5},
            "hard_limits": {"max_agents_absolute": 10, "min_agents_absolute": 1},
            "operator_overrides": [
                {
                    "name": "test_override",
                    "description": "Test",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "start_hour": 0,
                    "end_hour": 24,
                    "timezone": "UTC",
                    "multiplier": 2.0,
                    "active": True,
                }
            ],
        }
        gov = create_usage_governor(config)
        gov.set_token_tracker(_empty_tracker())
        decision = gov.get_concurrency_decision()
        assert decision.max_parallel_agents == 10  # 5 * 2.0 = 10, clamped to 10
        assert "test_override" in decision.applied_rules

    def test_create_usage_governor_without_overrides(self):
        """Backwards compatible: no overrides key => no change."""
        config = {
            "quota": {"tokens_per_day": 2400000},
            "concurrency": {"max_agents": 5},
        }
        gov = create_usage_governor(config)
        gov.set_token_tracker(_empty_tracker())
        decision = gov.get_concurrency_decision()
        assert decision.max_parallel_agents == 5


# ---------------------------------------------------------------------------
# Rounding edge cases
# ---------------------------------------------------------------------------

class TestRounding:
    def test_rounds_correctly(self):
        engine = RuleEngine([_rule("x", multiplier=0.6)], {"max_agents_absolute": 20})
        result = engine.evaluate(5, WED_10AM_ET)
        # 5 * 0.6 = 3.0
        assert result.effective_agents == 3

    def test_rounds_half_up(self):
        engine = RuleEngine([_rule("x", multiplier=0.5)], {"max_agents_absolute": 20})
        result = engine.evaluate(3, WED_10AM_ET)
        # 3 * 0.5 = 1.5 => round = 2
        assert result.effective_agents == 2
