#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# propagate.sh — Immediately sync code to all registered worker nodes
# ═══════════════════════════════════════════════════════════════════════════════
#
# Triggers git pull/reset on every worker via SSH. Runs syncs in parallel for
# speed. Uses the scoped SSH config (config/ssh_workers.conf) so only
# authorized workers are reachable.
#
# Usage:
#   scripts/ops/propagate.sh                   # propagate SWE-Squad repo
#   scripts/ops/propagate.sh --project linkedai # propagate LinkedAi on workers
#   scripts/ops/propagate.sh --dry-run          # show what would run
#
# Exit codes:
#   0 — all workers synced
#   1 — one or more workers failed (partial propagation)
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SSH_CONF="${SWE_SSH_CONFIG:-$PROJECT_ROOT/config/ssh_workers.conf}"
LOG_FILE="${PROJECT_ROOT}/logs/propagate.log"
DRY_RUN=false
PROJECT="linkedai"  # default project to propagate on workers
TIMEOUT=30

# ─── Parse arguments ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)   PROJECT="$2"; shift 2 ;;
        --dry-run)   DRY_RUN=true; shift ;;
        --timeout)   TIMEOUT="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: propagate.sh [--project linkedai|swe-squad] [--dry-run] [--timeout N]"
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ─── Project paths on workers ────────────────────────────────────────────────
declare -A PROJECT_PATHS=(
    [linkedai]="~/Projects/LinkedAi"
    [swe-squad]="~/Projects/SWE-Squad"
)

REMOTE_PATH="${PROJECT_PATHS[$PROJECT]:-}"
if [[ -z "$REMOTE_PATH" ]]; then
    echo "ERROR: Unknown project '$PROJECT'. Known: ${!PROJECT_PATHS[*]}"
    exit 1
fi

# ─── Worker list (parsed from SSH config) ────────────────────────────────────
WORKERS=()
if [[ -f "$SSH_CONF" ]]; then
    while IFS= read -r line; do
        host=$(echo "$line" | awk '{print $2}')
        [[ "$host" == "*" ]] && continue
        WORKERS+=("$host")
    done < <(grep -E '^Host ' "$SSH_CONF" | grep -v '\*')
fi

if [[ ${#WORKERS[@]} -eq 0 ]]; then
    echo "ERROR: No workers found in $SSH_CONF"
    exit 1
fi

# ─── Logging ─────────────────────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG_FILE")"
log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

# ─── Sync command per project ────────────────────────────────────────────────
build_sync_cmd() {
    local path="$1"
    cat <<REMOTE_CMD
cd $path 2>/dev/null || { echo "SKIP: $path not found"; exit 0; }
git fetch origin main --quiet 2>/dev/null
LOCAL=\$(git rev-parse HEAD)
REMOTE=\$(git rev-parse origin/main)
if [ "\$LOCAL" = "\$REMOTE" ]; then
    echo "UP-TO-DATE \$(git rev-parse --short HEAD)"
else
    BEHIND=\$(git rev-list HEAD..origin/main --count)
    git reset --hard origin/main --quiet 2>/dev/null
    echo "SYNCED +\${BEHIND} commits -> \$(git rev-parse --short HEAD)"
fi
REMOTE_CMD
}

SYNC_CMD=$(build_sync_cmd "$REMOTE_PATH")

# ─── Propagate to all workers in parallel ────────────────────────────────────
log "Propagating '$PROJECT' to ${#WORKERS[@]} workers..."
if $DRY_RUN; then
    log "DRY RUN — would SSH to: ${WORKERS[*]}"
    log "Command: $SYNC_CMD"
    exit 0
fi

FAIL_COUNT=0
PIDS=()
TMPDIR_PROP=$(mktemp -d)

for worker in "${WORKERS[@]}"; do
    (
        result=$(timeout "$TIMEOUT" ssh -F "$SSH_CONF" "$worker" "$SYNC_CMD" 2>&1) \
            || result="FAILED: $result"
        echo "$result" > "$TMPDIR_PROP/$worker"
    ) &
    PIDS+=($!)
done

# Wait for all and collect results
for i in "${!WORKERS[@]}"; do
    wait "${PIDS[$i]}" 2>/dev/null || true
    worker="${WORKERS[$i]}"
    result=$(cat "$TMPDIR_PROP/$worker" 2>/dev/null || echo "TIMEOUT")
    if [[ "$result" == FAILED* ]] || [[ "$result" == "TIMEOUT" ]]; then
        log "  FAIL  $worker: $result"
        ((FAIL_COUNT++))
    else
        log "  OK    $worker: $result"
    fi
done

rm -rf "$TMPDIR_PROP"

if [[ $FAIL_COUNT -gt 0 ]]; then
    log "Propagation complete: $((${#WORKERS[@]} - FAIL_COUNT))/${#WORKERS[@]} succeeded, $FAIL_COUNT failed"
    exit 1
else
    log "Propagation complete: all ${#WORKERS[@]} workers synced"
    exit 0
fi
