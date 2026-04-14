"""
Token accounting and cost tracking for SWE-Squad.

Tracks token usage per Claude CLI invocation, calculates costs,
and provides per-ticket cost breakdowns. Integrates with the
scheduler's quota_checker for budget-aware throttling.
"""
from __future__ import annotations

import json
import logging
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
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    task: str = ""  # investigate, develop, review, triage
    agent: str = "claude-code"
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TokenUsage":
        cleaned = {}
        int_fields = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"}
        for k, v in data.items():
            if k not in cls.__dataclass_fields__:
                continue
            if k in int_fields:
                try:
                    v = int(v)
                except (ValueError, TypeError):
                    v = 0
            elif k == "cost_usd":
                try:
                    v = float(v)
                except (ValueError, TypeError):
                    v = 0.0
            cleaned[k] = v
        return cls(**cleaned)


def calculate_cost(model: str, input_tokens: int, output_tokens: int, pricing: Optional[Dict] = None) -> float:
    """Calculate cost in USD for a given token usage."""
    pricing = pricing or DEFAULT_PRICING
    model_lower = model.lower() if model else ""
    if "opus" in model_lower:
        model_key = "opus"
    elif "sonnet" in model_lower:
        model_key = "sonnet"
    elif "haiku" in model_lower:
        model_key = "haiku"
    else:
        model_key = "default"
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
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> TokenUsage:
        """Record a token usage event. Returns the usage record with calculated cost."""
        cost = calculate_cost(model, input_tokens, output_tokens, self._pricing)
        usage = TokenUsage(
            session_id=session_id,
            ticket_id=ticket_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cost_usd=round(cost, 6),
            task=task,
            agent=agent,
            metadata=metadata or {},
        )

        # Append to JSONL file and update session totals inside the same lock
        key = ticket_id or session_id or "unknown"
        with self._lock:
            with open(self._path, "a") as f:
                f.write(json.dumps(usage.to_dict(), default=str) + "\n")
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
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        records = self._load_records()
        result = 0.0
        for r in records:
            try:
                ts = datetime.fromisoformat(r.timestamp)
                if ts >= cutoff:
                    result += r.cost_usd
            except (ValueError, TypeError):
                continue
        return result

    def check_budget(
        self,
        daily_cap: float = 0,
        hourly_cap: float = 0,
        per_ticket_cap: float = 0,
        ticket_id: str = "",
    ) -> tuple[bool, float]:
        """Check if we have budget remaining. Returns (has_budget, remaining_usd)."""
        remaining = float("inf")

        if daily_cap > 0:
            daily = self.get_daily_spend()
            if daily >= daily_cap:
                return False, 0.0
            remaining = daily_cap - daily

        if hourly_cap > 0:
            hourly = self.get_hourly_spend()
            if hourly >= hourly_cap:
                return False, 0.0
            remaining = min(remaining, hourly_cap - hourly)

        if per_ticket_cap > 0 and ticket_id:
            ticket_cost = self.get_ticket_cost(ticket_id)
            if ticket_cost["total_usd"] >= per_ticket_cap:
                return False, 0.0

        return True, remaining

    # ── Aggregation queries ──────────────────────────────────────────

    def _aggregate(self, records: List[TokenUsage], key_fn) -> list[dict]:
        """Group records by key_fn(record) and return aggregated dicts."""
        buckets: Dict[str, Dict[str, Any]] = {}
        for r in records:
            k = key_fn(r)
            if k not in buckets:
                buckets[k] = {
                    "period": k, "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "cost_usd": 0.0, "count": 0,
                }
            b = buckets[k]
            b["input_tokens"] += r.input_tokens
            b["output_tokens"] += r.output_tokens
            b["cache_read_tokens"] += r.cache_read_tokens
            b["cache_creation_tokens"] += r.cache_creation_tokens
            b["cost_usd"] += r.cost_usd
            b["count"] += 1
        for b in buckets.values():
            b["cost_usd"] = round(b["cost_usd"], 6)
        return sorted(buckets.values(), key=lambda x: x["period"])

    def _filter_since(self, delta: timedelta) -> List[TokenUsage]:
        cutoff = datetime.now(timezone.utc) - delta
        results = []
        for r in self._load_records():
            try:
                ts = datetime.fromisoformat(r.timestamp)
                if ts >= cutoff:
                    results.append(r)
            except (ValueError, TypeError):
                continue
        return results

    def _filter_range(self, start: datetime, end: datetime) -> List[TokenUsage]:
        """Filter records to those within [start, end] inclusive."""
        results = []
        for r in self._load_records():
            try:
                ts = datetime.fromisoformat(r.timestamp)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if start <= ts <= end:
                    results.append(r)
            except (ValueError, TypeError):
                continue
        return results

    def by_hour(self, since_hours: int = 24) -> list[dict]:
        """Token/cost totals grouped by hour."""
        records = self._filter_since(timedelta(hours=since_hours))
        return self._aggregate(records, lambda r: r.timestamp[:13])  # YYYY-MM-DDTHH

    def by_day(self, since_days: int = 7) -> list[dict]:
        """Token/cost totals grouped by day."""
        records = self._filter_since(timedelta(days=since_days))
        return self._aggregate(records, lambda r: r.timestamp[:10])  # YYYY-MM-DD

    def by_week(self, since_weeks: int = 4) -> list[dict]:
        """Token/cost totals grouped by ISO week."""
        records = self._filter_since(timedelta(weeks=since_weeks))
        def week_key(r):
            try:
                dt = datetime.fromisoformat(r.timestamp)
                iso = dt.isocalendar()
                return f"{iso[0]}-W{iso[1]:02d}"
            except (ValueError, TypeError):
                return "unknown"
        return self._aggregate(records, week_key)

    def by_month(self, since_months: int = 3) -> list[dict]:
        """Token/cost totals grouped by month."""
        records = self._filter_since(timedelta(days=since_months * 31))
        return self._aggregate(records, lambda r: r.timestamp[:7])  # YYYY-MM

    def by_range(self, start: datetime, end: datetime) -> list[dict]:
        """Token/cost totals grouped by day for an arbitrary date range."""
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        records = self._filter_range(start, end)
        return self._aggregate(records, lambda r: r.timestamp[:10])  # YYYY-MM-DD

    def by_agent(self, since_hours: int = 24) -> dict:
        """Token/cost totals grouped by agent/task field."""
        records = self._filter_since(timedelta(hours=since_hours))
        buckets: Dict[str, Dict[str, Any]] = {}
        for r in records:
            k = r.agent or "unknown"
            if k not in buckets:
                buckets[k] = {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "cost_usd": 0.0, "count": 0,
                }
            b = buckets[k]
            b["input_tokens"] += r.input_tokens
            b["output_tokens"] += r.output_tokens
            b["cache_read_tokens"] += r.cache_read_tokens
            b["cache_creation_tokens"] += r.cache_creation_tokens
            b["cost_usd"] += r.cost_usd
            b["count"] += 1
        for b in buckets.values():
            b["cost_usd"] = round(b["cost_usd"], 6)
        return buckets

    def by_ticket(self, since_hours: int = 24) -> dict:
        """Token/cost totals grouped by ticket_id."""
        records = self._filter_since(timedelta(hours=since_hours))
        buckets: Dict[str, Dict[str, Any]] = {}
        for r in records:
            k = r.ticket_id or "unknown"
            if k not in buckets:
                buckets[k] = {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "cost_usd": 0.0, "count": 0,
                }
            b = buckets[k]
            b["input_tokens"] += r.input_tokens
            b["output_tokens"] += r.output_tokens
            b["cache_read_tokens"] += r.cache_read_tokens
            b["cache_creation_tokens"] += r.cache_creation_tokens
            b["cost_usd"] += r.cost_usd
            b["count"] += 1
        for b in buckets.values():
            b["cost_usd"] = round(b["cost_usd"], 6)
        return buckets

    def by_agent_list(self, since_hours: int = 168) -> list[dict]:
        """Return agent cost data as a list for the frontend API.

        Returns a list of dicts with keys: agent, total_cost_usd, ticket_count.
        """
        data = self.by_agent(since_hours=since_hours)
        result = []
        for agent, values in data.items():
            result.append({
                "agent": agent,
                "total_cost_usd": values.get("cost_usd", 0.0),
                "ticket_count": values.get("count", 0),
            })
        # Sort by cost descending
        result.sort(key=lambda x: x.get("total_cost_usd", 0), reverse=True)
        return result

    def by_ticket_list(self, since_hours: int = 168, store=None) -> list[dict]:
        """Return ticket cost data as a list for the frontend API.

        Returns a list of dicts with keys: ticket_id, title, total_cost_usd.
        Uses the ticket store to get ticket titles.
        """
        data = self.by_ticket(since_hours=since_hours)
        result = []

        # Get ticket titles from store if available
        ticket_titles = {}
        if store:
            try:
                tickets = store.list_tickets()
                ticket_titles = {t.ticket_id: t.title for t in tickets}
            except Exception:
                pass  # Fall back to empty titles

        for ticket_id, values in data.items():
            if ticket_id == "unknown":
                continue
            result.append({
                "ticket_id": ticket_id,
                "title": ticket_titles.get(ticket_id, f"Ticket {ticket_id}"),
                "total_cost_usd": values.get("cost_usd", 0.0),
            })
        # Sort by cost descending
        result.sort(key=lambda x: x.get("total_cost_usd", 0), reverse=True)
        return result

    def by_bucket_list(self, since_hours: int = 168, granularity: str = "day") -> list[dict]:
        """Return cost buckets as a list for the frontend API.

        Returns a list of dicts with keys: bucket, input_tokens, output_tokens,
        cache_read_tokens, cache_creation_tokens, total_cost_usd.

        Args:
            since_hours: Hours of data to include.
            granularity: "hour", "day", "week", or "month".
        """
        if granularity == "hour":
            return self.by_hour(since_hours=since_hours)
        elif granularity == "day":
            return self.by_day(since_days=since_hours // 24)
        elif granularity == "week":
            return self.by_week(since_weeks=since_hours // (24 * 7))
        elif granularity == "month":
            return self.by_month(since_months=since_hours // (24 * 30))
        else:
            return self.by_day(since_days=7)

    def by_model(self, since_hours: int = 168) -> list[dict]:
        """Return cost data grouped by model.

        Returns a list of dicts with keys: model, total_cost_usd, input_tokens,
        output_tokens, count.
        """
        records = self._filter_since(timedelta(hours=since_hours))
        buckets: Dict[str, Dict[str, Any]] = {}

        for r in records:
            k = r.model or "unknown"
            if k not in buckets:
                buckets[k] = {
                    "model": k,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "total_cost_usd": 0.0,
                    "count": 0,
                }
            b = buckets[k]
            b["input_tokens"] += r.input_tokens
            b["output_tokens"] += r.output_tokens
            b["cache_read_tokens"] += r.cache_read_tokens
            b["cache_creation_tokens"] += r.cache_creation_tokens
            b["total_cost_usd"] += r.cost_usd
            b["count"] += 1

        for b in buckets.values():
            b["total_cost_usd"] = round(b["total_cost_usd"], 6)

        result = list(buckets.values())
        result.sort(key=lambda x: x.get("total_cost_usd", 0), reverse=True)
        return result

    def subscription_roi(self, monthly_fee: float, since_days: int = 30) -> dict:
        """Calculate ROI of subscription vs pay-per-token API pricing."""
        records = self._filter_since(timedelta(days=since_days))
        api_cost = sum(r.cost_usd for r in records)
        savings = api_cost - monthly_fee
        roi_pct = (savings / monthly_fee * 100) if monthly_fee > 0 else 0.0
        return {
            "api_equivalent_cost": round(api_cost, 4),
            "subscription_fee": round(monthly_fee, 4),
            "savings": round(savings, 4),
            "roi_percent": round(roi_pct, 2),
        }

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
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Corrupt token store at %s: %s", self._path, e)
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


class AdaptiveTimeout:
    """Rolling-window adaptive timeout using mean + 1.5 × stdev formula.

    Adjusts the timeout based on observed execution times, clamped to
    [min_val, max_val].  Thread-safe.

    Parameters
    ----------
    initial:
        Starting timeout in seconds.
    min_val:
        Floor — never go below this.
    max_val:
        Ceiling — never exceed this.
    window:
        Number of recent samples to keep (oldest evicted when full).
    min_samples:
        Minimum samples before auto-adjustment kicks in; uses *initial* until reached.
    """

    def __init__(
        self,
        initial: float,
        *,
        min_val: float = 30.0,
        max_val: float = 3600.0,
        window: int = 20,
        min_samples: int = 5,
    ) -> None:
        import threading as _threading
        self._current = float(initial)
        self._min = float(min_val)
        self._max = float(max_val)
        self._window = window
        self._min_samples = min_samples
        self._samples: list[float] = []
        self._lock = _threading.Lock()

    def get(self) -> int:
        """Return current timeout value as an integer number of seconds."""
        with self._lock:
            return int(self._current)

    @property
    def value(self) -> float:
        """Current timeout value in seconds (float)."""
        with self._lock:
            return self._current

    @property
    def sample_count(self) -> int:
        """Number of samples recorded so far."""
        with self._lock:
            return len(self._samples)

    def record(self, duration: float) -> None:
        """Record an observed execution duration and recalculate the timeout."""
        import math
        with self._lock:
            self._samples.append(duration)
            if len(self._samples) > self._window:
                self._samples.pop(0)
            if len(self._samples) >= self._min_samples:
                mean = sum(self._samples) / len(self._samples)
                variance = sum((x - mean) ** 2 for x in self._samples) / len(self._samples)
                stdev = math.sqrt(variance)
                candidate = mean + 1.5 * stdev
                self._current = max(self._min, min(self._max, candidate))

    def __float__(self) -> float:
        return self.value

    def __int__(self) -> int:
        return self.get()
