"""Tests for the dynamic throttle system (#15)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.swe_team.throttle import (
    CapacityAdapter,
    DemandAdapter,
    ResolvedCycleConfig,
    ThrottleConfig,
    ThrottleContext,
    ThrottlePolicy,
    TimeBasedAdapter,
    days_until_weekly_reset,
    _get_est_hour,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _FakeCycleConfig:
    max_new_tickets_per_cycle: int = 10
    max_investigations_per_cycle: int = 10
    max_developments_per_cycle: int = 5
    max_open_investigating: int = 8
    severity_filter: str = "medium"


def _ctx(hour_utc: int = 12, **kwargs) -> ThrottleContext:
    """Create a ThrottleContext at a specific UTC hour."""
    return ThrottleContext(
        now_utc=datetime(2026, 3, 19, hour_utc, 0, 0, tzinfo=timezone.utc),
        **kwargs,
    )


def _default_config() -> ThrottleConfig:
    return ThrottleConfig(enabled=True)


# ---------------------------------------------------------------------------
# TimeBasedAdapter
# ---------------------------------------------------------------------------

class TestTimeBasedAdapter:
    def test_business_hours(self):
        """8am-5pm ET → business band (1.0x)."""
        # 14:00 UTC = 10:00 AM EST (or 10:00 AM EDT in March)
        adapter = TimeBasedAdapter(_default_config())
        result = adapter.evaluate(_ctx(hour_utc=14), _FakeCycleConfig())
        assert result.multiplier == 1.0
        assert "business" in result.reason

    def test_evening_hours(self):
        """5pm-12am ET → evening band (2.0x)."""
        # 23:00 UTC = 6:00 PM EST (or 7:00 PM EDT)
        adapter = TimeBasedAdapter(_default_config())
        result = adapter.evaluate(_ctx(hour_utc=23), _FakeCycleConfig())
        assert result.multiplier == 2.0
        assert "evening" in result.reason

    def test_overnight_hours(self):
        """12am-8am ET → overnight band (4.0x)."""
        # 06:00 UTC = 1:00 AM EST (or 2:00 AM EDT)
        adapter = TimeBasedAdapter(_default_config())
        result = adapter.evaluate(_ctx(hour_utc=6), _FakeCycleConfig())
        assert result.multiplier == 4.0
        assert "overnight" in result.reason

    def test_custom_multipliers(self):
        """Custom time band multipliers from config."""
        config = ThrottleConfig(
            enabled=True,
            time_bands={"business": 0.5, "evening": 3.0, "overnight": 6.0},
        )
        adapter = TimeBasedAdapter(config)
        result = adapter.evaluate(_ctx(hour_utc=6), _FakeCycleConfig())
        assert result.multiplier == 6.0

    def test_no_severity_override(self):
        """Time adapter never overrides severity."""
        adapter = TimeBasedAdapter(_default_config())
        result = adapter.evaluate(_ctx(hour_utc=6), _FakeCycleConfig())
        assert result.severity_override is None


# ---------------------------------------------------------------------------
# CapacityAdapter
# ---------------------------------------------------------------------------

class TestCapacityAdapter:
    def test_below_threshold(self):
        """Usage below 80% → no change."""
        adapter = CapacityAdapter(_default_config())
        result = adapter.evaluate(
            _ctx(api_usage_pct=0.5, api_days_to_reset=5.0),
            _FakeCycleConfig(),
        )
        assert result.multiplier == 1.0
        assert result.severity_override is None

    def test_warning_level(self):
        """80%+ usage with >=2 days to reset → 0.5x, critical-only."""
        adapter = CapacityAdapter(_default_config())
        result = adapter.evaluate(
            _ctx(api_usage_pct=0.85, api_days_to_reset=3.0),
            _FakeCycleConfig(),
        )
        assert result.multiplier == 0.5
        assert result.severity_override == "critical"

    def test_warning_close_to_reset(self):
        """80%+ but <2 days to reset → no throttle (will reset soon)."""
        adapter = CapacityAdapter(_default_config())
        result = adapter.evaluate(
            _ctx(api_usage_pct=0.85, api_days_to_reset=1.0),
            _FakeCycleConfig(),
        )
        assert result.multiplier == 1.0

    def test_critical_level(self):
        """95%+ usage → emergency 0.1x."""
        adapter = CapacityAdapter(_default_config())
        result = adapter.evaluate(
            _ctx(api_usage_pct=0.97, api_days_to_reset=5.0),
            _FakeCycleConfig(),
        )
        assert result.multiplier == 0.1
        assert result.severity_override == "critical"

    def test_critical_regardless_of_reset(self):
        """95%+ triggers even if reset is tomorrow."""
        adapter = CapacityAdapter(_default_config())
        result = adapter.evaluate(
            _ctx(api_usage_pct=0.96, api_days_to_reset=0.5),
            _FakeCycleConfig(),
        )
        assert result.multiplier == 0.1


# ---------------------------------------------------------------------------
# DemandAdapter
# ---------------------------------------------------------------------------

class TestDemandAdapter:
    def test_normal_backlog(self):
        """Small backlog → no change."""
        adapter = DemandAdapter(_default_config())
        result = adapter.evaluate(
            _ctx(backlog_size=50, backlog_critical=5),
            _FakeCycleConfig(),
        )
        assert result.multiplier == 1.0

    def test_surge_backlog(self):
        """200+ tickets → 1.5x surge."""
        adapter = DemandAdapter(_default_config())
        result = adapter.evaluate(
            _ctx(backlog_size=250, backlog_critical=5),
            _FakeCycleConfig(),
        )
        assert result.multiplier == 1.5

    def test_critical_mass(self):
        """200+ tickets AND 20+ critical → 2.0x."""
        adapter = DemandAdapter(_default_config())
        result = adapter.evaluate(
            _ctx(backlog_size=300, backlog_critical=25),
            _FakeCycleConfig(),
        )
        assert result.multiplier == 2.0

    def test_pre_release_flag(self):
        """Pre-release flag → 1.5x even with small backlog."""
        adapter = DemandAdapter(_default_config())
        result = adapter.evaluate(
            _ctx(backlog_size=10, is_pre_release=True),
            _FakeCycleConfig(),
        )
        assert result.multiplier == 1.5

    def test_no_severity_override(self):
        """Demand adapter never overrides severity."""
        adapter = DemandAdapter(_default_config())
        result = adapter.evaluate(
            _ctx(backlog_size=300, backlog_critical=30),
            _FakeCycleConfig(),
        )
        assert result.severity_override is None


# ---------------------------------------------------------------------------
# ThrottlePolicy
# ---------------------------------------------------------------------------

class TestThrottlePolicy:
    def test_all_neutral(self):
        """All adapters return 1.0 → no change from base config."""
        base = _FakeCycleConfig()
        policy = ThrottlePolicy(base, [
            TimeBasedAdapter(ThrottleConfig(
                enabled=True, time_bands={"business": 1.0, "evening": 1.0, "overnight": 1.0},
            )),
        ])
        result = policy.resolve(_ctx(hour_utc=14))
        assert result.max_investigations_per_cycle == 10
        assert result.effective_multiplier == 1.0

    def test_multipliers_compose(self):
        """Overnight (4x) + demand surge (1.5x) = 6.0 → clamped to 4.0."""
        base = _FakeCycleConfig()
        config = _default_config()
        policy = ThrottlePolicy(base, [
            TimeBasedAdapter(config),
            DemandAdapter(config),
        ])
        # 06:00 UTC = overnight (4x), backlog 250 = surge (1.5x), product = 6.0 → capped at 4.0
        result = policy.resolve(_ctx(hour_utc=6, backlog_size=250))
        assert result.effective_multiplier == 4.0  # capped
        assert result.max_investigations_per_cycle == 40  # 10 * 4.0

    def test_capacity_constrains(self):
        """Overnight (4x) * capacity warning (0.5x) = 2.0x."""
        base = _FakeCycleConfig()
        config = _default_config()
        policy = ThrottlePolicy(base, [
            TimeBasedAdapter(config),
            CapacityAdapter(config),
        ])
        result = policy.resolve(_ctx(hour_utc=6, api_usage_pct=0.85, api_days_to_reset=3.0))
        assert result.effective_multiplier == 2.0  # 4.0 * 0.5
        assert result.severity_filter == "critical"  # from capacity override

    def test_floor_at_minimum(self):
        """Multiplier never goes below 0.1."""
        base = _FakeCycleConfig()
        config = _default_config()
        policy = ThrottlePolicy(base, [
            CapacityAdapter(config),
        ])
        result = policy.resolve(_ctx(api_usage_pct=0.99, api_days_to_reset=5.0))
        assert result.effective_multiplier == 0.1
        # All limits floored at 1
        assert result.max_investigations_per_cycle >= 1
        assert result.max_open_investigating >= 1
        assert result.max_developments_per_cycle >= 1
        assert result.max_new_tickets_per_cycle >= 1

    def test_severity_most_restrictive(self):
        """When multiple adapters override severity, most restrictive wins."""
        base = _FakeCycleConfig(severity_filter="low")
        config = _default_config()
        policy = ThrottlePolicy(base, [
            CapacityAdapter(config),
        ])
        result = policy.resolve(_ctx(api_usage_pct=0.85, api_days_to_reset=3.0))
        assert result.severity_filter == "critical"

    def test_adapter_error_fallback(self):
        """If an adapter throws, it falls back to 1.0x."""
        class BrokenAdapter:
            def evaluate(self, context, base):
                raise RuntimeError("boom")

        base = _FakeCycleConfig()
        policy = ThrottlePolicy(base, [BrokenAdapter()])
        result = policy.resolve(_ctx())
        assert result.effective_multiplier == 1.0
        assert result.max_investigations_per_cycle == 10

    def test_empty_adapters(self):
        """No adapters → passthrough of base config."""
        base = _FakeCycleConfig()
        policy = ThrottlePolicy(base, [])
        result = policy.resolve(_ctx())
        assert result.effective_multiplier == 1.0
        assert result.max_investigations_per_cycle == 10


# ---------------------------------------------------------------------------
# ResolvedCycleConfig
# ---------------------------------------------------------------------------

class TestResolvedCycleConfig:
    def test_duck_type_compatible(self):
        """ResolvedCycleConfig has same fields as CycleConfig."""
        rc = ResolvedCycleConfig()
        assert hasattr(rc, "max_new_tickets_per_cycle")
        assert hasattr(rc, "max_investigations_per_cycle")
        assert hasattr(rc, "max_developments_per_cycle")
        assert hasattr(rc, "max_open_investigating")
        assert hasattr(rc, "severity_filter")

    def test_extra_fields(self):
        """ResolvedCycleConfig has observability extras."""
        rc = ResolvedCycleConfig(effective_multiplier=2.5, reasons=["test"])
        assert rc.effective_multiplier == 2.5
        assert rc.reasons == ["test"]


# ---------------------------------------------------------------------------
# ThrottleConfig.from_dict
# ---------------------------------------------------------------------------

class TestThrottleConfig:
    def test_from_dict_defaults(self):
        """Empty dict → sensible defaults."""
        tc = ThrottleConfig.from_dict({})
        assert tc.enabled is False
        assert tc.weekly_budget_usd == 500.0
        assert "business" in tc.time_bands

    def test_from_dict_full(self):
        """Full dict round-trips correctly."""
        data = {
            "enabled": True,
            "weekly_budget_usd": 1000.0,
            "backlog_surge_threshold": 100,
            "critical_surge_threshold": 10,
            "time_bands": {
                "business": {"multiplier": 0.5},
                "evening": {"multiplier": 1.5},
                "overnight": {"multiplier": 3.0},
            },
            "capacity_thresholds": {
                "warning_pct": 0.7,
                "warning_multiplier": 0.6,
                "critical_pct": 0.9,
                "critical_multiplier": 0.2,
            },
        }
        tc = ThrottleConfig.from_dict(data)
        assert tc.enabled is True
        assert tc.weekly_budget_usd == 1000.0
        assert tc.time_bands["business"] == 0.5
        assert tc.capacity_warning_pct == 0.7
        assert tc.capacity_critical_multiplier == 0.2

    def test_to_dict(self):
        """to_dict round-trip."""
        tc = ThrottleConfig(enabled=True, weekly_budget_usd=750.0)
        d = tc.to_dict()
        assert d["enabled"] is True
        assert d["weekly_budget_usd"] == 750.0

    def test_to_dict_from_dict_roundtrip(self):
        """to_dict() output can be fed into from_dict() and round-trips correctly."""
        original = ThrottleConfig(
            enabled=True,
            weekly_budget_usd=300.0,
            capacity_warning_pct=0.75,
            capacity_critical_multiplier=0.05,
        )
        restored = ThrottleConfig.from_dict(original.to_dict())
        assert restored.enabled == original.enabled
        assert restored.weekly_budget_usd == original.weekly_budget_usd
        assert restored.capacity_warning_pct == original.capacity_warning_pct
        assert restored.capacity_critical_multiplier == original.capacity_critical_multiplier


# ---------------------------------------------------------------------------
# days_until_weekly_reset
# ---------------------------------------------------------------------------

class TestDaysUntilWeeklyReset:
    def test_monday(self):
        """On a Monday, reset is next Monday (7 days)."""
        monday = datetime(2026, 3, 16, 12, 0, 0, tzinfo=timezone.utc)  # Monday
        days = days_until_weekly_reset(monday)
        assert 6.0 < days <= 7.0

    def test_friday(self):
        """On Friday, reset is in 3 days."""
        friday = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)  # Friday
        days = days_until_weekly_reset(friday)
        assert 2.0 < days <= 3.0

    def test_sunday(self):
        """On Sunday, reset is tomorrow."""
        sunday = datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc)  # Sunday
        days = days_until_weekly_reset(sunday)
        assert 0.0 < days <= 1.0


# ---------------------------------------------------------------------------
# Integration: full three-adapter policy
# ---------------------------------------------------------------------------

class TestFullPolicyIntegration:
    def test_overnight_normal_capacity_normal_demand(self):
        """Overnight, no pressure → 4.0x."""
        base = _FakeCycleConfig()
        config = _default_config()
        policy = ThrottlePolicy(base, [
            TimeBasedAdapter(config),
            CapacityAdapter(config),
            DemandAdapter(config),
        ])
        result = policy.resolve(_ctx(hour_utc=6))
        assert result.effective_multiplier == 4.0

    def test_business_critical_capacity(self):
        """Business hours + critical API budget → 0.1x, critical-only."""
        base = _FakeCycleConfig()
        config = _default_config()
        policy = ThrottlePolicy(base, [
            TimeBasedAdapter(config),
            CapacityAdapter(config),
            DemandAdapter(config),
        ])
        result = policy.resolve(_ctx(hour_utc=14, api_usage_pct=0.97, api_days_to_reset=5.0))
        assert result.effective_multiplier == 0.1  # 1.0 * 0.1 * 1.0, floored
        assert result.severity_filter == "critical"

    def test_evening_surge_demand(self):
        """Evening (2x) + surge demand (1.5x) = 3.0x."""
        base = _FakeCycleConfig()
        config = _default_config()
        policy = ThrottlePolicy(base, [
            TimeBasedAdapter(config),
            CapacityAdapter(config),
            DemandAdapter(config),
        ])
        result = policy.resolve(_ctx(hour_utc=23, backlog_size=250))
        assert result.effective_multiplier == 3.0
