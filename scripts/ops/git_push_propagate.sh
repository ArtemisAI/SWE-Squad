#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# git_push_propagate.sh — Push to origin and immediately propagate to workers
# ═══════════════════════════════════════════════════════════════════════════════
#
# Drop-in replacement for `git push`. Runs the push, then fires propagation
# to all registered workers in the background.
#
# Usage:
#   scripts/ops/git_push_propagate.sh [git push args...]
#   git pushprop [args...]   # via git alias
#
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Run the actual git push (pass through all arguments)
echo "Pushing..."
git push "$@"
PUSH_EXIT=$?

if [[ $PUSH_EXIT -ne 0 ]]; then
    exit $PUSH_EXIT
fi

# Detect which project to propagate based on the repo
REMOTE_URL=$(git config --get remote.origin.url 2>/dev/null || echo "")

if [[ "$REMOTE_URL" == *"LinkedAi"* ]]; then
    PROJECT="linkedai"
elif [[ "$REMOTE_URL" == *"SWE-Squad"* ]]; then
    PROJECT="linkedai"  # SWE-Squad pushes still propagate LinkedAi workers
else
    echo "Unknown repo — skipping propagation"
    exit 0
fi

# Fire propagation in background
echo ""
echo "Propagating to workers..."
bash "$SCRIPT_DIR/propagate.sh" --project "$PROJECT" &
PROP_PID=$!

# Wait for it (or timeout after 30s)
if wait "$PROP_PID" 2>/dev/null; then
    echo "Done."
else
    echo "Propagation may still be running in background (PID $PROP_PID)"
fi
