"""
Tests for sandbox providers: LocalSandbox, DockerSandbox, ProxmoxSandbox,
and the provider registry (__init__.py).

Covers:
  1. Protocol compliance (all implementations satisfy SandboxProvider)
  2. create_sandbox (subprocess mocked)
  3. destroy_sandbox (subprocess mocked)
  4. list_sandboxes / status (subprocess mocked)
  5. exec_in_sandbox / run_command (subprocess mocked)
  6. Error handling (failures handled gracefully)
  7. Config validation (from_config factories)
  8. Registry (create_sandbox_provider, list_sandbox_providers)
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.swe_team.providers.sandbox.base import (
    SandboxInfo,
    SandboxProvider,
    SandboxSpec,
)
from src.swe_team.providers.sandbox.local import LocalSandbox
from src.swe_team.providers.sandbox.local import from_config as local_from_config
from src.swe_team.providers.sandbox.docker import (
    DockerSandbox,
    _BLOCKED_ENV_VARS,
    _parse_tag,
)
from src.swe_team.providers.sandbox.docker import from_config as docker_from_config
from src.swe_team.providers.sandbox.proxmox import ProxmoxSandbox
from src.swe_team.providers.sandbox.proxmox import from_config as proxmox_from_config


# ======================================================================
# Helpers
# ======================================================================

def _make_spec(**overrides: Any) -> SandboxSpec:
    defaults: Dict[str, Any] = {"name": "test-sandbox"}
    defaults.update(overrides)
    return SandboxSpec(**defaults)


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Return a mock subprocess.CompletedProcess."""
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _proxmox(
    gateway_url: str = "http://fake-gateway:8080",
    api_key: str = "test-key",
    **kwargs: Any,
) -> ProxmoxSandbox:
    return ProxmoxSandbox(gateway_url=gateway_url, api_key=api_key, **kwargs)


# ======================================================================
# 1. Protocol compliance
# ======================================================================


class TestProtocolCompliance:
    """All concrete providers must be recognized as SandboxProvider."""

    def test_local_is_sandbox_provider(self) -> None:
        assert isinstance(LocalSandbox(), SandboxProvider)

    def test_docker_is_sandbox_provider(self) -> None:
        assert isinstance(DockerSandbox(), SandboxProvider)

    def test_proxmox_is_sandbox_provider(self) -> None:
        p = _proxmox()
        assert isinstance(p, SandboxProvider)

    def test_local_has_name(self) -> None:
        assert LocalSandbox().name == "local"

    def test_docker_has_name(self) -> None:
        assert DockerSandbox().name == "docker"

    def test_proxmox_has_name(self) -> None:
        assert _proxmox().name == "proxmox"

    def test_sandbox_spec_defaults(self) -> None:
        s = SandboxSpec(name="x")
        assert s.cpu == 2
        assert s.ram_gb == 4
        assert s.disk_gb == 20
        assert s.ttl_hours == 2
        assert s.env_vars == {}
        assert s.tags == []

    def test_sandbox_info_defaults(self) -> None:
        info = SandboxInfo(
            sandbox_id="id", name="n", ip=None, status="running", provider="test"
        )
        assert info.metadata == {}
        assert info.creation_method is None


# ======================================================================
# 2. LocalSandbox
# ======================================================================


class TestLocalSandbox:
    def test_create_returns_info(self) -> None:
        sb = LocalSandbox()
        info = sb.create(_make_spec())
        assert info.sandbox_id == "local"
        assert info.status == "running"
        assert info.ip == "127.0.0.1"
        assert info.provider == "local"
        assert info.creation_method is not None
        assert info.creation_method.instance_type == "local_process"

    def test_status_always_running(self) -> None:
        sb = LocalSandbox()
        info = sb.status("anything")
        assert info.status == "running"

    @patch("src.swe_team.providers.sandbox.local.subprocess.run")
    def test_run_command(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(0, "hello\n", "")
        sb = LocalSandbox()
        rc, out, err = sb.run_command("local", ["echo", "hello"])
        assert rc == 0
        assert out == "hello\n"
        mock_run.assert_called_once()

    @patch("src.swe_team.providers.sandbox.local.subprocess.run")
    def test_run_command_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(1, "", "command not found")
        sb = LocalSandbox()
        rc, out, err = sb.run_command("local", ["bad"])
        assert rc == 1
        assert err == "command not found"

    def test_snapshot_returns_label(self) -> None:
        sb = LocalSandbox()
        assert sb.snapshot("local", "snap1") == "snap1"

    def test_rollback_no_error(self) -> None:
        sb = LocalSandbox()
        sb.rollback("local", "snap1")  # should not raise

    def test_delete_no_error(self) -> None:
        sb = LocalSandbox()
        sb.delete("local")  # should not raise

    def test_health_check_always_true(self) -> None:
        assert LocalSandbox().health_check() is True

    def test_from_config(self) -> None:
        sb = local_from_config({})
        assert isinstance(sb, LocalSandbox)


# ======================================================================
# 3. DockerSandbox — create
# ======================================================================


class TestDockerCreate:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid")
    def test_create_success(self, mock_uuid: MagicMock, mock_run: MagicMock) -> None:
        mock_uuid.uuid4.return_value = MagicMock(hex="aabbccdd11223344")
        mock_run.return_value = _mock_run(0, "container_id\n", "")
        sb = DockerSandbox()
        info = sb.create(_make_spec())
        assert info.sandbox_id == "aabbccdd11223344"
        assert info.status == "running"
        assert info.provider == "docker"
        assert info.metadata["container_name"] == "swe-sandbox-aabbccdd1122"
        # Verify docker run was called
        call_args = mock_run.call_args[0][0]
        assert call_args[0:3] == ["docker", "run", "-d"]

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid")
    def test_create_failure_raises(self, mock_uuid: MagicMock, mock_run: MagicMock) -> None:
        mock_uuid.uuid4.return_value = MagicMock(hex="aabbccdd11223344")
        mock_run.return_value = _mock_run(1, "", "no such image")
        sb = DockerSandbox()
        with pytest.raises(RuntimeError, match="container creation failed"):
            sb.create(_make_spec())

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid")
    def test_create_blocks_env_vars(self, mock_uuid: MagicMock, mock_run: MagicMock) -> None:
        mock_uuid.uuid4.return_value = MagicMock(hex="aabbccdd11223344")
        mock_run.return_value = _mock_run(0, "", "")
        sb = DockerSandbox()
        spec = _make_spec(env_vars={"SAFE_VAR": "ok", "GH_TOKEN": "secret"})
        sb.create(spec)
        call_args = mock_run.call_args[0][0]
        cmd_str = " ".join(call_args)
        assert "SAFE_VAR=ok" in cmd_str
        assert "GH_TOKEN" not in cmd_str

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    @patch("src.swe_team.providers.sandbox.docker.uuid")
    def test_create_with_workspace_tag(self, mock_uuid: MagicMock, mock_run: MagicMock) -> None:
        mock_uuid.uuid4.return_value = MagicMock(hex="aabbccdd11223344")
        mock_run.return_value = _mock_run(0, "", "")
        sb = DockerSandbox()
        spec = _make_spec(tags=["workspace:/tmp/repo"])
        sb.create(spec)
        call_args = mock_run.call_args[0][0]
        assert "-v" in call_args
        idx = call_args.index("-v")
        assert call_args[idx + 1] == "/tmp/repo:/workspace"


# ======================================================================
# 4. DockerSandbox — delete (destroy)
# ======================================================================


class TestDockerDelete:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_delete_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(0)
        sb = DockerSandbox()
        sb._containers["abc123"] = "swe-sandbox-abc123"
        sb.delete("abc123")
        call_args = mock_run.call_args[0][0]
        assert call_args == ["docker", "rm", "-f", "swe-sandbox-abc123"]
        assert "abc123" not in sb._containers

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_delete_failure_does_not_raise(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker rm", timeout=30)
        sb = DockerSandbox()
        sb._containers["abc123"] = "swe-sandbox-abc123"
        # Should not raise — failures are logged but swallowed
        sb.delete("abc123")
        # Container should still be cleaned from internal map
        assert "abc123" not in sb._containers

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_delete_unknown_id_uses_convention(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(0)
        sb = DockerSandbox()
        sb.delete("deadbeef12345678")
        call_args = mock_run.call_args[0][0]
        assert call_args == ["docker", "rm", "-f", "swe-sandbox-deadbeef1234"]


# ======================================================================
# 5. DockerSandbox — status (list)
# ======================================================================


class TestDockerStatus:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_status_running(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(0, "running\n")
        sb = DockerSandbox()
        sb._containers["abc"] = "swe-sandbox-abc"
        info = sb.status("abc")
        assert info.status == "running"
        assert info.provider == "docker"

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_status_exited_maps_to_stopped(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(0, "exited\n")
        sb = DockerSandbox()
        info = sb.status("abc")
        assert info.status == "stopped"

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_status_not_found_returns_deleted(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(1, "", "no such container")
        sb = DockerSandbox()
        info = sb.status("abc")
        assert info.status == "deleted"


# ======================================================================
# 6. DockerSandbox — run_command (exec)
# ======================================================================


class TestDockerRunCommand:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_run_command_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(0, "output", "")
        sb = DockerSandbox()
        sb._containers["abc"] = "swe-sandbox-abc"
        rc, out, err = sb.run_command("abc", ["python", "-c", "print('hi')"])
        assert rc == 0
        assert out == "output"
        call_args = mock_run.call_args[0][0]
        assert "docker" in call_args
        assert "exec" in call_args
        assert "swe-sandbox-abc" in call_args

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_run_command_with_env(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(0, "", "")
        sb = DockerSandbox()
        sb._containers["abc"] = "swe-sandbox-abc"
        sb.run_command("abc", ["echo"], env={"MY_VAR": "val"})
        call_args = mock_run.call_args[0][0]
        assert "-e" in call_args
        idx = call_args.index("-e")
        assert call_args[idx + 1] == "MY_VAR=val"

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_run_command_blocks_secret_env(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(0, "", "")
        sb = DockerSandbox()
        sb._containers["abc"] = "swe-sandbox-abc"
        sb.run_command("abc", ["echo"], env={"GH_TOKEN": "secret", "OK": "fine"})
        call_args = mock_run.call_args[0][0]
        cmd_str = " ".join(call_args)
        assert "GH_TOKEN" not in cmd_str
        assert "OK=fine" in cmd_str

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_run_command_timeout_raises(self, mock_run: MagicMock) -> None:
        # First call (exec) times out, second call (stop) succeeds
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="docker exec", timeout=300),
            _mock_run(0),
        ]
        sb = DockerSandbox()
        sb._containers["abc"] = "swe-sandbox-abc"
        with pytest.raises(TimeoutError, match="timed out"):
            sb.run_command("abc", ["long-running"])


# ======================================================================
# 7. DockerSandbox — snapshot & rollback
# ======================================================================


class TestDockerSnapshot:
    @patch("src.swe_team.providers.sandbox.docker.time")
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_snapshot_success(self, mock_run: MagicMock, mock_time: MagicMock) -> None:
        mock_time.time.return_value = 1000000
        mock_run.return_value = _mock_run(0)
        sb = DockerSandbox()
        sb._containers["abc123def456"] = "swe-sandbox-abc123def456"
        tag = sb.snapshot("abc123def456", "pre-fix")
        assert "pre-fix" in tag
        assert "1000000" in tag

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_snapshot_failure_raises(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(1, "", "commit error")
        sb = DockerSandbox()
        with pytest.raises(RuntimeError, match="snapshot failed"):
            sb.snapshot("abc", "label")

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_rollback_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(0)
        sb = DockerSandbox()
        sb._containers["abc"] = "swe-sandbox-abc"
        sb.rollback("abc", "snap-image-tag")
        # Two subprocess calls: rm -f, then run -d
        assert mock_run.call_count == 2

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_rollback_failure_raises(self, mock_run: MagicMock) -> None:
        # rm succeeds, run fails
        mock_run.side_effect = [_mock_run(0), _mock_run(1, "", "rollback err")]
        sb = DockerSandbox()
        sb._containers["abc"] = "swe-sandbox-abc"
        with pytest.raises(RuntimeError, match="rollback failed"):
            sb.rollback("abc", "bad-snap")


# ======================================================================
# 8. DockerSandbox — health_check
# ======================================================================


class TestDockerHealthCheck:
    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_healthy(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(0)
        assert DockerSandbox().health_check() is True

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_unhealthy_returncode(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(1)
        assert DockerSandbox().health_check() is False

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_unhealthy_not_installed(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("docker not found")
        assert DockerSandbox().health_check() is False

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_unhealthy_timeout(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker info", timeout=10)
        assert DockerSandbox().health_check() is False


# ======================================================================
# 9. DockerSandbox — _parse_tag helper
# ======================================================================


class TestParseTag:
    def test_found(self) -> None:
        assert _parse_tag(["workspace:/tmp/repo", "other:val"], "workspace") == "/tmp/repo"

    def test_not_found(self) -> None:
        assert _parse_tag(["workspace:/tmp/repo"], "missing") is None

    def test_empty_list(self) -> None:
        assert _parse_tag([], "workspace") is None

    def test_colon_in_value(self) -> None:
        assert _parse_tag(["env_file:/home/user/.env"], "env_file") == "/home/user/.env"


# ======================================================================
# 10. DockerSandbox — blocked env vars comprehensive
# ======================================================================


class TestBlockedEnvVars:
    def test_all_expected_vars_blocked(self) -> None:
        expected = {
            "SUPABASE_ANON_KEY", "SUPABASE_URL", "GH_TOKEN", "GITHUB_TOKEN",
            "TELEGRAM_BOT_TOKEN", "BASE_LLM_API_KEY", "PROXMOXAI_API_KEY",
            "WEBHOOK_SECRET", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        }
        assert _BLOCKED_ENV_VARS == expected


# ======================================================================
# 11. ProxmoxSandbox — create
# ======================================================================


class TestProxmoxCreate:
    @patch.object(ProxmoxSandbox, "_request")
    def test_create_success(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {"vmid": 1101, "ip": "10.0.0.5", "status": "starting"}
        p = _proxmox()
        info = p.create(_make_spec())
        assert info.sandbox_id == "1101"
        assert info.ip == "10.0.0.5"
        assert info.status == "starting"
        assert info.provider == "proxmox"
        mock_req.assert_called_once()
        call_args = mock_req.call_args
        assert call_args[0][0] == "POST"
        assert call_args[0][1] == "/vms"

    @patch.object(ProxmoxSandbox, "_request")
    def test_create_with_id_field(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {"id": 1102, "status": "starting"}
        info = _proxmox().create(_make_spec())
        assert info.sandbox_id == "1102"

    @patch.object(ProxmoxSandbox, "_request")
    def test_create_unknown_id(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {"status": "starting"}
        info = _proxmox().create(_make_spec())
        assert info.sandbox_id == "unknown"

    @patch.object(ProxmoxSandbox, "_request")
    def test_create_sends_ttl_as_string(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {"vmid": 1101, "status": "starting"}
        _proxmox().create(_make_spec())
        body = mock_req.call_args[1]["body"]
        assert "ttl" in body
        assert body["ttl"] == "2h"
        assert "ttl_hours" not in body


# ======================================================================
# 12. ProxmoxSandbox — delete (destroy)
# ======================================================================


class TestProxmoxDelete:
    @patch.object(ProxmoxSandbox, "_request")
    def test_delete_success(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {}
        p = _proxmox()
        p.delete("1101")  # should not raise
        mock_req.assert_called_once_with("DELETE", "/vms/1101?node=io")

    @patch.object(ProxmoxSandbox, "_request")
    def test_delete_failure_does_not_raise(self, mock_req: MagicMock) -> None:
        mock_req.side_effect = RuntimeError("HTTP 404")
        p = _proxmox()
        p.delete("1101")  # should not raise


# ======================================================================
# 13. ProxmoxSandbox — status
# ======================================================================


class TestProxmoxStatus:
    @patch.object(ProxmoxSandbox, "_request")
    def test_status_success(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {
            "name": "test-vm", "ip": "10.0.0.5", "status": "running",
        }
        info = _proxmox().status("1101")
        assert info.sandbox_id == "1101"
        assert info.status == "running"
        assert info.name == "test-vm"
        mock_req.assert_called_once_with("GET", "/vms/1101?node=io")


# ======================================================================
# 14. ProxmoxSandbox — run_command (exec via SSH)
# ======================================================================


class TestProxmoxRunCommand:
    @patch("src.swe_team.providers.sandbox.proxmox.subprocess.run")
    @patch.object(ProxmoxSandbox, "status")
    def test_run_command_success(self, mock_status: MagicMock, mock_run: MagicMock) -> None:
        mock_status.return_value = SandboxInfo(
            sandbox_id="1101", name="vm", ip="10.0.0.5",
            status="running", provider="proxmox",
        )
        mock_run.return_value = _mock_run(0, "result\n", "")
        rc, out, err = _proxmox().run_command("1101", ["echo", "hi"])
        assert rc == 0
        assert out == "result\n"
        call_args = mock_run.call_args[0][0]
        assert "ssh" in call_args
        assert "agent@10.0.0.5" in call_args

    @patch.object(ProxmoxSandbox, "status")
    def test_run_command_no_ip_raises(self, mock_status: MagicMock) -> None:
        mock_status.return_value = SandboxInfo(
            sandbox_id="1101", name="vm", ip=None,
            status="starting", provider="proxmox",
        )
        with pytest.raises(RuntimeError, match="no IP assigned"):
            _proxmox().run_command("1101", ["echo"])


# ======================================================================
# 15. ProxmoxSandbox — snapshot & rollback
# ======================================================================


class TestProxmoxSnapshotRollback:
    @patch.object(ProxmoxSandbox, "_request")
    def test_snapshot_success(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {}
        label = _proxmox().snapshot("1101", "pre-fix")
        assert label == "pre-fix"
        mock_req.assert_called_once_with(
            "POST", "/vms/1101/snapshots?node=io", body={"name": "pre-fix"}
        )

    @patch.object(ProxmoxSandbox, "_request")
    def test_rollback_success(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {}
        _proxmox().rollback("1101", "pre-fix")
        mock_req.assert_called_once_with(
            "POST", "/vms/1101/snapshots/pre-fix/rollback?node=io"
        )


# ======================================================================
# 15b. ProxmoxSandbox — extend_ttl
# ======================================================================


class TestProxmoxExtendTtl:
    @patch.object(ProxmoxSandbox, "_request")
    def test_extend_ttl_sends_correct_payload(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {}
        _proxmox().extend_ttl("1101", 24)
        mock_req.assert_called_once_with(
            "PUT", "/vms/1101/ttl?node=io", body={"ttl": "24h"}
        )

    @patch.object(ProxmoxSandbox, "_request")
    def test_extend_ttl_uses_node(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {}
        _proxmox(node="zion").extend_ttl("1102", 48)
        mock_req.assert_called_once_with(
            "PUT", "/vms/1102/ttl?node=zion", body={"ttl": "48h"}
        )


# ======================================================================
# 16. ProxmoxSandbox — health_check
# ======================================================================


class TestProxmoxHealthCheck:
    @patch.object(ProxmoxSandbox, "_request")
    def test_healthy(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {"status": "ok"}
        assert _proxmox().health_check() is True

    @patch.object(ProxmoxSandbox, "_request")
    def test_unhealthy_wrong_status(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {"status": "degraded"}
        assert _proxmox().health_check() is False

    @patch.object(ProxmoxSandbox, "_request")
    def test_unhealthy_exception(self, mock_req: MagicMock) -> None:
        mock_req.side_effect = RuntimeError("unreachable")
        assert _proxmox().health_check() is False


# ======================================================================
# 17. ProxmoxSandbox — extra methods (cluster_status, quota, list_vms)
# ======================================================================


class TestProxmoxExtras:
    @patch.object(ProxmoxSandbox, "_request")
    def test_cluster_status(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {"nodes": []}
        result = _proxmox().cluster_status()
        mock_req.assert_called_once_with("GET", "/cluster/status")
        assert result == {"nodes": []}

    @patch.object(ProxmoxSandbox, "_request")
    def test_quota(self, mock_req: MagicMock) -> None:
        mock_req.return_value = {"vm_count": 1, "vm_limit": 3}
        result = _proxmox().quota()
        mock_req.assert_called_once_with("GET", "/quota/me")
        assert result["vm_limit"] == 3

    @patch.object(ProxmoxSandbox, "_request")
    def test_list_vms(self, mock_req: MagicMock) -> None:
        mock_req.return_value = [{"vmid": 1101}]
        result = _proxmox().list_vms()
        mock_req.assert_called_once_with("GET", "/vms")
        assert len(result) == 1


# ======================================================================
# 18. ProxmoxSandbox — _request HTTP error handling
# ======================================================================


class TestProxmoxRequest:
    @patch("src.swe_team.providers.sandbox.proxmox.urllib.request.urlopen")
    def test_request_http_error(self, mock_urlopen: MagicMock) -> None:
        exc = urllib.error.HTTPError(
            url="http://fake/vms", code=403,
            msg="Forbidden", hdrs=None,  # type: ignore
            fp=MagicMock(read=lambda: b"not authorized"),
        )
        mock_urlopen.side_effect = exc
        with pytest.raises(RuntimeError, match="HTTP 403"):
            _proxmox()._request("GET", "/vms")

    @patch("src.swe_team.providers.sandbox.proxmox.urllib.request.urlopen")
    def test_request_url_error(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        with pytest.raises(RuntimeError, match="gateway unreachable"):
            _proxmox()._request("GET", "/health")

    def test_request_strips_trailing_slash_from_url(self) -> None:
        p = ProxmoxSandbox(gateway_url="http://gw:8080/", api_key="k")
        assert p._url == "http://gw:8080"


# ======================================================================
# 19. Config validation — from_config factories
# ======================================================================


class TestConfigValidation:
    def test_proxmox_from_config_full(self) -> None:
        cfg = {
            "gateway_url": "http://gw:8080",
            "api_key": "mykey",
            "node": "zion",
            "default_cpu": 4,
            "default_ram_gb": 8,
            "default_disk_gb": 40,
            "default_ttl_hours": 4,
        }
        p = proxmox_from_config(cfg)
        assert p._node == "zion"
        assert p._defaults["cpu"] == 4
        assert p._defaults["ram_gb"] == 8

    def test_proxmox_from_config_defaults(self) -> None:
        cfg = {"gateway_url": "http://gw:8080", "api_key": "k"}
        p = proxmox_from_config(cfg)
        assert p._node == "io"
        assert p._defaults["cpu"] == 2

    def test_proxmox_from_config_missing_url_raises(self) -> None:
        with pytest.raises(KeyError):
            proxmox_from_config({"api_key": "k"})

    def test_proxmox_from_config_missing_key_raises(self) -> None:
        with pytest.raises(KeyError):
            proxmox_from_config({"gateway_url": "http://gw:8080"})

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_docker_from_config_full(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(0)  # health_check
        cfg = {
            "image": "node:18-slim",
            "network": "bridge",
            "auto_install_deps": False,
            "cpu_limit": 2.0,
            "memory_mb": 1024,
        }
        d = docker_from_config(cfg)
        assert d._image == "node:18-slim"
        assert d._network == "bridge"
        assert d._cpu_limit == 2.0
        assert d._memory_mb == 1024

    @patch("src.swe_team.providers.sandbox.docker.subprocess.run")
    def test_docker_from_config_defaults(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(0)
        d = docker_from_config({})
        assert d._image == "python:3.11-slim"
        assert d._network == "none"

    def test_local_from_config_ignores_cfg(self) -> None:
        sb = local_from_config({"anything": "ignored"})
        assert isinstance(sb, LocalSandbox)


# ======================================================================
# 20. Registry — create_sandbox_provider & list_sandbox_providers
# ======================================================================


class TestRegistry:
    def test_list_providers(self) -> None:
        from src.swe_team.providers.sandbox import list_sandbox_providers
        providers = list_sandbox_providers()
        assert "local" in providers
        assert "docker" in providers
        assert "proxmox" in providers
        assert "aws" in providers
        assert "gcp" in providers
        assert "azure" in providers

    def test_create_local_provider(self) -> None:
        from src.swe_team.providers.sandbox import create_sandbox_provider
        sb = create_sandbox_provider("local")
        assert isinstance(sb, LocalSandbox)

    def test_create_unknown_provider_raises(self) -> None:
        from src.swe_team.providers.sandbox import create_sandbox_provider
        with pytest.raises(ValueError, match="Unknown sandbox provider"):
            create_sandbox_provider("nonexistent")

    def test_register_custom_provider(self) -> None:
        from src.swe_team.providers.sandbox import (
            register_sandbox_provider,
            create_sandbox_provider,
            _REGISTRY,
        )
        # Register and use a custom provider
        register_sandbox_provider("test_custom", lambda cfg: LocalSandbox())
        try:
            sb = create_sandbox_provider("test_custom")
            assert isinstance(sb, LocalSandbox)
        finally:
            _REGISTRY.pop("test_custom", None)


# ======================================================================
# 21. DockerSandbox — _resolve_container
# ======================================================================


class TestResolveContainer:
    def test_known_container(self) -> None:
        sb = DockerSandbox()
        sb._containers["myid"] = "custom-name"
        assert sb._resolve_container("myid") == "custom-name"

    def test_unknown_container_convention(self) -> None:
        sb = DockerSandbox()
        assert sb._resolve_container("abcdef123456extra") == "swe-sandbox-abcdef123456"


# ======================================================================
# 22. ProxmoxSandbox — constructor defaults
# ======================================================================


class TestProxmoxConstructorDefaults:
    def test_defaults(self) -> None:
        p = _proxmox()
        assert p._node == "io"
        assert p._defaults["cpu"] == 2
        assert p._defaults["ram_gb"] == 4
        assert p._defaults["disk_gb"] == 20
        assert p._defaults["ttl_hours"] == 2

    def test_custom_values(self) -> None:
        p = _proxmox(node="zion", default_cpu=8, default_ram_gb=16)
        assert p._node == "zion"
        assert p._defaults["cpu"] == 8
        assert p._defaults["ram_gb"] == 16
