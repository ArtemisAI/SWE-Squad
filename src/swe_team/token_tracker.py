"""
Token accounting and cost tracking for SWE-Squad.

Tracks token usage per Claude CLI invocation, calculates costs,
and provides per-ticket cost breakdowns. Integrates with the
scheduler's quota_checker for budget-aware throttling.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Default model pricing (USD per 1K tokens) — user-configurable via config
DEFAULT_PRICING = {
    "haiku": {"input": 0.00025, "output": 0.00125},
    "sonnet": {"input": 0.003, "output": 0.015},
    "opus": {"input": 0.015, "output": 0.075},
    # Fallback for unknown models
    "default": {"input": 0.003, "output": 0.015},
}


@dataclass
class TokenUsage:
    """A single token usage record."""
    session_id: str = ""
    ticket_id: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    task: str = ""  # investigate, develop, review, triage
    agent: str = "claude-code"
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TokenUsage":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def calculate_cost(model: str, input_tokens: int, output_tokens: int, pricing: Optional[Dict] = None) -> float:
    """Calculate cost in USD for a given token usage."""
    pricing = pricing or DEFAULT_PRICING
    model_key = model.lower().split("-")[0] if model else "default"
    rates = pricing.get(model_key, pricing.get("default", {"input": 0.003, "output": 0.015}))
    return (input_tokens / 1000 * rates["input"]) + (output_tokens / 1000 * rates["output"])


class TokenTracker:
    """Tracks token usage across sessions and tickets."""

    def __init__(self, store_path: Optional[Path] = None, pricing: Optional[Dict] = None):
        self._path = store_path or Path("data/swe_team/token_usage.jsonl")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._pricing = pricing or DEFAULT_PRICING
        self._lock = threading.Lock()
        self._session_totals: Dict[str, Dict[str, float]] = {}

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        task: str = "",
        ticket_id: str = "",
        session_id: str = "",
        agent: str = "claude-code",
        metadata: Optional[Dict] = None,
    ) -> TokenUsage:
        """Record a token usage event. Returns the usage record with calculated cost."""
        cost = calculate_cost(model, input_tokens, output_tokens, self._pricing)
        usage = TokenUsage(
            session_id=session_id,
            ticket_id=ticket_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(cost, 6),
            task=task,
            agent=agent,
            metadata=metadata or {},
        )

        # Append to JSONL file
        with self._lock:
            with open(self._path, "a") as f:
                f.write(json.dumps(usage.to_dict(), default=str) + "\n")

        # Update session totals
        key = ticket_id or session_id or "unknown"
        if key not in self._session_totals:
            self._session_totals[key] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        self._session_totals[key]["input_tokens"] += input_tokens
        self._session_totals[key]["output_tokens"] += output_tokens
        self._session_totals[key]["cost_usd"] += cost

        logger.info(
            "TOKEN: %s %s in=%d out=%d cost=$%.4f (ticket=%s)",
            model, task, input_tokens, output_tokens, cost, ticket_id or "—",
        )
        return usage

    def get_ticket_cost(self, ticket_id: str) -> Dict[str, Any]:
        """Get total cost breakdown for a specific ticket."""
        records = self._load_records(ticket_id=ticket_id)
        if not records:
            return {"total_usd": 0, "total_input_tokens": 0, "total_output_tokens": 0, "stages": {}}

        stages: Dict[str, Dict] = {}
        total_input = 0
        total_output = 0
        total_cost = 0.0

        for r in records:
            total_input += r.input_tokens
            total_output += r.output_tokens
            total_cost += r.cost_usd
            stage = r.task or "unknown"
            if stage not in stages:
                stages[stage] = {"tokens": 0, "cost": 0.0, "model": r.model}
            stages[stage]["tokens"] += r.input_tokens + r.output_tokens
            stages[stage]["cost"] += r.cost_usd

        return {
            "total_usd": round(total_cost, 4),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "stages": stages,
        }

    def get_daily_spend(self, date: Optional[datetime] = None) -> float:
        """Get total spend for a given day (defaults to today)."""
        date = date or datetime.now(timezone.utc)
        day_str = date.strftime("%Y-%m-%d")
        records = self._load_records()
        return sum(r.cost_usd for r in records if r.timestamp.startswith(day_str))

    def get_hourly_spend(self) -> float:
        """Get spend in the last hour."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        records = self._load_records()
        return sum(r.cost_usd for r in records if r.timestamp >= cutoff)

    def check_budget(
        self,
        daily_cap: float = 0,
        hourly_cap: float = 0,
        per_ticket_cap: float = 0,
        ticket_id: str = "",
    ) -> tuple[bool, float]:
        """Check if we have budget remaining. Returns (has_budget, remaining_usd)."""
        if daily_cap > 0:
            daily = self.get_daily_spend()
            if daily >= daily_cap:
                return False, 0.0
            remaining = daily_cap - daily
        elif hourly_cap > 0:
            hourly = self.get_hourly_spend()
            if hourly >= hourly_cap:
                return False, 0.0
            remaining = hourly_cap - hourly
        else:
            remaining = float("inf")

        if per_ticket_cap > 0 and ticket_id:
            ticket_cost = self.get_ticket_cost(ticket_id)
            if ticket_cost["total_usd"] >= per_ticket_cap:
                return False, 0.0

        return True, remaining

    def _load_records(self, ticket_id: Optional[str] = None) -> List[TokenUsage]:
        """Load records from JSONL file."""
        if not self._path.exists():
            return []
        records = []
        try:
            with open(self._path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = TokenUsage.from_dict(json.loads(line))
                    if ticket_id and r.ticket_id != ticket_id:
                        continue
                    records.append(r)
        except (json.JSONDecodeError, OSError):
            logger.warning("Error reading token usage file")
        return records

    def summary(self) -> Dict[str, Any]:
        """Get a summary of all usage."""
        records = self._load_records()
        by_model: Dict[str, Dict] = {}
        total_cost = 0.0
        for r in records:
            model = r.model or "unknown"
            if model not in by_model:
                by_model[model] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            by_model[model]["calls"] += 1
            by_model[model]["input_tokens"] += r.input_tokens
            by_model[model]["output_tokens"] += r.output_tokens
            by_model[model]["cost_usd"] += r.cost_usd
            total_cost += r.cost_usd
        return {
            "total_records": len(records),
            "total_cost_usd": round(total_cost, 4),
            "by_model": by_model,
            "daily_spend": round(self.get_daily_spend(), 4),
        }
