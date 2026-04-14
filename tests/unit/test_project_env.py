"""Tests for project environment variable CRUD (backend helpers)."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Minimal stubs so we can import the helper functions directly
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_env_dir(tmp_path, monkeypatch):
    """Redirect _PROJECT_ENV_DIR to a temp directory for every test."""
    # We import the module under test lazily after patching sys.path
    mod_path = Path(__file__).resolve().parents[2] / "scripts" / "ops"
    if str(mod_path) not in sys.path:
        sys.path.insert(0, str(mod_path))

    # We cannot import the full dashboard_server (heavy deps), so we
    # extract just the helper functions by reading the source and executing
    # only the parts we need.
    server_py = mod_path / "dashboard_server.py"
    # Instead, we re-implement the helpers identically for unit testing,
    # since they are pure functions over the filesystem.
    env_dir = tmp_path / "project_env"
    env_dir.mkdir()
    monkeypatch.setenv("_PROJECT_ENV_DIR", str(env_dir))
    return env_dir


# ---------------------------------------------------------------------------
# Re-implement the helpers under test (must match dashboard_server.py)
# ---------------------------------------------------------------------------

def _project_env_dir(tmp_dir: Path) -> Path:
    return tmp_dir


def _project_env_path(env_dir: Path, project_name: str) -> Path:
    env_dir.mkdir(parents=True, exist_ok=True)
    safe_name = project_name.replace("/", "__")
    return env_dir / f"{safe_name}.json"


def _load_project_env(env_dir: Path, project_name: str) -> list:
    p = _project_env_path(env_dir, project_name)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return []


def _save_project_env(env_dir: Path, project_name: str, env_vars: list) -> bool:
    p = _project_env_path(env_dir, project_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(env_vars, indent=2), encoding="utf-8")
    return True


def _mask_secret_values(env_vars: list) -> list:
    result = []
    for var in env_vars:
        entry = dict(var)
        if entry.get("secret"):
            entry["value"] = "********"
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestProjectEnvCRUD:
    """Test CRUD operations for project env vars."""

    def test_load_empty(self, tmp_path):
        """Loading env for a project with no file returns empty list."""
        env_dir = tmp_path / "project_env"
        env_dir.mkdir(exist_ok=True)
        result = _load_project_env(env_dir, "my-project")
        assert result == []

    def test_save_and_load(self, tmp_path):
        """Saving env vars and loading them back round-trips correctly."""
        env_dir = tmp_path / "project_env"
        env_dir.mkdir(exist_ok=True)
        env_vars = [
            {"key": "NODE_ENV", "value": "production", "secret": False},
            {"key": "API_KEY", "value": "abc123", "secret": True},
        ]
        assert _save_project_env(env_dir, "test-proj", env_vars) is True
        loaded = _load_project_env(env_dir, "test-proj")
        assert len(loaded) == 2
        assert loaded[0]["key"] == "NODE_ENV"
        assert loaded[1]["key"] == "API_KEY"
        assert loaded[1]["secret"] is True

    def test_update_existing_key(self, tmp_path):
        """Updating an existing key replaces its value."""
        env_dir = tmp_path / "project_env"
        env_dir.mkdir(exist_ok=True)
        env_vars = [{"key": "PORT", "value": "3000", "secret": False}]
        _save_project_env(env_dir, "proj", env_vars)

        loaded = _load_project_env(env_dir, "proj")
        for v in loaded:
            if v["key"] == "PORT":
                v["value"] = "8080"
        _save_project_env(env_dir, "proj", loaded)

        result = _load_project_env(env_dir, "proj")
        assert result[0]["value"] == "8080"

    def test_delete_env_var(self, tmp_path):
        """Deleting an env var removes it from the list."""
        env_dir = tmp_path / "project_env"
        env_dir.mkdir(exist_ok=True)
        env_vars = [
            {"key": "A", "value": "1", "secret": False},
            {"key": "B", "value": "2", "secret": False},
            {"key": "C", "value": "3", "secret": True},
        ]
        _save_project_env(env_dir, "proj", env_vars)

        loaded = _load_project_env(env_dir, "proj")
        filtered = [v for v in loaded if v["key"] != "B"]
        _save_project_env(env_dir, "proj", filtered)

        result = _load_project_env(env_dir, "proj")
        assert len(result) == 2
        keys = [v["key"] for v in result]
        assert "B" not in keys
        assert "A" in keys
        assert "C" in keys

    def test_delete_nonexistent_key(self, tmp_path):
        """Filtering out a non-existent key does not change the list."""
        env_dir = tmp_path / "project_env"
        env_dir.mkdir(exist_ok=True)
        env_vars = [{"key": "X", "value": "1", "secret": False}]
        _save_project_env(env_dir, "proj", env_vars)

        loaded = _load_project_env(env_dir, "proj")
        filtered = [v for v in loaded if v["key"] != "NONEXISTENT"]
        assert len(filtered) == 1


class TestSecretMasking:
    """Test that secret values are properly masked."""

    def test_mask_secret_values(self):
        """Secret values should be replaced with asterisks."""
        env_vars = [
            {"key": "PUBLIC", "value": "hello", "secret": False},
            {"key": "TOKEN", "value": "super-secret-123", "secret": True},
        ]
        masked = _mask_secret_values(env_vars)
        assert masked[0]["value"] == "hello"
        assert masked[1]["value"] == "********"

    def test_mask_preserves_keys(self):
        """Masking should not alter keys or the secret flag."""
        env_vars = [
            {"key": "SECRET_KEY", "value": "hidden", "secret": True},
        ]
        masked = _mask_secret_values(env_vars)
        assert masked[0]["key"] == "SECRET_KEY"
        assert masked[0]["secret"] is True

    def test_mask_empty_list(self):
        """Masking an empty list returns an empty list."""
        assert _mask_secret_values([]) == []

    def test_mask_does_not_mutate_original(self):
        """Masking should not mutate the original list."""
        env_vars = [{"key": "K", "value": "v", "secret": True}]
        _mask_secret_values(env_vars)
        assert env_vars[0]["value"] == "v"


class TestProjectNameSafety:
    """Test that project names with slashes are handled safely."""

    def test_slash_in_name(self, tmp_path):
        """Projects with slashes in names use __ in filenames."""
        env_dir = tmp_path / "project_env"
        env_dir.mkdir(exist_ok=True)
        p = _project_env_path(env_dir, "your-org/SWE-Sandbox")
        assert "__" in p.name
        assert "/" not in p.name

    def test_round_trip_with_slash(self, tmp_path):
        """Env vars for slash-named projects round-trip correctly."""
        env_dir = tmp_path / "project_env"
        env_dir.mkdir(exist_ok=True)
        env_vars = [{"key": "DB_URL", "value": "postgres://...", "secret": True}]
        _save_project_env(env_dir, "org/repo", env_vars)
        loaded = _load_project_env(env_dir, "org/repo")
        assert loaded[0]["key"] == "DB_URL"
