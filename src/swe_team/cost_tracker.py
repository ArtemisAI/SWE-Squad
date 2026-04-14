"""
Per-agent cost tracking with budget hard-stops.

Wraps TokenTracker to provide dollar-denominated cost accounting, budget
policies, and hard-stop enforcement.  Two backends are available:

- InMemoryCostTracker  — zero dependencies, suitable for tests and single-process
- SupabaseCostTracker  — persists to Supabase swe_cost_events / swe_budget_policies

Usage::

    tracker = InMemoryCostTracker()
    tracker.record_cost(
        team_id="agent-1",
        model="sonnet",
        input_tokens=1000,
        output_tokens=500,
        operation="investigate",
        ticket_id="ticket-42",
    )
    status = tracker.check_budget("agent-1")
    if status.is_over_budget:
        raise RuntimeError("Budget hard-stop: %s", status)
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.swe_team.providers.cost.base import BudgetStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model pricing table — per 1 million tokens in *cents*
# These match Anthropic's published API pricing as of early 2026.
# Update this dict when pricing changes; do not hardcode elsewhere.
# ---------------------------------------------------------------------------
PRICING: Dict[str, Dict[str, float]] = {
    # cents per 1M tokens
    "haiku": {"input": 80, "output": 400},
    "sonnet": {"input": 300, "output": 1500},
    "opus": {"input": 1500, "output": 7500},
    # Fallback for unknown model names
    "default": {"input": 300, "output": 1500},
}

# Default budget policy (can be overridden per-team via the store)
_DEFAULT_DAILY_CENTS = 5000     # $50 / day
_DEFAULT_MONTHLY_CENTS = 100000  # $1 000 / month
_DEFAULT_ALERT_PCT = 80         # warn at 80 %


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_model_key(model: str) -> str:
    """Normalise a model name to a PRICING key."""
    model_lower = (model or "").lower()
    if "opus" in model_lower:
        return "opus"
    if "sonnet" in model_lower:
        return "sonnet"
    if "haiku" in model_lower:
        return "haiku"
    return "default"


def compute_cost_cents(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return cost in cents for the given model and token counts.

    Uses the PRICING table.  Unknown model names fall back to "sonnet" pricing.

    Parameters
    ----------
    model:
        Model name string, e.g. "claude-3-5-sonnet-20241022" or "sonnet".
    input_tokens:
        Number of input/prompt tokens.
    output_tokens:
        Number of output/completion tokens.

    Returns
    -------
    Cost in cents (float, may be fractional).
    """
    key = _resolve_model_key(model)
    rates = PRICING.get(key, PRICING["default"])
    return (input_tokens / 1_000_000 * rates["input"]) + (
        output_tokens / 1_000_000 * rates["output"]
    )


# ---------------------------------------------------------------------------
# In-memory cost event (used by both backends internally)
# ---------------------------------------------------------------------------

@dataclass
class CostEvent:
    """A single recorded cost event."""

    team_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_cents: float
    operation: str  # 'investigate', 'develop', 'triage', 'embed'
    ticket_id: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Budget policy dataclass (mirrors swe_budget_policies table)
# ---------------------------------------------------------------------------

@dataclass
class BudgetPolicy:
    """Per-team budget policy."""

    team_id: str
    daily_budget_cents: int = _DEFAULT_DAILY_CENTS
    monthly_budget_cents: int = _DEFAULT_MONTHLY_CENTS
    alert_threshold_percent: int = _DEFAULT_ALERT_PCT
    hard_stop_enabled: bool = True


# ---------------------------------------------------------------------------
# InMemoryCostTracker
# ---------------------------------------------------------------------------

class InMemoryCostTracker:
    """Thread-safe in-memory cost tracker.

    Suitable for single-process use, testing, or as a local accumulator
    before flushing to a persistent backend.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: List[CostEvent] = []
        self._policies: Dict[str, BudgetPolicy] = {}

    # ── Policy management ────────────────────────────────────────────────

    def set_budget_policy(self, policy: BudgetPolicy) -> None:
        """Register or replace a budget policy for a team."""
        with self._lock:
            self._policies[policy.team_id] = policy

    def get_budget_policy(self, team_id: str) -> BudgetPolicy:
        """Return the budget policy for a team (creates default if absent)."""
        with self._lock:
            if team_id not in self._policies:
                self._policies[team_id] = BudgetPolicy(team_id=team_id)
            return self._policies[team_id]

    # ── Core recording ───────────────────────────────────────────────────

    def record_cost(
        self,
        team_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        operation: str,
        ticket_id: str = "",
    ) -> float:
        """Record a cost event. Returns cost in cents."""
        cost = compute_cost_cents(model, input_tokens, output_tokens)
        event = CostEvent(
            team_id=team_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cents=round(cost, 4),
            operation=operation,
            ticket_id=ticket_id,
        )
        with self._lock:
            self._events.append(event)
        logger.debug(
            "COST: team=%s model=%s op=%s in=%d out=%d cost=%.4f¢ (ticket=%s)",
            team_id, model, operation, input_tokens, output_tokens, cost,
            ticket_id or "—",
        )
        return cost

    # ── Aggregation ──────────────────────────────────────────────────────

    def get_daily_spend(self, team_id: str) -> float:
        """Return total cents spent today (UTC) for the given team."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            return sum(
                e.cost_cents
                for e in self._events
                if e.team_id == team_id and e.timestamp.startswith(today)
            )

    def get_monthly_spend(self, team_id: str) -> float:
        """Return total cents spent this month (UTC) for the given team."""
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        with self._lock:
            return sum(
                e.cost_cents
                for e in self._events
                if e.team_id == team_id and e.timestamp.startswith(month_prefix)
            )

    def get_spend_by_operation(self, team_id: str) -> Dict[str, float]:
        """Return spend in cents grouped by operation for this month."""
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        totals: Dict[str, float] = defaultdict(float)
        with self._lock:
            for e in self._events:
                if e.team_id == team_id and e.timestamp.startswith(month_prefix):
                    totals[e.operation] += e.cost_cents
        return dict(totals)

    def get_spend_by_ticket(self, team_id: str) -> Dict[str, float]:
        """Return spend in cents grouped by ticket_id for this month."""
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        totals: Dict[str, float] = defaultdict(float)
        with self._lock:
            for e in self._events:
                if e.team_id == team_id and e.timestamp.startswith(month_prefix):
                    key = e.ticket_id or "unknown"
                    totals[key] += e.cost_cents
        return dict(totals)

    # ── Budget check ─────────────────────────────────────────────────────

    def check_budget(self, team_id: str) -> BudgetStatus:
        """Evaluate budget gates and return a BudgetStatus.

        Returns
        -------
        BudgetStatus with ``status`` one of:
        - "ok"        — within normal usage
        - "warning"   — over alert threshold (default 80 %)
        - "hard_stop" — at or over 100 %
        """
        policy = self.get_budget_policy(team_id)
        daily_spent = self.get_daily_spend(team_id)
        monthly_spent = self.get_monthly_spend(team_id)

        daily_pct = (
            daily_spent / policy.daily_budget_cents * 100
            if policy.daily_budget_cents > 0
            else 0.0
        )
        monthly_pct = (
            monthly_spent / policy.monthly_budget_cents * 100
            if policy.monthly_budget_cents > 0
            else 0.0
        )
        percent_used = max(daily_pct, monthly_pct)

        if policy.hard_stop_enabled and percent_used >= 100.0:
            status = "hard_stop"
        elif percent_used >= policy.alert_threshold_percent:
            status = "warning"
        else:
            status = "ok"

        return BudgetStatus(
            status=status,
            daily_spent=daily_spent,
            daily_limit=float(policy.daily_budget_cents),
            monthly_spent=monthly_spent,
            monthly_limit=float(policy.monthly_budget_cents),
            percent_used=round(percent_used, 2),
            team_id=team_id,
        )

    # ── Dashboard / summary ──────────────────────────────────────────────

    def get_team_summary(self, team_id: str) -> Dict[str, Any]:
        """Return a human-readable summary dict for the dashboard."""
        status = self.check_budget(team_id)
        by_op = self.get_spend_by_operation(team_id)
        return {
            "team_id": team_id,
            "daily_spent_cents": round(status.daily_spent, 4),
            "daily_limit_cents": status.daily_limit,
            "monthly_spent_cents": round(status.monthly_spent, 4),
            "monthly_limit_cents": status.monthly_limit,
            "percent_used": status.percent_used,
            "budget_status": status.status,
            "daily_spent_usd": round(status.daily_spent / 100, 4),
            "monthly_spent_usd": round(status.monthly_spent / 100, 4),
            "by_operation": {k: round(v / 100, 4) for k, v in by_op.items()},
        }


# ---------------------------------------------------------------------------
# SupabaseCostTracker
# ---------------------------------------------------------------------------

class SupabaseCostTracker:
    """Supabase-backed cost tracker.

    Persists events to the ``swe_cost_events`` table and reads budget policies
    from ``swe_budget_policies``.  Falls back gracefully if the tables do not
    exist yet (e.g. migration not applied).

    Parameters
    ----------
    client:
        A Supabase ``Client`` instance (from ``supabase`` Python package).
    fallback:
        Optional InMemoryCostTracker used when Supabase is unavailable.
    """

    def __init__(self, client: Any, fallback: Optional[InMemoryCostTracker] = None) -> None:
        self._client = client
        self._fallback = fallback or InMemoryCostTracker()

    # ── Internal helpers ─────────────────────────────────────────────────

    def _insert_event(self, event: CostEvent) -> None:
        """Insert a cost event row into Supabase."""
        row = {
            "team_id": event.team_id,
            "ticket_id": event.ticket_id or None,
            "model": event.model,
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "cost_cents": float(event.cost_cents),
            "operation": event.operation,
            "timestamp": event.timestamp,
        }
        self._client.table("swe_cost_events").insert(row).execute()

    def _get_policy(self, team_id: str) -> BudgetPolicy:
        """Fetch budget policy from Supabase; return defaults if not found."""
        try:
            resp = (
                self._client.table("swe_budget_policies")
                .select("*")
                .eq("team_id", team_id)
                .limit(1)
                .execute()
            )
            if resp.data:
                row = resp.data[0]
                return BudgetPolicy(
                    team_id=team_id,
                    daily_budget_cents=row.get("daily_budget_cents", _DEFAULT_DAILY_CENTS),
                    monthly_budget_cents=row.get("monthly_budget_cents", _DEFAULT_MONTHLY_CENTS),
                    alert_threshold_percent=row.get("alert_threshold_percent", _DEFAULT_ALERT_PCT),
                    hard_stop_enabled=row.get("hard_stop_enabled", True),
                )
        except Exception as exc:
            logger.warning("SupabaseCostTracker: cannot fetch policy for %s: %s", team_id, exc)
        return BudgetPolicy(team_id=team_id)

    # ── Core recording ───────────────────────────────────────────────────

    def record_cost(
        self,
        team_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        operation: str,
        ticket_id: str = "",
    ) -> float:
        """Record a cost event in Supabase. Returns cost in cents."""
        cost = compute_cost_cents(model, input_tokens, output_tokens)
        event = CostEvent(
            team_id=team_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cents=round(cost, 4),
            operation=operation,
            ticket_id=ticket_id,
        )
        # Also record in fallback for in-process aggregation
        self._fallback.record_cost(
            team_id=team_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            operation=operation,
            ticket_id=ticket_id,
        )
        try:
            self._insert_event(event)
        except Exception as exc:
            logger.warning(
                "SupabaseCostTracker: failed to persist cost event: %s — using fallback", exc
            )
        logger.info(
            "COST: team=%s model=%s op=%s in=%d out=%d cost=%.4f¢ (ticket=%s)",
            team_id, model, operation, input_tokens, output_tokens, cost,
            ticket_id or "—",
        )
        return cost

    # ── Aggregation ──────────────────────────────────────────────────────

    def get_daily_spend(self, team_id: str) -> float:
        """Return total cents spent today for the given team (via Supabase RPC)."""
        try:
            resp = self._client.rpc(
                "get_daily_spend_cents", {"p_team_id": team_id}
            ).execute()
            if resp.data is not None:
                return float(resp.data)
        except Exception as exc:
            logger.warning("SupabaseCostTracker.get_daily_spend failed: %s", exc)
        return self._fallback.get_daily_spend(team_id)

    def get_monthly_spend(self, team_id: str) -> float:
        """Return total cents spent this month for the given team (via Supabase RPC)."""
        try:
            resp = self._client.rpc(
                "get_monthly_spend_cents", {"p_team_id": team_id}
            ).execute()
            if resp.data is not None:
                return float(resp.data)
        except Exception as exc:
            logger.warning("SupabaseCostTracker.get_monthly_spend failed: %s", exc)
        return self._fallback.get_monthly_spend(team_id)

    # ── Budget check ─────────────────────────────────────────────────────

    def check_budget(self, team_id: str) -> BudgetStatus:
        """Evaluate budget gates using live Supabase data."""
        policy = self._get_policy(team_id)
        daily_spent = self.get_daily_spend(team_id)
        monthly_spent = self.get_monthly_spend(team_id)

        daily_pct = (
            daily_spent / policy.daily_budget_cents * 100
            if policy.daily_budget_cents > 0
            else 0.0
        )
        monthly_pct = (
            monthly_spent / policy.monthly_budget_cents * 100
            if policy.monthly_budget_cents > 0
            else 0.0
        )
        percent_used = max(daily_pct, monthly_pct)

        if policy.hard_stop_enabled and percent_used >= 100.0:
            status = "hard_stop"
        elif percent_used >= policy.alert_threshold_percent:
            status = "warning"
        else:
            status = "ok"

        return BudgetStatus(
            status=status,
            daily_spent=daily_spent,
            daily_limit=float(policy.daily_budget_cents),
            monthly_spent=monthly_spent,
            monthly_limit=float(policy.monthly_budget_cents),
            percent_used=round(percent_used, 2),
            team_id=team_id,
        )

    def get_team_summary(self, team_id: str) -> Dict[str, Any]:
        """Return a human-readable summary dict for the dashboard."""
        status = self.check_budget(team_id)
        return {
            "team_id": team_id,
            "daily_spent_cents": round(status.daily_spent, 4),
            "daily_limit_cents": status.daily_limit,
            "monthly_spent_cents": round(status.monthly_spent, 4),
            "monthly_limit_cents": status.monthly_limit,
            "percent_used": status.percent_used,
            "budget_status": status.status,
            "daily_spent_usd": round(status.daily_spent / 100, 4),
            "monthly_spent_usd": round(status.monthly_spent / 100, 4),
        }


# ---------------------------------------------------------------------------
# CostTracker — default factory (returns InMemory unless Supabase configured)
# ---------------------------------------------------------------------------

def make_cost_tracker(
    supabase_client: Optional[Any] = None,
) -> "InMemoryCostTracker | SupabaseCostTracker":
    """Return the appropriate CostTracker backend.

    If a Supabase client is provided, returns a SupabaseCostTracker with an
    InMemoryCostTracker fallback.  Otherwise returns InMemoryCostTracker.
    """
    if supabase_client is not None:
        return SupabaseCostTracker(client=supabase_client)
    return InMemoryCostTracker()
