#!/bin/bash
# =============================================================================
# sync_public.sh — Sync DEV → Public repo (separate folder approach)
# =============================================================================
#
# Usage:
#   ./scripts/ops/sync_public.sh              # Preview what will sync
#   ./scripts/ops/sync_public.sh --push       # Execute the sync
#   ./scripts/ops/sync_public.sh --push -m "Custom commit message"
#
# Expects:
#   DEV repo:    /home/artemisai/PROJECTS/SWE-Squad/
#   Public repo: /home/artemisai/PROJECTS/SWE-Squad-Public/
#
# How it works:
#   1. rsync tracked files from DEV → Public folder (excludes .git, .env, docs/)
#   2. Commit in the Public folder with a clean message (no Co-Authored-By)
#   3. Push from Public folder to public origin
# =============================================================================

set -euo pipefail

# Paths — adjust if your layout differs
DEV_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PUBLIC_DIR="${DEV_DIR}/../SWE-Squad-Public"
SYNC_FILE="${DEV_DIR}/.last_public_sync"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

# Parse args
DO_PUSH=false
CUSTOM_MSG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --push) DO_PUSH=true; shift ;;
        -m) CUSTOM_MSG="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--push] [-m \"message\"]"
            echo ""
            echo "  (no args)   Preview changes"
            echo "  --push      Sync and push to public"
            echo "  -m MSG      Custom commit message"
            exit 0
            ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Validate directories
if [[ ! -d "$DEV_DIR/.git" ]]; then
    echo -e "${RED}Error: DEV repo not found at $DEV_DIR${NC}"; exit 1
fi
if [[ ! -d "$PUBLIC_DIR/.git" ]]; then
    echo -e "${RED}Error: Public repo not found at $PUBLIC_DIR${NC}"
    echo "Clone it first: git clone https://github.com/ArtemisAI/SWE-Squad.git $PUBLIC_DIR"
    exit 1
fi

# Ensure DEV working tree is clean
if [[ -n "$(cd "$DEV_DIR" && git status --porcelain)" ]]; then
    echo -e "${RED}Error: DEV repo has uncommitted changes. Commit or stash first.${NC}"
    exit 1
fi

# Get DEV HEAD info
DEV_HEAD=$(cd "$DEV_DIR" && git rev-parse HEAD)
LAST_SYNC=""
if [[ -f "$SYNC_FILE" ]]; then
    LAST_SYNC=$(cat "$SYNC_FILE")
fi

if [[ "$LAST_SYNC" == "$DEV_HEAD" ]]; then
    echo -e "${GREEN}Already in sync. No changes since last sync.${NC}"
    exit 0
fi

# Gather commits since last sync
if [[ -n "$LAST_SYNC" ]]; then
    COMMIT_COUNT=$(cd "$DEV_DIR" && git rev-list --count "$LAST_SYNC..$DEV_HEAD" 2>/dev/null || echo "?")
    COMMITS=$(cd "$DEV_DIR" && git log --oneline "$LAST_SYNC..$DEV_HEAD" 2>/dev/null || echo "(all)")
else
    COMMIT_COUNT=$(cd "$DEV_DIR" && git rev-list --count HEAD)
    COMMITS=$(cd "$DEV_DIR" && git log --oneline)
fi

# Dry-run rsync to see what would change
RSYNC_PREVIEW=$(rsync -avn --delete \
    --exclude='.git' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='!.env.example' \
    --exclude='.last_public_sync' \
    --exclude='docs/' \
    --exclude='data/' \
    --exclude='logs/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache/' \
    "$DEV_DIR/" "$PUBLIC_DIR/" 2>/dev/null | grep -E '^(deleting |>|<)' | head -30 || echo "(no file changes)")

echo ""
echo -e "${BOLD}=== Public Sync Preview ===${NC}"
echo "  DEV repo:     $DEV_DIR"
echo "  Public repo:  $PUBLIC_DIR"
echo "  Last sync:    ${LAST_SYNC:-"(never)"}"
echo "  DEV HEAD:     $DEV_HEAD"
echo "  New commits:  $COMMIT_COUNT"
echo ""
echo -e "${BOLD}  DEV commits since last sync:${NC}"
echo "$COMMITS" | sed 's/^/    /'
echo ""
echo -e "${BOLD}  File changes:${NC}"
echo "$RSYNC_PREVIEW" | sed 's/^/    /'
echo ""

# Build commit message
if [[ -n "$CUSTOM_MSG" ]]; then
    SYNC_MSG="$CUSTOM_MSG"
else
    SUBJECTS=$(echo "$COMMITS" | sed 's/^[a-f0-9]* /- /' | grep -v 'Co-Authored-By')
    SYNC_MSG="$(cat <<MSGEOF
Update — ${COMMIT_COUNT} change(s)

${SUBJECTS}
MSGEOF
)"
fi

echo -e "${BOLD}  Commit message:${NC}"
echo "$SYNC_MSG" | sed 's/^/    /'
echo ""

if [[ "$DO_PUSH" != true ]]; then
    echo -e "${YELLOW}Dry run — no changes made. Use --push to execute.${NC}"
    exit 0
fi

# ── Execute ──────────────────────────────────────────────────────────

echo -e "${GREEN}Syncing files...${NC}"

# rsync DEV → Public (excluding secrets and internal files)
rsync -a --delete \
    --exclude='.git' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='!.env.example' \
    --exclude='.last_public_sync' \
    --exclude='docs/' \
    --exclude='data/' \
    --exclude='logs/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache/' \
    "$DEV_DIR/" "$PUBLIC_DIR/"

# Scan for secrets before committing
echo "  Scanning for secrets..."
# Scan for real secrets (not template placeholders like "your_xxx_here")
SECRETS_CHECK=$(cd "$PUBLIC_DIR" && git ls-files 2>/dev/null | xargs grep -l -PE 'github_pat_[A-Za-z0-9]{30,}|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{32,}|BOT_TOKEN=[0-9]{8,}:[A-Za-z]' 2>/dev/null || true)
if [[ -n "$SECRETS_CHECK" ]]; then
    echo -e "${RED}ABORT: Potential secrets detected:${NC}"
    echo "$SECRETS_CHECK"
    echo "Reverting public folder..."
    (cd "$PUBLIC_DIR" && git checkout -- . && git clean -fd)
    exit 1
fi

# Commit and push from public folder
echo "  Committing..."
cd "$PUBLIC_DIR"
git add -A

# Check if there are actual changes
if git diff --cached --quiet; then
    echo -e "${GREEN}No file differences — repos already match.${NC}"
    echo "$DEV_HEAD" > "$SYNC_FILE"
    exit 0
fi

git commit -m "$SYNC_MSG"

echo "  Pushing to public..."
git push origin main

# Record sync point in DEV repo
echo "$DEV_HEAD" > "$SYNC_FILE"

echo ""
echo -e "${GREEN}Sync complete.${NC}"
echo "  DEV HEAD:      $DEV_HEAD"
echo "  Public HEAD:   $(git rev-parse HEAD)"
echo "  Public repo:   https://github.com/ArtemisAI/SWE-Squad"
