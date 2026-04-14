"""
Budgets API module for SWE-Squad WebUI.

Provides endpoints for budget policies and incident tracking,
wrapping the CostTrackerProvider functionality from cost_tracker.py.

Endpoints:
    GET /api/budget/policies  — Get budget policies for a team
    GET /api/budget/policies/<team_id>  — Get policy for specific team
    PUT /api/budget/policies/<team_id>  — Update budget policy
    GET /api/budget/policies/<team_id>/history  — Get incident history
    GET /api/budget/incidents  — Get budget incidents and alerts
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional

from src.swe_team.cost_tracker import BudgetPolicy
from src.swe_team.providers.cost.base import BudgetStatus, CostTrackerProvider

logger = logging.getLogger(__name__)

# Patterns: /api/budget/policies/<team_id>
#           /api/budget/policies/<team_id>/history
_BUDGET_POLICY_RE = re.compile(r"^/api/budget/policies/([a-zA-Z0-9_-]+)$")
_BUDGET_HISTORY_RE = re.compile(r"^/api/budget/policies/([a-zA-Z0-9_-]+)/history$")

# Thread-safe incident history store (in-memory)
_incident_history_lock = threading.Lock()
_incident_history: Dict[str, List[Dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Dataclasses for API responses
# ---------------------------------------------------------------------------


@dataclass
class BudgetPolicyResponse:
    """Budget policy as returned by the API."""

    id: str
    team_id: str
    policy_type: str  # "daily" | "monthly" | "rolling" - SWE-Squad uses daily/monthly
    daily_limit_cents: int
    monthly_limit_cents: int
    rolling_window_days: int = 0
    rolling_limit_cents: int = 0
    enabled: bool = True
    alert_thresholds: Dict[str, int] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "team_id": self.team_id,
            "policy_type": self.policy_type,
            "daily_limit_cents": self.daily_limit_cents,
            "monthly_limit_cents": self.monthly_limit_cents,
            "rolling_window_days": self.rolling_window_days,
            "rolling_limit_cents": self.rolling_limit_cents,
            "enabled": self.enabled,
            "alert_thresholds": self.alert_thresholds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_budget_policy(cls, policy: BudgetPolicy) -> "BudgetPolicyResponse":
        """Create API response from internal BudgetPolicy dataclass."""
        return cls(
            id=f"policy-{policy.team_id}",
            team_id=policy.team_id,
            policy_type="monthly",  # SWE-Squad defaults to monthly budget
            daily_limit_cents=policy.daily_budget_cents,
            monthly_limit_cents=policy.monthly_budget_cents,
            rolling_window_days=0,
            rolling_limit_cents=0,
            enabled=policy.hard_stop_enabled,
            alert_thresholds={
                "warning_pct": policy.alert_threshold_percent,
                "critical_pct": 100,  # Critical is the hard stop
            },
            created_at="",
            updated_at="",
        )

    @classmethod
    def to_budget_policy(cls, response: "BudgetPolicyResponse") -> BudgetPolicy:
        """Convert API response to internal BudgetPolicy."""
        return BudgetPolicy(
            team_id=response.team_id,
            daily_budget_cents=response.daily_limit_cents,
            monthly_budget_cents=response.monthly_limit_cents,
            alert_threshold_percent=response.alert_thresholds.get("warning_pct", 80),
            hard_stop_enabled=response.enabled,
        )


@dataclass
class BudgetIncident:
    """A budget incident or alert."""

    id: str
    team_id: str
    level: str  # "ok" | "warning" | "critical" | "exceeded"
    incident_type: str  # "daily_exceeded" | "monthly_exceeded" | "projected_exceed"
    message: str
    triggered_at: str
    resolved_at: Optional[str] = None
    spend_cents: float = 0.0
    limit_cents: float = 0.0
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "team_id": self.team_id,
            "level": self.level,
            "incident_type": self.incident_type,
            "message": self.message,
            "triggered_at": self.triggered_at,
            "resolved_at": self.resolved_at,
            "spend_cents": self.spend_cents,
            "limit_cents": self.limit_cents,
            "context": self.context,
        }


@dataclass
class BudgetStatusResponse:
    """Complete budget status response with policy and incidents."""

    configured: bool
    team_id: str
    budget_status: str  # "ok" | "warning" | "hard_stop"
    percent_used: float
    daily_spent_cents: float
    daily_limit_cents: float
    monthly_spent_cents: float
    monthly_limit_cents: float
    incidents: List[BudgetIncident] = field(default_factory=list)
    policy: Optional[BudgetPolicyResponse] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "configured": self.configured,
            "team_id": self.team_id,
            "budget_status": self.budget_status,
            "percent_used": self.percent_used,
            "daily_spent_cents": self.daily_spent_cents,
            "daily_limit_cents": self.daily_limit_cents,
            "monthly_spent_cents": self.monthly_spent_cents,
            "monthly_limit_cents": self.monthly_limit_cents,
            "incidents": [i.to_dict() for i in self.incidents],
            "policy": self.policy.to_dict() if self.policy else None,
        }

    @classmethod
    def from_budget_status(
        cls,
        status: BudgetStatus,
        policy: Optional[BudgetPolicy] = None,
    ) -> "BudgetStatusResponse":
        """Create API response from internal BudgetStatus."""
        incidents = _generate_incidents(status)
        policy_response = (
            BudgetPolicyResponse.from_budget_policy(policy)
            if policy
            else None
        )

        return cls(
            configured=True,
            team_id=status.team_id,
            budget_status=status.status,
            percent_used=status.percent_used,
            daily_spent_cents=status.daily_spent,
            daily_limit_cents=status.daily_limit,
            monthly_spent_cents=status.monthly_spent,
            monthly_limit_cents=status.monthly_limit,
            incidents=incidents,
            policy=policy_response,
        )


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _json_response(
    handler: BaseHTTPRequestHandler,
    data: Any,
    status: int = 200,
) -> None:
    """Send a JSON response."""
    try:
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        logger.debug("Client disconnected during response")


def _error_response(
    handler: BaseHTTPRequestHandler,
    message: str,
    status: int = 400,
) -> None:
    """Send a JSON error response."""
    _json_response(handler, {"error": message}, status=status)


def _read_json_body(handler: BaseHTTPRequestHandler) -> Optional[Dict[str, Any]]:
    """Read and parse JSON request body."""
    try:
        content_length = int(handler.headers.get("Content-Length", 0))
        if content_length == 0:
            return None
        body = handler.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse JSON body: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Incident generation and history helpers
# ---------------------------------------------------------------------------


def _generate_incidents(status: BudgetStatus) -> List[BudgetIncident]:
    """Generate incident records based on budget status."""
    incidents: List[BudgetIncident] = []
    now = datetime.now(timezone.utc).isoformat()

    daily_pct = (
        status.daily_spent / status.daily_limit * 100
        if status.daily_limit > 0
        else 0
    )
    monthly_pct = (
        status.monthly_spent / status.monthly_limit * 100
        if status.monthly_limit > 0
        else 0
    )

    # Check daily budget
    if daily_pct >= 100:
        incidents.append(
            BudgetIncident(
                id=f"daily-exceeded-{status.team_id}-{int(datetime.now(timezone.utc).timestamp())}",
                team_id=status.team_id,
                level="exceeded",
                incident_type="daily_exceeded",
                message=f"Daily budget exceeded: {status.daily_spent:.2f}¢ / {status.daily_limit:.2f}¢",
                triggered_at=now,
                resolved_at=None,
                spend_cents=status.daily_spent,
                limit_cents=status.daily_limit,
                context={"period": "daily"},
            )
        )
    elif daily_pct >= 80:
        incidents.append(
            BudgetIncident(
                id=f"daily-warning-{status.team_id}-{int(datetime.now(timezone.utc).timestamp())}",
                team_id=status.team_id,
                level="warning",
                incident_type="projected_exceed",
                message=f"Daily budget warning: {daily_pct:.1f}% used",
                triggered_at=now,
                resolved_at=None,
                spend_cents=status.daily_spent,
                limit_cents=status.daily_limit,
                context={"period": "daily"},
            )
        )

    # Check monthly budget
    if monthly_pct >= 100:
        incidents.append(
            BudgetIncident(
                id=f"monthly-exceeded-{status.team_id}-{int(datetime.now(timezone.utc).timestamp())}",
                team_id=status.team_id,
                level="exceeded",
                incident_type="monthly_exceeded",
                message=f"Monthly budget exceeded: {status.monthly_spent:.2f}¢ / {status.monthly_limit:.2f}¢",
                triggered_at=now,
                resolved_at=None,
                spend_cents=status.monthly_spent,
                limit_cents=status.monthly_limit,
                context={"period": "monthly"},
            )
        )
    elif monthly_pct >= 80:
        incidents.append(
            BudgetIncident(
                id=f"monthly-warning-{status.team_id}-{int(datetime.now(timezone.utc).timestamp())}",
                team_id=status.team_id,
                level="warning",
                incident_type="projected_exceed",
                message=f"Monthly budget warning: {monthly_pct:.1f}% used",
                triggered_at=now,
                resolved_at=None,
                spend_cents=status.monthly_spent,
                limit_cents=status.monthly_limit,
                context={"period": "monthly"},
            )
        )

    # Critical status maps to hard_stop
    if status.status == "hard_stop":
        for inc in incidents:
            if inc.incident_type in ("daily_exceeded", "monthly_exceeded"):
                inc.level = "critical"

    # Store incidents in history
    if incidents:
        _store_incidents(status.team_id, incidents)

    return incidents


def _store_incidents(team_id: str, incidents: List[BudgetIncident]) -> None:
    """Store incidents in in-memory history."""
    with _incident_history_lock:
        if team_id not in _incident_history:
            _incident_history[team_id] = []
        for incident in incidents:
            # Check if this is a new incident (not already in history)
            existing_ids = {inc["id"] for inc in _incident_history[team_id]}
            if incident.id not in existing_ids:
                _incident_history[team_id].append(incident.to_dict())
        # Keep last 100 incidents per team
        if len(_incident_history[team_id]) > 100:
            _incident_history[team_id] = _incident_history[team_id][-100:]


def _get_incident_history(team_id: str, days: Optional[int] = None) -> List[Dict[str, Any]]:
    """Get incident history for a team, optionally filtered by days."""
    with _incident_history_lock:
        history = _incident_history.get(team_id, []).copy()

    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        history = [
            inc for inc in history
            if datetime.fromisoformat(inc["triggered_at"].replace("Z", "+00:00")) >= cutoff
        ]

    return history


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def handle_get(
    handler: BaseHTTPRequestHandler,
    cost_tracker: Optional[CostTrackerProvider] = None,
) -> bool:
    """Handle GET requests for budgets. Returns True if handled."""
    path = handler.path

    # GET /api/budget/policies - List all policies (or single team)
    if path == "/api/budget/policies":
        return _handle_policies(handler, cost_tracker)

    # GET /api/budget/policies/<team_id> - Get policy for specific team
    match = _BUDGET_POLICY_RE.match(path)
    if match:
        team_id = match.group(1)
        return _handle_policy(handler, cost_tracker, team_id)

    # GET /api/budget/policies/<team_id>/history - Get incident history
    match = _BUDGET_HISTORY_RE.match(path)
    if match:
        team_id = match.group(1)
        return _handle_policy_history(handler, team_id)

    # GET /api/budget/incidents - Get budget incidents
    if path == "/api/budget/incidents":
        return _handle_incidents(handler, cost_tracker)

    return False


def handle_post(
    handler: BaseHTTPRequestHandler,
    cost_tracker: Optional[CostTrackerProvider] = None,
) -> bool:
    """Handle POST requests for budgets. Returns True if handled."""
    path = handler.path

    # POST /api/budget/policies - Create budget policy
    if path == "/api/budget/policies":
        return _handle_create_policy(handler, cost_tracker)

    return False


def handle_put(
    handler: BaseHTTPRequestHandler,
    cost_tracker: Optional[CostTrackerProvider] = None,
) -> bool:
    """Handle PUT requests for budgets. Returns True if handled."""
    path = handler.path

    # PUT /api/budget/policies/<team_id> - Update budget policy
    match = _BUDGET_POLICY_RE.match(path)
    if match:
        team_id = match.group(1)
        return _handle_update_policy(handler, cost_tracker, team_id)

    return False


def handle_delete(
    handler: BaseHTTPRequestHandler,
    cost_tracker: Optional[CostTrackerProvider] = None,
) -> bool:
    """Handle DELETE requests for budgets. Returns True if handled."""
    path = handler.path

    # DELETE /api/budget/policies/<team_id> - Delete budget policy
    match = _BUDGET_POLICY_RE.match(path)
    if match:
        team_id = match.group(1)
        return _handle_delete_policy(handler, team_id)

    return False


# ---------------------------------------------------------------------------
# Specific handlers
# ---------------------------------------------------------------------------


def _handle_policies(
    handler: BaseHTTPRequestHandler,
    cost_tracker: Optional[CostTrackerProvider],
) -> bool:
    """GET /api/budget/policies - List budget policies."""
    try:
        if cost_tracker is None:
            _json_response(handler, [])
            return True

        team_id = getattr(handler.server, "_team_id", "") or "default"

        if not hasattr(cost_tracker, "get_budget_policy"):
            _json_response(handler, [])
            return True

        policy = cost_tracker.get_budget_policy(team_id)
        _json_response(
            handler, [BudgetPolicyResponse.from_budget_policy(policy).to_dict()]
        )
        return True
    except Exception as exc:
        logger.exception("Error getting budget policies")
        _error_response(handler, str(exc), 500)
        return True


def _handle_policy(
    handler: BaseHTTPRequestHandler,
    cost_tracker: Optional[CostTrackerProvider],
    team_id: str,
) -> bool:
    """GET /api/budget/policies/<team_id> - Get policy for a specific team."""
    try:
        if cost_tracker is None:
            _error_response(handler, "CostTracker not configured", 503)
            return True

        if not hasattr(cost_tracker, "get_budget_policy"):
            _error_response(handler, "CostTracker does not support policy queries", 501)
            return True

        policy = cost_tracker.get_budget_policy(team_id)
        _json_response(handler, BudgetPolicyResponse.from_budget_policy(policy).to_dict())
        return True
    except Exception as exc:
        logger.exception("Error getting budget policy")
        _error_response(handler, str(exc), 500)
        return True


def _handle_policy_history(
    handler: BaseHTTPRequestHandler,
    team_id: str,
) -> bool:
    """GET /api/budget/policies/<team_id>/history - Get incident history."""
    try:
        # Parse optional days query parameter
        days: Optional[int] = None
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(handler.path)
        query_params = parse_qs(parsed.query)
        if "days" in query_params:
            try:
                days = int(query_params["days"][0])
            except ValueError:
                _error_response(handler, "Invalid days parameter", 400)
                return True

        history = _get_incident_history(team_id, days)
        _json_response(handler, history)
        return True
    except Exception as exc:
        logger.exception("Error getting incident history")
        _error_response(handler, str(exc), 500)
        return True


def _handle_create_policy(
    handler: BaseHTTPRequestHandler,
    cost_tracker: Optional[CostTrackerProvider],
) -> bool:
    """POST /api/budget/policies - Create budget policy."""
    try:
        body = _read_json_body(handler)
        if body is None:
            _error_response(handler, "Invalid JSON body", 400)
            return True

        team_id = body.get("team_id")
        if not team_id:
            _error_response(handler, "Missing team_id", 400)
            return True

        # Create policy response from request
        policy_response = BudgetPolicyResponse(
            id=f"policy-{team_id}",
            team_id=team_id,
            policy_type=body.get("policy_type", "monthly"),
            daily_limit_cents=body.get("daily_limit_cents", 5000),
            monthly_limit_cents=body.get("monthly_limit_cents", 100000),
            rolling_window_days=body.get("rolling_window_days", 0),
            rolling_limit_cents=body.get("rolling_limit_cents", 0),
            enabled=body.get("enabled", True),
            alert_thresholds=body.get("alert_thresholds", {"warning_pct": 80, "critical_pct": 100}),
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

        # Convert to internal policy and set
        if cost_tracker and hasattr(cost_tracker, "set_budget_policy"):
            internal_policy = BudgetPolicyResponse.to_budget_policy(policy_response)
            cost_tracker.set_budget_policy(internal_policy)
        else:
            logger.warning("CostTracker does not support set_budget_policy, policy not persisted")

        _json_response(handler, policy_response.to_dict(), status=201)
        return True
    except Exception as exc:
        logger.exception("Error creating budget policy")
        _error_response(handler, str(exc), 500)
        return True


def _handle_update_policy(
    handler: BaseHTTPRequestHandler,
    cost_tracker: Optional[CostTrackerProvider],
    team_id: str,
) -> bool:
    """PUT /api/budget/policies/<team_id> - Update budget policy."""
    try:
        body = _read_json_body(handler)
        if body is None:
            _error_response(handler, "Invalid JSON body", 400)
            return True

        # Get existing policy or create default
        if cost_tracker and hasattr(cost_tracker, "get_budget_policy"):
            existing_policy = cost_tracker.get_budget_policy(team_id)
        else:
            logger.warning("CostTracker does not support get_budget_policy, using defaults")
            existing_policy = BudgetPolicy(team_id=team_id)

        # Create updated policy response
        policy_response = BudgetPolicyResponse(
            id=f"policy-{team_id}",
            team_id=team_id,
            policy_type=body.get("policy_type", "monthly"),
            daily_limit_cents=body.get("daily_limit_cents", existing_policy.daily_budget_cents),
            monthly_limit_cents=body.get("monthly_limit_cents", existing_policy.monthly_budget_cents),
            rolling_window_days=body.get("rolling_window_days", 0),
            rolling_limit_cents=body.get("rolling_limit_cents", 0),
            enabled=body.get("enabled", existing_policy.hard_stop_enabled),
            alert_thresholds=body.get("alert_thresholds", {
                "warning_pct": body.get("alert_threshold_percent", existing_policy.alert_threshold_percent),
                "critical_pct": 100,
            }),
            created_at="",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

        # Convert to internal policy and set
        if cost_tracker and hasattr(cost_tracker, "set_budget_policy"):
            internal_policy = BudgetPolicyResponse.to_budget_policy(policy_response)
            cost_tracker.set_budget_policy(internal_policy)
        else:
            logger.warning("CostTracker does not support set_budget_policy, policy not persisted")

        _json_response(handler, policy_response.to_dict())
        return True
    except Exception as exc:
        logger.exception("Error updating budget policy")
        _error_response(handler, str(exc), 500)
        return True


def _handle_delete_policy(
    handler: BaseHTTPRequestHandler,
    team_id: str,
) -> bool:
    """DELETE /api/budget/policies/<team_id> - Delete budget policy."""
    try:
        # Note: InMemoryCostTracker doesn't support delete, just reset to defaults
        _error_response(handler, "Budget policy deletion not implemented", 501)
        return True
    except Exception as exc:
        logger.exception("Error deleting budget policy")
        _error_response(handler, str(exc), 500)
        return True


def _handle_incidents(
    handler: BaseHTTPRequestHandler,
    cost_tracker: Optional[CostTrackerProvider],
) -> bool:
    """GET /api/budget/incidents - Get budget incidents and alerts."""
    try:
        if cost_tracker is None:
            _json_response(handler, [])
            return True

        team_id = getattr(handler.server, "_team_id", "") or "default"

        if not hasattr(cost_tracker, "check_budget"):
            _json_response(handler, [])
            return True

        status = cost_tracker.check_budget(team_id)
        incidents = _generate_incidents(status)

        _json_response(handler, [i.to_dict() for i in incidents])
        return True
    except Exception as exc:
        logger.exception("Error getting budget incidents")
        _error_response(handler, str(exc), 500)
        return True
