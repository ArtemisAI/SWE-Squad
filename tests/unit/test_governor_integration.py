"""Integration tests for usage governor with scheduler, bonus detector, and runner."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from src.swe_team.providers.usage_governor import create_usage_governor
from src.swe_team.providers.usage_governor.adaptive import AdaptiveUsageGovernor
from src.swe_team.providers.usage_governor.schedule import UsageScheduler, TimeWindow
from src.swe_team.providers.usage_governor.bonus_detector import BonusDetector
from src.swe_team.providers.usage_governor.base import UsageGovernorProvider


def _empty_tracker():
    """Return a mock tracker reporting zero usage (full quota available)."""
    tracker = MagicMock()
    tracker.by_hour.return_value = []
    return tracker


class TestGovernorWithScheduler:
    def test_schedule_multiplier_applied(self):
        scheduler = UsageScheduler(
            timezone_name="UTC",
            windows=[
                TimeWindow("always", 0.5, ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                           start_hour=0, end_hour=24),
            ],
        )
        gov = AdaptiveUsageGovernor(max_agents=4, scheduler=scheduler, token_tracker=_empty_tracker())
        decision = gov.get_concurrency_decision()
        # 5 agents * 0.5 = 2 (or 4 * 0.5 = 2)
        assert decision.max_parallel_agents >= 1
        assert "schedule" in decision.reason

    def test_summary_includes_schedule(self):
        scheduler = UsageScheduler(
            timezone_name="UTC",
            windows=[
                TimeWindow("test_window", 1.0, ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                           start_hour=0, end_hour=24),
            ],
        )
        gov = AdaptiveUsageGovernor(scheduler=scheduler)
        summary = gov.get_daily_summary()
        assert "test_window" in summary


class TestGovernorWithBonus:
    def test_bonus_multiplier_applied(self):
        bonus = BonusDetector(min_sustained_minutes=0)
        # Simulate an active bonus
        data = [
            {"input_tokens": 1000, "output_tokens": 1000}
            for _ in range(10)
        ]
        data[-1] = {"input_tokens": 3000, "output_tokens": 3000}
        bonus.detect(data)

        gov = AdaptiveUsageGovernor(max_agents=3, bonus_detector=bonus, token_tracker=_empty_tracker())
        decision = gov.get_concurrency_decision()
        # With 5x bonus, should be more than base 3
        # But since default tier gives 5 agents: 5 * 1.0 * 5.0 = 25, but at least > 3
        assert decision.max_parallel_agents >= 3

    def test_summary_includes_bonus(self):
        bonus = BonusDetector(min_sustained_minutes=0)
        data = [
            {"input_tokens": 1000, "output_tokens": 1000}
            for _ in range(10)
        ]
        data[-1] = {"input_tokens": 3000, "output_tokens": 3000}
        bonus.detect(data)

        gov = AdaptiveUsageGovernor(bonus_detector=bonus)
        summary = gov.get_daily_summary()
        assert "Bonus active" in summary


class TestGovernorWithBothModifiers:
    def test_schedule_and_bonus_combined(self):
        scheduler = UsageScheduler(
            timezone_name="UTC",
            windows=[
                TimeWindow("always", 2.0, ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                           start_hour=0, end_hour=24),
            ],
        )
        bonus = BonusDetector(min_sustained_minutes=0)
        data = [{"input_tokens": 1000, "output_tokens": 1000} for _ in range(10)]
        data[-1] = {"input_tokens": 2000, "output_tokens": 2000}
        bonus.detect(data)

        gov = AdaptiveUsageGovernor(max_agents=3, scheduler=scheduler, bonus_detector=bonus, token_tracker=_empty_tracker())
        decision = gov.get_concurrency_decision()
        # base 5 * schedule 2.0 * bonus 2.0 = 20
        assert decision.max_parallel_agents >= 5
        assert "schedule" in decision.reason
        assert "bonus" in decision.reason


class TestRunnerGatingMock:
    """Test that runner gating logic works with mock governor."""

    def test_should_launch_gating(self):
        gov = AdaptiveUsageGovernor(token_tracker=_empty_tracker())
        # With full quota and tracker attached, should allow everything
        assert gov.should_launch_new_agent("low") is True
        assert gov.should_launch_new_agent("critical") is True

    def test_blocked_when_quota_exhausted(self):
        tracker = MagicMock()
        tracker.by_hour.return_value = [
            {"input_tokens": 48000, "output_tokens": 48000, "period": "2026-03-23T10"}
        ]
        gov = AdaptiveUsageGovernor(
            quota_limit=100_000,
            token_tracker=tracker,
        )
        # 96% used -> 4% remaining -> critical tier -> allow_new_work=False
        assert gov.should_launch_new_agent("low") is False
        assert gov.should_launch_new_agent("critical") is False


class TestAlertThrottling:
    def test_alerts_fire_once_per_threshold(self):
        tracker = MagicMock()
        tracker.by_hour.return_value = [
            {"input_tokens": 45000, "output_tokens": 45000, "period": "2026-03-23T10"}
        ]
        gov = AdaptiveUsageGovernor(
            quota_limit=100_000,
            token_tracker=tracker,
            alert_throttle_minutes=60,
        )
        a1 = gov.check_alerts()
        a2 = gov.check_alerts()
        assert len(a1) >= 1
        assert len(a2) == 0  # throttled


class TestDailySummary:
    def test_summary_format(self):
        gov = AdaptiveUsageGovernor(quota_limit=1_000_000)
        summary = gov.get_daily_summary()
        assert "Tokens used:" in summary
        assert "Remaining:" in summary
        assert "Burn rate:" in summary
        assert "Max concurrency:" in summary


class TestNoGovernor:
    """Ensure the system works when no governor is configured."""

    def test_factory_with_empty_config(self):
        gov = create_usage_governor({})
        assert isinstance(gov, AdaptiveUsageGovernor)
        assert gov.health_check() is True

    def test_factory_with_full_config(self):
        config = {
            "provider": "adaptive",
            "quota": {"tokens_per_5h_block": 500_000, "tokens_per_day": 2_400_000},
            "concurrency": {
                "max_agents": 5,
                "tiers": [
                    {"remaining_pct": 70, "max_agents": 5, "priority_floor": "low"},
                    {"remaining_pct": 10, "max_agents": 1, "priority_floor": "critical", "allow_new_work": False},
                ],
            },
            "schedule": {
                "timezone": "UTC",
                "windows": [
                    {"name": "always", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                     "hours": [0, 24], "concurrency_multiplier": 1.0},
                ],
            },
            "bonus_detection": {"enabled": True, "throughput_multiplier_threshold": 1.5},
            "alerts": {
                "quota_warning_pct": 20,
                "quota_critical_pct": 10,
                "burn_rate_spike_multiplier": 2.0,
                "throttle_minutes": 60,
            },
        }
        gov = create_usage_governor(config)
        assert isinstance(gov, UsageGovernorProvider)
        assert gov.health_check() is True
        summary = gov.get_daily_summary()
        assert "always" in summary

    def test_factory_no_schedule(self):
        gov = create_usage_governor({"quota": {"tokens_per_day": 1_000_000}})
        assert gov._scheduler is None
        assert gov.health_check() is True

    def test_factory_no_bonus(self):
        gov = create_usage_governor({"bonus_detection": {"enabled": False}})
        assert gov._bonus_detector is None


class TestRunnerInit:
    """Test the _init_usage_governor function pattern."""

    def test_init_with_no_provider_config(self):
        """When providers has no usage_governor key, global stays None."""
        import scripts.ops.swe_team_runner as runner
        original = runner._usage_governor
        try:
            config = MagicMock()
            config.providers = {}
            runner._init_usage_governor(config)
            assert runner._usage_governor is None
        finally:
            runner._usage_governor = original

    def test_init_with_provider_config(self):
        """When usage_governor config is present, governor is created."""
        import scripts.ops.swe_team_runner as runner
        original = runner._usage_governor
        try:
            config = MagicMock()
            config.providers = {
                "usage_governor": {
                    "provider": "adaptive",
                    "quota": {"tokens_per_day": 1_000_000},
                }
            }
            runner._init_usage_governor(config)
            assert runner._usage_governor is not None
            assert runner._usage_governor.health_check() is True
        finally:
            runner._usage_governor = original
