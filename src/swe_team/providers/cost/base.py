"""Protocol and dataclasses for the CostTracker provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class BudgetStatus:
    """Result of a budget check for a team."""

    status: str           # "ok", "warning", "hard_stop"
    daily_spent: float    # cents spent today
    daily_limit: float    # daily budget in cents
    monthly_spent: float  # cents spent this month
    monthly_limit: float  # monthly budget in cents
    percent_used: float   # higher of daily/monthly usage as a percentage
    team_id: str = ""

    @property
    def is_over_budget(self) -> bool:
        return self.status == "hard_stop"

    @property
    def is_warning(self) -> bool:
        return self.status == "warning"


@runtime_checkable
class CostTrackerProvider(Protocol):
    """Interface for per-agent cost tracking with budget enforcement."""

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
        ...

    def get_daily_spend(self, team_id: str) -> float:
        """Return cents spent today for the given team."""
        ...

    def get_monthly_spend(self, team_id: str) -> float:
        """Return cents spent this month for the given team."""
        ...

    def check_budget(self, team_id: str) -> BudgetStatus:
        """Evaluate budget gates and return a BudgetStatus."""
        ...
