"""
Local sandbox provider — runs tests in a subprocess on the current machine.

Fallback when no remote sandbox is configured. Zero infrastructure required.
Suitable for unit tests only — not for browser/live integration tests.

Configuration (swe_team.yaml):
  providers:
    sandbox:
      provider: local
"""
from __future__ import annotations

import logging
import subprocess
from typing import Any, Dict, List

from .base import InstanceCreationMethod, SandboxInfo, SandboxSpec

logger = logging.getLogger(__name__)


class LocalSandbox:
    """Runs commands directly on the host — no VM/container provisioned."""

    name = "local"

    def get_instance_creation_method(self) -> InstanceCreationMethod:
        return InstanceCreationMethod(
            instance_type="local_process",
            provisioning_flow="subprocess",
            connection_method="local_exec",
        )

    def create(self, spec: SandboxSpec) -> SandboxInfo:
        logger.info("LocalSandbox: no VM provisioned — commands run on host")
        return SandboxInfo(sandbox_id="local", name=spec.name, ip="127.0.0.1",
                           status="running", provider=self.name,
                           creation_method=self.get_instance_creation_method())

    def status(self, sandbox_id: str) -> SandboxInfo:
        return SandboxInfo(sandbox_id="local", name="local", ip="127.0.0.1",
                           status="running", provider=self.name,
                           creation_method=self.get_instance_creation_method())

    def run_command(self, sandbox_id: str, command: List[str]) -> tuple[int, str, str]:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        return result.returncode, result.stdout, result.stderr

    def snapshot(self, sandbox_id: str, label: str) -> str:
        logger.warning("LocalSandbox: snapshots not supported")
        return label

    def rollback(self, sandbox_id: str, label: str) -> None:
        logger.warning("LocalSandbox: rollback not supported")

    def delete(self, sandbox_id: str) -> None:
        pass  # nothing to clean up

    def health_check(self) -> bool:
        return True


def from_config(cfg: Dict[str, Any]) -> "LocalSandbox":
    return LocalSandbox()
