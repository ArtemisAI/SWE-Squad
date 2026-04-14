"""
Agents API module for SWE-Squad WebUI.

Provides endpoints for agent management, runs, stats, and environment testing.
Wraps SWE-Squad agent configuration from models.py and config.py.

Endpoints:
    GET /api/agents  — List all agents
    GET /api/agents/{name}  — Get a single agent by name
    POST /api/agents  — Create a new agent
    PUT /api/agents/{name}  — Update an agent
    DELETE /api/agents/{name}  — Delete an agent
    GET /api/agents/{name}/runs  — Get runs for an agent
    GET /api/agents/{name}/stats  — Get stats for an agent
    GET /api/agents/{name}/keys  — Get environment keys for an agent
    GET /api/agents/models  — Get available models
    POST /api/agents/{name}/environment-test  — Test agent environment

Maps to SWE-Squad models.py (SWEAgentConfig, AgentRole) and config.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional

from src.swe_team.config import load_config
from src.swe_team.models import AgentRole, SWEAgentConfig

logger = logging.getLogger(__name__)

# URL patterns
_AGENTS_RE = re.compile(r"^/api/agents/([^/]+)$")
_AGENT_RUNS_RE = re.compile(r"^/api/agents/([^/]+)/runs$")
_AGENT_STATS_RE = re.compile(r"^/api/agents/([^/]+)/stats$")
_AGENT_KEYS_RE = re.compile(r"^/api/agents/([^/]+)/keys$")
_AGENT_ENV_TEST_RE = re.compile(r"^/api/agents/([^/]+)/environment-test$")

# Model tiers from config
_MODEL_TIERS = {
    "t1": "opus",      # Heavy tier for architecture, orchestration, critical bugs
    "t2": "sonnet",    # Standard tier for feature implementation, routine fixes
    "t3": "haiku",     # Fast tier for docs, scanning, simple tasks
}

# Available models from config
_AVAILABLE_MODELS: List[Dict[str, Any]] = [
    {"name": "opus", "tier": "t1", "description": "Heavy tier - architecture, orchestration, critical bugs"},
    {"name": "sonnet", "tier": "t2", "description": "Standard tier - feature implementation, routine fixes"},
    {"name": "haiku", "tier": "t3", "description": "Fast tier - docs, scanning, simple tasks"},
]


# ---------------------------------------------------------------------------
# Dataclasses for API responses
# ---------------------------------------------------------------------------


@dataclass
class Agent:
    """Agent as returned by the API."""

    name: str
    role: str
    description: str = ""
    model: str = "sonnet"
    tools: List[str] = field(default_factory=list)
    max_concurrent_tasks: int = 1
    enabled: bool = False
    node: str = "primary"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "description": self.description,
            "model": self.model,
            "tools": self.tools,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "enabled": self.enabled,
            "node": self.node,
        }

    @classmethod
    def from_swe_agent_config(cls, config: SWEAgentConfig) -> "Agent":
        """Create API response from internal SWEAgentConfig dataclass."""
        return cls(
            name=config.name,
            role=config.role.value,
            description=config.description,
            model=config.model,
            tools=config.tools,
            max_concurrent_tasks=config.max_concurrent_tasks,
            enabled=config.enabled,
            node=config.node,
        )


@dataclass
class AgentRun:
    """A single agent run."""

    run_id: str
    agent_name: str
    ticket_id: Optional[str] = None
    status: str = "completed"
    started_at: str = ""
    ended_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    result: Optional[str] = None
    transcript: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "ticket_id": self.ticket_id,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "result": self.result,
            "transcript": self.transcript,
        }


@dataclass
class AgentStats:
    """Statistics for an agent."""

    agent_name: str
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    avg_duration_seconds: Optional[float] = None
    success_rate: float = 0.0
    last_run: Optional[str] = None
    last_24h_runs: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "total_runs": self.total_runs,
            "successful_runs": self.successful_runs,
            "failed_runs": self.failed_runs,
            "avg_duration_seconds": self.avg_duration_seconds,
            "success_rate": self.success_rate,
            "last_run": self.last_run,
            "last_24h_runs": self.last_24h_runs,
        }


@dataclass
class AgentKey:
    """An environment key for an agent."""

    name: str
    value_preview: Optional[str] = None
    masked: bool = True
    last_updated: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value_preview": self.value_preview,
            "masked": self.masked,
            "last_updated": self.last_updated,
        }


@dataclass
class Model:
    """An available model."""

    name: str
    tier: str
    description: Optional[str] = None
    provider: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier,
            "description": self.description,
            "provider": self.provider,
        }


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


def _read_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    """Read request body from handler."""
    content_length = int(handler.headers.get("Content-Length", 0))
    return handler.rfile.read(content_length)


# ---------------------------------------------------------------------------
# Agent run storage (in-memory for demo)
# ---------------------------------------------------------------------------

# In a real implementation, this would be persisted to disk or database
_agent_runs: Dict[str, List[AgentRun]] = {}
_agent_stats: Dict[str, AgentStats] = {}


def _get_runs_for_agent(agent_name: str) -> List[AgentRun]:
    """Get runs for a specific agent."""
    return _agent_runs.get(agent_name, [])


def _add_run_for_agent(run: AgentRun) -> None:
    """Add a run for an agent."""
    if run.agent_name not in _agent_runs:
        _agent_runs[run.agent_name] = []
    _agent_runs[run.agent_name].append(run)
    _update_agent_stats(run.agent_name)


def _update_agent_stats(agent_name: str) -> None:
    """Update statistics for an agent based on runs."""
    runs = _get_runs_for_agent(agent_name)
    now = datetime.now(timezone.utc)
    yesterday = now.replace(day=now.day - 1 if now.day > 1 else 1)

    successful = sum(1 for r in runs if r.status == "completed")
    failed = sum(1 for r in runs if r.status == "failed")
    total = len(runs)

    durations = [r.duration_seconds for r in runs if r.duration_seconds is not None]
    avg_duration = sum(durations) / len(durations) if durations else None

    last_24h_runs = sum(
        1
        for r in runs
        if datetime.fromisoformat(r.started_at.replace("Z", "+00:00")) >= yesterday
    )

    last_run = runs[-1].started_at if runs else None

    success_rate = (successful / total * 100) if total > 0 else 0.0

    _agent_stats[agent_name] = AgentStats(
        agent_name=agent_name,
        total_runs=total,
        successful_runs=successful,
        failed_runs=failed,
        avg_duration_seconds=avg_duration,
        success_rate=success_rate,
        last_run=last_run,
        last_24h_runs=last_24h_runs,
    )


# ---------------------------------------------------------------------------
# Config access helpers
# ---------------------------------------------------------------------------


def _get_agent_configs() -> List[SWEAgentConfig]:
    """Get agent configurations from SWE team config."""
    try:
        config = load_config()
        return config.agents
    except Exception:
        logger.warning("Failed to load SWE team config, returning empty list")
        return []


def _get_agent_config(name: str) -> Optional[SWEAgentConfig]:
    """Get a specific agent configuration by name."""
    for agent in _get_agent_configs():
        if agent.name == name:
            return agent
    return None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def handle_get(
    handler: BaseHTTPRequestHandler,
) -> bool:
    """Handle GET requests for agents. Returns True if handled."""
    path = handler.path

    # GET /api/agents - List all agents
    if path == "/api/agents":
        return _handle_list_agents(handler)

    # GET /api/agents/models - Get available models
    if path == "/api/agents/models":
        return _handle_list_models(handler)

    # GET /api/agents/{name} - Get a single agent
    match = _AGENTS_RE.match(path)
    if match:
        name = match.group(1)
        return _handle_get_agent(handler, name)

    # GET /api/agents/{name}/runs - Get runs for an agent
    match = _AGENT_RUNS_RE.match(path)
    if match:
        name = match.group(1)
        return _handle_get_runs(handler, name)

    # GET /api/agents/{name}/stats - Get stats for an agent
    match = _AGENT_STATS_RE.match(path)
    if match:
        name = match.group(1)
        return _handle_get_stats(handler, name)

    # GET /api/agents/{name}/keys - Get keys for an agent
    match = _AGENT_KEYS_RE.match(path)
    if match:
        name = match.group(1)
        return _handle_get_keys(handler, name)

    return False


def handle_post(
    handler: BaseHTTPRequestHandler,
) -> bool:
    """Handle POST requests for agents. Returns True if handled."""
    # POST /api/agents - Create a new agent
    if handler.path == "/api/agents":
        return _handle_create_agent(handler)

    # POST /api/agents/{name}/environment-test - Test environment
    match = _AGENT_ENV_TEST_RE.match(handler.path)
    if match:
        name = match.group(1)
        return _handle_environment_test(handler, name)

    return False


def handle_put(
    handler: BaseHTTPRequestHandler,
) -> bool:
    """Handle PUT requests for agents. Returns True if handled."""
    # PUT /api/agents/{name} - Update an agent
    match = _AGENTS_RE.match(handler.path)
    if match:
        name = match.group(1)
        return _handle_update_agent(handler, name)

    return False


def handle_delete(
    handler: BaseHTTPRequestHandler,
) -> bool:
    """Handle DELETE requests for agents. Returns True if handled."""
    # DELETE /api/agents/{name} - Delete an agent
    match = _AGENTS_RE.match(handler.path)
    if match:
        name = match.group(1)
        return _handle_delete_agent(handler, name)

    return False


# ---------------------------------------------------------------------------
# Specific handlers
# ---------------------------------------------------------------------------


def _handle_list_agents(handler: BaseHTTPRequestHandler) -> bool:
    """GET /api/agents - List all agents."""
    try:
        configs = _get_agent_configs()
        agents = [Agent.from_swe_agent_config(c).to_dict() for c in configs]
        _json_response(handler, agents)
        return True
    except Exception as exc:
        logger.exception("Error listing agents")
        _error_response(handler, str(exc), 500)
        return True


def _handle_list_models(handler: BaseHTTPRequestHandler) -> bool:
    """GET /api/agents/models - Get available models."""
    try:
        models = [Model(**m).to_dict() for m in _AVAILABLE_MODELS]
        _json_response(handler, models)
        return True
    except Exception as exc:
        logger.exception("Error listing models")
        _error_response(handler, str(exc), 500)
        return True


def _handle_get_agent(handler: BaseHTTPRequestHandler, name: str) -> bool:
    """GET /api/agents/{name} - Get a single agent."""
    try:
        config = _get_agent_config(name)
        if config is None:
            _error_response(handler, f"Agent '{name}' not found", 404)
            return True

        agent = Agent.from_swe_agent_config(config).to_dict()
        _json_response(handler, agent)
        return True
    except Exception as exc:
        logger.exception("Error getting agent")
        _error_response(handler, str(exc), 500)
        return True


def _handle_create_agent(handler: BaseHTTPRequestHandler) -> bool:
    """POST /api/agents - Create a new agent.

    Note: In production, this should persist to config file.
    Currently returns a mock response for demonstration.
    """
    try:
        body = _read_request_body(handler)
        data = json.loads(body.decode("utf-8"))

        # Validate required fields
        if "name" not in data:
            _error_response(handler, "Missing required field: name", 400)
            return True

        if "role" not in data:
            _error_response(handler, "Missing required field: role", 400)
            return True

        # Validate role
        try:
            AgentRole(data["role"])
        except ValueError:
            _error_response(
                handler,
                f"Invalid role: {data['role']}. "
                f"Valid roles: {[r.value for r in AgentRole]}",
                400,
            )
            return True

        # Create mock agent (would persist in production)
        agent = Agent(
            name=data["name"],
            role=data["role"],
            description=data.get("description", ""),
            model=data.get("model", "sonnet"),
            tools=data.get("tools", []),
            max_concurrent_tasks=data.get("max_concurrent_tasks", 1),
            enabled=data.get("enabled", False),
            node=data.get("node", "primary"),
        )

        logger.info("Creating agent: %s", agent.name)
        _json_response(handler, agent.to_dict(), 201)
        return True
    except json.JSONDecodeError:
        _error_response(handler, "Invalid JSON in request body", 400)
        return True
    except Exception as exc:
        logger.exception("Error creating agent")
        _error_response(handler, str(exc), 500)
        return True


def _handle_update_agent(handler: BaseHTTPRequestHandler, name: str) -> bool:
    """PUT /api/agents/{name} - Update an agent.

    Note: In production, this should persist to config file.
    Currently returns a mock response for demonstration.
    """
    try:
        body = _read_request_body(handler)
        data = json.loads(body.decode("utf-8"))

        # Check if agent exists
        config = _get_agent_config(name)
        if config is None:
            _error_response(handler, f"Agent '{name}' not found", 404)
            return True

        # Validate role if provided
        if "role" in data:
            try:
                AgentRole(data["role"])
            except ValueError:
                _error_response(
                    handler,
                    f"Invalid role: {data['role']}. "
                    f"Valid roles: {[r.value for r in AgentRole]}",
                    400,
                )
                return True

        # Create updated agent (would persist in production)
        agent = Agent(
            name=name,
            role=data.get("role", config.role.value),
            description=data.get("description", config.description),
            model=data.get("model", config.model),
            tools=data.get("tools", config.tools),
            max_concurrent_tasks=data.get(
                "max_concurrent_tasks", config.max_concurrent_tasks
            ),
            enabled=data.get("enabled", config.enabled),
            node=data.get("node", config.node),
        )

        logger.info("Updating agent: %s", name)
        _json_response(handler, agent.to_dict())
        return True
    except json.JSONDecodeError:
        _error_response(handler, "Invalid JSON in request body", 400)
        return True
    except Exception as exc:
        logger.exception("Error updating agent")
        _error_response(handler, str(exc), 500)
        return True


def _handle_delete_agent(handler: BaseHTTPRequestHandler, name: str) -> bool:
    """DELETE /api/agents/{name} - Delete an agent.

    Note: In production, this should remove from config file.
    Currently returns a mock response for demonstration.
    """
    try:
        # Check if agent exists
        config = _get_agent_config(name)
        if config is None:
            _error_response(handler, f"Agent '{name}' not found", 404)
            return True

        logger.info("Deleting agent: %s", name)
        _json_response(handler, {"ok": True})
        return True
    except Exception as exc:
        logger.exception("Error deleting agent")
        _error_response(handler, str(exc), 500)
        return True


def _handle_get_runs(handler: BaseHTTPRequestHandler, name: str) -> bool:
    """GET /api/agents/{name}/runs - Get runs for an agent."""
    try:
        # Check if agent exists
        config = _get_agent_config(name)
        if config is None:
            _error_response(handler, f"Agent '{name}' not found", 404)
            return True

        runs = _get_runs_for_agent(name)
        _json_response(handler, [r.to_dict() for r in runs])
        return True
    except Exception as exc:
        logger.exception("Error getting agent runs")
        _error_response(handler, str(exc), 500)
        return True


def _handle_get_stats(handler: BaseHTTPRequestHandler, name: str) -> bool:
    """GET /api/agents/{name}/stats - Get stats for an agent."""
    try:
        # Check if agent exists
        config = _get_agent_config(name)
        if config is None:
            _error_response(handler, f"Agent '{name}' not found", 404)
            return True

        # Get or compute stats
        stats = _agent_stats.get(name)
        if stats is None:
            _update_agent_stats(name)
            stats = _agent_stats.get(name)

        if stats is None:
            # No runs yet, return empty stats
            stats = AgentStats(agent_name=name)

        _json_response(handler, stats.to_dict())
        return True
    except Exception as exc:
        logger.exception("Error getting agent stats")
        _error_response(handler, str(exc), 500)
        return True


def _handle_get_keys(handler: BaseHTTPRequestHandler, name: str) -> bool:
    """GET /api/agents/{name}/keys - Get environment keys for an agent."""
    try:
        # Check if agent exists
        config = _get_agent_config(name)
        if config is None:
            _error_response(handler, f"Agent '{name}' not found", 404)
            return True

        # Get relevant environment variables based on agent role
        # In production, this would query the actual environment
        keys: List[AgentKey] = []

        # Common keys
        if os.getenv("SWE_TEAM_ID"):
            keys.append(
                AgentKey(name="SWE_TEAM_ID", value_preview="***", masked=True)
            )
        if os.getenv("SWE_TEAM_CONFIG"):
            keys.append(
                AgentKey(name="SWE_TEAM_CONFIG", value_preview="***", masked=True)
            )

        # Role-specific keys (masked for security)
        role = config.role.value
        if role == "investigator" or role == "developer":
            if os.getenv("ANTHROPIC_BASE_URL"):
                keys.append(
                    AgentKey(
                        name="ANTHROPIC_BASE_URL",
                        value_preview=os.getenv("ANTHROPIC_BASE_URL")[:20] + "...",
                        masked=False,
                    )
                )
        if role == "developer":
            if os.getenv("SWE_GITHUB_REPO"):
                keys.append(
                    AgentKey(
                        name="SWE_GITHUB_REPO",
                        value_preview=os.getenv("SWE_GITHUB_REPO"),
                        masked=False,
                    )
                )

        _json_response(handler, [k.to_dict() for k in keys])
        return True
    except Exception as exc:
        logger.exception("Error getting agent keys")
        _error_response(handler, str(exc), 500)
        return True


def _handle_environment_test(
    handler: BaseHTTPRequestHandler, name: str
) -> bool:
    """POST /api/agents/{name}/environment-test - Test agent environment."""
    try:
        # Check if agent exists
        config = _get_agent_config(name)
        if config is None:
            _error_response(handler, f"Agent '{name}' not found", 404)
            return True

        body = _read_request_body(handler)
        data = json.loads(body.decode("utf-8"))

        # Check if required environment variables are set
        required_vars = ["SWE_TEAM_ID"]
        missing = [v for v in required_vars if not os.getenv(v)]

        if missing:
            _error_response(
                handler,
                f"Missing environment variables: {', '.join(missing)}",
                503,
            )
            return True

        # Model-specific checks
        model = data.get("model", config.model)
        if model == "opus" or model == "sonnet" or model == "haiku":
            if not os.getenv("ANTHROPIC_BASE_URL"):
                _error_response(
                    handler, "ANTHROPIC_BASE_URL not set for Claude model", 503
                )
                return True

        logger.info("Environment test passed for agent: %s", name)
        _json_response(handler, {"ok": True, "message": "Environment test passed"})
        return True
    except json.JSONDecodeError:
        _error_response(handler, "Invalid JSON in request body", 400)
        return True
    except Exception as exc:
        logger.exception("Error testing environment")
        _error_response(handler, str(exc), 500)
        return True
