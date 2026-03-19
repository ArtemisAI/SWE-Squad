"""Tests for the multi-project registry."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from src.swe_team.ops.project_registry import (
    Project,
    ProjectBudget,
    ProjectCredentials,
    ProjectInfra,
    ProjectRegistry,
)

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_YAML = """\
project:
  name: TestProject
  repo: org/test-repo
  local_path: /tmp/test-repo

credentials:
  github_token_env: GH_TOKEN_TEST
  api_keys:
    - name: anthropic
      env: ANTHROPIC_API_KEY_TEST

budget:
  daily_cap_usd: 50.0
  monthly_cap_usd: 800.0
  alert_threshold_pct: 75

infrastructure:
  ssh_config: config/ssh_test.conf
  ssh_key: ~/.ssh/test_key
  workers:
    - name: worker-1
      ssh: worker-1-alias
"""

MINIMAL_YAML = """\
project:
  name: Minimal
  repo: org/minimal
"""

INVALID_YAML = "{{{{not yaml at all"


def _write_yaml(directory: Path, filename: str, content: str) -> Path:
    p = directory / filename
    p.write_text(content)
    return p


# ── Project.from_dict round-trip ─────────────────────────────────────────────

class TestProjectFromDict:
    def test_round_trip_full(self):
        import yaml as _yaml
        data = _yaml.safe_load(SAMPLE_YAML)
        proj = Project.from_dict(data)
        assert proj.name == "TestProject"
        assert proj.repo == "org/test-repo"
        assert proj.local_path == "/tmp/test-repo"
        assert proj.credentials.github_token_env == "GH_TOKEN_TEST"
        assert len(proj.credentials.api_keys) == 1
        assert proj.credentials.api_keys[0]["env"] == "ANTHROPIC_API_KEY_TEST"
        assert proj.budget.daily_cap_usd == 50.0
        assert proj.budget.monthly_cap_usd == 800.0
        assert proj.budget.alert_threshold_pct == 75
        assert proj.infrastructure.ssh_config == "config/ssh_test.conf"
        assert proj.infrastructure.ssh_key == "~/.ssh/test_key"
        assert len(proj.infrastructure.workers) == 1

    def test_round_trip_minimal(self):
        import yaml as _yaml
        data = _yaml.safe_load(MINIMAL_YAML)
        proj = Project.from_dict(data)
        assert proj.name == "Minimal"
        assert proj.repo == "org/minimal"
        assert proj.local_path == ""
        assert proj.budget.daily_cap_usd == 0.0
        assert proj.budget.alert_threshold_pct == 80  # default

    def test_from_empty_dict(self):
        proj = Project.from_dict({})
        assert proj.name == ""
        assert proj.repo == ""


# ── ProjectCredentials.validate ──────────────────────────────────────────────

class TestProjectCredentials:
    def test_validate_detects_missing_github_token(self):
        creds = ProjectCredentials(github_token_env="MISSING_VAR_XYZ")
        with mock.patch.dict(os.environ, {}, clear=True):
            missing = creds.validate()
        assert "MISSING_VAR_XYZ" in missing

    def test_validate_detects_missing_api_keys(self):
        creds = ProjectCredentials(
            api_keys=[{"name": "test", "env": "MISSING_KEY_ABC"}]
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            missing = creds.validate()
        assert "MISSING_KEY_ABC" in missing

    def test_validate_passes_when_set(self):
        creds = ProjectCredentials(
            github_token_env="MY_TOKEN",
            api_keys=[{"name": "k", "env": "MY_KEY"}],
        )
        with mock.patch.dict(os.environ, {"MY_TOKEN": "t", "MY_KEY": "k"}):
            missing = creds.validate()
        assert missing == []

    def test_validate_empty_credentials(self):
        creds = ProjectCredentials()
        assert creds.validate() == []


# ── ProjectRegistry ─────────────────────────────────────────────────────────

class TestProjectRegistry:
    def test_loads_from_directory(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write_yaml(d, "proj.yaml", SAMPLE_YAML)
            reg = ProjectRegistry(projects_dir=d)
            assert len(reg.list_projects()) == 1
            assert reg.list_projects()[0].name == "TestProject"

    def test_get_by_name(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write_yaml(d, "proj.yaml", SAMPLE_YAML)
            reg = ProjectRegistry(projects_dir=d)
            assert reg.get("TestProject") is not None
            assert reg.get("NonExistent") is None

    def test_get_by_repo(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write_yaml(d, "proj.yaml", SAMPLE_YAML)
            reg = ProjectRegistry(projects_dir=d)
            assert reg.get_by_repo("org/test-repo") is not None
            assert reg.get_by_repo("org/nope") is None

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as td:
            reg = ProjectRegistry(projects_dir=Path(td))
            assert reg.list_projects() == []

    def test_nonexistent_directory_created(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "sub" / "projects"
            assert not d.exists()
            reg = ProjectRegistry(projects_dir=d)
            assert d.exists()
            assert reg.list_projects() == []

    def test_invalid_yaml_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write_yaml(d, "bad.yaml", INVALID_YAML)
            _write_yaml(d, "good.yaml", SAMPLE_YAML)
            reg = ProjectRegistry(projects_dir=d)
            # good one loaded, bad one skipped
            assert len(reg.list_projects()) == 1
            assert reg.list_projects()[0].name == "TestProject"

    def test_validate_all_returns_missing(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write_yaml(d, "proj.yaml", SAMPLE_YAML)
            reg = ProjectRegistry(projects_dir=d)
            with mock.patch.dict(os.environ, {}, clear=True):
                result = reg.validate_all()
            assert "TestProject" in result
            assert "GH_TOKEN_TEST" in result["TestProject"]

    def test_validate_all_clean_when_vars_set(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write_yaml(d, "proj.yaml", SAMPLE_YAML)
            reg = ProjectRegistry(projects_dir=d)
            env = {"GH_TOKEN_TEST": "x", "ANTHROPIC_API_KEY_TEST": "y"}
            with mock.patch.dict(os.environ, env, clear=True):
                result = reg.validate_all()
            assert result == {}

    def test_reload(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write_yaml(d, "proj.yaml", SAMPLE_YAML)
            reg = ProjectRegistry(projects_dir=d)
            assert len(reg.list_projects()) == 1
            _write_yaml(d, "proj2.yaml", MINIMAL_YAML)
            reg.reload()
            assert len(reg.list_projects()) == 2

    def test_multiple_projects_loaded(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write_yaml(d, "a.yaml", SAMPLE_YAML)
            _write_yaml(d, "b.yaml", MINIMAL_YAML)
            reg = ProjectRegistry(projects_dir=d)
            names = {p.name for p in reg.list_projects()}
            assert names == {"TestProject", "Minimal"}


# ── Budget fields ────────────────────────────────────────────────────────────

class TestProjectBudget:
    def test_defaults(self):
        b = ProjectBudget()
        assert b.daily_cap_usd == 0.0
        assert b.monthly_cap_usd == 0.0
        assert b.alert_threshold_pct == 80

    def test_parsed_from_yaml(self):
        import yaml as _yaml
        data = _yaml.safe_load(SAMPLE_YAML)
        proj = Project.from_dict(data)
        assert proj.budget.daily_cap_usd == 50.0
        assert proj.budget.monthly_cap_usd == 800.0
        assert proj.budget.alert_threshold_pct == 75


# ── ProjectInfra ─────────────────────────────────────────────────────────────

class TestProjectInfra:
    def test_defaults(self):
        i = ProjectInfra()
        assert i.ssh_config == ""
        assert i.workers == []

    def test_parsed_from_yaml(self):
        import yaml as _yaml
        data = _yaml.safe_load(SAMPLE_YAML)
        proj = Project.from_dict(data)
        assert proj.infrastructure.ssh_config == "config/ssh_test.conf"
        assert len(proj.infrastructure.workers) == 1
        assert proj.infrastructure.workers[0]["name"] == "worker-1"
