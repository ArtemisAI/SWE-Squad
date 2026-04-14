"""
Unified Guardrails Coordinator.

Single entry point for all safety gates: circuit breaker, stability gate,
usage governor, throttle, and RBAC. Eliminates fragmented gate evaluation
by centralizing the decision into one call.

Usage::

    guardrails = GuardrailsCoordinator(config)
    guardrails.set_circuit_breaker(circuit_breaker)
    guardrails.set_stability_gate(ralph_gate)
    guardrails.set_usage_governor(governor)
    guardrails.set_throttle(throttle_policy)

    decision = guardrails.can_proceed(
        task_type="investigate",
        ticket_severity="CRITICAL",
    )
    if not decision.allowed:
        logger.warning("Blocked: %s", decision.reason)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.swe_team.cost_tracker import BudgetPolicy

logger = logging.getLogger(__name__)


@dataclass
class GuardrailDecision:
    """Result of a unified guardrail evaluation."""

    allowed: bool
    reason: str
    gate: str  # which gate blocked (or "all_clear")
    details: Dict[str, Any] = field(default_factory=dict)
    evaluated_gates: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def blocked(self) -> bool:
        return not self.allowed


@dataclass
class GuardrailHealth:
    """Health snapshot of all guardrail components."""

    circuit_breaker_paused: bool = False
    circuit_breaker_failure_rate: float = 0.0
    stability_verdict: str = "unknown"
    governor_allow_new_work: bool = True
    governor_max_agents: int = 5
    throttle_multiplier: float = 1.0
    queue_depth: int = 0
    dead_letter_count: int = 0
    budget_status: str = "ok"         # "ok", "warning", "hard_stop", or "unconfigured"
    budget_percent_used: float = 0.0


class GuardrailsCoordinator:
    """Unified coordinator for all safety gates.

    Evaluates gates in strict priority order:
    1. Circuit breaker (hard block if paused — system is unhealthy)
    2. Budget gate (hard-stop if dollar budget exceeded — #347)
    3. Usage governor (quota/concurrency limits)
    4. Stability gate (bug count thresholds)
    5. Throttle (time/capacity/demand adjustments)

    Each gate is optional — if not set, it's skipped. This allows
    incremental adoption: start with just circuit breaker, add gates
    as they become available.
    """

    def __init__(self, config: Optional[Any] = None) -> None:
        self._circuit_breaker: Any = None
        self._team_circuit_breakers: Dict[str, Any] = {}
        self._stability_gate: Any = None
        self._usage_governor: Any = None
        self._throttle_policy: Any = None
        self._queued_dispatcher: Any = None
        self._cost_tracker: Any = None   # CostTrackerProvider
        self._team_id: str = ""          # team scope for budget checks
        self._team_overrides: Dict[str, Dict[str, Any]] = {}
        self._global_overrides: Dict[str, Any] = {}
        self._load_overrides_from_config(config)

    def _load_overrides_from_config(self, config: Optional[Any]) -> None:
        if config is None:
            return
        if isinstance(config, dict):
            cfg = dict(config)
        else:
            cfg = {}
            teams_val = getattr(config, "teams", None)
            if isinstance(teams_val, dict):
                cfg["teams"] = teams_val
            for key in ("budget_daily", "max_concurrent", "circuit_breaker_threshold"):
                val = getattr(config, key, None)
                if val is not None:
                    cfg[key] = val
        teams = cfg.get("teams", {})
        if isinstance(teams, dict):
            self._team_overrides = {
                team_id: team_cfg
                for team_id, team_cfg in teams.items()
                if isinstance(team_cfg, dict)
            }
        for key in ("budget_daily", "max_concurrent", "circuit_breaker_threshold"):
            if key in cfg:
                self._global_overrides[key] = cfg[key]

    def _get_effective_overrides(self, team_id: str) -> Dict[str, Any]:
        team_cfg = self._team_overrides.get(team_id, {})
        return {
            "budget_daily": team_cfg.get(
                "budget_daily",
                self._global_overrides.get("budget_daily"),
            ),
            "max_concurrent": team_cfg.get(
                "max_concurrent",
                self._global_overrides.get("max_concurrent"),
            ),
            "circuit_breaker_threshold": team_cfg.get(
                "circuit_breaker_threshold",
                self._global_overrides.get("circuit_breaker_threshold"),
            ),
        }

    def set_circuit_breaker(self, cb: Any, team_id: str = "") -> None:
        if team_id:
            self._team_circuit_breakers[team_id] = cb
            return
        self._circuit_breaker = cb

    def set_stability_gate(self, gate: Any) -> None:
        self._stability_gate = gate

    def set_usage_governor(self, gov: Any) -> None:
        self._usage_governor = gov

    def set_throttle(self, policy: Any) -> None:
        self._throttle_policy = policy

    def set_queued_dispatcher(self, dispatcher: Any) -> None:
        self._queued_dispatcher = dispatcher

    def set_cost_tracker(self, tracker: Any, team_id: str = "") -> None:
        """Attach a CostTrackerProvider for budget-gate enforcement."""
        self._cost_tracker = tracker
        self._team_id = team_id

    def _apply_team_budget_override(self, team_id: str, overrides: Dict[str, Any]) -> None:
        if (
            self._cost_tracker is None
            or not team_id
            or "budget_daily" not in overrides
            or overrides["budget_daily"] is None
            or not hasattr(self._cost_tracker, "set_budget_policy")
        ):
            return
        policy = BudgetPolicy(team_id=team_id)
        if hasattr(self._cost_tracker, "get_budget_policy"):
            try:
                policy = self._cost_tracker.get_budget_policy(team_id)
            except Exception:
                policy = BudgetPolicy(team_id=team_id)
        try:
            policy.daily_budget_cents = int(float(overrides["budget_daily"]) * 100)
            self._cost_tracker.set_budget_policy(policy)
        except Exception as exc:
            logger.warning("Failed applying team budget override for %s: %s", team_id, exc)

    @staticmethod
    def _get_max_agents(decision: Any) -> int:
        max_agents = getattr(decision, "max_agents", None)
        if max_agents is None:
            max_agents = getattr(decision, "max_parallel_agents", 0)
        return int(max_agents)

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def evaluate(
        self,
        task_type: str = "investigate",
        ticket_severity: str = "MEDIUM",
        current_agents: int = 0,
        team_id: Optional[str] = None,
    ) -> GuardrailDecision:
        active_team_id = team_id or self._team_id
        overrides = self._get_effective_overrides(active_team_id)
        self._apply_team_budget_override(active_team_id, overrides)
        circuit_breaker = (
            self._team_circuit_breakers.get(active_team_id) if active_team_id else None
        ) or self._circuit_breaker

        evaluated = []

        # ── Gate 1: Circuit Breaker ────────────────────────────────
        if circuit_breaker is not None:
            evaluated.append("circuit_breaker")
            if circuit_breaker.is_paused:
                return GuardrailDecision(
                    allowed=False,
                    reason=f"Circuit breaker paused (failure rate {circuit_breaker.failure_rate:.0%})",
                    gate="circuit_breaker",
                    details={
                        "failure_rate": circuit_breaker.failure_rate,
                        "paused_until": getattr(circuit_breaker, "_paused_until", None),
                    },
                    evaluated_gates=evaluated,
                )
            threshold = overrides.get("circuit_breaker_threshold")
            threshold_value = self._safe_float(threshold)
            if threshold_value is not None and circuit_breaker.failure_rate >= threshold_value:
                return GuardrailDecision(
                    allowed=False,
                    reason=(
                        "Circuit breaker threshold exceeded "
                        f"({circuit_breaker.failure_rate:.0%} >= {threshold_value:.0%})"
                    ),
                    gate="circuit_breaker",
                    details={
                        "failure_rate": circuit_breaker.failure_rate,
                        "threshold": threshold_value,
                    },
                    evaluated_gates=evaluated,
                )

        # ── Gate 2: Budget Gate ────────────────────────────────────
        if self._cost_tracker is not None and active_team_id:
            evaluated.append("budget_gate")
            try:
                budget_status = self._cost_tracker.check_budget(active_team_id)
                if budget_status.is_over_budget:
                    return GuardrailDecision(
                        allowed=False,
                        reason=(
                            f"Budget hard-stop: {budget_status.percent_used:.1f}% of budget used "
                            f"(daily ${budget_status.daily_spent / 100:.2f}/"
                            f"${budget_status.daily_limit / 100:.2f}, "
                            f"monthly ${budget_status.monthly_spent / 100:.2f}/"
                            f"${budget_status.monthly_limit / 100:.2f})"
                        ),
                        gate="budget_gate",
                        details={
                            "status": budget_status.status,
                            "percent_used": budget_status.percent_used,
                            "daily_spent_cents": budget_status.daily_spent,
                            "daily_limit_cents": budget_status.daily_limit,
                            "monthly_spent_cents": budget_status.monthly_spent,
                            "monthly_limit_cents": budget_status.monthly_limit,
                        },
                        evaluated_gates=evaluated,
                    )
                if budget_status.is_warning:
                    logger.warning(
                        "Budget warning: team=%s %.1f%% of budget used "
                        "(daily $%.2f/$%.2f)",
                        active_team_id,
                        budget_status.percent_used,
                        budget_status.daily_spent / 100,
                        budget_status.daily_limit / 100,
                    )
            except Exception as exc:
                logger.warning("Budget gate check failed: %s — failing open", exc)

        # ── Gate 3: Usage Governor ─────────────────────────────────
        if self._usage_governor is not None:
            evaluated.append("usage_governor")
            try:
                decision = self._usage_governor.get_concurrency_decision()
                if not decision.allow_new_work:
                    return GuardrailDecision(
                        allowed=False,
                        reason=f"Usage governor: new work blocked ({decision.audit_trail})",
                        gate="usage_governor",
                        details={
                            "max_agents": self._get_max_agents(decision),
                            "priority_floor": decision.priority_floor,
                            "audit_trail": decision.audit_trail,
                        },
                        evaluated_gates=evaluated,
                    )
                # Check if severity meets priority floor
                severity_priority = {
                    "CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4,
                }
                sev_num = severity_priority.get(ticket_severity, 2)
                if sev_num > decision.priority_floor:
                    return GuardrailDecision(
                        allowed=False,
                        reason=f"Usage governor: ticket severity {ticket_severity} below priority floor {decision.priority_floor}",
                        gate="usage_governor",
                        details={"priority_floor": decision.priority_floor},
                        evaluated_gates=evaluated,
                    )
                # Check agent count
                max_agents = self._get_max_agents(decision)
                max_concurrent_override = overrides.get("max_concurrent")
                max_concurrent = self._safe_int(max_concurrent_override)
                if max_concurrent is not None:
                    max_agents = min(max_agents, max_concurrent)
                if current_agents >= max_agents:
                    return GuardrailDecision(
                        allowed=False,
                        reason=f"Usage governor: {current_agents} agents running (max {max_agents})",
                        gate="usage_governor",
                        details={"current": current_agents, "max": max_agents},
                        evaluated_gates=evaluated,
                    )
            except Exception as exc:
                logger.warning("Usage governor check failed: %s — failing open for now", exc)

        # ── Gate 4: Stability Gate ─────────────────────────────────
        if self._stability_gate is not None and task_type in ("deploy", "creative"):
            evaluated.append("stability_gate")
            try:
                report = self._stability_gate.evaluate()
                if report.verdict == "BLOCK":
                    return GuardrailDecision(
                        allowed=False,
                        reason=f"Stability gate BLOCK: {report.reason}",
                        gate="stability_gate",
                        details={
                            "verdict": report.verdict,
                            "reason": report.reason,
                            "open_critical": getattr(report, "open_critical", 0),
                            "open_high": getattr(report, "open_high", 0),
                        },
                        evaluated_gates=evaluated,
                    )
            except Exception as exc:
                logger.warning("Stability gate check failed: %s", exc)

        # ── Gate 5: Throttle ───────────────────────────────────────
        if self._throttle_policy is not None:
            evaluated.append("throttle")
            # Throttle adjusts limits but doesn't hard-block; it's informational
            # The actual enforcement happens via adjusted cycle config

        evaluated.append("all_clear")
        return GuardrailDecision(
            allowed=True,
            reason="All guardrails passed",
            gate="all_clear",
            evaluated_gates=evaluated,
        )

    def can_proceed(
        self,
        task_type: str = "investigate",
        ticket_severity: str = "MEDIUM",
        current_agents: int = 0,
        team_id: Optional[str] = None,
    ) -> GuardrailDecision:
        """Backward-compatible wrapper around evaluate()."""
        return self.evaluate(
            task_type=task_type,
            ticket_severity=ticket_severity,
            current_agents=current_agents,
            team_id=team_id,
        )

    def health(self, team_id: Optional[str] = None) -> GuardrailHealth:
        """Return a health snapshot of all guardrail components."""
        h = GuardrailHealth()
        active_team_id = team_id or self._team_id
        circuit_breaker = (
            self._team_circuit_breakers.get(active_team_id) if active_team_id else None
        ) or self._circuit_breaker

        if circuit_breaker is not None:
            h.circuit_breaker_paused = circuit_breaker.is_paused
            h.circuit_breaker_failure_rate = circuit_breaker.failure_rate

        if self._usage_governor is not None:
            try:
                decision = self._usage_governor.get_concurrency_decision()
                h.governor_allow_new_work = decision.allow_new_work
                h.governor_max_agents = self._get_max_agents(decision)
                overrides = self._get_effective_overrides(active_team_id)
                max_concurrent_override = overrides.get("max_concurrent")
                max_concurrent = self._safe_int(max_concurrent_override)
                if max_concurrent is not None:
                    h.governor_max_agents = min(
                        h.governor_max_agents,
                        max_concurrent,
                    )
            except Exception:
                pass

        if self._stability_gate is not None:
            try:
                report = self._stability_gate.evaluate()
                h.stability_verdict = report.verdict
            except Exception:
                pass

        if self._queued_dispatcher is not None:
            try:
                qh = self._queued_dispatcher.health()
                h.queue_depth = qh.get("investigate_depth", 0) + qh.get("develop_depth", 0)
                h.dead_letter_count = qh.get("dead_letter_count", 0)
            except Exception:
                pass

        if self._cost_tracker is not None and active_team_id:
            self._apply_team_budget_override(active_team_id, self._get_effective_overrides(active_team_id))
            try:
                budget = self._cost_tracker.check_budget(active_team_id)
                h.budget_status = budget.status
                h.budget_percent_used = budget.percent_used
            except Exception:
                h.budget_status = "error"
        else:
            h.budget_status = "unconfigured"

        return h
