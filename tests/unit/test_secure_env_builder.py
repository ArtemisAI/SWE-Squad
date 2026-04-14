"""
Tests for the SecureEnvBuilder (DotenvEnvProvider) and EnvProvider protocol.

Validates role-based allowlists, blocked variable stripping, per-execution
overrides, and correct integration with subprocess env whitelisting.
"""
from __future__ import annotations

import os
from unittest import mock

import pytest

from src.swe_team.providers.env.base import (
    BLOCKED_ENV_VARS,
    DEFAULT_ALLOWLISTS,
    EnvProvider,
    EnvSpec,
)
from src.swe_team.providers.env.dotenv_provider import DotenvEnvProvider


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestEnvProviderProtocol:
    """Verify DotenvEnvProvider satisfies the EnvProvider protocol."""

    def test_is_instance_of_protocol(self):
        provider = DotenvEnvProvider()
        assert isinstance(provider, EnvProvider)

    def test_has_build_env(self):
        provider = DotenvEnvProvider()
        assert callable(getattr(provider, "build_env", None))

    def test_has_allowed_keys(self):
        provider = DotenvEnvProvider()
        assert callable(getattr(provider, "allowed_keys", None))

    def test_has_is_blocked(self):
        provider = DotenvEnvProvider()
        assert callable(getattr(provider, "is_blocked", None))

    def test_has_health_check(self):
        provider = DotenvEnvProvider()
        assert callable(getattr(provider, "health_check", None))


# ---------------------------------------------------------------------------
# Blocked variables
# ---------------------------------------------------------------------------

class TestBlockedVars:
    """BLOCKED_ENV_VARS must never leak into subprocess environments."""

    @pytest.mark.parametrize("blocked_var", sorted(BLOCKED_ENV_VARS))
    def test_blocked_vars_never_in_output_investigator(self, blocked_var):
        """No blocked var appears in investigator env, even if set in os.environ."""
        with mock.patch.dict(os.environ, {blocked_var: "SECRET_VALUE"}, clear=False):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="investigator"))
            assert blocked_var not in env

    @pytest.mark.parametrize("blocked_var", sorted(BLOCKED_ENV_VARS - {"ANTHROPIC_API_KEY"}))
    def test_blocked_vars_never_in_output_developer(self, blocked_var):
        """No blocked var (except those in allowlist) appears in developer env."""
        with mock.patch.dict(os.environ, {blocked_var: "SECRET_VALUE"}, clear=False):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="developer"))
            assert blocked_var not in env

    @pytest.mark.parametrize("blocked_var", sorted(BLOCKED_ENV_VARS))
    def test_blocked_vars_never_in_output_test_runner(self, blocked_var):
        """No blocked var appears in test_runner env."""
        with mock.patch.dict(os.environ, {blocked_var: "SECRET_VALUE"}, clear=False):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="test_runner"))
            assert blocked_var not in env

    def test_blocked_var_in_overrides_is_stripped(self):
        """Even if you try to inject a blocked var via overrides, it gets stripped."""
        provider = DotenvEnvProvider()
        env = provider.build_env(EnvSpec(
            role="investigator",
            overrides={"SUPABASE_ANON_KEY": "injected_secret"},
        ))
        assert "SUPABASE_ANON_KEY" not in env

    def test_multiple_blocked_vars_in_overrides_stripped(self):
        """Multiple blocked vars injected via overrides are all stripped."""
        provider = DotenvEnvProvider()
        env = provider.build_env(EnvSpec(
            role="test_runner",
            overrides={
                "TELEGRAM_BOT_TOKEN": "tok123",
                "WEBHOOK_SECRET": "sec456",
                "BASE_LLM_API_KEY": "key789",
            },
        ))
        for k in ("TELEGRAM_BOT_TOKEN", "WEBHOOK_SECRET", "BASE_LLM_API_KEY"):
            assert k not in env

    def test_is_blocked_returns_true_for_blocked(self):
        provider = DotenvEnvProvider()
        for var in BLOCKED_ENV_VARS:
            assert provider.is_blocked(var) is True

    def test_is_blocked_returns_false_for_safe(self):
        provider = DotenvEnvProvider()
        assert provider.is_blocked("PATH") is False
        assert provider.is_blocked("HOME") is False
        assert provider.is_blocked("GH_TOKEN") is False


# ---------------------------------------------------------------------------
# Role allowlists
# ---------------------------------------------------------------------------

class TestRoleAllowlists:
    """Each role should only see its allowlisted variables."""

    def test_role_allowlist_respected_investigator_no_gh_token(self):
        """Investigator role must NOT get GH_TOKEN."""
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghp_secret123"}, clear=False):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="investigator"))
            assert "GH_TOKEN" not in env

    def test_developer_gets_gh_token(self):
        """Developer role MUST get GH_TOKEN when set."""
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghp_secret123"}, clear=False):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="developer"))
            assert env.get("GH_TOKEN") == "ghp_secret123"

    def test_developer_gets_git_identity(self):
        """Developer gets GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, etc."""
        git_vars = {
            "GIT_AUTHOR_NAME": "SWE Bot",
            "GIT_AUTHOR_EMAIL": "bot@example.com",
            "GIT_COMMITTER_NAME": "SWE Bot",
            "GIT_COMMITTER_EMAIL": "bot@example.com",
        }
        with mock.patch.dict(os.environ, git_vars, clear=False):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="developer"))
            for k, v in git_vars.items():
                assert env.get(k) == v

    def test_claude_cli_gets_anthropic_api_key(self):
        """claude_cli role MUST get ANTHROPIC_API_KEY (it needs it to authenticate)."""
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-123"}, clear=False):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="claude_cli"))
            assert env.get("ANTHROPIC_API_KEY") == "sk-ant-123"

    def test_investigator_does_not_get_anthropic_key(self):
        """Investigator should NOT get ANTHROPIC_API_KEY (it's blocked for non-claude_cli)."""
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-123"}, clear=False):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="investigator"))
            assert "ANTHROPIC_API_KEY" not in env

    def test_reviewer_gets_gh_token(self):
        """Reviewer needs GH_TOKEN for PR reviews."""
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghp_review"}, clear=False):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="reviewer"))
            assert env.get("GH_TOKEN") == "ghp_review"

    def test_test_runner_excludes_secrets_and_dev_vars(self):
        """test_runner must NOT get GH_TOKEN, ANTHROPIC_API_KEY, or SWE_GITHUB_REPO."""
        extras = {
            "GH_TOKEN": "ghp_nope",
            "ANTHROPIC_API_KEY": "sk-nope",
            "SWE_GITHUB_REPO": "nope",
        }
        with mock.patch.dict(os.environ, extras, clear=False):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="test_runner"))
            assert "GH_TOKEN" not in env
            assert "ANTHROPIC_API_KEY" not in env
            assert "SWE_GITHUB_REPO" not in env

    @pytest.mark.parametrize("var,value", [
        ("PYTHONDONTWRITEBYTECODE", "1"),
        ("PYTHONUNBUFFERED", "1"),
        ("VIRTUAL_ENV", "/home/user/.venv"),
        ("PYTEST_CURRENT_TEST", "tests/test_foo.py::test_bar"),
        ("NODE_ENV", "test"),
        ("NODE_PATH", "/usr/lib/node_modules"),
        ("NODE_OPTIONS", "--max-old-space-size=4096"),
        ("NPM_CONFIG_PREFIX", "/home/user/.npm-global"),
        ("CI", "true"),
        ("TERM", "xterm-256color"),
        ("TZ", "UTC"),
        ("TMPDIR", "/tmp"),
        ("TEMP", "/tmp"),
        ("TMP", "/tmp"),
        ("GOPATH", "/home/user/go"),
        ("GOROOT", "/usr/local/go"),
        ("GOCACHE", "/home/user/.cache/go-build"),
        ("DATABASE_URL", "postgres://localhost/testdb"),
        ("TEST_DATABASE_URL", "postgres://localhost/testdb"),
        ("REDIS_URL", "redis://localhost:6379"),
        ("COVERAGE_FILE", ".coverage"),
        ("COVERAGE_RCFILE", ".coveragerc"),
    ])
    def test_test_runner_gets_enriched_env_var(self, var, value):
        """test_runner allowlist includes commonly needed test framework vars."""
        with mock.patch.dict(os.environ, {var: value}, clear=False):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="test_runner"))
            assert env.get(var) == value

    def test_allowed_keys_returns_correct_list(self):
        provider = DotenvEnvProvider()
        keys = provider.allowed_keys("developer")
        assert "GH_TOKEN" in keys
        assert "GIT_AUTHOR_NAME" in keys

    def test_allowed_keys_unknown_role(self):
        provider = DotenvEnvProvider()
        keys = provider.allowed_keys("nonexistent_role")
        assert "PATH" in keys
        assert "HOME" in keys
        assert len(keys) == 2


# ---------------------------------------------------------------------------
# PATH and HOME always present
# ---------------------------------------------------------------------------

class TestMinimalEnv:
    """PATH and HOME must always be present regardless of role."""

    def test_path_always_present(self):
        provider = DotenvEnvProvider()
        for role in DEFAULT_ALLOWLISTS:
            env = provider.build_env(EnvSpec(role=role))
            assert "PATH" in env

    def test_home_always_present(self):
        provider = DotenvEnvProvider()
        for role in DEFAULT_ALLOWLISTS:
            env = provider.build_env(EnvSpec(role=role))
            assert "HOME" in env

    def test_unknown_role_gets_minimal_env(self):
        """Unknown role gets PATH + HOME only, no secrets."""
        extras = {
            "GH_TOKEN": "ghp_secret",
            "SUPABASE_ANON_KEY": "supakey",
            "ANTHROPIC_API_KEY": "sk-ant",
        }
        with mock.patch.dict(os.environ, extras, clear=False):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="totally_unknown"))
            assert "PATH" in env
            assert "HOME" in env
            assert "GH_TOKEN" not in env
            assert "SUPABASE_ANON_KEY" not in env
            assert "ANTHROPIC_API_KEY" not in env

    def test_path_fallback_when_not_in_environ(self):
        """PATH gets a safe fallback if not in os.environ."""
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="investigator"))
            assert "PATH" in env
            assert env["PATH"] == "/usr/bin:/bin"

    def test_home_fallback_when_not_in_environ(self):
        """HOME gets /tmp fallback if not in os.environ."""
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = DotenvEnvProvider()
            env = provider.build_env(EnvSpec(role="investigator"))
            assert env["HOME"] == "/tmp"


# ---------------------------------------------------------------------------
# Per-execution overrides
# ---------------------------------------------------------------------------

class TestOverrides:
    """Overrides allow injecting extra vars for a single call."""

    def test_per_exec_overrides_work(self):
        """Overrides dict adds vars for that call only."""
        provider = DotenvEnvProvider()
        env = provider.build_env(EnvSpec(
            role="investigator",
            overrides={"CUSTOM_VAR": "custom_value"},
        ))
        assert env.get("CUSTOM_VAR") == "custom_value"

    def test_overrides_do_not_persist(self):
        """Overrides from one call don't affect the next."""
        provider = DotenvEnvProvider()
        env1 = provider.build_env(EnvSpec(
            role="investigator",
            overrides={"TEMP_VAR": "temp_value"},
        ))
        assert "TEMP_VAR" in env1

        env2 = provider.build_env(EnvSpec(role="investigator"))
        assert "TEMP_VAR" not in env2

    def test_overrides_can_set_role_vars(self):
        """Overrides can provide values for allowlisted vars."""
        provider = DotenvEnvProvider()
        env = provider.build_env(EnvSpec(
            role="developer",
            overrides={"GH_TOKEN": "override_token"},
        ))
        assert env.get("GH_TOKEN") == "override_token"


# ---------------------------------------------------------------------------
# Config-based allowlist extension
# ---------------------------------------------------------------------------

class TestConfigAllowlists:
    """Config allowlists extend (not replace) the defaults."""

    def test_config_extends_defaults(self):
        """Custom config adds vars to an existing role."""
        provider = DotenvEnvProvider(
            config_allowlists={"investigator": ["EXTRA_VAR"]},
        )
        keys = provider.allowed_keys("investigator")
        assert "EXTRA_VAR" in keys
        assert "PATH" in keys  # default still there

    def test_config_adds_new_role(self):
        """Custom config can define entirely new roles."""
        provider = DotenvEnvProvider(
            config_allowlists={"custom_role": ["PATH", "HOME", "CUSTOM_KEY"]},
        )
        keys = provider.allowed_keys("custom_role")
        assert "CUSTOM_KEY" in keys

    def test_config_does_not_duplicate_keys(self):
        """Adding a key that already exists doesn't create duplicates."""
        provider = DotenvEnvProvider(
            config_allowlists={"investigator": ["PATH", "HOME"]},
        )
        keys = provider.allowed_keys("investigator")
        assert keys.count("PATH") == 1
        assert keys.count("HOME") == 1


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealthCheck:
    """Health check always returns True for dotenv provider."""

    def test_health_check_returns_true(self):
        provider = DotenvEnvProvider()
        assert provider.health_check() is True


# ---------------------------------------------------------------------------
# strip_blocked=False escape hatch
# ---------------------------------------------------------------------------

class TestStripBlockedFlag:
    """When strip_blocked=False, blocked vars are not stripped."""

    def test_strip_blocked_false_keeps_blocked_in_overrides(self):
        """With strip_blocked=False, blocked vars from overrides survive."""
        provider = DotenvEnvProvider()
        env = provider.build_env(EnvSpec(
            role="investigator",
            overrides={"SUPABASE_ANON_KEY": "keep_me"},
            strip_blocked=False,
        ))
        assert env.get("SUPABASE_ANON_KEY") == "keep_me"


# ---------------------------------------------------------------------------
# EnvSpec dataclass
# ---------------------------------------------------------------------------

class TestEnvSpec:
    """EnvSpec dataclass defaults."""

    def test_default_overrides_empty(self):
        spec = EnvSpec(role="developer")
        assert spec.overrides == {}

    def test_default_strip_blocked_true(self):
        spec = EnvSpec(role="developer")
        assert spec.strip_blocked is True

    def test_role_is_required(self):
        with pytest.raises(TypeError):
            EnvSpec()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# DEFAULT_ALLOWLISTS coverage
# ---------------------------------------------------------------------------

class TestDefaultAllowlists:
    """Verify DEFAULT_ALLOWLISTS has expected roles and keys."""

    def test_all_expected_roles_present(self):
        expected_roles = {"investigator", "developer", "test_runner", "reviewer", "claude_cli"}
        assert set(DEFAULT_ALLOWLISTS.keys()) == expected_roles

    def test_all_roles_have_path(self):
        for role, keys in DEFAULT_ALLOWLISTS.items():
            assert "PATH" in keys, f"Role {role} missing PATH"

    def test_all_roles_have_home(self):
        for role, keys in DEFAULT_ALLOWLISTS.items():
            assert "HOME" in keys, f"Role {role} missing HOME"

    def test_claude_cli_has_anthropic_key(self):
        assert "ANTHROPIC_API_KEY" in DEFAULT_ALLOWLISTS["claude_cli"]

    def test_developer_has_gh_token(self):
        assert "GH_TOKEN" in DEFAULT_ALLOWLISTS["developer"]

    def test_investigator_no_gh_token(self):
        assert "GH_TOKEN" not in DEFAULT_ALLOWLISTS["investigator"]

    def test_test_runner_has_enriched_vars(self):
        """test_runner allowlist includes Python, Node, CI, Go, DB, and coverage vars."""
        tr = DEFAULT_ALLOWLISTS["test_runner"]
        expected = [
            "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "VIRTUAL_ENV",
            "PYTEST_CURRENT_TEST",
            "NODE_ENV", "NODE_PATH", "NODE_OPTIONS", "NPM_CONFIG_PREFIX",
            "CI", "TERM", "TZ", "TMPDIR", "TEMP", "TMP",
            "GOPATH", "GOROOT", "GOCACHE",
            "DATABASE_URL", "TEST_DATABASE_URL", "REDIS_URL",
            "COVERAGE_FILE", "COVERAGE_RCFILE",
        ]
        for var in expected:
            assert var in tr, f"test_runner missing {var}"

    def test_test_runner_enriched_vars_not_in_investigator(self):
        """Enriched test vars should not leak into other roles."""
        inv = DEFAULT_ALLOWLISTS["investigator"]
        for var in ("PYTEST_CURRENT_TEST", "NODE_ENV", "GOPATH", "DATABASE_URL"):
            assert var not in inv


# ---------------------------------------------------------------------------
# BLOCKED_ENV_VARS coverage
# ---------------------------------------------------------------------------

class TestBlockedEnvVarsSet:
    """Verify BLOCKED_ENV_VARS contains expected secrets."""

    def test_contains_supabase_key(self):
        assert "SUPABASE_ANON_KEY" in BLOCKED_ENV_VARS

    def test_contains_telegram_token(self):
        assert "TELEGRAM_BOT_TOKEN" in BLOCKED_ENV_VARS

    def test_contains_webhook_secret(self):
        assert "WEBHOOK_SECRET" in BLOCKED_ENV_VARS

    def test_contains_anthropic_key(self):
        assert "ANTHROPIC_API_KEY" in BLOCKED_ENV_VARS

    def test_contains_proxmoxai_key(self):
        assert "PROXMOXAI_API_KEY" in BLOCKED_ENV_VARS

    def test_contains_base_llm_key(self):
        assert "BASE_LLM_API_KEY" in BLOCKED_ENV_VARS

    def test_is_frozenset(self):
        assert isinstance(BLOCKED_ENV_VARS, frozenset)
