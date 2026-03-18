#!/usr/bin/env python3
"""
SWE-Squad Self-Healing Monitor
================================
Runs every 5 minutes from cron. Detects system problems and responds
in two tiers:

  Tier 1 — Immediate auto-fix (no LLM):
    • Daemon stall / dead process → restart
    • Stalled tickets (>2h in investigating/in_development) → reset
    • False regression tickets (gh-issue-* fingerprints) → resolve

  Tier 2 — Claude Code invocation (rate-limited, max once per 30 min):
    • Repeated ERRORs in logs suggesting a code bug
    • Regression explosion (>5 regression tickets in 35 min)
    • Any issue self_heal cannot fix automatically

Outputs: logs/self_heal.log  (structured, human-readable)
         Telegram notification on any action taken

Usage (cron, every 5 min):
    */5 * * * * cd /home/agent/SWE-Squad && ./.venv/bin/python3 scripts/ops/self_heal.py >> logs/self_heal.log 2>&1
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Project root ──────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Load .env
_env_file = REPO_ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# ── Constants ─────────────────────────────────────────────────────────────────
DAEMON_PIDFILE   = Path("/tmp/swe_squad_daemon.pid")
DAEMON_RUNNER    = REPO_ROOT / "scripts/ops/swe_team_runner.py"
LOG_FILE         = REPO_ROOT / "logs/swe_team.log"
SELF_HEAL_LOCK   = Path("/tmp/swe_squad_self_heal.lock")
CLAUDE_LOCK      = Path("/tmp/swe_squad_claude_invoked.lock")
CLAUDE_COOLDOWN  = 1800   # seconds (30 min) between Claude invocations
STALL_THRESHOLD  = timedelta(hours=2)
DAEMON_STALL_AGE = 5400   # seconds (90 min) — log not updated → daemon stalled
LOG_ERROR_THRESHOLD = 4   # >N ERROR lines in last 100 → invoke Claude
REGRESSION_BURST_THRESHOLD = 5   # >N regression tickets in 35 min → invoke Claude
PYTHON           = sys.executable

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] self_heal: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)
logger = logging.getLogger("self_heal")


# ── Telegram notification ─────────────────────────────────────────────────────

def notify(msg: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat  = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        subprocess.run(
            [
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                "-d", f"chat_id={chat}",
                "-d", f"text=🔧 SWE-Squad self_heal: {msg[:3000]}",
            ],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _daemon_pid() -> int | None:
    if DAEMON_PIDFILE.exists():
        try:
            return int(DAEMON_PIDFILE.read_text().strip())
        except Exception:
            pass
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ── Tier 1: Daemon check ──────────────────────────────────────────────────────

def check_and_restart_daemon() -> bool:
    """Return True if daemon was restarted."""
    pid = _daemon_pid()

    # Check by PID file
    if pid and _pid_alive(pid):
        # Daemon is alive — check for log stall (process alive but not writing)
        if LOG_FILE.exists():
            age = time.time() - LOG_FILE.stat().st_mtime
            if age > DAEMON_STALL_AGE:
                logger.warning("Daemon PID %d alive but log stalled (%ds) — killing", pid, age)
                _kill_daemon(pid)
            else:
                return False  # healthy
        else:
            return False  # no log yet, daemon just started

    # Daemon not running — start it
    logger.warning("Daemon not running (pid=%s) — restarting", pid)
    _start_daemon()
    return True


def _kill_daemon(pid: int) -> None:
    try:
        os.kill(pid, 15)
        time.sleep(2)
        if _pid_alive(pid):
            os.kill(pid, 9)
    except Exception:
        pass
    DAEMON_PIDFILE.unlink(missing_ok=True)


def _start_daemon() -> int:
    env = {**os.environ, "SWE_TEAM_ENABLED": "true"}
    proc = subprocess.Popen(
        [PYTHON, str(DAEMON_RUNNER), "--daemon", "--interval", "3600"],
        stdout=open(LOG_FILE, "a"),
        stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
        env=env,
    )
    DAEMON_PIDFILE.write_text(str(proc.pid))
    logger.info("Daemon started — PID %d", proc.pid)
    notify(f"Daemon restarted — PID {proc.pid}")
    return proc.pid


# ── Tier 1: Stalled ticket reset ─────────────────────────────────────────────

def reset_stalled_tickets() -> list[str]:
    """Reset IN_DEVELOPMENT/INVESTIGATING tickets stalled >2h. Returns list of reset IDs."""
    try:
        from src.swe_team.supabase_store import SupabaseTicketStore
        from src.swe_team.models import TicketStatus

        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_ANON_KEY")
        if not url or not key:
            return []

        store = SupabaseTicketStore(supabase_url=url, supabase_key=key, team_id="swe-squad-1")
        all_tickets = store.list_all()
        now = _now()

        stall_statuses = {TicketStatus.INVESTIGATING, TicketStatus.IN_DEVELOPMENT}
        reset_ids = []

        for t in all_tickets:
            if t.status not in stall_statuses:
                continue
            updated = _parse_ts(t.updated_at)
            if updated and (now - updated) > STALL_THRESHOLD:
                logger.warning(
                    "Resetting stalled ticket %s [%s] age=%s",
                    t.ticket_id, t.status.value, now - updated,
                )
                t.status = TicketStatus.INVESTIGATION_COMPLETE
                t.updated_at = now.isoformat()
                t.metadata.pop("branch", None)
                store.add(t)
                reset_ids.append(t.ticket_id)

        if reset_ids:
            notify(f"Reset {len(reset_ids)} stalled ticket(s): {', '.join(reset_ids[:5])}")

        return reset_ids
    except Exception as exc:
        logger.warning("reset_stalled_tickets failed: %s", exc)
        return []


# ── Tier 1: False regression cleanup ─────────────────────────────────────────

def clear_false_regressions() -> list[str]:
    """Resolve tickets with is_regression=True and gh-issue-* fingerprints."""
    try:
        from src.swe_team.supabase_store import SupabaseTicketStore
        from src.swe_team.models import TicketStatus

        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_ANON_KEY")
        if not url or not key:
            return []

        store = SupabaseTicketStore(supabase_url=url, supabase_key=key, team_id="swe-squad-1")
        all_tickets = store.list_all()

        false_reg = [
            t for t in all_tickets
            if t.metadata.get("is_regression")
            and str(t.metadata.get("fingerprint", "")).startswith("gh-issue-")
        ]

        fixed = []
        for t in false_reg:
            try:
                t.metadata["resolution_note"] = "false_regression_guard"
                t.metadata["is_regression"] = False
                t.transition(TicketStatus.RESOLVED)
                store.add(t)
                fixed.append(t.ticket_id)
                logger.info("Cleared false regression %s", t.ticket_id)
            except Exception as e:
                logger.warning("Could not clear false regression %s: %s", t.ticket_id, e)

        if fixed:
            notify(f"Cleared {len(fixed)} false regression ticket(s)")

        return fixed
    except Exception as exc:
        logger.warning("clear_false_regressions failed: %s", exc)
        return []


# ── Tier 1: PR health check ──────────────────────────────────────────────────

def check_pr_health() -> list[str]:
    """Tier 1: Check for stale and conflicting PRs.

    Returns list of alert messages (empty = all healthy).
    """
    alerts: list[str] = []
    try:
        from src.swe_team.knowledge_store import KnowledgeGraphStore
        from src.swe_team.models import EdgeType
    except ImportError:
        return alerts

    try:
        team_id = os.environ.get("SWE_TEAM_ID", "default")
        store = KnowledgeGraphStore(team_id=team_id)

        open_prs = store.list_open_prs()
        if not open_prs:
            return alerts

        now = _now()

        for pr in open_prs:
            # Stale PR check (>48h without review)
            try:
                created = datetime.fromisoformat(pr.created_at.replace("Z", "+00:00"))
                age = now - created
                if age > timedelta(hours=48) and pr.review_status == "pending":
                    msg = f"Stale PR: {pr.pr_id} ({pr.title[:50]}) — open {age.days}d without review"
                    alerts.append(msg)
                    logger.warning(msg)
            except (ValueError, TypeError):
                pass

            # Conflict check
            try:
                edges = store.get_edges(pr.pr_id, edge_type=EdgeType.CONFLICTS_WITH)
                if edges:
                    targets = [e.target_id if e.source_id == pr.pr_id else e.source_id for e in edges]
                    msg = f"PR conflict: {pr.pr_id} conflicts with {', '.join(targets[:3])}"
                    alerts.append(msg)
                    logger.warning(msg)
            except Exception:
                pass

    except Exception:
        logger.warning("PR health check failed (non-fatal)", exc_info=True)

    return alerts


# ── Tier 2 detection: log error rate ─────────────────────────────────────────

def count_recent_errors() -> tuple[int, list[str]]:
    """Return (count, [lines]) of ERROR/CRITICAL lines in the last 100 log lines."""
    if not LOG_FILE.exists():
        return 0, []
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines()[-100:]
        error_lines = [l for l in lines if " [ERROR] " in l or " [CRITICAL] " in l]
        # Exclude expected noise
        noise = ("rsync", "ssh:", "Could not resolve hostname", "remote log")
        error_lines = [l for l in error_lines if not any(n in l for n in noise)]
        return len(error_lines), error_lines
    except Exception:
        return 0, []


def count_regression_burst() -> int:
    """Return count of regression tickets created in the last 35 min."""
    try:
        from src.swe_team.supabase_store import SupabaseTicketStore

        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_ANON_KEY")
        if not url or not key:
            return 0

        store = SupabaseTicketStore(supabase_url=url, supabase_key=key, team_id="swe-squad-1")
        cutoff = _now() - timedelta(minutes=35)
        return sum(
            1 for t in store.list_all()
            if t.metadata.get("is_regression") and _parse_ts(t.created_at) and _parse_ts(t.created_at) > cutoff
        )
    except Exception:
        return 0


# ── Tier 2: Claude invocation ─────────────────────────────────────────────────

def _claude_cooldown_ok() -> bool:
    """True if enough time has passed since the last Claude invocation."""
    if not CLAUDE_LOCK.exists():
        return True
    age = time.time() - CLAUDE_LOCK.stat().st_mtime
    return age >= CLAUDE_COOLDOWN


def invoke_claude(reason: str, error_lines: list[str]) -> None:
    """Invoke claude --print with the health audit prompt."""
    if not _claude_cooldown_ok():
        remaining = CLAUDE_COOLDOWN - (time.time() - CLAUDE_LOCK.stat().st_mtime)
        logger.info("Claude cooldown active — %.0fs remaining, skipping invocation", remaining)
        return

    prompt_path = REPO_ROOT / "config/swe_team/programs/health_audit_auto.md"
    if not prompt_path.exists():
        logger.warning("health_audit_auto.md not found — cannot invoke Claude")
        return

    # Stamp cooldown immediately to prevent concurrent invocations
    CLAUDE_LOCK.touch()

    # Build the context snippet to inject into the prompt
    error_context = "\n".join(error_lines[-20:]) if error_lines else "(no specific errors)"
    prompt_template = prompt_path.read_text()
    prompt = prompt_template.replace("{{TRIGGER_REASON}}", reason)
    prompt = prompt.replace("{{ERROR_CONTEXT}}", error_context)
    prompt = prompt.replace("{{TIMESTAMP}}", _now().isoformat())

    heal_log = REPO_ROOT / "logs/self_heal_claude.log"
    logger.info("Invoking Claude Code for health audit (reason: %s)", reason)
    notify(f"Invoking Claude Code health audit — reason: {reason}")

    try:
        result = subprocess.run(
            ["/usr/bin/claude", "--print", "--dangerously-skip-permissions", prompt],
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max
            cwd=str(REPO_ROOT),
            env={**os.environ, "SWE_TEAM_ENABLED": "true"},
        )
        output = (result.stdout or "") + (result.stderr or "")
        with open(heal_log, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[{_now().isoformat()}] TRIGGER: {reason}\n")
            f.write(f"{'='*60}\n")
            f.write(output[:10000])
            f.write("\n")

        logger.info(
            "Claude audit complete (rc=%d, %d chars output)",
            result.returncode, len(output),
        )
        # Brief summary to Telegram
        summary = output[:500].replace("\n", " ")
        notify(f"Claude audit done: {summary}")

    except subprocess.TimeoutExpired:
        logger.warning("Claude invocation timed out after 600s")
        notify("Claude health audit timed out after 10 min")
    except Exception as exc:
        logger.warning("Claude invocation failed: %s", exc)
        notify(f"Claude health audit failed: {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Prevent concurrent self_heal runs
    lock_fd = open(SELF_HEAL_LOCK, "w")
    import fcntl
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.debug("Another self_heal instance is running — exiting")
        return

    logger.info("--- self_heal run starting ---")
    actions_taken = []
    invoke_claude_flag = False
    claude_reason = ""
    error_lines: list[str] = []

    # ── Tier 1: Daemon check ──────────────────────────────────────────────────
    if check_and_restart_daemon():
        actions_taken.append("daemon_restarted")

    # ── Tier 1: Stalled tickets ───────────────────────────────────────────────
    reset_ids = reset_stalled_tickets()
    if reset_ids:
        actions_taken.append(f"reset_stalled:{len(reset_ids)}")

    # ── Tier 1: False regressions ─────────────────────────────────────────────
    fixed_reg = clear_false_regressions()
    if fixed_reg:
        actions_taken.append(f"cleared_false_regressions:{len(fixed_reg)}")

    # ── Tier 1: PR health ─────────────────────────────────────────────────────
    pr_alerts = check_pr_health()
    if pr_alerts:
        actions_taken.append(f"pr_health_alerts:{len(pr_alerts)}")
        notify("\n".join(pr_alerts))
    else:
        logger.info("PR health: all healthy")

    # ── Tier 2 detection: log errors ─────────────────────────────────────────
    error_count, error_lines = count_recent_errors()
    if error_count >= LOG_ERROR_THRESHOLD:
        logger.warning(
            "%d ERROR lines in recent log — threshold %d exceeded",
            error_count, LOG_ERROR_THRESHOLD,
        )
        invoke_claude_flag = True
        claude_reason = f"{error_count} ERROR lines in swe_team.log"
        actions_taken.append(f"error_threshold_exceeded:{error_count}")

    # ── Tier 2 detection: regression burst ───────────────────────────────────
    reg_burst = count_regression_burst()
    if reg_burst >= REGRESSION_BURST_THRESHOLD:
        logger.warning(
            "Regression burst: %d regression tickets in last 35 min",
            reg_burst,
        )
        invoke_claude_flag = True
        claude_reason = claude_reason or f"{reg_burst} regression tickets in 35 min"
        actions_taken.append(f"regression_burst:{reg_burst}")

    # ── Tier 2: Invoke Claude if needed ──────────────────────────────────────
    if invoke_claude_flag:
        invoke_claude(claude_reason, error_lines)
        actions_taken.append("claude_invoked")
    elif actions_taken:
        logger.info("Tier 1 fixes applied: %s", ", ".join(actions_taken))
    else:
        logger.info("All healthy — no action needed")

    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()


if __name__ == "__main__":
    main()
