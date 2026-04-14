#!/usr/bin/env bash
# Cutover from Python runner to TypeScript control plane.
# Usage: bash control-plane/deploy/cutover.sh
#
# This script:
# 1. Stops the Python runner (cron + any running process)
# 2. Validates the TypeScript control plane (dual-run)
# 3. Starts the TypeScript daemon
# 4. Monitors for 5 minutes
# 5. Reports status
#
# Run from the project root (SWE-Squad/).
#
# Rollback: bash control-plane/deploy/rollback.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_DIR"

echo "=== SWE-Squad Cutover: Python -> TypeScript ==="
echo "Project: $PROJECT_DIR"
echo "Time:    $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo ""

# ---------------------------------------------------------------------------
# 1. Stop Python runner
# ---------------------------------------------------------------------------

echo "[1/5] Stopping Python runner..."

# Comment out Python cron entries (preserving for rollback)
if crontab -l 2>/dev/null | grep -q "swe_team_runner.py"; then
  crontab -l 2>/dev/null | sed 's|^\([^#].*swe_team_runner.py.*\)|# CUTOVER: \1|' | crontab -
  echo "  Python cron entries commented out"
else
  echo "  No Python cron entries found"
fi

# Kill any running Python cycle
if pgrep -f "swe_team_runner.py" >/dev/null 2>&1; then
  pkill -f "swe_team_runner.py" 2>/dev/null || true
  echo "  Python runner process killed"
else
  echo "  No Python runner process found"
fi

echo "  Python runner stopped"

# ---------------------------------------------------------------------------
# 2. Run dual-run validation
# ---------------------------------------------------------------------------

echo ""
echo "[2/5] Running dual-run validation..."

VALIDATION_EXIT=0
npx tsx control-plane/src/validate/dual-run.ts || VALIDATION_EXIT=$?

if [ "$VALIDATION_EXIT" -ne 0 ]; then
  echo ""
  echo "  WARNING: Validation found diffs (exit code $VALIDATION_EXIT)."
  echo -n "  Continue with cutover? (y/n): "
  read -r CONTINUE
  if [ "$CONTINUE" != "y" ]; then
    echo "  Aborting cutover. Restoring Python cron..."
    if crontab -l 2>/dev/null | grep -q "# CUTOVER:"; then
      crontab -l | sed 's|^# CUTOVER: ||' | crontab -
      echo "  Python cron restored"
    fi
    exit 1
  fi
  echo "  Continuing despite diffs..."
fi

# ---------------------------------------------------------------------------
# 3. Start TypeScript daemon
# ---------------------------------------------------------------------------

echo ""
echo "[3/5] Starting TypeScript daemon..."

# Ensure service is installed
if ! systemctl --user cat swe-manager.service &>/dev/null; then
  echo "  Installing systemd service first..."
  mkdir -p ~/.config/systemd/user
  sed "s|WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|" \
    control-plane/deploy/swe-manager.service > "$HOME/.config/systemd/user/swe-manager.service"
  systemctl --user daemon-reload
fi

systemctl --user start swe-manager
sleep 2

if systemctl --user is-active swe-manager &>/dev/null; then
  echo "  Daemon running (PID: $(systemctl --user show swe-manager --property=MainPID --value))"
else
  echo "  FAIL: Daemon not running!"
  echo "  Check: journalctl --user -u swe-manager --no-pager -n 20"
  echo ""
  echo "  Rolling back..."
  bash control-plane/deploy/rollback.sh
  exit 1
fi

# ---------------------------------------------------------------------------
# 4. Wait and monitor
# ---------------------------------------------------------------------------

echo ""
echo "[4/5] Monitoring for 5 minutes..."

for i in $(seq 1 5); do
  sleep 60

  # Check daemon status
  daemon_status=$(systemctl --user is-active swe-manager 2>/dev/null || echo "STOPPED")
  echo -n "  Minute $i/5: daemon=$daemon_status"

  # Check status.json was written
  if [ -f data/swe_team/status.json ]; then
    # Try to extract TS-format fields first, then Python-format
    status_info=$(python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    # TS format uses camelCase
    open_count = d.get("ticketsOpen", d.get("tickets_open", "?"))
    verdict = d.get("stabilityVerdict", d.get("gate_verdict", "?"))
    print(f"open={open_count}, verdict={verdict}")
except Exception as e:
    print(f"parse error: {e}")
' < data/swe_team/status.json 2>/dev/null || echo "status.json unreadable")
    echo ", status: $status_info"
  else
    echo ", status.json: not yet written"
  fi

  # If daemon stopped, bail out
  if [ "$daemon_status" = "STOPPED" ] || [ "$daemon_status" = "failed" ]; then
    echo ""
    echo "  FAIL: Daemon stopped during monitoring!"
    echo "  Check: journalctl --user -u swe-manager --no-pager -n 30"
    echo ""
    echo "  Consider rolling back: bash control-plane/deploy/rollback.sh"
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# 5. Final status
# ---------------------------------------------------------------------------

echo ""
echo "[5/5] Cutover complete"
echo ""
systemctl --user status swe-manager --no-pager || true
echo ""
echo "=== Cutover successful ==="
echo ""
echo "Monitor:  journalctl --user -u swe-manager -f"
echo "Status:   cat data/swe_team/status.json | python3 -m json.tool"
echo "Rollback: bash control-plane/deploy/rollback.sh"
