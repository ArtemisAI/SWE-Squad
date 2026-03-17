"""Collect logs from remote worker machines via SSH."""
import logging
import subprocess
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# Worker nodes to collect logs from.
# Override via environment variable SWE_REMOTE_NODES (JSON array) or
# configure in swe_team.yaml under monitor.remote_nodes.
#
# Example:
#   [{"name": "worker-1", "ssh": "agent@10.0.0.1", "log_dir": "~/project/logs"}]

def _load_remote_nodes():
    """Load remote node config from env var or return empty default."""
    import json
    import os
    raw = os.environ.get("SWE_REMOTE_NODES", "")
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return []

REMOTE_NODES = _load_remote_nodes()


def collect_remote_logs(local_dir: str = "logs/remote", timeout: int = 30) -> List[str]:
    """SSH into each worker, rsync their logs to a local directory.

    Returns list of local directories containing remote logs.
    """
    collected: List[str] = []
    local_base = Path(local_dir)

    for node in REMOTE_NODES:
        node_dir = local_base / node["name"]
        node_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Use rsync over SSH to pull logs (only *.log files, skip huge files)
            result = subprocess.run(
                [
                    "rsync", "-az", "--include=*.log", "--exclude=*",
                    "--max-size=10M", "--timeout=15",
                    "-e", "ssh -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
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
                result = subprocess.run(
                    [
                        "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                        node["ssh"],
                        f"find {node['log_dir']} -name '*.log' -mmin -180 -exec cat {{}} \\;",
                    ],
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
