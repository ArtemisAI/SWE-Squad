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


def fetch_worker_logs(
    worker_name: str,
    *,
    since_minutes: int = 60,
    max_lines: int = 500,
    log_pattern: str = "*.log",
    timeout: int = 20,
) -> Optional[str]:
    """Fetch recent log lines from a specific worker on demand.

    Used by InvestigatorAgent during root-cause analysis to pull fresh logs
    from a particular worker rather than relying on the last monitor scan.

    Returns combined log tail as a string, or None on failure.
    """
    ssh_conf = _ssh_config_path()
    if not ssh_conf:
        logger.warning("No SSH config found — cannot fetch worker logs")
        return None

    # Build SSH command to tail recent logs on the remote
    remote_cmd = (
        f"find ~/Projects/LinkedAi/logs -name '{log_pattern}' "
        f"-mmin -{since_minutes} -type f "
        f"-exec tail -n {max_lines} {{}} + 2>/dev/null | tail -n {max_lines}"
    )

    ssh_cmd = ["ssh", "-F", ssh_conf, worker_name, remote_cmd]

    try:
        result = subprocess.run(
            ssh_cmd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info(
                "Fetched %d bytes of logs from %s (last %d min)",
                len(result.stdout), worker_name, since_minutes,
            )
            return result.stdout
        logger.warning(
            "No recent logs from %s (rc=%d)", worker_name, result.returncode,
        )
        return None
    except subprocess.TimeoutExpired:
        logger.warning("Timeout fetching logs from %s", worker_name)
        return None
    except Exception:
        logger.exception("Failed to fetch logs from %s", worker_name)
        return None


def list_available_workers(timeout: int = 10) -> List[Dict[str, str]]:
    """Return list of reachable workers from ssh_workers.conf.

    Each entry has keys: name, reachable (bool), hostname.
    Used for health checks and investigator worker selection.
    """
    ssh_conf = _ssh_config_path()
    if not ssh_conf:
        return []

    # Parse hosts from the SSH config
    conf_path = Path(ssh_conf)
    if not conf_path.is_file():
        return []

    workers: List[Dict[str, str]] = []
    current_host = None
    current_hostname = None
    for line in conf_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("Host ") and "*" not in stripped:
            if current_host:
                workers.append({"name": current_host, "hostname": current_hostname or ""})
            current_host = stripped.split()[1]
            current_hostname = None
        elif stripped.startswith("HostName ") and current_host:
            current_hostname = stripped.split()[1]
    if current_host:
        workers.append({"name": current_host, "hostname": current_hostname or ""})

    # Probe reachability
    for w in workers:
        try:
            result = subprocess.run(
                ["ssh", "-F", ssh_conf, w["name"], "echo ok"],
                capture_output=True, text=True, timeout=timeout,
            )
            w["reachable"] = str(result.returncode == 0)
        except Exception:
            w["reachable"] = "False"

    return workers
