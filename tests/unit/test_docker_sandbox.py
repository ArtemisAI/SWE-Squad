"""Tests for DockerSandboxProvider — all subprocess calls are mocked."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from src.swe_team.providers.sandbox.docker import (
    DockerSandbox,
    _BLOCKED_ENV_VARS,
    _parse_tag,
    from_config,
)
from src.swe_team.providers.sandbox.base import SandboxInfo, SandboxSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def provider() -> DockerSandbox:
    """Create a DockerSandbox with defaults, bypassing health_check."""
    return DockerSandbox()


@pytest.fixture
def spec() -> SandboxSpec:
    """Basic sandbox spec for testing."""
    return SandboxSpec(
        name="test-sandbox",
        cpu=2,
        ram_gb=4,
        disk_gb=20,
        env_vars={"APP_ENV": "test", "DEBUG": "1"},
        tags=["workspace:/tmp/myrepo"],
    )


def _mock_run_ok(stdout: str = "", stderr: str = "") -> MagicMock:
    """Return a mock CompletedProcess with returncode 0."""
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = 0
    m.stdout = stdout
    m.stderr = stderr
    return m


def _mock_run_fail(stderr: str = "error") -> MagicMock:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = 1
    m.stdout = ""
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------

class TestHealthCheck:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_health_check_true_when_docker_available(self, mock_run, provider):
        mock_run.return_value = _mock_run_ok()
        assert provider.health_check() is True
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["docker", "info"]

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_health_check_false_when_docker_missing(self, mock_run, provider):
        mock_run.return_value = _mock_run_fail()
        assert provider.health_check() is False

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_health_check_false_on_file_not_found(self, mock_run, provider):
        mock_run.side_effect = FileNotFoundError("docker not found")
        assert provider.health_check() is False

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_health_check_false_on_timeout(self, mock_run, provider):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker info", timeout=10)
        assert provider.health_check() is False


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

class TestCreate:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_create_builds_correct_docker_run_command(self, mock_uuid, mock_run, provider, spec):
        mock_uuid.return_value = MagicMock(hex="abcdef1234567890abcdef1234567890")
        mock_run.return_value = _mock_run_ok(stdout="container_id_123\n")

        info = provider.create(spec)

        assert info.sandbox_id == "abcdef1234567890abcdef1234567890"
        assert info.status == "running"
        assert info.provider == "docker"
        assert info.name == "test-sandbox"

        cmd = mock_run.call_args[0][0]
        assert "docker" in cmd
        assert "run" in cmd
        assert "-d" in cmd
        assert "--network=none" in cmd
        assert "--cpus=2" in cmd
        assert "--memory=4096m" in cmd  # 4 GB * 1024
        assert "--name" in cmd
        assert "swe-sandbox-abcdef123456" in cmd

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_env_vars_passed_as_env_flags(self, mock_uuid, mock_run, provider, spec):
        mock_uuid.return_value = MagicMock(hex="a" * 32)
        mock_run.return_value = _mock_run_ok()

        provider.create(spec)
        cmd = mock_run.call_args[0][0]

        # Find all --env flags
        env_flags = []
        for i, arg in enumerate(cmd):
            if arg == "--env" and i + 1 < len(cmd):
                env_flags.append(cmd[i + 1])

        assert "APP_ENV=test" in env_flags
        assert "DEBUG=1" in env_flags

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_blocked_vars_not_in_docker_run(self, mock_uuid, mock_run, provider):
        mock_uuid.return_value = MagicMock(hex="b" * 32)
        mock_run.return_value = _mock_run_ok()

        blocked_spec = SandboxSpec(
            name="blocked-test",
            env_vars={
                "SAFE_VAR": "ok",
                "SUPABASE_ANON_KEY": "secret123",
                "GH_TOKEN": "ghp_xxx",
                "TELEGRAM_BOT_TOKEN": "bot_tok",
            },
        )
        provider.create(blocked_spec)
        cmd_str = " ".join(mock_run.call_args[0][0])

        assert "SAFE_VAR=ok" in cmd_str
        assert "SUPABASE_ANON_KEY" not in cmd_str
        assert "GH_TOKEN" not in cmd_str
        assert "TELEGRAM_BOT_TOKEN" not in cmd_str

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_no_privileged_flag_ever(self, mock_uuid, mock_run, provider, spec):
        mock_uuid.return_value = MagicMock(hex="c" * 32)
        mock_run.return_value = _mock_run_ok()

        provider.create(spec)
        cmd = mock_run.call_args[0][0]
        assert "--privileged" not in cmd

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_workspace_mount_when_path_in_tags(self, mock_uuid, mock_run, provider, spec):
        mock_uuid.return_value = MagicMock(hex="d" * 32)
        mock_run.return_value = _mock_run_ok()

        provider.create(spec)
        cmd = mock_run.call_args[0][0]

        # Find -v mount flag
        v_idx = cmd.index("-v")
        mount_arg = cmd[v_idx + 1]
        assert mount_arg == "/tmp/myrepo:/workspace"

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_no_workspace_mount_when_no_tag(self, mock_uuid, mock_run, provider):
        mock_uuid.return_value = MagicMock(hex="e" * 32)
        mock_run.return_value = _mock_run_ok()

        spec_no_ws = SandboxSpec(name="no-ws")
        provider.create(spec_no_ws)
        cmd = mock_run.call_args[0][0]
        assert "-v" not in cmd

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_env_file_mount_readonly(self, mock_uuid, mock_run, provider):
        mock_uuid.return_value = MagicMock(hex="f" * 32)
        mock_run.return_value = _mock_run_ok()

        spec = SandboxSpec(name="env-file-test", tags=["env_file:/tmp/.env"])
        provider.create(spec)
        cmd = mock_run.call_args[0][0]

        # Find -v flags (there may be multiple)
        v_indices = [i for i, x in enumerate(cmd) if x == "-v"]
        mount_args = [cmd[i + 1] for i in v_indices]
        assert "/tmp/.env:/workspace/.env:ro" in mount_args

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_create_raises_on_docker_failure(self, mock_uuid, mock_run, provider, spec):
        mock_uuid.return_value = MagicMock(hex="a" * 32)
        mock_run.return_value = _mock_run_fail("daemon not running")

        with pytest.raises(RuntimeError, match="Docker container creation failed"):
            provider.create(spec)

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_create_uses_custom_image(self, mock_uuid, mock_run):
        mock_uuid.return_value = MagicMock(hex="a" * 32)
        mock_run.return_value = _mock_run_ok()

        p = DockerSandbox(image="node:20-slim")
        p.create(SandboxSpec(name="node-test"))
        cmd = mock_run.call_args[0][0]
        assert "node:20-slim" in cmd

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_create_uses_custom_network(self, mock_uuid, mock_run):
        mock_uuid.return_value = MagicMock(hex="a" * 32)
        mock_run.return_value = _mock_run_ok()

        p = DockerSandbox(network="bridge")
        p.create(SandboxSpec(name="bridge-test"))
        cmd = mock_run.call_args[0][0]
        assert "--network=bridge" in cmd

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_create_security_opt_no_new_privileges(self, mock_uuid, mock_run, provider, spec):
        mock_uuid.return_value = MagicMock(hex="a" * 32)
        mock_run.return_value = _mock_run_ok()

        provider.create(spec)
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--security-opt")
        assert cmd[idx + 1] == "no-new-privileges"

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_container_registered_after_create(self, mock_uuid, mock_run, provider, spec):
        mock_uuid.return_value = MagicMock(hex="a" * 32)
        mock_run.return_value = _mock_run_ok()

        info = provider.create(spec)
        assert info.sandbox_id in provider._containers


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------

class TestRunCommand:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_run_command_uses_docker_exec(self, mock_run, provider):
        mock_run.return_value = _mock_run_ok(stdout="hello\n")
        provider._containers["abc123"] = "swe-sandbox-abc123"

        rc, out, err = provider.run_command("abc123", ["echo", "hello"])

        assert rc == 0
        assert out == "hello\n"
        cmd = mock_run.call_args[0][0]
        assert cmd[0:2] == ["docker", "exec"]
        assert "swe-sandbox-abc123" in cmd
        assert "echo" in cmd
        assert "hello" in cmd

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_per_exec_env_overrides(self, mock_run, provider):
        mock_run.return_value = _mock_run_ok()
        provider._containers["abc123"] = "swe-sandbox-abc123"

        provider.run_command("abc123", ["test"], env={"CUSTOM": "val1"})
        cmd = mock_run.call_args[0][0]

        # Find -e flags
        e_flags = []
        for i, arg in enumerate(cmd):
            if arg == "-e" and i + 1 < len(cmd):
                e_flags.append(cmd[i + 1])
        assert "CUSTOM=val1" in e_flags

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_per_exec_env_blocks_secrets(self, mock_run, provider):
        mock_run.return_value = _mock_run_ok()
        provider._containers["abc123"] = "swe-sandbox-abc123"

        provider.run_command(
            "abc123", ["test"], env={"SAFE": "ok", "GH_TOKEN": "secret"}
        )
        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "SAFE=ok" in cmd_str
        assert "GH_TOKEN" not in cmd_str

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_timeout_stops_container(self, mock_run, provider):
        provider._containers["abc123"] = "swe-sandbox-abc123"

        # First call (exec) times out, second call (stop) succeeds
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="docker exec", timeout=5),
            _mock_run_ok(),  # docker stop
        ]

        with pytest.raises(TimeoutError, match="timed out"):
            provider.run_command("abc123", ["slow-command"], timeout=5)

        # Verify docker stop was called
        assert mock_run.call_count == 2
        stop_cmd = mock_run.call_args_list[1][0][0]
        assert stop_cmd[0:2] == ["docker", "stop"]
        assert "swe-sandbox-abc123" in stop_cmd

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_run_command_fallback_container_name(self, mock_run, provider):
        """When sandbox_id is not in _containers, use naming convention."""
        mock_run.return_value = _mock_run_ok()
        provider.run_command("abcdef1234567890", ["ls"])
        cmd = mock_run.call_args[0][0]
        assert "swe-sandbox-abcdef123456" in cmd


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

class TestSnapshot:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_snapshot_calls_docker_commit(self, mock_run, provider):
        mock_run.return_value = _mock_run_ok(stdout="sha256:abc123\n")
        provider._containers["abc123"] = "swe-sandbox-abc123"

        tag = provider.snapshot("abc123", "before-fix")
        cmd = mock_run.call_args[0][0]

        assert cmd[0:2] == ["docker", "commit"]
        assert "swe-sandbox-abc123" in cmd
        assert "before-fix" in tag
        assert tag.startswith("swe-snapshot-")

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_snapshot_raises_on_failure(self, mock_run, provider):
        mock_run.return_value = _mock_run_fail("no such container")
        provider._containers["abc123"] = "swe-sandbox-abc123"

        with pytest.raises(RuntimeError, match="Docker snapshot failed"):
            provider.snapshot("abc123", "fail-snap")


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

class TestRollback:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_rollback_removes_and_restarts(self, mock_run, provider):
        mock_run.return_value = _mock_run_ok()
        provider._containers["abc123"] = "swe-sandbox-abc123"

        provider.rollback("abc123", "swe-snapshot-abc1-label-12345")

        assert mock_run.call_count == 2
        # First call: docker rm -f
        rm_cmd = mock_run.call_args_list[0][0][0]
        assert rm_cmd[0:2] == ["docker", "rm"]
        assert "-f" in rm_cmd

        # Second call: docker run from snapshot
        run_cmd = mock_run.call_args_list[1][0][0]
        assert run_cmd[0:2] == ["docker", "run"]
        assert "swe-snapshot-abc1-label-12345" in run_cmd

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_rollback_raises_on_run_failure(self, mock_run, provider):
        mock_run.side_effect = [_mock_run_ok(), _mock_run_fail("image not found")]
        provider._containers["abc123"] = "swe-sandbox-abc123"

        with pytest.raises(RuntimeError, match="Docker rollback failed"):
            provider.rollback("abc123", "bad-snapshot")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

class TestDelete:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_delete_calls_docker_rm_force(self, mock_run, provider):
        mock_run.return_value = _mock_run_ok()
        provider._containers["abc123"] = "swe-sandbox-abc123"

        provider.delete("abc123")
        cmd = mock_run.call_args[0][0]
        assert cmd == ["docker", "rm", "-f", "swe-sandbox-abc123"]

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_delete_removes_from_registry(self, mock_run, provider):
        mock_run.return_value = _mock_run_ok()
        provider._containers["abc123"] = "swe-sandbox-abc123"

        provider.delete("abc123")
        assert "abc123" not in provider._containers

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_delete_tolerates_failure(self, mock_run, provider):
        mock_run.side_effect = Exception("docker not running")
        provider._containers["abc123"] = "swe-sandbox-abc123"

        # Should not raise
        provider.delete("abc123")
        assert "abc123" not in provider._containers


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

class TestStatus:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_status_running(self, mock_run, provider):
        mock_run.return_value = _mock_run_ok(stdout="running\n")
        provider._containers["abc123"] = "swe-sandbox-abc123"

        info = provider.status("abc123")
        assert info.status == "running"
        assert info.provider == "docker"

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_status_exited_maps_to_stopped(self, mock_run, provider):
        mock_run.return_value = _mock_run_ok(stdout="exited\n")
        provider._containers["abc123"] = "swe-sandbox-abc123"

        info = provider.status("abc123")
        assert info.status == "stopped"

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_status_deleted_when_inspect_fails(self, mock_run, provider):
        mock_run.return_value = _mock_run_fail()
        provider._containers["abc123"] = "swe-sandbox-abc123"

        info = provider.status("abc123")
        assert info.status == "deleted"


# ---------------------------------------------------------------------------
# from_config factory
# ---------------------------------------------------------------------------

class TestFromConfig:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_from_config_defaults(self, mock_run):
        mock_run.return_value = _mock_run_ok()  # health_check
        p = from_config({})
        assert p._image == "python:3.11-slim"
        assert p._network == "none"
        assert p._memory_mb == 512
        assert p._cpu_limit == 1.0

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_from_config_custom(self, mock_run):
        mock_run.return_value = _mock_run_ok()
        p = from_config({
            "image": "node:20",
            "network": "bridge",
            "cpu_limit": 2.0,
            "memory_mb": 1024,
        })
        assert p._image == "node:20"
        assert p._network == "bridge"
        assert p._cpu_limit == 2.0
        assert p._memory_mb == 1024


# ---------------------------------------------------------------------------
# _parse_tag helper
# ---------------------------------------------------------------------------

class TestParseTag:
    def test_parse_existing_tag(self):
        assert _parse_tag(["workspace:/tmp/repo", "other:val"], "workspace") == "/tmp/repo"

    def test_parse_missing_tag(self):
        assert _parse_tag(["other:val"], "workspace") is None

    def test_parse_empty_tags(self):
        assert _parse_tag([], "workspace") is None


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

class TestProtocolCompliance:
    def test_has_name_attribute(self, provider):
        assert provider.name == "docker"

    def test_has_all_protocol_methods(self, provider):
        required = ["create", "status", "run_command", "snapshot", "rollback", "delete", "health_check"]
        for method in required:
            assert hasattr(provider, method), f"Missing method: {method}"
            assert callable(getattr(provider, method))

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_create_returns_sandbox_info(self, mock_uuid, mock_run, provider, spec):
        mock_uuid.return_value = MagicMock(hex="a" * 32)
        mock_run.return_value = _mock_run_ok()
        result = provider.create(spec)
        assert isinstance(result, SandboxInfo)

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_status_returns_sandbox_info(self, mock_run, provider):
        mock_run.return_value = _mock_run_ok(stdout="running\n")
        provider._containers["x"] = "swe-sandbox-x"
        result = provider.status("x")
        assert isinstance(result, SandboxInfo)


# ---------------------------------------------------------------------------
# Blocked env vars coverage
# ---------------------------------------------------------------------------

class TestBlockedEnvVars:
    def test_all_critical_secrets_are_blocked(self):
        """Verify the blocked list covers all critical secret env vars."""
        critical = {
            "SUPABASE_ANON_KEY", "GH_TOKEN", "GITHUB_TOKEN",
            "TELEGRAM_BOT_TOKEN", "BASE_LLM_API_KEY", "PROXMOXAI_API_KEY",
            "WEBHOOK_SECRET",
        }
        assert critical.issubset(_BLOCKED_ENV_VARS)

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_no_blocked_var_appears_in_any_create_command(self, mock_uuid, mock_run, provider):
        """Exhaustive: try injecting every blocked var and verify none appear."""
        mock_uuid.return_value = MagicMock(hex="a" * 32)
        mock_run.return_value = _mock_run_ok()

        env_all_blocked = {k: "secret" for k in _BLOCKED_ENV_VARS}
        env_all_blocked["SAFE_VAR"] = "ok"
        spec = SandboxSpec(name="blocked-all", env_vars=env_all_blocked)

        provider.create(spec)
        cmd_str = " ".join(mock_run.call_args[0][0])
        for blocked in _BLOCKED_ENV_VARS:
            assert blocked not in cmd_str
        assert "SAFE_VAR=ok" in cmd_str


# ---------------------------------------------------------------------------
# Docker socket never mounted
# ---------------------------------------------------------------------------

class TestSecurityGuardrails:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_docker_socket_never_mounted(self, mock_uuid, mock_run, provider, spec):
        mock_uuid.return_value = MagicMock(hex="a" * 32)
        mock_run.return_value = _mock_run_ok()

        provider.create(spec)
        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "/var/run/docker.sock" not in cmd_str

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid.uuid4")
    def test_no_privileged_in_rollback(self, mock_uuid, mock_run, provider):
        mock_run.return_value = _mock_run_ok()
        provider._containers["abc123"] = "swe-sandbox-abc123"
        provider.rollback("abc123", "some-snapshot")

        for c in mock_run.call_args_list:
            cmd_str = " ".join(c[0][0])
            assert "--privileged" not in cmd_str
