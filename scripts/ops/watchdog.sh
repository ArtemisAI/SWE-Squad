#!/usr/bin/env bash
# =============================================================================
# SWE-Squad Watchdog — ensures the daemon stays running 24/7
#
# Designed to be called from cron every 15 minutes AND at @reboot.
# If the daemon is alive: exits silently.
# If dead or stale: cleans up and restarts it.
#
# Logs to: logs/watchdog.log
# PID file: /tmp/swe_squad_daemon.pid
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PIDFILE="/tmp/swe_squad_daemon.pid"
LOCKFILE="/tmp/swe_squad_watchdog.lock"
LOGFILE="$REPO_ROOT/logs/swe_team.log"
WATCHDOG_LOG="$REPO_ROOT/logs/watchdog.log"
RUNNER="$REPO_ROOT/scripts/ops/swe_team_runner.py"
PYTHON="/usr/bin/python3"

mkdir -p "$REPO_ROOT/logs"
exec >> "$WATCHDOG_LOG" 2>&1

ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

# --- Prevent concurrent watchdog instances ---
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "$(ts): watchdog already running — exiting"
  exit 0
fi

# --- Check if daemon is alive ---
ALIVE=false
if [[ -f "$PIDFILE" ]]; then
  PID=$(cat "$PIDFILE")
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    ALIVE=true
    echo "$(ts): daemon alive (PID $PID)"
  else
    echo "$(ts): stale PID $PID in $PIDFILE — cleaning up"
    rm -f "$PIDFILE"
  fi
fi

# Also double-check via pgrep in case PID file is missing
if [[ "$ALIVE" == "false" ]]; then
  FOUND=$(pgrep -f "swe_team_runner.*daemon" || true)
  if [[ -n "$FOUND" ]]; then
    echo "$(ts): daemon found via pgrep (PID $FOUND) but PID file missing — writing"
    echo "$FOUND" > "$PIDFILE"
    ALIVE=true
  fi
fi

if [[ "$ALIVE" == "true" ]]; then
  # Check for stall: if last log line is > 90 min old, kill and restart
  if [[ -f "$LOGFILE" ]]; then
    LAST_MOD=$(stat -c %Y "$LOGFILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    AGE=$(( NOW - LAST_MOD ))
    if [[ $AGE -gt 5400 ]]; then  # 90 minutes
      PID=$(cat "$PIDFILE" 2>/dev/null || true)
      echo "$(ts): STALL DETECTED — log not updated for ${AGE}s — killing PID $PID and restarting"
      kill "$PID" 2>/dev/null || true
      sleep 2
      kill -9 "$PID" 2>/dev/null || true
      rm -f "$PIDFILE"
      ALIVE=false
    fi
  fi
fi

# --- Start daemon if not alive ---
if [[ "$ALIVE" == "false" ]]; then
  echo "$(ts): starting SWE-Squad daemon..."
  cd "$REPO_ROOT"

  # Load env
  if [[ -f "$REPO_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +a
  fi

  SWE_TEAM_ENABLED=true nohup \
    "$PYTHON" "$RUNNER" --daemon --interval 3600 \
    >> "$LOGFILE" 2>&1 &

  DAEMON_PID=$!
  echo "$DAEMON_PID" > "$PIDFILE"
  echo "$(ts): daemon started — PID $DAEMON_PID"
fi
