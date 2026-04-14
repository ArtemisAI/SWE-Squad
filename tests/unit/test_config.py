"""
Unit tests for src/swe_team/config.py

Tests cover:
- GovernanceConfig, MonitorConfig, ModelConfig, RateLimitConfig, CycleConfig
- RoutingConfig, FallbackAgentConfig, MemoryConfig, AgentTimingConfig
- SWETeamConfig from_dict / to_dict
- load_config() with missing file (defaults) and YAML file
- Environment variable overrides (SWE_TEAM_ENABLED, SWE_TEAM_ID, SWE_GITHUB_ACCOUNT,
  T1_MODEL, T2_MODEL, T3_MODEL)
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from src.swe_team.config import (
    AgentTimingConfig,
    CycleConfig,
    FallbackAgentConfig,
    GovernanceConfig,
    MemoryConfig,
    ModelConfig,
    MonitorConfig,
    RateLimitConfig,
    RoutingConfig,
    SWETeamConfig,
    TeamConfig,
    load_config,
)


# ---------------------------------------------------------------------------
# GovernanceConfig
# ---------------------------------------------------------------------------

class TestGovernanceConfig:
    def test_defaults(self):
        g = GovernanceConfig()
        assert g.max_open_critical == 0
        assert g.max_open_high == 3
        assert g.max_failing_tests == 0
        assert g.require_ci_green is True
        assert g.check_interval_hours == 6
        assert g.enabled is False

    def test_from_dict_full(self):
        data = {
            "max_open_critical": 1,
            "max_open_high": 5,
            "max_failing_tests": 2,
            "require_ci_green": False,
            "check_interval_hours": 12,
            "enabled": True,
        }
        g = GovernanceConfig.from_dict(data)
        assert g.max_open_critical == 1
        assert g.max_open_high == 5
        assert g.require_ci_green is False
        assert g.enabled is True

    def test_from_dict_empty_uses_defaults(self):
        g = GovernanceConfig.from_dict({})
        assert g.max_open_critical == 0
        assert g.enabled is False

    def test_to_dict_roundtrip(self):
        g = GovernanceConfig(max_open_critical=2, max_open_high=10, enabled=True)
        d = g.to_dict()
        g2 = GovernanceConfig.from_dict(d)
        assert g2.max_open_critical == 2
        assert g2.max_open_high == 10
        assert g2.enabled is True


# ---------------------------------------------------------------------------
# ModelConfig
# ---------------------------------------------------------------------------

class TestModelConfig:
    def test_defaults(self):
        m = ModelConfig()
        assert m.t1_heavy == "opus"
        assert m.t2_standard == "sonnet"
        assert m.t3_fast == "haiku"

    def test_from_dict(self):
        m = ModelConfig.from_dict({"t1_heavy": "gpt-4", "t2_standard": "gpt-3.5"})
        assert m.t1_heavy == "gpt-4"
        assert m.t2_standard == "gpt-3.5"
        assert m.t3_fast == "haiku"  # default

    def test_to_dict(self):
        m = ModelConfig()
        d = m.to_dict()
        assert d == {"t1_heavy": "opus", "t2_standard": "sonnet", "t3_fast": "haiku"}

    def test_apply_env_overrides(self):
        m = ModelConfig()
        with patch.dict(os.environ, {"T1_MODEL": "my-t1", "T2_MODEL": "my-t2", "T3_MODEL": "my-t3"}):
            m.apply_env_overrides()
        assert m.t1_heavy == "my-t1"
        assert m.t2_standard == "my-t2"
        assert m.t3_fast == "my-t3"

    def test_apply_env_overrides_partial(self):
        m = ModelConfig()
        # Only T1_MODEL is set; others remain at defaults
        env = {"T1_MODEL": "custom-opus"}
        with patch.dict(os.environ, env, clear=False):
            # Ensure T2/T3 not in env
            env2 = {k: v for k, v in os.environ.items()}
            env2.pop("T2_MODEL", None)
            env2.pop("T3_MODEL", None)
            with patch.dict(os.environ, env2, clear=True):
                # Re-add only T1
                os.environ["T1_MODEL"] = "custom-opus"
                m.apply_env_overrides()
        assert m.t1_heavy == "custom-opus"
        assert m.t2_standard == "sonnet"
        assert m.t3_fast == "haiku"


# ---------------------------------------------------------------------------
# CycleConfig
# ---------------------------------------------------------------------------

class TestCycleConfig:
    def test_defaults(self):
        c = CycleConfig()
        assert c.max_new_tickets_per_cycle == 20
        assert c.max_investigations_per_cycle == 5
        assert c.max_developments_per_cycle == 2
        assert c.max_open_investigating == 3
        assert c.severity_filter == "high"
        assert c.max_investigation_workers == 8

    def test_from_dict_overrides(self):
        c = CycleConfig.from_dict({
            "max_new_tickets_per_cycle": 10,
            "max_investigations_per_cycle": 3,
            "severity_filter": "critical",
        })
        assert c.max_new_tickets_per_cycle == 10
        assert c.max_investigations_per_cycle == 3
        assert c.severity_filter == "critical"
        assert c.max_developments_per_cycle == 2  # default

    def test_to_dict_roundtrip(self):
        c = CycleConfig(max_new_tickets_per_cycle=7, severity_filter="low")
        d = c.to_dict()
        c2 = CycleConfig.from_dict(d)
        assert c2.max_new_tickets_per_cycle == 7
        assert c2.severity_filter == "low"


# ---------------------------------------------------------------------------
# RoutingConfig
# ---------------------------------------------------------------------------

class TestRoutingConfig:
    def test_defaults(self):
        r = RoutingConfig()
        assert r.external_agents_enabled is False
        assert r.complexity_threshold == 50
        assert "investigation" in r.capability_map

    def test_from_dict_enabled(self):
        r = RoutingConfig.from_dict({
            "external_agents_enabled": True,
            "complexity_threshold": 100,
            "capability_map": {"investigation": "opencode"},
        })
        assert r.external_agents_enabled is True
        assert r.complexity_threshold == 100
        assert r.capability_map["investigation"] == "opencode"

    def test_from_dict_empty_capability_map_uses_default(self):
        r = RoutingConfig.from_dict({"capability_map": {}})
        # Empty map should fall back to default
        assert "investigation" in r.capability_map

    def test_to_dict_roundtrip(self):
        r = RoutingConfig(external_agents_enabled=True, complexity_threshold=75)
        d = r.to_dict()
        r2 = RoutingConfig.from_dict(d)
        assert r2.external_agents_enabled is True
        assert r2.complexity_threshold == 75


# ---------------------------------------------------------------------------
# RateLimitConfig
# ---------------------------------------------------------------------------

class TestRateLimitConfig:
    def test_defaults(self):
        r = RateLimitConfig()
        assert r.max_retries_on_429 == 5
        assert r.initial_backoff_seconds == 60
        assert r.max_backoff_seconds == 900

    def test_from_dict(self):
        r = RateLimitConfig.from_dict({
            "max_retries_on_429": 5,
            "initial_backoff_seconds": 60.0,
        })
        assert r.max_retries_on_429 == 5
        assert r.initial_backoff_seconds == 60.0

    def test_to_dict(self):
        r = RateLimitConfig()
        d = r.to_dict()
        assert d["max_retries_on_429"] == 5


# ---------------------------------------------------------------------------
# MonitorConfig
# ---------------------------------------------------------------------------

class TestMonitorConfig:
    def test_defaults(self):
        m = MonitorConfig()
        assert "logs/" in m.log_directories
        assert "ERROR" in m.log_patterns
        assert m.scan_interval_minutes == 30
        assert m.dedup_window_hours == 24
        assert m.enabled is False

    def test_from_dict_remote_workers(self):
        data = {
            "remote_workers": [{"name": "worker1", "ssh": "worker1", "log_dir": "~/logs"}]
        }
        m = MonitorConfig.from_dict(data)
        assert len(m.remote_workers) == 1
        assert m.remote_workers[0]["name"] == "worker1"

    def test_to_dict_roundtrip(self):
        m = MonitorConfig(scan_interval_minutes=15, enabled=True)
        d = m.to_dict()
        m2 = MonitorConfig.from_dict(d)
        assert m2.scan_interval_minutes == 15
        assert m2.enabled is True


# ---------------------------------------------------------------------------
# FallbackAgentConfig
# ---------------------------------------------------------------------------

class TestFallbackAgentConfig:
    def test_defaults(self):
        f = FallbackAgentConfig()
        assert f.name == ""
        assert f.enabled is False
        assert f.priority == 100
        assert f.timeout == 120

    def test_from_dict(self):
        f = FallbackAgentConfig.from_dict({
            "name": "gemini",
            "command": "/usr/bin/gemini",
            "enabled": True,
            "priority": 10,
            "skills": ["investigation"],
        })
        assert f.name == "gemini"
        assert f.enabled is True
        assert f.priority == 10
        assert "investigation" in f.skills

    def test_to_dict_roundtrip(self):
        f = FallbackAgentConfig(name="opencode", enabled=True, priority=5)
        d = f.to_dict()
        f2 = FallbackAgentConfig.from_dict(d)
        assert f2.name == "opencode"
        assert f2.priority == 5


# ---------------------------------------------------------------------------
# MemoryConfig
# ---------------------------------------------------------------------------

class TestMemoryConfig:
    def test_defaults(self):
        m = MemoryConfig()
        assert m.embedding_model == "bge-m3"
        assert m.embedding_dimensions == 1024
        assert m.top_k == 5
        assert m.similarity_floor == 0.75
        assert m.dedup_threshold == 0.92

    def test_from_dict(self):
        m = MemoryConfig.from_dict({"top_k": 10, "dedup_threshold": 0.95})
        assert m.top_k == 10
        assert m.dedup_threshold == 0.95


# ---------------------------------------------------------------------------
# AgentTimingConfig
# ---------------------------------------------------------------------------

class TestAgentTimingConfig:
    def test_defaults(self):
        a = AgentTimingConfig()
        assert a.investigation_timeout == 300
        assert a.opus_timeout == 600
        assert a.agent_registry_ttl == 300

    def test_from_dict(self):
        a = AgentTimingConfig.from_dict({"investigation_timeout": 120})
        assert a.investigation_timeout == 120
        assert a.opus_timeout == 600  # default


# ---------------------------------------------------------------------------
# SWETeamConfig
# ---------------------------------------------------------------------------

class TestSWETeamConfig:
    def test_defaults(self):
        cfg = SWETeamConfig()
        assert cfg.enabled is False
        assert cfg.team_id == "default"
        assert cfg.github_account == ""
        assert cfg.ticket_store_path == "data/swe_team/tickets.json"
        assert cfg.a2a_hub_url == "http://localhost:18790"
        assert isinstance(cfg.governance, GovernanceConfig)
        assert isinstance(cfg.cycle, CycleConfig)
        assert isinstance(cfg.models, ModelConfig)

    def test_from_dict_empty(self):
        cfg = SWETeamConfig.from_dict({})
        assert cfg.enabled is False
        assert cfg.team_id == "default"

    def test_from_dict_with_values(self):
        data = {
            "enabled": True,
            "team_id": "squad-1",
            "github_account": "ArtemisBot",
            "ticket_store_path": "/tmp/tickets.json",
            "regression_window_hours": 48,
        }
        cfg = SWETeamConfig.from_dict(data)
        assert cfg.enabled is True
        assert cfg.team_id == "squad-1"
        assert cfg.github_account == "ArtemisBot"
        assert cfg.regression_window_hours == 48

    def test_get_agents_by_role_empty(self):
        from src.swe_team.models import AgentRole
        cfg = SWETeamConfig()
        result = cfg.get_agents_by_role(AgentRole.MONITOR)
        assert result == []

    def test_to_dict_contains_key_sections(self):
        cfg = SWETeamConfig()
        d = cfg.to_dict()
        assert "governance" in d
        assert "monitor" in d
        assert "models" in d
        assert "cycle" in d
        assert "enabled" in d

    def test_from_dict_parses_teams(self):
        cfg = SWETeamConfig.from_dict({
            "teams": {
                "alpha": {
                    "vm": "primary",
                    "github_account": "your-bot-alpha",
                    "role": "developer",
                    "max_concurrent": 3,
                    "cost_budget_daily": 50.0,
                    "specialization": ["frontend", "astro"],
                }
            }
        })
        assert "alpha" in cfg.teams
        assert isinstance(cfg.teams["alpha"], TeamConfig)
        assert cfg.teams["alpha"].name == "alpha"
        assert cfg.teams["alpha"].role == "developer"

    def test_from_dict_missing_teams_is_backwards_compatible(self):
        cfg = SWETeamConfig.from_dict({})
        assert cfg.teams == {}

    def test_from_dict_invalid_team_specialization_raises(self):
        with pytest.raises(ValueError, match="expected list"):
            SWETeamConfig.from_dict({
                "teams": {
                    "alpha": {
                        "specialization": "frontend",
                    }
                }
            })

    def test_from_dict_invalid_team_role_raises(self):
        with pytest.raises(ValueError, match="invalid role"):
            SWETeamConfig.from_dict({
                "teams": {
                    "alpha": {
                        "role": "qa",
                    }
                }
            })

    def test_from_dict_invalid_team_limits_raise(self):
        with pytest.raises(ValueError, match="max_concurrent"):
            SWETeamConfig.from_dict({
                "teams": {
                    "alpha": {
                        "max_concurrent": 0,
                    }
                }
            })
        with pytest.raises(ValueError, match="cost_budget_daily"):
            SWETeamConfig.from_dict({
                "teams": {
                    "alpha": {
                        "cost_budget_daily": 0,
                    }
                }
            })


# ---------------------------------------------------------------------------
# load_config()
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_load_config_missing_file_returns_defaults(self, tmp_path):
        nonexistent = str(tmp_path / "does_not_exist.yaml")
        # Clear SWE_TEAM_ENABLED so .env leakage doesn't affect defaults test
        with patch.dict(os.environ, {"SWE_TEAM_ENABLED": ""}, clear=False):
            os.environ.pop("SWE_TEAM_ENABLED", None)
            cfg = load_config(path=nonexistent)
        assert isinstance(cfg, SWETeamConfig)
        assert cfg.enabled is False

    def test_load_config_from_yaml(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            enabled: true
            team_id: "test-team"
            github_account: "TestBot"
            governance:
              max_open_critical: 1
              enabled: true
            models:
              t1_heavy: "claude-opus-4"
              t2_standard: "claude-sonnet-4"
        """)
        cfg_file = tmp_path / "swe_team.yaml"
        cfg_file.write_text(yaml_content)
        # Strip all SWE_TEAM_* env vars so .env leakage doesn't override yaml values
        _SWE_VARS = ["SWE_TEAM_ENABLED", "SWE_TEAM_ID", "SWE_TEAM_CONFIG", "SWE_GITHUB_ACCOUNT"]
        saved = {k: os.environ.pop(k, None) for k in _SWE_VARS}
        try:
            cfg = load_config(path=str(cfg_file))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
        assert cfg.enabled is True
        assert cfg.team_id == "test-team"
        assert cfg.governance.max_open_critical == 1
        assert cfg.models.t1_heavy == "claude-opus-4"
        assert cfg.models.t2_standard == "claude-sonnet-4"

    def test_load_config_env_override_enabled_true(self, tmp_path):
        nonexistent = str(tmp_path / "no.yaml")
        with patch.dict(os.environ, {"SWE_TEAM_ENABLED": "true"}):
            cfg = load_config(path=nonexistent)
        assert cfg.enabled is True

    def test_load_config_env_override_enabled_false(self, tmp_path):
        yaml_content = "enabled: true\n"
        cfg_file = tmp_path / "cfg.yaml"
        cfg_file.write_text(yaml_content)
        with patch.dict(os.environ, {"SWE_TEAM_ENABLED": "false"}):
            cfg = load_config(path=str(cfg_file))
        assert cfg.enabled is False

    def test_load_config_env_team_id(self, tmp_path):
        nonexistent = str(tmp_path / "no.yaml")
        with patch.dict(os.environ, {"SWE_TEAM_ID": "my-team"}):
            cfg = load_config(path=nonexistent)
        assert cfg.team_id == "my-team"

    def test_load_config_env_github_account(self, tmp_path):
        nonexistent = str(tmp_path / "no.yaml")
        with patch.dict(os.environ, {"SWE_GITHUB_ACCOUNT": "your-bot-alpha"}):
            cfg = load_config(path=nonexistent)
        assert cfg.github_account == "your-bot-alpha"

    def test_load_config_model_env_overrides(self, tmp_path):
        nonexistent = str(tmp_path / "no.yaml")
        env = {"T1_MODEL": "my-opus", "T2_MODEL": "my-sonnet", "T3_MODEL": "my-haiku"}
        with patch.dict(os.environ, env):
            cfg = load_config(path=nonexistent)
        assert cfg.models.t1_heavy == "my-opus"
        assert cfg.models.t2_standard == "my-sonnet"
        assert cfg.models.t3_fast == "my-haiku"

    def test_load_config_swe_team_config_env(self, tmp_path):
        yaml_content = "enabled: true\nteam_id: env-driven\n"
        cfg_file = tmp_path / "via_env.yaml"
        cfg_file.write_text(yaml_content)
        # Override all SWE_TEAM_* vars to control exactly what load_config sees
        env_override = {
            "SWE_TEAM_CONFIG": str(cfg_file),
            "SWE_TEAM_ENABLED": "true",
            "SWE_TEAM_ID": "",   # clear so yaml's team_id wins
        }
        with patch.dict(os.environ, env_override):
            os.environ.pop("SWE_TEAM_ID", None)  # patch.dict sets empty str; pop for clean removal
            cfg = load_config()  # no explicit path
        assert cfg.enabled is True
        assert cfg.team_id == "env-driven"

    def test_load_config_enabled_accepts_1_and_yes(self, tmp_path):
        nonexistent = str(tmp_path / "no.yaml")
        for val in ("1", "yes", "YES", "True", "TRUE"):
            with patch.dict(os.environ, {"SWE_TEAM_ENABLED": val}):
                cfg = load_config(path=nonexistent)
            assert cfg.enabled is True, f"Expected enabled=True for SWE_TEAM_ENABLED={val!r}"

    def test_load_config_scheduler_from_yaml(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            scheduler:
              enabled: true
              tick_interval_seconds: 60
              max_workers: 5
        """)
        cfg_file = tmp_path / "sched.yaml"
        cfg_file.write_text(yaml_content)
        cfg = load_config(path=str(cfg_file))
        assert cfg.scheduler.enabled is True
        assert cfg.scheduler.tick_interval_seconds == 60
        assert cfg.scheduler.max_workers == 5

    def test_load_config_parses_teams(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            teams:
              alpha:
                vm: "primary"
                github_account: "your-bot-alpha"
                role: developer
                max_concurrent: 3
                cost_budget_daily: 50.0
                specialization: [frontend, astro, vue]
        """)
        cfg_file = tmp_path / "teams.yaml"
        cfg_file.write_text(yaml_content)
        cfg = load_config(path=str(cfg_file))
        assert "alpha" in cfg.teams
        assert cfg.teams["alpha"].github_account == "your-bot-alpha"
