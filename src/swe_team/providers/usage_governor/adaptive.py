"""Adaptive usage governor — core concurrency and quota management."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .base import ConcurrencyDecision, QuotaStatus, UsageGovernorProvider
from .schedule import UsageScheduler
from .bonus_detector import BonusDetector
from .rules import GovernanceRule, RuleEngine

logger = logging.getLogger(__name__)

_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

_DEFAULT_TIERS = [
    {"remaining_pct": 70, "max_agents": 5, "priority_floor": "low", "allow_new_work": True},
    {"remaining_pct": 50, "max_agents": 3, "priority_floor": "medium", "allow_new_work": True},
    {"remaining_pct": 30, "max_agents": 2, "priority_floor": "high", "allow_new_work": True},
    {"remaining_pct": 10, "max_agents": 1, "priority_floor": "critical", "allow_new_work": False},
]


class AdaptiveUsageGovernor:
    """Adaptive governor that manages concurrency based on quota, schedule, and bonus windows."""

    def __init__(
        self,
        quota_limit: int = 2_400_000,
        tokens_per_5h_block: int = 500_000,
        max_agents: int = 5,
        tiers: list[dict] | None = None,
        scheduler: UsageScheduler | None = None,
        bonus_detector: BonusDetector | None = None,
        token_tracker: object | None = None,
        quota_warning_pct: float = 20,
        quota_critical_pct: float = 10,
        burn_rate_spike_multiplier: float = 2.0,
        alert_throttle_minutes: int = 60,
        operator_overrides: list[dict] | None = None,
        hard_limits: dict | None = None,
    ) -> None:
        self._quota_limit = quota_limit
        self._tokens_per_5h_block = tokens_per_5h_block
        self._max_agents = max_agents
        self._tiers = tiers if tiers else _DEFAULT_TIERS
        self._scheduler = scheduler
        self._bonus_detector = bonus_detector
        self._token_tracker = token_tracker
        if self._token_tracker is None:
            logger.warning(
                "UsageGovernor: no TokenTracker attached — defaulting to "
                "fail-closed (1 agent, no new work). Attach a tracker via "
                "set_token_tracker() to enable normal operation."
            )
        self._quota_warning_pct = quota_warning_pct
        self._quota_critical_pct = quota_critical_pct
        self._burn_rate_spike_multiplier = burn_rate_spike_multiplier
        self._alert_throttle_minutes = alert_throttle_minutes
        # Alert state: threshold_key -> last_alert_time (monotonic)
        self._alert_times: dict[str, float] = {}
        self._no_tracker_warned: bool = False  # suppress repeated fail-closed warnings

        # Build governance rules for the RuleEngine
        self._hard_limits = hard_limits or {}
        self._rule_engine = self._build_rule_engine(operator_overrides or [])

    def _build_rule_engine(self, operator_overrides: list[dict]) -> RuleEngine:
        """Construct a RuleEngine from operator overrides."""
        rules: list[GovernanceRule] = []

        # Operator overrides at precedence 2
        for ov in operator_overrides:
            rules.append(GovernanceRule(
                name=ov.get("name", "operator_override"),
                description=ov.get("description", ""),
                multiplier=ov.get("multiplier", 1.0),
                precedence=2,
                active=ov.get("active", True),
                source="config",
                schedule_days=ov.get("days"),
                schedule_start_hour=ov.get("start_hour"),
                schedule_end_hour=ov.get("end_hour"),
                schedule_timezone=ov.get("timezone"),
            ))

        return RuleEngine(rules, self._hard_limits)

    def _get_dynamic_rules(self) -> list[GovernanceRule]:
        """Build dynamic rules from schedule and bonus detector (evaluated each call)."""
        rules: list[GovernanceRule] = []

        if self._scheduler:
            mult = self._scheduler.get_concurrency_multiplier()
            if mult != 1.0:
                window = self._scheduler.get_current_window()
                rules.append(GovernanceRule(
                    name=f"schedule_{window.name}",
                    description=f"Schedule window: {window.name}",
                    multiplier=mult,
                    precedence=4,
                    active=True,
                    source="detected",
                ))

        if self._bonus_detector:
            mult = self._bonus_detector.get_multiplier()
            if mult != 1.0:
                rules.append(GovernanceRule(
                    name="bonus",
                    description="Detected bonus throughput window",
                    multiplier=mult,
                    precedence=3,
                    active=True,
                    source="detected",
                ))

        return rules

    def set_token_tracker(self, tracker: object) -> None:
        """Attach a TokenTracker instance for live usage data."""
        self._token_tracker = tracker

    def _get_usage_data(self) -> tuple[int, float]:
        """Return (total_tokens_used_today, burn_rate_tokens_per_hour)."""
        if self._token_tracker is None:
            return 0, 0.0
        try:
            hourly = self._token_tracker.by_hour(since_hours=24)
        except Exception:
            return 0, 0.0

        total = sum(
            e.get("input_tokens", 0) + e.get("output_tokens", 0) for e in hourly
        )
        if hourly:
            burn_rate = total / max(len(hourly), 1)
        else:
            burn_rate = 0.0
        return total, burn_rate

    def get_quota_status(self) -> QuotaStatus:
        """Return current quota usage status."""
        total_used, burn_rate = self._get_usage_data()
        remaining = max(0, self._quota_limit - total_used)
        remaining_pct = (remaining / self._quota_limit * 100) if self._quota_limit > 0 else 0.0

        if burn_rate > 0:
            hours_left = remaining / burn_rate
        else:
            hours_left = None

        return QuotaStatus(
            total_tokens_used=total_used,
            quota_limit=self._quota_limit,
            remaining_pct=round(remaining_pct, 2),
            burn_rate_tokens_per_hour=round(burn_rate, 2),
            estimated_hours_until_exhaustion=round(hours_left, 2) if hours_left is not None else None,
            current_period=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )

    def _get_tier(self, remaining_pct: float) -> dict:
        """Find the matching tier for the given remaining percentage."""
        # Tiers are checked from lowest remaining_pct to highest
        sorted_tiers = sorted(self._tiers, key=lambda t: t.get("remaining_pct", 0))
        matched = sorted_tiers[0] if sorted_tiers else _DEFAULT_TIERS[0]
        for tier in sorted_tiers:
            if remaining_pct >= tier.get("remaining_pct", 0):
                matched = tier
        return matched

    def get_concurrency_decision(self) -> ConcurrencyDecision:
        """Compute concurrency decision from tier + hierarchical rule engine."""
        # Fail-closed: no tracker means we cannot assess quota, so be conservative
        if self._token_tracker is None:
            if not self._no_tracker_warned:
                logger.warning(
                    "UsageGovernor: no TokenTracker — fail-closed: "
                    "max_agents=1, allow_new_work=False"
                )
                self._no_tracker_warned = True
            return ConcurrencyDecision(
                max_parallel_agents=1,
                reason="no TokenTracker attached (fail-closed)",
                priority_floor="critical",
                allow_new_work=False,
                applied_rules=[],
                audit_trail="fail-closed: no token tracker",
            )

        status = self.get_quota_status()
        tier = self._get_tier(status.remaining_pct)

        base_agents = tier.get("max_agents", self._max_agents)
        priority_floor = tier.get("priority_floor", "low")
        allow_new_work = tier.get("allow_new_work", True)

        # Combine static (operator overrides) + dynamic (schedule, bonus) rules
        dynamic_rules = self._get_dynamic_rules()
        all_rules = list(self._rule_engine._rules) + dynamic_rules
        engine = RuleEngine(all_rules, self._hard_limits)

        now = datetime.now(timezone.utc)
        result = engine.evaluate(base_agents, now)

        reasons = [f"tier({tier.get('remaining_pct', '?')}%): {base_agents} agents"]
        if result.applied_rules:
            reasons.append(result.audit_trail)

        return ConcurrencyDecision(
            max_parallel_agents=result.effective_agents,
            reason="; ".join(reasons),
            priority_floor=priority_floor,
            allow_new_work=allow_new_work,
            applied_rules=[r.name for r in result.applied_rules],
            audit_trail=result.audit_trail,
        )

    def get_max_concurrency(self) -> int:
        """Return the maximum number of parallel agents allowed right now."""
        return self.get_concurrency_decision().max_parallel_agents

    def should_launch_new_agent(self, priority: str) -> bool:
        """Check if a new agent with given priority should be launched."""
        decision = self.get_concurrency_decision()
        if not decision.allow_new_work:
            logger.info("Governor: blocking new work (allow_new_work=False)")
            return False
        pri_rank = _PRIORITY_RANK.get(priority.lower(), 0)
        floor_rank = _PRIORITY_RANK.get(decision.priority_floor.lower(), 0)
        if pri_rank < floor_rank:
            logger.info(
                "Governor: blocking priority=%s (floor=%s)",
                priority, decision.priority_floor,
            )
            return False
        return True

    def check_alerts(self) -> list[str]:
        """Check for alert conditions. Returns list of alert messages (throttled)."""
        alerts: list[str] = []
        status = self.get_quota_status()
        now = time.monotonic()

        def _should_fire(key: str) -> bool:
            last = self._alert_times.get(key, 0)
            if now - last >= self._alert_throttle_minutes * 60:
                self._alert_times[key] = now
                return True
            return False

        if status.remaining_pct <= self._quota_critical_pct:
            if _should_fire("quota_critical"):
                alerts.append(
                    f"CRITICAL: Quota at {status.remaining_pct:.1f}% remaining "
                    f"({status.total_tokens_used}/{status.quota_limit} tokens)"
                )
        elif status.remaining_pct <= self._quota_warning_pct:
            if _should_fire("quota_warning"):
                alerts.append(
                    f"WARNING: Quota at {status.remaining_pct:.1f}% remaining "
                    f"({status.total_tokens_used}/{status.quota_limit} tokens)"
                )

        # Burn rate spike detection
        _, burn_rate = self._get_usage_data()
        if self._token_tracker is not None and burn_rate > 0:
            try:
                hourly = self._token_tracker.by_hour(since_hours=24)
                if len(hourly) >= 2:
                    all_rates = [
                        e.get("input_tokens", 0) + e.get("output_tokens", 0)
                        for e in hourly
                    ]
                    avg_rate = sum(all_rates) / len(all_rates)
                    if avg_rate > 0 and burn_rate > avg_rate * self._burn_rate_spike_multiplier:
                        if _should_fire("burn_rate_spike"):
                            alerts.append(
                                f"WARNING: Burn rate spike {burn_rate:.0f} tok/h "
                                f"(avg: {avg_rate:.0f} tok/h, {burn_rate/avg_rate:.1f}x)"
                            )
            except Exception:
                pass

        return alerts

    def get_daily_summary(self) -> str:
        """Return formatted daily summary text."""
        status = self.get_quota_status()
        decision = self.get_concurrency_decision()
        lines = [
            "=== Usage Governor Daily Summary ===",
            f"Tokens used: {status.total_tokens_used:,} / {status.quota_limit:,}",
            f"Remaining: {status.remaining_pct:.1f}%",
            f"Burn rate: {status.burn_rate_tokens_per_hour:,.0f} tokens/hour",
        ]
        if status.estimated_hours_until_exhaustion is not None:
            lines.append(f"Estimated exhaustion: {status.estimated_hours_until_exhaustion:.1f} hours")
        else:
            lines.append("Estimated exhaustion: N/A (no burn rate)")
        lines.append(f"Max concurrency: {decision.max_parallel_agents} agents")
        lines.append(f"Priority floor: {decision.priority_floor}")
        lines.append(f"Allow new work: {decision.allow_new_work}")
        lines.append(f"Reason: {decision.reason}")

        if self._scheduler:
            window = self._scheduler.get_current_window()
            lines.append(f"Schedule window: {window.name} (x{window.concurrency_multiplier})")

        if self._bonus_detector and self._bonus_detector.is_bonus_active():
            lines.append(f"Bonus active: x{self._bonus_detector.get_multiplier()}")

        return "\n".join(lines)

    def health_check(self) -> bool:
        """Return True if the governor is operational."""
        try:
            self.get_quota_status()
            return True
        except Exception:
            return False
