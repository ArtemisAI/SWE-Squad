"""
Dynamic throttle system for SWE-Squad cycle limits.

Replaces hardcoded cycle config values with dynamically computed limits
based on time-of-day, API capacity, and backlog demand signals.

Usage::

    from src.swe_team.throttle import (
        ThrottlePolicy, ThrottleContext,
        TimeBasedAdapter, CapacityAdapter, DemandAdapter,
    )

    policy = ThrottlePolicy(
        base_config=config.cycle,
        adapters=[TimeBasedAdapter(tc), CapacityAdapter(tc), DemandAdapter(tc)],
    )
    ctx = ThrottleContext(now_utc=datetime.now(timezone.utc), ...)
    effective = policy.resolve(ctx)
    # effective.max_investigations_per_cycle, etc.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Severity ranking used for override comparison
_SEV_RANK: Dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Multiplier bounds — prevent runaway scaling in either direction
_MIN_MULTIPLIER = 0.1
_MAX_MULTIPLIER = 4.0


# ---------------------------------------------------------------------------
# Configuration dataclass (loaded from swe_team.yaml throttle: section)
# ---------------------------------------------------------------------------

@dataclass
class ThrottleConfig:
    """Configuration for the dynamic throttle system."""

    enabled: bool = False
    weekly_budget_usd: float = 500.0
    backlog_surge_threshold: int = 200
    critical_surge_threshold: int = 20

    # Time band multipliers (keyed by band name)
    time_bands: Dict[str, float] = field(default_factory=lambda: {
        "business": 1.0,    # 8am-5pm EST
        "evening": 2.0,     # 5pm-12am EST
        "overnight": 4.0,   # 12am-8am EST
    })

    # Capacity thresholds
    capacity_warning_pct: float = 0.8
    capacity_warning_multiplier: float = 0.5
    capacity_critical_pct: float = 0.95
    capacity_critical_multiplier: float = 0.1

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ThrottleConfig":
        time_bands_raw = data.get("time_bands", {})
        time_bands = {}
        for band_name, band_data in time_bands_raw.items():
            if isinstance(band_data, dict):
                time_bands[band_name] = band_data.get("multiplier", 1.0)
            else:
                time_bands[band_name] = float(band_data)

        cap = data.get("capacity_thresholds", {})
        return cls(
            enabled=data.get("enabled", False),
            weekly_budget_usd=data.get("weekly_budget_usd", 500.0),
            backlog_surge_threshold=data.get("backlog_surge_threshold", 200),
            critical_surge_threshold=data.get("critical_surge_threshold", 20),
            time_bands=time_bands or {
                "business": 1.0, "evening": 2.0, "overnight": 4.0,
            },
            capacity_warning_pct=cap.get("warning_pct", 0.8),
            capacity_warning_multiplier=cap.get("warning_multiplier", 0.5),
            capacity_critical_pct=cap.get("critical_pct", 0.95),
            capacity_critical_multiplier=cap.get("critical_multiplier", 0.1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "weekly_budget_usd": self.weekly_budget_usd,
            "backlog_surge_threshold": self.backlog_surge_threshold,
            "critical_surge_threshold": self.critical_surge_threshold,
            "time_bands": self.time_bands,
            "capacity_warning_pct": self.capacity_warning_pct,
            "capacity_warning_multiplier": self.capacity_warning_multiplier,
            "capacity_critical_pct": self.capacity_critical_pct,
            "capacity_critical_multiplier": self.capacity_critical_multiplier,
        }


# ---------------------------------------------------------------------------
# Context and result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ThrottleContext:
    """Input signals gathered at cycle start for throttle evaluation."""

    now_utc: datetime
    api_usage_pct: float = 0.0          # 0.0-1.0, weekly API usage fraction
    api_days_to_reset: float = 7.0      # Days until weekly usage resets
    backlog_size: int = 0               # Count of OPEN+TRIAGED tickets
    backlog_critical: int = 0           # Critical tickets in backlog
    is_pre_release: bool = False        # Manual flag for release pressure
    rate_limit_cooling: bool = False    # From RateLimitTracker.is_cooling_down()


@dataclass
class ThrottleResult:
    """Output from a single throttle adapter."""

    multiplier: float = 1.0
    severity_override: Optional[str] = None
    reason: str = ""


@dataclass
class ResolvedCycleConfig:
    """Dynamically computed cycle limits — duck-type compatible with CycleConfig."""

    max_new_tickets_per_cycle: int = 20
    max_investigations_per_cycle: int = 5
    max_developments_per_cycle: int = 2
    max_open_investigating: int = 3
    severity_filter: str = "high"
    effective_multiplier: float = 1.0
    reasons: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Adapter base class
# ---------------------------------------------------------------------------

class ThrottleAdapter(abc.ABC):
    """Base class for throttle strategy adapters."""

    @abc.abstractmethod
    def evaluate(self, context: ThrottleContext, base: Any) -> ThrottleResult:
        """Evaluate this adapter's throttle signal.

        Parameters
        ----------
        context:
            Signals gathered at cycle start.
        base:
            The static CycleConfig from YAML (for reference values).

        Returns
        -------
        ThrottleResult with multiplier and optional severity override.
        """


# ---------------------------------------------------------------------------
# Time-based adapter
# ---------------------------------------------------------------------------

def _get_est_hour(utc_dt: datetime) -> int:
    """Get the current hour in US Eastern time (handles EDT/EST)."""
    try:
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
    except ImportError:
        # Fallback: assume EST (UTC-5) if zoneinfo not available
        from datetime import timedelta
        est_dt = utc_dt - timedelta(hours=5)
        return est_dt.hour

    est_dt = utc_dt.astimezone(eastern)
    return est_dt.hour


class TimeBasedAdapter(ThrottleAdapter):
    """Adjusts capacity based on time of day (US Eastern).

    Bands:
    - business (8am-5pm EST): baseline (1.0x)
    - evening (5pm-12am EST): increased (2.0x)
    - overnight (12am-8am EST): maximum (4.0x)
    """

    def __init__(self, config: ThrottleConfig) -> None:
        self._config = config

    def evaluate(self, context: ThrottleContext, base: Any) -> ThrottleResult:
        hour = _get_est_hour(context.now_utc)

        if 8 <= hour < 17:
            band = "business"
        elif 17 <= hour < 24:
            band = "evening"
        else:  # 0 <= hour < 8
            band = "overnight"

        multiplier = self._config.time_bands.get(band, 1.0)
        return ThrottleResult(
            multiplier=multiplier,
            reason=f"time={band} ({hour}:00 EST) → {multiplier}x",
        )


# ---------------------------------------------------------------------------
# Capacity-based adapter
# ---------------------------------------------------------------------------

class CapacityAdapter(ThrottleAdapter):
    """Adjusts capacity based on API budget consumption.

    When weekly API usage is high and days-to-reset are far away,
    throttles down to preserve budget for critical work only.
    """

    def __init__(self, config: ThrottleConfig) -> None:
        self._config = config

    def evaluate(self, context: ThrottleContext, base: Any) -> ThrottleResult:
        pct = context.api_usage_pct
        days = context.api_days_to_reset

        # Emergency: >95% used regardless of days to reset
        if pct >= self._config.capacity_critical_pct:
            return ThrottleResult(
                multiplier=self._config.capacity_critical_multiplier,
                severity_override="critical",
                reason=f"capacity=critical ({pct:.0%} used) → {self._config.capacity_critical_multiplier}x, critical-only",
            )

        # Warning: >80% used with >=2 days until reset
        if pct >= self._config.capacity_warning_pct and days >= 2:
            return ThrottleResult(
                multiplier=self._config.capacity_warning_multiplier,
                severity_override="critical",
                reason=f"capacity=warning ({pct:.0%} used, {days:.1f}d to reset) → {self._config.capacity_warning_multiplier}x, critical-only",
            )

        return ThrottleResult(
            multiplier=1.0,
            reason=f"capacity=ok ({pct:.0%} used, {days:.1f}d to reset)",
        )


# ---------------------------------------------------------------------------
# Demand-based adapter
# ---------------------------------------------------------------------------

class DemandAdapter(ThrottleAdapter):
    """Adjusts capacity based on backlog pressure and release deadlines."""

    def __init__(self, config: ThrottleConfig) -> None:
        self._config = config

    def evaluate(self, context: ThrottleContext, base: Any) -> ThrottleResult:
        surge = self._config.backlog_surge_threshold
        crit_surge = self._config.critical_surge_threshold

        # Critical mass: large backlog AND many critical tickets
        if context.backlog_size >= surge and context.backlog_critical >= crit_surge:
            return ThrottleResult(
                multiplier=2.0,
                reason=f"demand=critical-mass (backlog={context.backlog_size}, critical={context.backlog_critical}) → 2.0x",
            )

        # High pressure: large backlog or pre-release
        if context.backlog_size >= surge or context.is_pre_release:
            reason_parts = []
            if context.backlog_size >= surge:
                reason_parts.append(f"backlog={context.backlog_size}")
            if context.is_pre_release:
                reason_parts.append("pre-release")
            return ThrottleResult(
                multiplier=1.5,
                reason=f"demand=surge ({', '.join(reason_parts)}) → 1.5x",
            )

        return ThrottleResult(
            multiplier=1.0,
            reason=f"demand=normal (backlog={context.backlog_size})",
        )


# ---------------------------------------------------------------------------
# Throttle policy — orchestrator
# ---------------------------------------------------------------------------

class ThrottlePolicy:
    """Combines multiple throttle adapters to produce resolved cycle limits.

    The policy evaluates each adapter, multiplies their multipliers together
    (clamped to [0.1, 4.0]), and applies the most restrictive severity
    override. All numeric limits are floored at 1 (never fully stop).
    """

    def __init__(self, base_config: Any, adapters: List[ThrottleAdapter]) -> None:
        self._base = base_config
        self._adapters = adapters

    def resolve(self, context: ThrottleContext) -> ResolvedCycleConfig:
        """Evaluate all adapters and compute effective cycle limits."""
        results: List[ThrottleResult] = []

        for adapter in self._adapters:
            try:
                result = adapter.evaluate(context, self._base)
                results.append(result)
            except Exception:
                logger.exception(
                    "Throttle adapter %s failed — using 1.0x",
                    type(adapter).__name__,
                )
                results.append(ThrottleResult(multiplier=1.0, reason=f"{type(adapter).__name__}: error fallback"))

        # Combine multipliers (product, clamped)
        combined = 1.0
        for r in results:
            combined *= r.multiplier
        combined = max(_MIN_MULTIPLIER, min(_MAX_MULTIPLIER, combined))

        # Severity: use the most restrictive override
        severity = self._base.severity_filter
        for r in results:
            if r.severity_override and _SEV_RANK.get(r.severity_override, 0) > _SEV_RANK.get(severity, 0):
                severity = r.severity_override

        reasons = [r.reason for r in results if r.reason]

        return ResolvedCycleConfig(
            max_new_tickets_per_cycle=max(1, int(self._base.max_new_tickets_per_cycle * combined)),
            max_investigations_per_cycle=max(1, int(self._base.max_investigations_per_cycle * combined)),
            max_developments_per_cycle=max(1, int(self._base.max_developments_per_cycle * combined)),
            max_open_investigating=max(1, int(self._base.max_open_investigating * combined)),
            severity_filter=severity,
            effective_multiplier=round(combined, 3),
            reasons=reasons,
        )


# ---------------------------------------------------------------------------
# Utility: days until weekly reset (Monday 00:00 UTC)
# ---------------------------------------------------------------------------

def days_until_weekly_reset(now_utc: Optional[datetime] = None) -> float:
    """Calculate days until next Monday 00:00 UTC (weekly API reset)."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    # Monday is weekday 0
    days_ahead = (7 - now_utc.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # If it's Monday, next reset is next Monday
    next_monday = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    next_monday += timedelta(days=days_ahead)
    delta = next_monday - now_utc
    return delta.total_seconds() / 86400
