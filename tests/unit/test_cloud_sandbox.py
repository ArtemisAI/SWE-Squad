from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.sandbox.base import SandboxSpec
from src.swe_team.providers.sandbox.cloud import CloudVMSandbox, from_config


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestCloudCreationMethods:
    @pytest.mark.parametrize(
        ("platform", "provisioning_flow"),
        [("aws", "aws_cli"), ("gcp", "gcloud_cli"), ("azure", "az_cli")],
    )
    def test_creation_method_for_each_platform(self, platform: str, provisioning_flow: str) -> None:
        sb = CloudVMSandbox(platform=platform)
        method = sb.get_instance_creation_method()
        assert method.instance_type == f"{platform}_vm"
        assert method.provisioning_flow == provisioning_flow
        assert method.connection_method == "ssh"


class TestCloudProviderFactory:
    @pytest.mark.parametrize("platform", ["aws", "gcp", "azure"])
    def test_from_config_sets_platform_name(self, platform: str) -> None:
        sb = from_config(platform, {"region": "test-region"})
        assert sb.name == platform

    def test_invalid_platform_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unsupported cloud sandbox platform"):
            CloudVMSandbox(platform="digitalocean")


class TestCloudLifecycle:
    def test_create_returns_starting_instance(self) -> None:
        sb = CloudVMSandbox(platform="aws", region="us-east-1")
        info = sb.create(SandboxSpec(name="cloud-test"))
        assert info.status == "starting"
        assert info.provider == "aws"
        assert info.metadata["region"] == "us-east-1"

    def test_status_unknown_instance_is_deleted(self) -> None:
        sb = CloudVMSandbox(platform="gcp")
        info = sb.status("missing")
        assert info.status == "deleted"
        assert info.creation_method is not None

    def test_run_command_without_ip_raises(self) -> None:
        sb = CloudVMSandbox(platform="azure")
        info = sb.create(SandboxSpec(name="vm"))
        with pytest.raises(RuntimeError, match="has no IP assigned yet"):
            sb.run_command(info.sandbox_id, ["echo", "hello"])

    @patch("src.swe_team.providers.sandbox.cloud.subprocess.run")
    def test_run_command_uses_strict_host_key_accept_new(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(returncode=0, stdout="ok")
        sb = CloudVMSandbox(platform="aws")
        info = sb.create(SandboxSpec(name="vm"))
        sb._instances[info.sandbox_id].ip = "10.0.0.2"
        sb.run_command(info.sandbox_id, ["echo", "hello"])
        cmd = mock_run.call_args[0][0]
        assert "StrictHostKeyChecking=accept-new" in cmd

    @patch("src.swe_team.providers.sandbox.cloud.subprocess.run")
    def test_create_command_failure_has_context(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(returncode=1, stderr="boom")
        sb = CloudVMSandbox(platform="gcp", create_command=["false"])
        with pytest.raises(RuntimeError, match="create sandbox_id=.*name=vm"):
            sb.create(SandboxSpec(name="vm"))

    @patch("src.swe_team.providers.sandbox.cloud.subprocess.run")
    def test_health_check_uses_platform_cli(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _mock_run(returncode=0)
        sb = CloudVMSandbox(platform="aws")
        assert sb.health_check() is True
        assert mock_run.call_args[0][0] == ["aws", "--version"]
