"""
ProxmoxAI sandbox provider — provisions VMs via the ProxmoxAI REST gateway.

Uses the HTTP API directly (urllib, stdlib only — zero extra dependencies).
No arc CLI required.

Cluster configuration is deployment-specific — set node names in swe_team.yaml.

Configuration (swe_team.yaml):
  providers:
    sandbox:
      provider: proxmox
      gateway_url: ""          # set via PROXMOXAI_GATEWAY_URL env var
      api_key: ""              # set via PROXMOXAI_API_KEY env var
      node: node-1             # target Proxmox node name
      default_cpu: 2
      default_ram_gb: 4
      default_disk_gb: 20
      default_ttl_hours: 2

Environment variables (never hardcode these):
  PROXMOXAI_GATEWAY_URL   e.g. http://<gateway-host>:8080
  PROXMOXAI_API_KEY       worker-tier key
"""
from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .base import InstanceCreationMethod, SandboxInfo, SandboxSpec

logger = logging.getLogger(__name__)

_AUTH_HEADER = "X-API-Key"


class ProxmoxSandbox:
    """
    SandboxProvider backed by the ProxmoxAI REST gateway (stdlib urllib, zero deps).

    All config injected via constructor — never reads os.environ directly.
    Register in swe_team.yaml providers.sandbox; loaded by ProviderRegistry.
    """

    name = "proxmox"

    def get_instance_creation_method(self) -> InstanceCreationMethod:
        return InstanceCreationMethod(
            instance_type="proxmox_vm",
            provisioning_flow="proxmox_api",
            connection_method="ssh",
        )

    def __init__(
        self,
        gateway_url: str,
        api_key: str,
        node: str = "io",
        default_cpu: int = 2,
        default_ram_gb: int = 4,
        default_disk_gb: int = 20,
        default_ttl_hours: int = 2,
    ) -> None:
        self._url = gateway_url.rstrip("/")
        self._key = api_key
        self._node = node
        self._defaults = {
            "cpu": default_cpu,
            "ram_gb": default_ram_gb,
            "disk_gb": default_disk_gb,
            "ttl_hours": default_ttl_hours,
        }

    # ------------------------------------------------------------------
    # SandboxProvider interface
    # ------------------------------------------------------------------

    def create(self, spec: SandboxSpec) -> SandboxInfo:
        """POST /vms — provision a new VM."""
        ttl_hours = spec.ttl_hours or self._defaults["ttl_hours"]
        payload = {
            "name": spec.name,
            "node": self._node,
            "cpu": spec.cpu or self._defaults["cpu"],
            "ram_gb": spec.ram_gb or self._defaults["ram_gb"],
            "disk_gb": spec.disk_gb or self._defaults["disk_gb"],
            "ttl": f"{ttl_hours}h",
        }
        logger.info("ProxmoxSandbox: creating VM '%s' on node %s", spec.name, self._node)
        data = self._request("POST", "/vms", body=payload)
        vmid = str(data.get("vmid", data.get("id", "unknown")))
        return SandboxInfo(
            sandbox_id=vmid,
            name=spec.name,
            ip=data.get("ip"),
            status=data.get("status", "starting"),
            provider=self.name,
            creation_method=self.get_instance_creation_method(),
            metadata={"node": self._node, "raw": data},
        )

    def status(self, sandbox_id: str) -> SandboxInfo:
        """GET /vms/{vmid}?node={node}"""
        data = self._request("GET", f"/vms/{sandbox_id}?node={self._node}")
        return SandboxInfo(
            sandbox_id=sandbox_id,
            name=data.get("name", ""),
            ip=data.get("ip"),
            status=data.get("status", "unknown"),
            provider=self.name,
            creation_method=self.get_instance_creation_method(),
            metadata=data,
        )

    def run_command(self, sandbox_id: str, command: List[str]) -> tuple[int, str, str]:
        """SSH into the VM and run a command."""
        info = self.status(sandbox_id)
        if not info.ip:
            raise RuntimeError(f"VM {sandbox_id} has no IP assigned yet")
        ssh_cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"agent@{info.ip}",
        ] + command
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=300)
        return result.returncode, result.stdout, result.stderr

    def snapshot(self, sandbox_id: str, label: str) -> str:
        """POST /vms/{vmid}/snapshots?node={node}"""
        self._request("POST", f"/vms/{sandbox_id}/snapshots?node={self._node}", body={"name": label})
        logger.info("ProxmoxSandbox: snapshot '%s' on VM %s", label, sandbox_id)
        return label

    def rollback(self, sandbox_id: str, label: str) -> None:
        """POST /vms/{vmid}/snapshots/{snap_name}/rollback?node={node}"""
        self._request("POST", f"/vms/{sandbox_id}/snapshots/{label}/rollback?node={self._node}")
        logger.info("ProxmoxSandbox: rolled back VM %s to '%s'", sandbox_id, label)

    def delete(self, sandbox_id: str) -> None:
        """DELETE /vms/{vmid}?node={node}"""
        try:
            self._request("DELETE", f"/vms/{sandbox_id}?node={self._node}")
            logger.info("ProxmoxSandbox: deleted VM %s", sandbox_id)
        except Exception as exc:
            logger.warning("ProxmoxSandbox: delete VM %s failed: %s", sandbox_id, exc)

    def extend_ttl(self, sandbox_id: str, hours: int) -> None:
        """PUT /vms/{vmid}/ttl?node={node} — extend the TTL of a running VM."""
        self._request(
            "PUT",
            f"/vms/{sandbox_id}/ttl?node={self._node}",
            body={"ttl": f"{hours}h"},
        )
        logger.info("ProxmoxSandbox: extended TTL of VM %s by %dh", sandbox_id, hours)

    def health_check(self) -> bool:
        """GET /health"""
        try:
            data = self._request("GET", "/health")
            return data.get("status") == "ok"
        except Exception:
            return False

    def cluster_status(self) -> Dict[str, Any]:
        """GET /cluster/status — node list with CPU/RAM/disk."""
        return self._request("GET", "/cluster/status")

    def quota(self) -> Dict[str, Any]:
        """GET /quota/me — usage vs limits for this API key."""
        return self._request("GET", "/quota/me")

    def list_vms(self) -> List[Dict[str, Any]]:
        """GET /vms — all VMs owned by this key."""
        return self._request("GET", "/vms")

    # ------------------------------------------------------------------
    # Internal HTTP helper (stdlib urllib — no requests dependency)
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, body: Optional[Dict] = None) -> Any:
        url = f"{self._url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={
                _AUTH_HEADER: self._key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"ProxmoxAI {method} {path} → HTTP {exc.code}: {body_text}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"ProxmoxAI gateway unreachable at {self._url}: {exc.reason}"
            ) from exc


def from_config(cfg: Dict[str, Any]) -> ProxmoxSandbox:
    """
    Factory called by ProviderRegistry.
    All secrets come from cfg (injected from swe_team.yaml + env var expansion).
    Never hardcode credentials here.
    """
    return ProxmoxSandbox(
        gateway_url=cfg["gateway_url"],
        api_key=cfg["api_key"],
        node=cfg.get("node", "io"),
        default_cpu=cfg.get("default_cpu", 2),
        default_ram_gb=cfg.get("default_ram_gb", 4),
        default_disk_gb=cfg.get("default_disk_gb", 20),
        default_ttl_hours=cfg.get("default_ttl_hours", 2),
    )
