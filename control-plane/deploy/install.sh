#!/usr/bin/env bash
# Install SWE-Squad TypeScript control plane on a VM.
# Usage: bash control-plane/deploy/install.sh
#
# Prerequisites: Node.js >= 20.6.0, pnpm, .env configured
#
# This script:
# 1. Validates Node.js version
# 2. Installs dependencies
# 3. Runs typecheck + tests
# 4. Runs smoke test
# 5. Installs systemd service
# 6. Runs dual-run validation
#
# Run from the project root (SWE-Squad/).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== SWE-Squad Control Plane Install ==="
echo "Project: $PROJECT_DIR"
echo ""

# ---------------------------------------------------------------------------
# 1. Check Node.js version
# ---------------------------------------------------------------------------

echo "[1/7] Checking Node.js..."
if ! command -v node &>/dev/null; then
  echo "  ERROR: Node.js not found. Install Node.js >= 20.6.0"
  exit 1
fi

node_version=$(node --version | sed 's/v//')
node_major=$(echo "$node_version" | cut -d. -f1)
node_minor=$(echo "$node_version" | cut -d. -f2)

echo "  Node.js: v$node_version"

if [ "$node_major" -lt 20 ] || { [ "$node_major" -eq 20 ] && [ "$node_minor" -lt 6 ]; }; then
  echo "  ERROR: Node.js >= 20.6.0 required (found $node_version)"
  exit 1
fi
echo "  OK"

# ---------------------------------------------------------------------------
# 2. Check pnpm
# ---------------------------------------------------------------------------

echo "[2/7] Checking pnpm..."
if ! command -v pnpm &>/dev/null; then
  echo "  ERROR: pnpm not found. Install: npm install -g pnpm"
  exit 1
fi
echo "  pnpm: $(pnpm --version)"

# ---------------------------------------------------------------------------
# 3. Install dependencies
# ---------------------------------------------------------------------------

echo "[3/7] Installing dependencies..."
cd "$PROJECT_DIR"
pnpm install --filter @swe-squad/control-plane
echo "  Dependencies installed"

# ---------------------------------------------------------------------------
# 4. Typecheck + tests
# ---------------------------------------------------------------------------

echo "[4/7] Running typecheck..."
pnpm --filter @swe-squad/control-plane run typecheck
echo "  Typecheck passed"

echo "[5/7] Running tests..."
pnpm --filter @swe-squad/control-plane run test
echo "  Tests passed"

# ---------------------------------------------------------------------------
# 5. Smoke test
# ---------------------------------------------------------------------------

echo "[6/7] Running smoke test..."
npx tsx control-plane/src/smoke-test.ts || {
  echo "  WARN: Smoke test had failures (non-blocking for install)"
}

# ---------------------------------------------------------------------------
# 6. Install systemd service
# ---------------------------------------------------------------------------

echo "[7/7] Installing systemd service..."
mkdir -p ~/.config/systemd/user

UNIT_FILE="$HOME/.config/systemd/user/swe-manager.service"

# Substitute the working directory to match this machine
sed "s|WorkingDirectory=.*|WorkingDirectory=$PROJECT_DIR|" \
  "$PROJECT_DIR/control-plane/deploy/swe-manager.service" > "$UNIT_FILE"

systemctl --user daemon-reload
echo "  Systemd service installed: $UNIT_FILE"

# ---------------------------------------------------------------------------
# 7. Dual-run validation (optional -- depends on Supabase access)
# ---------------------------------------------------------------------------

echo ""
echo "=== Dual-run validation ==="
if [ -n "${SUPABASE_URL:-}" ] && [ -n "${SUPABASE_ANON_KEY:-}" ]; then
  npx tsx control-plane/src/validate/dual-run.ts || {
    echo "WARN: Dual-run diffs detected (review above)"
  }
else
  echo "SKIP: SUPABASE_URL/SUPABASE_ANON_KEY not set -- skipping dual-run"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "=== Install complete ==="
echo ""
echo "To start:          systemctl --user start swe-manager"
echo "To enable on boot: systemctl --user enable swe-manager"
echo "To check status:   systemctl --user status swe-manager"
echo "To view logs:      journalctl --user -u swe-manager -f"
echo ""
echo "Cutover script:    bash control-plane/deploy/cutover.sh"
echo "Rollback script:   bash control-plane/deploy/rollback.sh"
