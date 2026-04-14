"""
Budget API for SWE-Squad WebUI.

Provides budget policy management, incident tracking, quota monitoring,
and subscription status for the costs dashboard.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses for budget API responses
# ---------------------------------------------------------------------------

@dataclass
class BudgetPolicy:
    """Budget policy configuration for a team."""
    id: str
    team_id: str
    policy_type: str = "daily"  # daily, monthly, rolling
    daily_limit_cents: int = 5000  # $50 default
    monthly_limit_cents: int = 100000  # $1000 default
    rolling_window_days: int = 7
    rolling_limit_cents: int = 50000
    enabled: bool = True
    alert_thresholds: Dict[str, int] = None
    created_at: str = None
    updated_at: str = None

    def __post_init__(self):
        if self.alert_thresholds is None:
            self.alert_thresholds = {"warning_pct": 80, "critical_pct": 95}
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if self.updated_at is None:
            self.updated_at = self.created_at
        if self.id is None:
            self.id = f"{self.team_id}_policy"


@dataclass
class BudgetIncident:
    """Budget incident/alert record."""
    id: str
    team_id: str
    level: str  # ok, warning, critical, exceeded
    incident_type: str
    message: str
    triggered_at: str
    resolved_at: Optional[str] = None
    spend_cents: float = 0.0
    limit_cents: int = 0
    context: Dict[str, Any] = None

    def __post_init__(self):
        if self.context is None:
            self.context = {}


@dataclass
class ProviderQuota:
    """Provider (API vendor) quota status."""
    provider: str  # anthropic, openai, google, etc.
    model: str
    quota_type: str = "tokens"  # tokens, requests, cost
    total_tokens: int = 0
    used_tokens: int = 0
    remaining_pct: float = 100.0
    reset_at: str = ""
    tier: str = "unknown"
    is_hard_limit: bool = False
    current_period: Dict[str, str] = None
    burn_rate: Dict[str, Any] = None

    def __post_init__(self):
        if self.current_period is None:
            now = datetime.now(timezone.utc)
            next_month = now.replace(day=1, month=((now.month % 12) + 1)) if now.month < 12 else now.replace(year=now.year + 1, month=1)
            self.current_period = {"start": now.strftime("%Y-%m-%d"), "end": next_month.strftime("%Y-%m-%d")}
        if self.burn_rate is None:
            self.burn_rate = {"tokens_per_hour": 0, "estimated_hours_until_exhaustion": float("inf")}
        if not self.reset_at:
            next_month = datetime.now(timezone.utc).replace(day=1, month=((datetime.now(timezone.utc).month % 12) + 1))
            if datetime.now(timezone.utc).month == 12:
                next_month = next_month.replace(year=datetime.now(timezone.utc).year + 1, month=1)
            self.reset_at = next_month.isoformat()


@dataclass
class SpendWindow:
    """Rolling window spend data."""
    window_start: str
    window_end: str
    total_cents: float
    by_provider: Dict[str, float]
    by_agent: Dict[str, float]
    by_model: Dict[str, float]
    tickets_count: int = 0
    request_count: int = 0


@dataclass
class Subscription:
    """Subscription/billing information."""
    id: str = "default_subscription"
    provider: str = "anthropic"
    plan: str = "claude-pro"
    status: str = "active"  # active, inactive, past_due, cancelled
    billing_period: Dict[str, str] = None
    included_tokens: int = 0
    used_tokens: int = 0
    overage_tokens: int = 0
    next_invoice_amount_cents: float = 0.0
    currency: str = "USD"

    def __post_init__(self):
        if self.billing_period is None:
            now = datetime.now(timezone.utc)
            next_month = now.replace(day=1, month=((now.month % 12) + 1)) if now.month < 12 else now.replace(year=now.year + 1, month=1)
            self.billing_period = {"start": now.strftime("%Y-%m-%d"), "end": next_month.strftime("%Y-%m-%d")}


@dataclass
class AccountingModel:
    """Cost accounting model for chargeback/allocation."""
    id: str
    team_id: str
    name: str
    allocation_type: str  # proportional, fixed, tiered
    cost_centers: List[Dict[str, Any]]
    rules: List[Dict[str, Any]]
    enabled: bool = True
    created_at: str = None

    def __post_init__(self):
        if self.cost_centers is None:
            self.cost_centers = []
        if self.rules is None:
            self.rules = []
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# BudgetAPI Handler
# ---------------------------------------------------------------------------

class BudgetAPI:
    """Budget API handler for dashboard server.

    Provides methods to manage budget policies, track incidents,
    monitor provider quotas, and calculate subscription ROI.
    """

    def __init__(self, data_dir: Optional[Path] = None):
        """Initialize BudgetAPI.

        Args:
            data_dir: Directory to store budget data files.
        """
        self._data_dir = Path(data_dir or "data/swe_team")
        self._data_dir.mkdir(parents=True, exist_ok=True)

        # Data files
        self._policies_file = self._data_dir / "budget_policies.json"
        self._incidents_file = self._data_dir / "budget_incidents.json"

    # ── Budget Policies ────────────────────────────────────────────────────────

    def get_policies(self) -> List[BudgetPolicy]:
        """Get all budget policies."""
        if not self._policies_file.exists():
            # Return default policy
            return [BudgetPolicy(id="default", team_id="default")]

        try:
            with open(self._policies_file) as f:
                data = json.load(f)
                return [BudgetPolicy(**item) for item in data]
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            logger.warning("Failed to load budget policies: %s", exc)
            return [BudgetPolicy(id="default", team_id="default")]

    def get_policy(self, team_id: str) -> Optional[BudgetPolicy]:
        """Get budget policy for a specific team."""
        policies = self.get_policies()
        for policy in policies:
            if policy.team_id == team_id:
                return policy
        return None

    def set_policy(self, policy: BudgetPolicy) -> BudgetPolicy:
        """Set or update a budget policy."""
        policies = self.get_policies()
        updated = False
        for i, p in enumerate(policies):
            if p.team_id == policy.team_id or p.id == policy.id:
                policy.updated_at = datetime.now(timezone.utc).isoformat()
                policies[i] = policy
                updated = True
                break
        if not updated:
            policy.updated_at = datetime.now(timezone.utc).isoformat()
            policies.append(policy)

        # Save to file
        try:
            with open(self._policies_file, "w") as f:
                json.dump([asdict(p) for p in policies], f, indent=2, default=str)
        except OSError as exc:
            logger.error("Failed to save budget policy: %s", exc)
            raise

        return policy

    def delete_policy(self, policy_id: str) -> bool:
        """Delete a budget policy."""
        policies = self.get_policies()
        original_len = len(policies)
        policies = [p for p in policies if p.id != policy_id]

        if len(policies) == original_len:
            return False  # Policy not found

        try:
            with open(self._policies_file, "w") as f:
                json.dump([asdict(p) for p in policies], f, indent=2, default=str)
            return True
        except OSError as exc:
            logger.error("Failed to delete budget policy: %s", exc)
            return False

    # ── Budget Incidents ───────────────────────────────────────────────────────

    def get_incidents(self, team_id: Optional[str] = None, resolved: Optional[bool] = None) -> List[BudgetIncident]:
        """Get budget incidents.

        Args:
            team_id: Filter by team (optional).
            resolved: Filter by resolution status (optional).
        """
        if not self._incidents_file.exists():
            return []

        try:
            with open(self._incidents_file) as f:
                data = json.load(f)
                incidents = [BudgetIncident(**item) for item in data]
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            logger.warning("Failed to load budget incidents: %s", exc)
            return []

        # Apply filters
        if team_id:
            incidents = [i for i in incidents if i.team_id == team_id]
        if resolved is not None:
            if resolved:
                incidents = [i for i in incidents if i.resolved_at is not None]
            else:
                incidents = [i for i in incidents if i.resolved_at is None]

        # Sort by triggered_at descending
        incidents.sort(key=lambda x: x.triggered_at, reverse=True)
        return incidents

    def create_incident(self, incident: BudgetIncident) -> BudgetIncident:
        """Create a new budget incident."""
        incidents = self.get_incidents()
        incident.triggered_at = datetime.now(timezone.utc).isoformat()

        if not incident.id:
            incident.id = f"incident_{datetime.now(timezone.utc).timestamp()}"

        incidents.insert(0, incident)  # Add to front

        # Save to file
        try:
            with open(self._incidents_file, "w") as f:
                json.dump([asdict(i) for i in incidents], f, indent=2, default=str)
        except OSError as exc:
            logger.error("Failed to save budget incident: %s", exc)
            raise

        return incident

    def resolve_incident(self, incident_id: str) -> bool:
        """Mark an incident as resolved."""
        incidents = self.get_incidents()
        resolved = False

        for incident in incidents:
            if incident.id == incident_id and incident.resolved_at is None:
                incident.resolved_at = datetime.now(timezone.utc).isoformat()
                resolved = True
                break

        if not resolved:
            return False

        try:
            with open(self._incidents_file, "w") as f:
                json.dump([asdict(i) for i in incidents], f, indent=2, default=str)
            return True
        except OSError as exc:
            logger.error("Failed to resolve budget incident: %s", exc)
            return False

    def check_and_create_incident(self, team_id: str, status: str, spent_cents: float, limit_cents: int, period: str = "daily") -> Optional[BudgetIncident]:
        """Check budget status and create incident if needed.

        Returns the created incident if one was created, None otherwise.
        """
        if status == "ok":
            return None

        # Check for existing unresolved incident of same type
        existing = self.get_incidents(team_id=team_id, resolved=False)
        for inc in existing:
            if inc.incident_type == f"{period}_exceeded":
                return None  # Already have an active incident

        # Determine incident level and type
        if status == "hard_stop":
            level = "exceeded"
            incident_type = f"{period}_exceeded"
            message = f"Budget exceeded for {period}: ${spent_cents/100:.2f} of ${limit_cents/100:.2f}"
        else:  # warning
            level = "warning"
            incident_type = "warning_threshold"
            message = f"Budget warning for {period}: ${spent_cents/100:.2f} of ${limit_cents/100:.2f} ({spent_cents/limit_cents*100:.1f}%)"

        incident = BudgetIncident(
            id=f"{team_id}_{incident_type}_{datetime.now(timezone.utc).timestamp()}",
            team_id=team_id,
            level=level,
            incident_type=incident_type,
            message=message,
            triggered_at=datetime.now(timezone.utc).isoformat(),
            spend_cents=spent_cents,
            limit_cents=limit_cents,
            context={"period": period},
        )

        return self.create_incident(incident)

    # ── Provider Quotas ───────────────────────────────────────────────────────

    def get_provider_quotas(self) -> List[ProviderQuota]:
        """Get provider quota status for all providers.

        In a real implementation, this would query external APIs.
        Returns simulated data for demonstration.
        """
        # Simulated Anthropic quota
        anthropic_quota = ProviderQuota(
            provider="anthropic",
            model="claude-3-5-sonnet",
            quota_type="tokens",
            total_tokens=5_000_000,  # 5M tokens per month (example)
            used_tokens=1_234_567,
            remaining_pct=75.3,
            reset_at=self._next_month_start().isoformat(),
            tier="claude-pro",
            is_hard_limit=False,
            burn_rate={
                "tokens_per_hour": 28_000,  # Estimated
                "estimated_hours_until_exhaustion": 135,
            },
        )

        # Simulated OpenAI quota (if used)
        openai_quota = ProviderQuota(
            provider="openai",
            model="gpt-4o",
            quota_type="tokens",
            total_tokens=1_000_000,
            used_tokens=123_456,
            remaining_pct=87.7,
            reset_at=self._next_month_start().isoformat(),
            tier="gpt-4o-api",
            is_hard_limit=True,
            burn_rate={
                "tokens_per_hour": 5_000,
                "estimated_hours_until_exhaustion": 175,
            },
        )

        return [anthropic_quota, openai_quota]

    def _next_month_start(self) -> datetime:
        """Get the start of next month."""
        now = datetime.now(timezone.utc)
        if now.month == 12:
            return now.replace(year=now.year + 1, month=1, day=1)
        return now.replace(month=now.month + 1, day=1)

    # ── Rolling Window Spend ────────────────────────────────────────────────────

    def get_spend_window(self, days: int = 7, team_id: str = "default") -> SpendWindow:
        """Get rolling window spend data.

        Args:
            days: Number of days in the window.
            team_id: Team ID (for filtering).
        """
        now = datetime.now(timezone.utc)
        window_end = now
        window_start = now - timedelta(days=days)

        # This would query the cost tracker in a real implementation
        # For now, return simulated data
        total_cents = 125_000.0  # $1,250

        return SpendWindow(
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            total_cents=total_cents,
            by_provider={
                "anthropic": 100_000.0,
                "openai": 25_000.0,
            },
            by_agent={
                "investigator": 40_000.0,
                "developer": 60_000.0,
                "triage": 10_000.0,
                "reviewer": 15_000.0,
            },
            by_model={
                "claude-3-5-sonnet": 80_000.0,
                "claude-3-opus": 20_000.0,
                "gpt-4o": 25_000.0,
            },
            tickets_count=42,
            request_count=1_234,
        )

    # ── Subscriptions ───────────────────────────────────────────────────────────

    def get_subscriptions(self) -> List[Subscription]:
        """Get subscription/billing information."""
        # Simulated subscription data
        return [
            Subscription(
                id="anthropic-pro",
                provider="anthropic",
                plan="claude-pro",
                status="active",
                billing_period={
                    "start": "2026-04-01",
                    "end": "2026-05-01",
                },
                included_tokens=5_000_000,
                used_tokens=1_234_567,
                overage_tokens=0,
                next_invoice_amount_cents=20_000.0,  # $200
            ),
        ]

    # ── Accounting Models ────────────────────────────────────────────────────────

    def get_accounting_models(self, team_id: str = "default") -> List[AccountingModel]:
        """Get cost accounting models for chargeback/allocation."""
        return [
            AccountingModel(
                id="proportional",
                team_id=team_id,
                name="Proportional by Agent",
                allocation_type="proportional",
                cost_centers=[
                    {"id": "investigator", "name": "Investigation", "budget_cents": 50_000, "spend_cents": 40_000},
                    {"id": "developer", "name": "Development", "budget_cents": 100_000, "spend_cents": 60_000},
                    {"id": "triage", "name": "Triage", "budget_cents": 20_000, "spend_cents": 10_000},
                ],
                rules=[
                    {"id": "rule1", "condition": "agent == 'investigator'", "allocation_pct": 40},
                    {"id": "rule2", "condition": "agent == 'developer'", "allocation_pct": 60},
                ],
            ),
        ]


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

_budget_api_instance: Optional[BudgetAPI] = None


def get_budget_api(data_dir: Optional[Path] = None) -> BudgetAPI:
    """Get or create the singleton BudgetAPI instance."""
    global _budget_api_instance
    if _budget_api_instance is None:
        _budget_api_instance = BudgetAPI(data_dir=data_dir)
    return _budget_api_instance
