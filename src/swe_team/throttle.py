"""
Dynamic throttle system for SWE-Squad cycle limits.

Replaces hardcoded cycle config values with dynamically computed limits
based on time-of-day, API capacity, and backlog demand signals.

Config can be provided via ThrottleConfig dataclass; YAML integration is
optional and not included by default.

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

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Severity ranking used for override comparison
_SEV_RANK: Dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Multiplier bounds — prevent runaway scaling in either direction
_MIN_MULTIPLIER = 0.1
_MAX_MULTIPLIER = 4.0


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------

@dataclass
class ThrottleConfig:
    """Configuration for the dynamic throttle system."""

    enabled: bool = False
    weekly_budget_usd: float = 500.0
    backlog_surge_threshold: int = 200
    critical_surge_threshold: int = 20

    # Ordered time windows loaded from config. Each item supports:
    # {start_hour, end_hour, multiplier, timezone?}
    time_bands: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Capacity thresholds
    capacity_warning_pct: float = 0.8
    capacity_warning_days_remaining: float = 2.0
    capacity_warning_multiplier: float = 0.5
    capacity_critical_pct: float = 0.95
    capacity_critical_multiplier: float = 0.1

    # Demand thresholds
    backlog_surge_multiplier: float = 1.5
    critical_surge_multiplier: float = 2.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ThrottleConfig":
        def _normalize_pct(value: Any, default: float) -> float:
            if value is None:
                return default
            try:
                pct = float(value)
            except (TypeError, ValueError):
                return default
            # Accept both 0.8 and 80 forms in config.
            if pct > 1.0:
                pct /= 100.0
            return pct

        time_bands_raw = data.get("time_bands", {})
        time_bands: Dict[str, Dict[str, Any]] = {}
        for band_name, band_data in time_bands_raw.items():
            if isinstance(band_data, dict):
                time_bands[band_name] = {
                    "start_hour": int(band_data.get("start_hour", 0)),
                    "end_hour": int(band_data.get("end_hour", 24)),
                    "multiplier": float(band_data.get("multiplier", 1.0)),
                    "timezone": str(band_data.get("timezone", "UTC")),
                }
            else:
                # Legacy shorthand: scalar multiplier means all-day UTC window.
                time_bands[band_name] = {
                    "start_hour": 0,
                    "end_hour": 24,
                    "multiplier": float(band_data),
                    "timezone": "UTC",
                }

        cap = data.get("capacity_thresholds") or data.get("capacity", {})
        demand = data.get("demand", {})
        return cls(
            enabled=data.get("enabled", False),
            weekly_budget_usd=data.get("weekly_budget_usd", 500.0),
            backlog_surge_threshold=data.get(
                "backlog_surge_threshold",
                demand.get("high_backlog_threshold", 200),
            ),
            critical_surge_threshold=data.get(
                "critical_surge_threshold",
                demand.get("critical_backlog_threshold", 20),
            ),
            time_bands=time_bands,
            capacity_warning_pct=_normalize_pct(cap.get("warning_pct", cap.get("warn_usage_pct")), 0.8),
            capacity_warning_days_remaining=float(cap.get("warn_days_remaining", 2.0)),
            capacity_warning_multiplier=float(cap.get("warning_multiplier", cap.get("warn_multiplier", 0.5))),
            capacity_critical_pct=_normalize_pct(cap.get("critical_pct", cap.get("critical_usage_pct")), 0.95),
            capacity_critical_multiplier=float(cap.get("critical_multiplier", 0.1)),
            backlog_surge_multiplier=float(demand.get("high_backlog_multiplier", 1.5)),
            critical_surge_multiplier=float(demand.get("surge_multiplier", 2.0)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "weekly_budget_usd": self.weekly_budget_usd,
            "backlog_surge_threshold": self.backlog_surge_threshold,
            "critical_surge_threshold": self.critical_surge_threshold,
            "time_bands": self.time_bands,
            "capacity_thresholds": {
                "warning_pct": self.capacity_warning_pct,
                "warn_days_remaining": self.capacity_warning_days_remaining,
                "warning_multiplier": self.capacity_warning_multiplier,
                "critical_pct": self.capacity_critical_pct,
                "critical_multiplier": self.capacity_critical_multiplier,
            },
            "demand": {
                "high_backlog_threshold": self.backlog_surge_threshold,
                "high_backlog_multiplier": self.backlog_surge_multiplier,
                "critical_backlog_threshold": self.critical_surge_threshold,
                "surge_multiplier": self.critical_surge_multiplier,
            },
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

def _hour_in_window(hour: int, start: int, end: int) -> bool:
    """Return True when `hour` falls in [start, end), supporting overnight windows."""
    if start == 0 and end == 24:
        return True
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


class TimeBasedAdapter(ThrottleAdapter):
    """Adjust capacity using configured time windows.

    Rules are loaded from `throttle.time_bands` and evaluated in config order.
    Each window supports `start_hour`, `end_hour`, `multiplier`, and optional
    `timezone` (default UTC). No band names are hardcoded.
    """

    def __init__(self, config: ThrottleConfig) -> None:
        self._config = config

    def evaluate(self, context: ThrottleContext, base: Any) -> ThrottleResult:
        if not self._config.time_bands:
            return ThrottleResult(multiplier=1.0, reason="time=no windows configured → 1.0x")

        now_utc = context.now_utc
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        for band_name, band in self._config.time_bands.items():
            tz_name = str(band.get("timezone", "UTC"))
            start = int(band.get("start_hour", 0))
            end = int(band.get("end_hour", 24))
            multiplier = float(band.get("multiplier", 1.0))

            local_dt = now_utc.astimezone(ZoneInfo(tz_name))
            if _hour_in_window(local_dt.hour, start, end):
                return ThrottleResult(
                    multiplier=multiplier,
                    reason=(
                        f"time={band_name} ({local_dt.hour}:00 {tz_name}, "
                        f"window={start}-{end}) → {multiplier}x"
                    ),
                )

        return ThrottleResult(multiplier=1.0, reason="time=no active window → 1.0x")


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
        if pct >= self._config.capacity_warning_pct and days >= self._config.capacity_warning_days_remaining:
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
                multiplier=self._config.critical_surge_multiplier,
                reason=(
                    "demand=critical-mass "
                    f"(backlog={context.backlog_size}, critical={context.backlog_critical}) "
                    f"→ {self._config.critical_surge_multiplier}x"
                ),
            )

        # High pressure: large backlog or pre-release
        if context.backlog_size >= surge or context.is_pre_release:
            reason_parts = []
            if context.backlog_size >= surge:
                reason_parts.append(f"backlog={context.backlog_size}")
            if context.is_pre_release:
                reason_parts.append("pre-release")
            return ThrottleResult(
                multiplier=self._config.backlog_surge_multiplier,
                reason=f"demand=surge ({', '.join(reason_parts)}) → {self._config.backlog_surge_multiplier}x",
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

        # Severity: use the most restrictive override, but never raise above
        # the configured base severity_filter (the admin sets the floor).
        severity = self._base.severity_filter
        base_rank = _SEV_RANK.get(severity, 0)
        for r in results:
            if r.severity_override:
                override_rank = _SEV_RANK.get(r.severity_override, 0)
                # Only apply if more restrictive AND within base config ceiling
                if override_rank > _SEV_RANK.get(severity, 0):
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
