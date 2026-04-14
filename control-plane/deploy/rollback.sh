#!/usr/bin/env bash
# Rollback from TypeScript control plane to Python runner.
# Usage: bash control-plane/deploy/rollback.sh
#
# This script:
# 1. Stops and disables the TypeScript daemon
# 2. Restores Python cron entries
# 3. Reports status
#
# Run from the project root (SWE-Squad/).

set -euo pipefail

echo "=== SWE-Squad Rollback: TypeScript -> Python ==="
echo "Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo ""

# ---------------------------------------------------------------------------
# 1. Stop TypeScript daemon
# ---------------------------------------------------------------------------

echo "[1/3] Stopping TypeScript daemon..."

if systemctl --user is-active swe-manager &>/dev/null; then
  systemctl --user stop swe-manager
  echo "  Daemon stopped"
else
  echo "  Daemon was not running"
fi

systemctl --user disable swe-manager 2>/dev/null || true
echo "  Daemon disabled (will not start on boot)"

# ---------------------------------------------------------------------------
# 2. Restore Python cron
# ---------------------------------------------------------------------------

echo "[2/3] Restoring Python cron entries..."

if crontab -l 2>/dev/null | grep -q "# CUTOVER:"; then
  crontab -l | sed 's|^# CUTOVER: ||' | crontab -
  echo "  Python cron entries restored"
else
  echo "  No CUTOVER markers found in crontab (Python cron may not have been modified)"
fi

# ---------------------------------------------------------------------------
# 3. Verify
# ---------------------------------------------------------------------------

echo "[3/3] Verifying..."

echo ""
echo "  Cron entries:"
crontab -l 2>/dev/null | grep "swe_team_runner" || echo "    (none found)"

echo ""
echo "  TypeScript daemon:"
systemctl --user is-active swe-manager 2>/dev/null && echo "    RUNNING (unexpected!)" || echo "    stopped"

echo ""
echo "=== Rollback complete ==="
echo ""
echo "Python runner will resume at next cron interval."
echo "Check: crontab -l | grep swe_team_runner"
