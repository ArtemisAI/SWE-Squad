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
    scan_interval_minutes: int = 30
    dedup_window_hours: int = 24    # Avoid re-filing the same issue
    enabled: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MonitorConfig":
        return cls(
            log_directories=data.get("log_directories", ["logs/", "data/a2a/"]),
            log_patterns=data.get(
                "log_patterns", ["ERROR", "CRITICAL", "Traceback", "FAILED"]
            ),
            scan_interval_minutes=data.get("scan_interval_minutes", 30),
            dedup_window_hours=data.get("dedup_window_hours", 24),
            enabled=data.get("enabled", False),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "log_directories": self.log_directories,
            "log_patterns": self.log_patterns,
            "scan_interval_minutes": self.scan_interval_minutes,
            "dedup_window_hours": self.dedup_window_hours,
            "enabled": self.enabled,
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
    ticket_store_path: str = "data/swe_team/tickets.json"
    a2a_hub_url: str = "http://localhost:18790"
    enabled: bool = False
    team_id: str = "default"
    github_account: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SWETeamConfig":
        agents = [
            SWEAgentConfig.from_dict(a) for a in data.get("agents", [])
        ]
        gov = GovernanceConfig.from_dict(data.get("governance", {}))
        mon = MonitorConfig.from_dict(data.get("monitor", {}))
        return cls(
            agents=agents,
            governance=gov,
            monitor=mon,
            ticket_store_path=data.get(
                "ticket_store_path", "data/swe_team/tickets.json"
            ),
            a2a_hub_url=data.get("a2a_hub_url", "http://localhost:18790"),
            enabled=data.get("enabled", False),
            team_id=data.get("team_id", "default"),
            github_account=data.get("github_account", ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agents": [a.to_dict() for a in self.agents],
            "governance": self.governance.to_dict(),
            "monitor": self.monitor.to_dict(),
            "ticket_store_path": self.ticket_store_path,
            "a2a_hub_url": self.a2a_hub_url,
            "enabled": self.enabled,
            "team_id": self.team_id,
            "github_account": self.github_account,
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

    return config
