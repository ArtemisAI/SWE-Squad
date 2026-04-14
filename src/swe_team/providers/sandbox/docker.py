"""
Docker sandbox provider — runs agent work in isolated Docker containers.

Uses stdlib subprocess only — no Docker SDK dependency.
Containers are network-isolated by default, resource-limited, and never privileged.

Configuration (swe_team.yaml):
  providers:
    sandbox:
      provider: docker
      image: "python:3.11-slim"
      network: "none"
      auto_install_deps: true
      cpu_limit: 1.0
      memory_mb: 512

Environment variables (never hardcode these):
  None required — Docker must be installed and daemon running on the host.
"""
from __future__ import annotations

import logging
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional

from .base import InstanceCreationMethod, SandboxInfo, SandboxSpec

logger = logging.getLogger(__name__)

# Environment variables that must NEVER be passed into a sandbox container.
_BLOCKED_ENV_VARS = frozenset({
    "SUPABASE_ANON_KEY",
    "SUPABASE_URL",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "BASE_LLM_API_KEY",
    "PROXMOXAI_API_KEY",
    "WEBHOOK_SECRET",
    "AWS_SECRET_ACCESS_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
})


def _parse_tag(tags: List[str], prefix: str) -> Optional[str]:
    """Extract value from a tag list entry like 'prefix:value'."""
    for tag in tags:
        if tag.startswith(f"{prefix}:"):
            return tag[len(prefix) + 1:]
    return None


class DockerSandbox:
    """
    SandboxProvider backed by Docker containers (stdlib subprocess, zero deps).

    All config injected via constructor — never reads os.environ directly.
    Register in swe_team.yaml providers.sandbox; loaded by from_config().
    """

    name = "docker"

    def get_instance_creation_method(self) -> InstanceCreationMethod:
        return InstanceCreationMethod(
            instance_type="docker_container",
            provisioning_flow="docker_run",
            connection_method="docker_exec",
        )

    def __init__(
        self,
        image: str = "python:3.11-slim",
        network: str = "none",
        auto_install_deps: bool = True,
        cpu_limit: float = 1.0,
        memory_mb: int = 512,
    ) -> None:
        self._image = image
        self._network = network
        self._auto_install_deps = auto_install_deps
        self._cpu_limit = cpu_limit
        self._memory_mb = memory_mb
        # sandbox_id -> container_name mapping
        self._containers: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # SandboxProvider interface
    # ------------------------------------------------------------------

    def create(self, spec: SandboxSpec) -> SandboxInfo:
        """
        Provision a new Docker container.

        docker run -d --name swe-sandbox-{id[:12]} \\
          --network none --cpus {cpu} --memory {ram}m \\
          --env KEY=VAL ... \\
          [-v workspace:/workspace] \\
          {image} sleep infinity
        """
        sandbox_id = uuid.uuid4().hex
        container_name = f"swe-sandbox-{sandbox_id[:12]}"

        cmd: List[str] = [
            "docker", "run", "-d",
            "--name", container_name,
            f"--network={self._network}",
            f"--cpus={spec.cpu or self._cpu_limit}",
            f"--memory={spec.ram_gb * 1024 if spec.ram_gb else self._memory_mb}m",
        ]

        # Scoped env injection — only spec.env_vars, never host env
        for key, val in spec.env_vars.items():
            if key in _BLOCKED_ENV_VARS:
                logger.warning(
                    "DockerSandbox: blocked env var '%s' — not injecting into container",
                    key,
                )
                continue
            cmd.extend(["--env", f"{key}={val}"])

        # Workspace mount (tag format: "workspace:/path/to/repo")
        workspace_path = _parse_tag(spec.tags, "workspace")
        if workspace_path:
            cmd.extend(["-v", f"{workspace_path}:/workspace"])

        # .env file mount (tag format: "env_file:/path/to/.env")
        env_file_path = _parse_tag(spec.tags, "env_file")
        if env_file_path:
            cmd.extend(["-v", f"{env_file_path}:/workspace/.env:ro"])

        # Security: never privileged, never mount docker socket
        cmd.extend([
            "--security-opt", "no-new-privileges",
            self._image,
            "sleep", "infinity",
        ])

        logger.info(
            "DockerSandbox: creating container '%s' (image=%s, network=%s)",
            container_name, self._image, self._network,
        )

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(
                f"Docker container creation failed: {result.stderr.strip()}"
            )

        self._containers[sandbox_id] = container_name
        return SandboxInfo(
            sandbox_id=sandbox_id,
            name=spec.name,
            ip=None,  # containers use docker exec, no IP needed
            status="running",
            provider=self.name,
            creation_method=self.get_instance_creation_method(),
            metadata={"container_name": container_name, "image": self._image},
        )

    def status(self, sandbox_id: str) -> SandboxInfo:
        """docker inspect {container} -> map to SandboxInfo."""
        container_name = self._resolve_container(sandbox_id)
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", container_name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return SandboxInfo(
                sandbox_id=sandbox_id,
                name=container_name,
                ip=None,
                status="deleted",
                provider=self.name,
                creation_method=self.get_instance_creation_method(),
            )
        docker_status = result.stdout.strip()
        # Map Docker status to our status enum
        status_map = {
            "running": "running",
            "created": "starting",
            "exited": "stopped",
            "dead": "stopped",
            "paused": "stopped",
            "restarting": "starting",
        }
        return SandboxInfo(
            sandbox_id=sandbox_id,
            name=container_name,
            ip=None,
            status=status_map.get(docker_status, "stopped"),
            provider=self.name,
            creation_method=self.get_instance_creation_method(),
            metadata={"docker_status": docker_status},
        )

    def run_command(
        self,
        sandbox_id: str,
        command: List[str],
        env: Optional[Dict[str, str]] = None,
        timeout: int = 300,
    ) -> tuple[int, str, str]:
        """
        docker exec [-e KEY=VAL ...] {container_name} {command}

        Additional per-exec env vars override sandbox-level vars.
        """
        container_name = self._resolve_container(sandbox_id)
        cmd: List[str] = ["docker", "exec"]

        # Per-exec environment overrides
        if env:
            for key, val in env.items():
                if key in _BLOCKED_ENV_VARS:
                    continue
                cmd.extend(["-e", f"{key}={val}"])

        cmd.append(container_name)
        cmd.extend(command)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            # Kill the container on timeout to prevent runaway processes
            logger.warning(
                "DockerSandbox: command timed out after %ds, stopping container %s",
                timeout, container_name,
            )
            subprocess.run(
                ["docker", "stop", "-t", "5", container_name],
                capture_output=True, text=True, timeout=30,
            )
            raise TimeoutError(
                f"Command timed out after {timeout}s in container {container_name}"
            )

    def snapshot(self, sandbox_id: str, label: str) -> str:
        """docker commit {container} swe-snapshot-{sandbox_id}-{label}"""
        container_name = self._resolve_container(sandbox_id)
        timestamp = int(time.time())
        snapshot_tag = f"swe-snapshot-{sandbox_id[:12]}-{label}-{timestamp}"

        result = subprocess.run(
            ["docker", "commit", container_name, snapshot_tag],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Docker snapshot failed: {result.stderr.strip()}"
            )

        logger.info(
            "DockerSandbox: snapshot '%s' of container %s",
            snapshot_tag, container_name,
        )
        return snapshot_tag

    def rollback(self, sandbox_id: str, label: str) -> None:
        """Stop current container, start new one from snapshot image, update registry."""
        container_name = self._resolve_container(sandbox_id)

        # Stop and remove current container
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True, text=True, timeout=30,
        )

        # Start new container from snapshot image
        cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            f"--network={self._network}",
            label,  # the snapshot image tag
            "sleep", "infinity",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(
                f"Docker rollback failed: {result.stderr.strip()}"
            )

        logger.info(
            "DockerSandbox: rolled back container %s to snapshot '%s'",
            container_name, label,
        )

    def delete(self, sandbox_id: str) -> None:
        """docker rm -f {container_name}"""
        container_name = self._resolve_container(sandbox_id)
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True, text=True, timeout=30,
            )
            logger.info("DockerSandbox: deleted container %s", container_name)
        except Exception as exc:
            logger.warning(
                "DockerSandbox: delete container %s failed: %s",
                container_name, exc,
            )
        finally:
            self._containers.pop(sandbox_id, None)

    def health_check(self) -> bool:
        """Return True if Docker daemon is reachable."""
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_container(self, sandbox_id: str) -> str:
        """Look up container name from sandbox_id, fallback to naming convention."""
        if sandbox_id in self._containers:
            return self._containers[sandbox_id]
        # Convention-based fallback
        return f"swe-sandbox-{sandbox_id[:12]}"


def from_config(cfg: Dict[str, Any]) -> DockerSandbox:
    """
    Factory called by ProviderRegistry.
    All config comes from swe_team.yaml providers.sandbox section.
    """
    provider = DockerSandbox(
        image=cfg.get("image", "python:3.11-slim"),
        network=cfg.get("network", "none"),
        auto_install_deps=cfg.get("auto_install_deps", True),
        cpu_limit=float(cfg.get("cpu_limit", 1.0)),
        memory_mb=int(cfg.get("memory_mb", 512)),
    )
    if not provider.health_check():
        logger.warning(
            "DockerSandbox: Docker daemon not available — "
            "container operations will fail until Docker is running"
        )
    return provider
