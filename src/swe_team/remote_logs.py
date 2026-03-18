"""Collect logs from remote worker machines via SSH.

SSH access is scoped via a dedicated config file (SWE_SSH_CONFIG env var or
``config/ssh_workers.conf`` relative to the project root).  The config uses
``IdentitiesOnly yes`` with a project-specific key so the runner can ONLY
reach explicitly listed worker nodes — never the primary orchestrator.
"""
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Worker nodes to collect logs from.
# Override via environment variable SWE_REMOTE_NODES (JSON array) or
# configure in swe_team.yaml under monitor.remote_nodes.
#
# Example:
#   [{"name": "worker-1", "ssh": "linkedai-browser-1", "log_dir": "~/Projects/LinkedAi/logs"}]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def _ssh_config_path() -> Optional[str]:
    """Return path to the scoped SSH config, or None if not found."""
    explicit = os.environ.get("SWE_SSH_CONFIG")
    if explicit and Path(explicit).is_file():
        return explicit
    default = _PROJECT_ROOT / "config" / "ssh_workers.conf"
    if default.is_file():
        return str(default)
    return None


def _load_remote_nodes():
    """Load remote node config from env var or return empty default."""
    import json
    raw = os.environ.get("SWE_REMOTE_NODES", "")
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return []

REMOTE_NODES = _load_remote_nodes()


def collect_remote_logs(local_dir: str = "logs/remote", timeout: int = 30, nodes: Optional[List[Dict]] = None) -> List[str]:
    """SSH into each worker, rsync their logs to a local directory.

    nodes: list of dicts with keys: name, ssh, log_dir
           Falls back to REMOTE_NODES (from SWE_REMOTE_NODES env var) if not provided.

    Returns list of local directories containing remote logs.
    """
    effective_nodes = nodes if nodes is not None else REMOTE_NODES
    if not effective_nodes:
        return []
    collected: List[str] = []
    local_base = Path(local_dir)

    for node in effective_nodes:
        node_dir = local_base / node["name"]
        node_dir.mkdir(parents=True, exist_ok=True)

        ssh_conf = _ssh_config_path()
        ssh_base = "ssh"
        if ssh_conf:
            ssh_base = f"ssh -F {ssh_conf}"

        try:
            # Use rsync over SSH to pull logs (only *.log files, skip huge files)
            result = subprocess.run(
                [
                    "rsync", "-az", "--include=*.log", "--exclude=*",
                    "--max-size=10M", "--timeout=15",
                    "-e", ssh_base,
                    f"{node['ssh']}:{node['log_dir']}/",
                    str(node_dir) + "/",
                ],
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0:
                log_count = len(list(node_dir.glob("*.log")))
                logger.info("Collected %d logs from %s", log_count, node["name"])
                collected.append(str(node_dir))
            else:
                logger.warning("rsync from %s failed: %s", node["name"], result.stderr[:200])
        except subprocess.TimeoutExpired:
            logger.warning("Timeout collecting logs from %s", node["name"])
        except FileNotFoundError:
            # rsync not installed, fall back to SSH cat
            try:
                ssh_cmd = ["ssh"]
                if ssh_conf:
                    ssh_cmd.extend(["-F", ssh_conf])
                ssh_cmd.append(node["ssh"])
                ssh_cmd.append(
                    f"find {node['log_dir']} -name '*.log' -mmin -180 -exec cat {{}} \\;"
                )
                result = subprocess.run(
                    ssh_cmd,
                    capture_output=True, text=True, timeout=timeout,
                )
                if result.returncode == 0 and result.stdout:
                    combined = node_dir / f"{node['name']}_combined.log"
                    combined.write_text(result.stdout)
                    logger.info("Collected combined log from %s (%d bytes)", node["name"], len(result.stdout))
                    collected.append(str(node_dir))
            except Exception:
                logger.warning("Failed to collect logs from %s via SSH", node["name"])
        except Exception:
            logger.exception("Error collecting logs from %s", node["name"])

    return collected
