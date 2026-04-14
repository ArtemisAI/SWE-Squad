"""
SandboxProvider interface — pluggable dev environment provisioning.

Implement this to add support for any sandbox backend
(ProxmoxAI, Docker, local subprocess, GitHub Codespaces, etc.)
without touching core agent code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class InstanceCreationMethod:
    """Describes how a provider provisions and connects to an instance."""

    instance_type: str
    provisioning_flow: str
    connection_method: str


@dataclass
class SandboxSpec:
    """Resource specification for a sandbox VM/container."""
    name: str
    cpu: int = 2
    ram_gb: int = 4
    disk_gb: int = 20
    ttl_hours: int = 2
    env_vars: Dict[str, str] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)


@dataclass
class SandboxInfo:
    """Runtime info about a provisioned sandbox."""
    sandbox_id: str
    name: str
    ip: Optional[str]
    status: str          # "starting" | "running" | "stopped" | "deleted"
    provider: str        # e.g. "proxmox", "docker", "local", "aws", "gcp", "azure"
    creation_method: Optional[InstanceCreationMethod] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SandboxProvider(Protocol):
    """
    Interface all sandbox providers must implement.

    Providers are registered in config/swe_team.yaml under providers.sandbox.
    The active provider is loaded by name — no core code changes required
    when switching backends.
    """

    @property
    def name(self) -> str:
        """Provider identifier (e.g. 'proxmox', 'docker', 'local', 'aws')."""
        ...

    def create(self, spec: SandboxSpec) -> SandboxInfo:
        """Provision a new sandbox. Blocks until IP is reachable or raises."""
        ...

    def status(self, sandbox_id: str) -> SandboxInfo:
        """Return current status of a sandbox."""
        ...

    def run_command(self, sandbox_id: str, command: List[str]) -> tuple[int, str, str]:
        """Run a command inside the sandbox. Returns (returncode, stdout, stderr)."""
        ...

    def snapshot(self, sandbox_id: str, label: str) -> str:
        """Create a named snapshot. Returns snapshot ID."""
        ...

    def rollback(self, sandbox_id: str, label: str) -> None:
        """Roll back sandbox to a named snapshot."""
        ...

    def delete(self, sandbox_id: str) -> None:
        """Destroy the sandbox and release resources."""
        ...

    def health_check(self) -> bool:
        """Return True if the provider backend is reachable."""
        ...

    def get_instance_creation_method(self) -> InstanceCreationMethod:
        """Return instance type + provisioning + connection details."""
        ...
