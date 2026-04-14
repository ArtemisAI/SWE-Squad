"""
Configuration for the Autonomous SWE Team.

Loads agent definitions, governance thresholds, and operational settings
from ``config/swe_team.yaml`` with environment-variable overrides.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.swe_team.models import AgentRole, SWEAgentConfig
from src.swe_team.parallel_executor import ExecutionConfig
from src.swe_team.throttle import ThrottleConfig

logger = logging.getLogger(__name__)

# Default config path relative to repo root
_DEFAULT_CONFIG_PATH = "config/swe_team.yaml"


# ---------------------------------------------------------------------------
# Governance thresholds
# ---------------------------------------------------------------------------

@dataclass
class GovernanceConfig:
    """Ralph-Wiggum stability-gate thresholds.

    The gate blocks new feature work when critical/high bugs exceed the
    configured ceiling or CI is red.
    """

    max_open_critical: int = 0      # Block if any critical bugs exist
    max_open_high: int = 3          # Block if > N high bugs exist
    max_failing_tests: int = 0      # Block if any test failures
    require_ci_green: bool = True   # Block if CI is not green
    check_interval_hours: int = 6   # How often the monitor runs
    enabled: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GovernanceConfig":
        return cls(
            max_open_critical=data.get("max_open_critical", 0),
            max_open_high=data.get("max_open_high", 3),
            max_failing_tests=data.get("max_failing_tests", 0),
            require_ci_green=data.get("require_ci_green", True),
            check_interval_hours=data.get("check_interval_hours", 6),
            enabled=data.get("enabled", False),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_open_critical": self.max_open_critical,
            "max_open_high": self.max_open_high,
            "max_failing_tests": self.max_failing_tests,
            "require_ci_green": self.require_ci_green,
            "check_interval_hours": self.check_interval_hours,
            "enabled": self.enabled,
        }


# ---------------------------------------------------------------------------
# Monitor settings
# ---------------------------------------------------------------------------

@dataclass
class MonitorConfig:
    """Settings for the error-monitoring agent."""

    log_directories: List[str] = field(
        default_factory=lambda: ["logs/", "data/a2a/"]
    )
    log_patterns: List[str] = field(
        default_factory=lambda: [
            "ERROR",
            "CRITICAL",
            "Traceback",
            "FAILED",
        ]
    )
    exclude_patterns: List[str] = field(
        default_factory=lambda: ["swe_team", "swe_team_runner"]
    )
    scan_interval_minutes: int = 30
    dedup_window_hours: int = 24    # Avoid re-filing the same issue
    enabled: bool = False
    remote_workers: List[Dict[str, str]] = field(default_factory=list)
    worker_module_map: Dict[str, List[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MonitorConfig":
        return cls(
            log_directories=data.get("log_directories", ["logs/", "data/a2a/"]),
            log_patterns=data.get(
                "log_patterns", ["ERROR", "CRITICAL", "Traceback", "FAILED"]
            ),
            exclude_patterns=data.get(
                "exclude_patterns", ["swe_team", "swe_team_runner"]
            ),
            scan_interval_minutes=data.get("scan_interval_minutes", 30),
            dedup_window_hours=data.get("dedup_window_hours", 24),
            enabled=data.get("enabled", False),
            remote_workers=data.get("remote_workers", []),
            worker_module_map=data.get("worker_module_map", {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "log_directories": self.log_directories,
            "log_patterns": self.log_patterns,
            "exclude_patterns": self.exclude_patterns,
            "scan_interval_minutes": self.scan_interval_minutes,
            "dedup_window_hours": self.dedup_window_hours,
            "enabled": self.enabled,
            "remote_workers": self.remote_workers,
            "worker_module_map": self.worker_module_map,
        }


# ---------------------------------------------------------------------------
# Model cost tiers
# ---------------------------------------------------------------------------

@dataclass
class ModelConfig:
    """Model cost tiers for SWE agent operations.

    Three tiers allow cost-conscious routing:
      - t1_heavy: Architecture, orchestration, critical bugs (e.g. Opus)
      - t2_standard: Feature implementation, routine fixes (e.g. Sonnet)
      - t3_fast: Docs, scanning, simple tasks (e.g. Haiku)

    Environment variable overrides: T1_MODEL, T2_MODEL, T3_MODEL
    """

    t1_heavy: str = "opus"
    t2_standard: str = "sonnet"
    t3_fast: str = "haiku"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelConfig":
        return cls(
            t1_heavy=data.get("t1_heavy", "opus"),
            t2_standard=data.get("t2_standard", "sonnet"),
            t3_fast=data.get("t3_fast", "haiku"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "t1_heavy": self.t1_heavy,
            "t2_standard": self.t2_standard,
            "t3_fast": self.t3_fast,
        }

    def apply_env_overrides(self) -> None:
        """Apply environment variable overrides for model tiers."""
        env_t1 = os.environ.get("T1_MODEL")
        if env_t1:
            self.t1_heavy = env_t1
        env_t2 = os.environ.get("T2_MODEL")
        if env_t2:
            self.t2_standard = env_t2
        env_t3 = os.environ.get("T3_MODEL")
        if env_t3:
            self.t3_fast = env_t3


# ---------------------------------------------------------------------------
# Rate limit backoff settings
# ---------------------------------------------------------------------------

@dataclass
class RateLimitConfig:
    """Settings for exponential backoff on Claude Code CLI rate limits (429).

    Controls retry behaviour when the CLI returns a rate limit error.
    """

    max_retries_on_429: int = 5
    initial_backoff_seconds: float = 60
    max_backoff_seconds: float = 900

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RateLimitConfig":
        return cls(
            max_retries_on_429=data.get("max_retries_on_429", 5),
            initial_backoff_seconds=data.get("initial_backoff_seconds", 60),
            max_backoff_seconds=data.get("max_backoff_seconds", 900),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_retries_on_429": self.max_retries_on_429,
            "initial_backoff_seconds": self.initial_backoff_seconds,
            "max_backoff_seconds": self.max_backoff_seconds,
        }


# ---------------------------------------------------------------------------
# Per-cycle throttle settings
# ---------------------------------------------------------------------------

@dataclass
class CycleConfig:
    """Throttle knobs that control how much work each cycle does.

    These prevent the squad from flooding the LLM API or overwhelming a
    repo with simultaneous PRs when a large backlog is present.

    Attributes:
        max_new_tickets_per_cycle:  Cap on newly-triaged tickets per cycle.
                                    Oldest/highest-severity tickets are
                                    processed first; the rest wait for the
                                    next cycle.
        max_investigations_per_cycle: Max tickets sent to InvestigatorAgent
                                    per cycle (Claude CLI calls — expensive).
        max_developments_per_cycle: Max tickets sent to DeveloperAgent per
                                    cycle.
        max_open_investigating:     Hard cap on tickets allowed to be in
                                    INVESTIGATING state simultaneously.
                                    New investigations are skipped when this
                                    limit is reached.
        severity_filter:            Only triage/investigate tickets at or
                                    above this severity.
                                    Values: "low"|"medium"|"high"|"critical"
    """

    max_new_tickets_per_cycle: int = 20
    max_investigations_per_cycle: int = 5
    max_developments_per_cycle: int = 2
    max_open_investigating: int = 3
    severity_filter: str = "high"   # Ignore tickets below this severity
    max_investigation_workers: int = 8  # Thread-pool size for parallel investigations
    max_reinvestigations: int = 1   # Max re-investigation attempts after developer failure
    blocked_ticket_timeout_hours: int = 4
    blocked_ticket_escalation_hours: int = 24

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CycleConfig":
        stale_cfg = data.get("stale_ticket_timeouts", {}) or {}
        return cls(
            max_new_tickets_per_cycle=data.get("max_new_tickets_per_cycle", 20),
            max_investigations_per_cycle=data.get("max_investigations_per_cycle", 5),
            max_developments_per_cycle=data.get("max_developments_per_cycle", 2),
            max_open_investigating=data.get("max_open_investigating", 3),
            severity_filter=data.get("severity_filter", "high"),
            max_investigation_workers=data.get("max_investigation_workers", 8),
            max_reinvestigations=data.get("max_reinvestigations", 1),
            blocked_ticket_timeout_hours=data.get(
                "blocked_ticket_timeout_hours",
                stale_cfg.get("blocked_ticket_timeout_hours", 4),
            ),
            blocked_ticket_escalation_hours=data.get(
                "blocked_ticket_escalation_hours",
                stale_cfg.get("blocked_ticket_escalation_hours", 24),
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_new_tickets_per_cycle": self.max_new_tickets_per_cycle,
            "max_investigations_per_cycle": self.max_investigations_per_cycle,
            "max_developments_per_cycle": self.max_developments_per_cycle,
            "max_open_investigating": self.max_open_investigating,
            "severity_filter": self.severity_filter,
            "max_investigation_workers": self.max_investigation_workers,
            "max_reinvestigations": self.max_reinvestigations,
            "stale_ticket_timeouts": {
                "blocked_ticket_timeout_hours": self.blocked_ticket_timeout_hours,
                "blocked_ticket_escalation_hours": self.blocked_ticket_escalation_hours,
            },
        }


# ---------------------------------------------------------------------------
# Multi-agent fallback settings
# ---------------------------------------------------------------------------

@dataclass
class FallbackAgentConfig:
    """Configuration for a fallback coding agent used when the primary is rate-limited.

    Each entry defines a CLI agent that can be invoked as an alternative
    to Claude Code when rate limits are hit.

    Attributes:
        name:       Human-readable agent name.
        command:    Path to the CLI binary (e.g. ``/usr/bin/gemini``).
        args_template: CLI arguments.  ``{prompt}`` and ``{model}`` are substituted.
        default_model: Default model for this agent.
        enabled:    Whether this fallback is active.
        priority:   Selection priority (lower = preferred).
        timeout:    Default timeout in seconds.
        prompt_via_stdin: If True, send prompt via stdin rather than args.
        skills:     List of skill IDs this agent can handle.
    """

    name: str = ""
    command: str = ""
    args_template: List[str] = field(default_factory=list)
    default_model: str = ""
    enabled: bool = False
    priority: int = 100
    timeout: int = 120
    prompt_via_stdin: bool = False
    skills: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FallbackAgentConfig":
        return cls(
            name=data.get("name", ""),
            command=data.get("command", ""),
            args_template=data.get("args_template", []),
            default_model=data.get("default_model", ""),
            enabled=data.get("enabled", False),
            priority=data.get("priority", 100),
            timeout=data.get("timeout", 120),
            prompt_via_stdin=data.get("prompt_via_stdin", False),
            skills=data.get("skills", []),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "args_template": self.args_template,
            "default_model": self.default_model,
            "enabled": self.enabled,
            "priority": self.priority,
            "timeout": self.timeout,
            "prompt_via_stdin": self.prompt_via_stdin,
            "skills": self.skills,
        }


# ---------------------------------------------------------------------------
# Semantic memory settings
# ---------------------------------------------------------------------------

@dataclass
class MemoryConfig:
    """Settings for semantic ticket memory (pgvector embeddings) and memory worker service."""

    embedding_model: str = "bge-m3"
    embedding_dimensions: int = 1024
    top_k: int = 5
    similarity_floor: float = 0.75
    store_on_investigation_complete: bool = True
    auto_resolve_threshold: float = 0.90
    cluster_threshold: float = 0.85
    dedup_threshold: float = 0.92
    similarity_edge_threshold: float = 0.80
    # Memory worker service (claude-mem Supabase adaptation)
    enabled: bool = False
    worker_host: str = "127.0.0.1"
    worker_port: int = 37777

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryConfig":
        return cls(
            embedding_model=data.get("embedding_model", "bge-m3"),
            embedding_dimensions=data.get("embedding_dimensions", 1024),
            top_k=data.get("top_k", 5),
            similarity_floor=data.get("similarity_floor", 0.75),
            store_on_investigation_complete=data.get(
                "store_on_investigation_complete",
                True,
            ),
            auto_resolve_threshold=data.get("auto_resolve_threshold", 0.90),
            cluster_threshold=data.get("cluster_threshold", 0.85),
            dedup_threshold=data.get("dedup_threshold", 0.92),
            similarity_edge_threshold=data.get("similarity_edge_threshold", 0.80),
            enabled=data.get("enabled", False),
            worker_host=data.get(
                "worker_host",
                os.environ.get("MEMORY_WORKER_HOST", "127.0.0.1"),
            ),
            worker_port=int(
                data.get(
                    "worker_port",
                    os.environ.get("MEMORY_WORKER_PORT", 37777),
                )
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "top_k": self.top_k,
            "similarity_floor": self.similarity_floor,
            "store_on_investigation_complete": self.store_on_investigation_complete,
            "auto_resolve_threshold": self.auto_resolve_threshold,
            "cluster_threshold": self.cluster_threshold,
            "dedup_threshold": self.dedup_threshold,
            "similarity_edge_threshold": self.similarity_edge_threshold,
            "enabled": self.enabled,
            "worker_host": self.worker_host,
            "worker_port": self.worker_port,
        }


# ---------------------------------------------------------------------------
# Agent timing settings
# ---------------------------------------------------------------------------

@dataclass
class AgentTimingConfig:
    """Timeout and TTL settings for agent operations."""

    investigation_timeout: int = 300
    opus_timeout: int = 600
    agent_registry_ttl: int = 300

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentTimingConfig":
        return cls(
            investigation_timeout=data.get("investigation_timeout", 300),
            opus_timeout=data.get("opus_timeout", 600),
            agent_registry_ttl=data.get("agent_registry_ttl", 300),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "investigation_timeout": self.investigation_timeout,
            "opus_timeout": self.opus_timeout,
            "agent_registry_ttl": self.agent_registry_ttl,
        }


# ---------------------------------------------------------------------------
# Job scheduler settings
# ---------------------------------------------------------------------------

@dataclass
class SchedulerConfig:
    """Job scheduler configuration."""
    enabled: bool = False
    tick_interval_seconds: int = 30
    max_workers: int = 3
    job_store_path: str = "data/swe_team/jobs.json"
    peak_start_hour: int = 13
    peak_end_hour: int = 19
    peak_days: str = "0,1,2,3,4"
    default_jobs: list = field(default_factory=list)


@dataclass
class RoutingConfig:
    """External agent routing configuration for TriageAgent."""

    external_agents_enabled: bool = False
    complexity_threshold: int = 50  # error_log lines to trigger external routing
    capability_map: dict = field(default_factory=lambda: {
        "investigation": "gemini",
        "code_generation": "opencode",
    })

    @classmethod
    def from_dict(cls, d: dict) -> "RoutingConfig":
        cap = d.get("capability_map", {})
        return cls(
            external_agents_enabled=bool(d.get("external_agents_enabled", False)),
            complexity_threshold=int(d.get("complexity_threshold", 50)),
            capability_map=cap if cap else {
                "investigation": "gemini",
                "code_generation": "opencode",
            },
        )

    def to_dict(self) -> dict:
        return {
            "external_agents_enabled": self.external_agents_enabled,
            "complexity_threshold": self.complexity_threshold,
            "capability_map": self.capability_map,
        }


@dataclass
class TeamConfig:
    """Configuration for an execution team in the SWE fleet."""

    name: str
    vm: str = ""
    github_account: str = ""
    role: str = "full"  # developer | investigator | full
    max_concurrent: int = 3
    cost_budget_daily: float = 50.0
    specialization: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, data: Dict[str, Any]) -> "TeamConfig":
        role = data.get("role", "full")
        if role not in {"developer", "investigator", "full"}:
            raise ValueError(
                f"Team {name!r} has invalid role {role!r}: expected one of developer|investigator|full"
            )

        max_concurrent = int(data.get("max_concurrent", 3))
        if max_concurrent <= 0:
            raise ValueError(
                f"Team {name!r} has invalid max_concurrent={max_concurrent}: expected > 0"
            )

        cost_budget_daily = float(data.get("cost_budget_daily", 50.0))
        if cost_budget_daily <= 0:
            raise ValueError(
                f"Team {name!r} has invalid cost_budget_daily={cost_budget_daily}: expected > 0"
            )

        raw_specialization = data.get("specialization")
        if raw_specialization is not None and not isinstance(raw_specialization, list):
            raise ValueError(
                f"Team {name!r} has invalid specialization: expected list, got {type(raw_specialization).__name__}"
            )
        return cls(
            name=name,
            vm=data.get("vm", ""),
            github_account=data.get("github_account", ""),
            role=role,
            max_concurrent=max_concurrent,
            cost_budget_daily=cost_budget_daily,
            specialization=raw_specialization or [],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vm": self.vm,
            "github_account": self.github_account,
            "role": self.role,
            "max_concurrent": self.max_concurrent,
            "cost_budget_daily": self.cost_budget_daily,
            "specialization": self.specialization,
        }


# ---------------------------------------------------------------------------
# Stale ticket timeout settings
# ---------------------------------------------------------------------------

@dataclass
class StaleTicketTimeoutsConfig:
    """Timeout thresholds for stale ticket recovery."""

    investigating_hours: int = 4
    in_development_hours: int = 2
    in_review_hours: int = 24

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StaleTicketTimeoutsConfig":
        return cls(
            investigating_hours=int(data.get("investigating_hours", 4)),
            in_development_hours=int(data.get("in_development_hours", 2)),
            in_review_hours=int(data.get("in_review_hours", 24)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "investigating_hours": self.investigating_hours,
            "in_development_hours": self.in_development_hours,
            "in_review_hours": self.in_review_hours,
        }


# ---------------------------------------------------------------------------
# Top-level SWE team configuration
# ---------------------------------------------------------------------------

@dataclass
class SWETeamConfig:
    """Complete configuration for the autonomous SWE team."""

    agents: List[SWEAgentConfig] = field(default_factory=list)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)
    cycle: CycleConfig = field(default_factory=CycleConfig)
    fallback_agents: List[FallbackAgentConfig] = field(default_factory=list)
    timing: AgentTimingConfig = field(default_factory=AgentTimingConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    throttle: ThrottleConfig = field(default_factory=ThrottleConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    # Fleet team registry keyed by team id (e.g., "alpha", "beta", "gamma")
    teams: Dict[str, TeamConfig] = field(default_factory=dict)
    stale_ticket_timeouts: StaleTicketTimeoutsConfig = field(
        default_factory=StaleTicketTimeoutsConfig
    )
    repos: List[Dict[str, Any]] = field(default_factory=list)
    github_repos: List[str] = field(default_factory=list)
    ticket_store_path: str = "data/swe_team/tickets.json"
    a2a_hub_url: str = "http://localhost:18790"
    enabled: bool = False
    team_id: str = "default"
    github_account: str = ""
    regression_window_hours: int = 24
    auto_accept_invites: bool = False
    invite_allowlist: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SWETeamConfig":
        agents = [
            SWEAgentConfig.from_dict(a) for a in data.get("agents", [])
        ]
        gov = GovernanceConfig.from_dict(data.get("governance", {}))
        mon = MonitorConfig.from_dict(data.get("monitor", {}))
        memory = MemoryConfig.from_dict(data.get("memory", {}))
        models = ModelConfig.from_dict(data.get("models", {}))
        rate_limits = RateLimitConfig.from_dict(data.get("rate_limits", {}))
        cycle = CycleConfig.from_dict(data.get("cycle", {}))
        fallbacks = [
            FallbackAgentConfig.from_dict(f)
            for f in data.get("fallback_agents", [])
        ]
        timing = AgentTimingConfig.from_dict(data.get("timing", {}))
        throttle = ThrottleConfig.from_dict(data.get("throttle", {}))
        execution = ExecutionConfig.from_dict(data.get("execution", {}))
        routing = RoutingConfig.from_dict(data.get("routing", {}))
        raw_teams = data.get("teams") or {}
        teams = {
            team_name: TeamConfig.from_dict(team_name, team_data or {})
            for team_name, team_data in raw_teams.items()
        }
        stale_ticket_timeouts = StaleTicketTimeoutsConfig.from_dict(
            data.get("stale_ticket_timeouts", {})
        )
        return cls(
            agents=agents,
            governance=gov,
            monitor=mon,
            memory=memory,
            models=models,
            rate_limits=rate_limits,
            cycle=cycle,
            fallback_agents=fallbacks,
            timing=timing,
            throttle=throttle,
            execution=execution,
            routing=routing,
            teams=teams,
            stale_ticket_timeouts=stale_ticket_timeouts,
            repos=data.get("repos", []),
            github_repos=data.get("github_repos", []),
            ticket_store_path=data.get(
                "ticket_store_path", "data/swe_team/tickets.json"
            ),
            a2a_hub_url=data.get("a2a_hub_url", "http://localhost:18790"),
            enabled=data.get("enabled", False),
            team_id=data.get("team_id", "default"),
            github_account=data.get("github_account", ""),
            regression_window_hours=data.get("regression_window_hours", 24),
            auto_accept_invites=data.get("auto_accept_invites", False),
            invite_allowlist=data.get("invite_allowlist") or [],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agents": [a.to_dict() for a in self.agents],
            "governance": self.governance.to_dict(),
            "monitor": self.monitor.to_dict(),
            "memory": self.memory.to_dict(),
            "models": self.models.to_dict(),
            "rate_limits": self.rate_limits.to_dict(),
            "cycle": self.cycle.to_dict(),
            "fallback_agents": [f.to_dict() for f in self.fallback_agents],
            "timing": self.timing.to_dict(),
            "execution": self.execution.to_dict(),
            "teams": {name: team.to_dict() for name, team in self.teams.items()},
            "stale_ticket_timeouts": self.stale_ticket_timeouts.to_dict(),
            "repos": self.repos,
            "github_repos": self.github_repos,
            "ticket_store_path": self.ticket_store_path,
            "a2a_hub_url": self.a2a_hub_url,
            "enabled": self.enabled,
            "team_id": self.team_id,
            "github_account": self.github_account,
            "regression_window_hours": self.regression_window_hours,
            "auto_accept_invites": self.auto_accept_invites,
            "invite_allowlist": self.invite_allowlist,
        }

    def get_agents_by_role(self, role: AgentRole) -> List[SWEAgentConfig]:
        """Return all agents with the given *role*."""
        return [a for a in self.agents if a.role == role and a.enabled]


def load_config(path: Optional[str] = None) -> SWETeamConfig:
    """Load SWE team configuration from YAML.

    Falls back to built-in defaults when the file does not exist.
    Environment variable ``SWE_TEAM_CONFIG`` can override *path*.
    Environment variable ``SWE_TEAM_ENABLED`` can override the
    ``enabled`` flag (accepts ``true``/``false``, case-insensitive).
    """
    config_path = path or os.environ.get("SWE_TEAM_CONFIG", _DEFAULT_CONFIG_PATH)
    p = Path(config_path)
    if p.is_file():
        with open(p) as fh:
            raw = yaml.safe_load(fh) or {}
        logger.info("Loaded SWE team config from %s", p)
        config = SWETeamConfig.from_dict(raw)
    else:
        logger.info("SWE team config %s not found — using defaults", p)
        config = SWETeamConfig()

    # Scheduler section
    sched_raw = raw.get("scheduler", {}) if p.is_file() else {}
    if sched_raw:
        config.scheduler = SchedulerConfig(
            enabled=sched_raw.get("enabled", False),
            tick_interval_seconds=sched_raw.get("tick_interval_seconds", 30),
            max_workers=sched_raw.get("max_workers", 3),
            job_store_path=sched_raw.get("job_store_path", "data/swe_team/jobs.json"),
            peak_start_hour=sched_raw.get("peak_start_hour", 13),
            peak_end_hour=sched_raw.get("peak_end_hour", 19),
            peak_days=str(sched_raw.get("peak_days", "0,1,2,3,4")),
            default_jobs=sched_raw.get("default_jobs", []),
        )

    # Environment variable overrides
    env_enabled = os.environ.get("SWE_TEAM_ENABLED")
    if env_enabled is not None:
        config.enabled = env_enabled.lower() in ("true", "1", "yes")
        logger.info("SWE_TEAM_ENABLED=%s → enabled=%s", env_enabled, config.enabled)

    env_team_id = os.environ.get("SWE_TEAM_ID")
    if env_team_id:
        config.team_id = env_team_id
        logger.info("SWE_TEAM_ID=%s", env_team_id)

    env_gh_account = os.environ.get("SWE_GITHUB_ACCOUNT")
    if env_gh_account:
        config.github_account = env_gh_account
        logger.info("SWE_GITHUB_ACCOUNT=%s", env_gh_account)

    # Apply model tier env overrides (T1_MODEL, T2_MODEL, T3_MODEL)
    config.models.apply_env_overrides()

    return config
