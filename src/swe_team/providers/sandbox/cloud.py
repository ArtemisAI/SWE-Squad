"""
Cloud VM sandbox providers for AWS/GCP/Azure.

This provider defines cloud instance creation methods and keeps a lightweight
runtime view of created instances. Actual provisioning command execution is
optional and can be configured per provider via ``create_command``.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
import uuid
from typing import Any, Dict, List, Optional

from .base import InstanceCreationMethod, SandboxInfo, SandboxSpec

logger = logging.getLogger(__name__)

_CLOUD_PROVISIONING_FLOW = {
    "aws": "aws_cli",
    "gcp": "gcloud_cli",
    "azure": "az_cli",
}

_CLOUD_DEFAULT_SSH_USER = {
    "aws": "ec2-user",
    "gcp": "ubuntu",
    "azure": "azureuser",
}


class CloudVMSandbox:
    """Sandbox provider for cloud VM provisioning via provider CLIs."""

    def __init__(
        self,
        platform: str,
        region: str = "",
        ssh_user: Optional[str] = None,
        create_command: Optional[List[str]] = None,
        status_command: Optional[List[str]] = None,
        delete_command: Optional[List[str]] = None,
    ) -> None:
        if platform not in _CLOUD_PROVISIONING_FLOW:
            supported = ", ".join(sorted(_CLOUD_PROVISIONING_FLOW))
            raise ValueError(f"Unsupported cloud sandbox platform '{platform}'. Supported: {supported}")
        self.name = platform
        self._platform = platform
        self._region = region
        self._ssh_user = ssh_user or _CLOUD_DEFAULT_SSH_USER[platform]
        self._create_command = create_command or []
        self._status_command = status_command or []
        self._delete_command = delete_command or []
        self._instances: Dict[str, SandboxInfo] = {}

    def get_instance_creation_method(self) -> InstanceCreationMethod:
        return InstanceCreationMethod(
            instance_type=f"{self._platform}_vm",
            provisioning_flow=_CLOUD_PROVISIONING_FLOW[self._platform],
            connection_method="ssh",
        )

    def create(self, spec: SandboxSpec) -> SandboxInfo:
        sandbox_id = uuid.uuid4().hex
        if self._create_command:
            self._run_shell_command(
                self._create_command,
                context=f"create sandbox_id={sandbox_id} name={spec.name}",
            )

        info = SandboxInfo(
            sandbox_id=sandbox_id,
            name=spec.name,
            ip=None,
            status="starting",
            provider=self.name,
            creation_method=self.get_instance_creation_method(),
            metadata={
                "region": self._region,
                "ssh_user": self._ssh_user,
            },
        )
        self._instances[sandbox_id] = info
        return info

    def status(self, sandbox_id: str) -> SandboxInfo:
        if self._status_command and sandbox_id in self._instances:
            self._run_shell_command(
                self._status_command,
                context=f"status sandbox_id={sandbox_id}",
            )
        return self._instances.get(
            sandbox_id,
            SandboxInfo(
                sandbox_id=sandbox_id,
                name="",
                ip=None,
                status="deleted",
                provider=self.name,
                creation_method=self.get_instance_creation_method(),
            ),
        )

    def run_command(self, sandbox_id: str, command: List[str]) -> tuple[int, str, str]:
        info = self.status(sandbox_id)
        if not info.ip:
            raise RuntimeError(f"Cloud VM {sandbox_id} has no IP assigned yet")
        ssh_cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=10",
            f"{self._ssh_user}@{info.ip}",
        ] + command
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=300)
        return result.returncode, result.stdout, result.stderr

    def snapshot(self, sandbox_id: str, label: str) -> str:
        logger.warning("%s sandbox snapshots are not supported", self._platform)
        return label

    def rollback(self, sandbox_id: str, label: str) -> None:
        logger.warning("%s sandbox rollback is not supported", self._platform)

    def delete(self, sandbox_id: str) -> None:
        if self._delete_command:
            self._run_shell_command(
                self._delete_command,
                context=f"delete sandbox_id={sandbox_id}",
            )
        self._instances.pop(sandbox_id, None)

    def health_check(self) -> bool:
        check_cmd = {
            "aws": ["aws", "--version"],
            "gcp": ["gcloud", "--version"],
            "azure": ["az", "version"],
        }[self._platform]
        try:
            result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _run_shell_command(self, command: List[str], *, context: str = "") -> None:
        if not command:
            return
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            rendered = " ".join(shlex.quote(part) for part in command)
            context_msg = f", context={context}" if context else ""
            raise RuntimeError(
                f"{self._platform} cloud command failed ({rendered}{context_msg}): {result.stderr.strip()}"
            )


def from_config(platform: str, cfg: Dict[str, Any]) -> CloudVMSandbox:
    """Build a cloud VM sandbox provider from config."""
    return CloudVMSandbox(
        platform=platform,
        region=cfg.get("region", ""),
        ssh_user=cfg.get("ssh_user"),
        create_command=cfg.get("create_command"),
        status_command=cfg.get("status_command"),
        delete_command=cfg.get("delete_command"),
    )
