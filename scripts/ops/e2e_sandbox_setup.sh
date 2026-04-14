#!/usr/bin/env bash
# ── E2E Sandbox Setup Script ─────────────────────────────────────────────────
# Run this on the SWE-Squad bot VM to:
#   1. Accept all pending repo invitations
#   2. Assign test issues to the bot account
#   3. Verify access and clone repos
#
# Prerequisites:
#   - gh auth login as $SWE_GITHUB_ACCOUNT
#   - This script is idempotent (safe to re-run)
#
# Configuration (env vars):
#   SWE_GITHUB_ACCOUNT — GitHub bot account to use (required)
#   SWE_GITHUB_ORG     — GitHub org owning the sandbox repos (required)
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

GITHUB_ACCOUNT="${SWE_GITHUB_ACCOUNT:?SWE_GITHUB_ACCOUNT env var must be set}"
ORG="${SWE_GITHUB_ORG:?SWE_GITHUB_ORG env var must be set}"
REPOS=(
    "$ORG/SWE-Sandbox"
    "$ORG/SWE-Sandbox-HealthTrack"
    "$ORG/SWE-Sandbox-ShopStream"
    "$ORG/SWE-Sandbox-GreenGrid"
    "$ORG/SWE-Sandbox-EduPath"
)

echo "═══════════════════════════════════════════════════"
echo "  SWE-Squad E2E Sandbox Setup"
echo "═══════════════════════════════════════════════════"
echo ""

# ── Step 1: Accept all pending invitations ───────────────────────────────────
echo "Step 1: Accepting pending repository invitations..."
INVITATIONS=$(gh api user/repository_invitations --jq '.[].id' 2>/dev/null || echo "")
if [ -n "$INVITATIONS" ]; then
    for INV_ID in $INVITATIONS; do
        echo "  Accepting invitation $INV_ID..."
        gh api -X PATCH "user/repository_invitations/$INV_ID" 2>/dev/null || true
    done
    echo "  Done — accepted $(echo "$INVITATIONS" | wc -w) invitation(s)"
else
    echo "  No pending invitations (already accepted or none pending)"
fi
echo ""

# ── Step 2: Verify collaborator access ───────────────────────────────────────
echo "Step 2: Verifying collaborator access..."
ALL_OK=true
for REPO in "${REPOS[@]}"; do
    if gh api "repos/$REPO" --jq '.name' &>/dev/null; then
        echo "  ✓ $REPO — accessible"
    else
        echo "  ✗ $REPO — NOT accessible"
        ALL_OK=false
    fi
done
if [ "$ALL_OK" = false ]; then
    echo ""
    echo "ERROR: Some repos are not accessible. Check invitations."
    echo "You may need to wait for invitation acceptance to propagate."
    exit 1
fi
echo ""

# ── Step 3: Assign test issues ───────────────────────────────────────────────
echo "Step 3: Assigning test issues to $GITHUB_ACCOUNT..."

# HealthTrack: Assign issue #1 (CRITICAL)
echo "  Assigning $ORG/SWE-Sandbox-HealthTrack#1..."
gh api "repos/$ORG/SWE-Sandbox-HealthTrack/issues/1" \
    -X PATCH -f "assignees[]=$GITHUB_ACCOUNT" 2>/dev/null && \
    echo "  ✓ HealthTrack#1 assigned" || \
    echo "  ✗ HealthTrack#1 failed (may need push access)"

# ShopStream: Assign issue #1 (HIGH)
echo "  Assigning $ORG/SWE-Sandbox-ShopStream#1..."
gh api "repos/$ORG/SWE-Sandbox-ShopStream/issues/1" \
    -X PATCH -f "assignees[]=$GITHUB_ACCOUNT" 2>/dev/null && \
    echo "  ✓ ShopStream#1 assigned" || \
    echo "  ✗ ShopStream#1 failed (may need push access)"

echo ""
echo "  NOT assigning any issues on GreenGrid and EduPath (test: agent should skip)"
echo ""

# ── Step 4: Clone repos locally ──────────────────────────────────────────────
echo "Step 4: Cloning repos to ~/Projects/..."
PROJECTS_DIR="$HOME/Projects"
mkdir -p "$PROJECTS_DIR"

for REPO in "${REPOS[@]}"; do
    REPO_NAME=$(basename "$REPO")
    LOCAL_PATH="$PROJECTS_DIR/$REPO_NAME"
    if [ -d "$LOCAL_PATH/.git" ]; then
        echo "  ✓ $REPO_NAME — already cloned, pulling..."
        (cd "$LOCAL_PATH" && git pull --ff-only 2>/dev/null || true)
    else
        echo "  Cloning $REPO_NAME..."
        gh repo clone "$REPO" "$LOCAL_PATH" 2>/dev/null && \
            echo "  ✓ $REPO_NAME — cloned" || \
            echo "  ✗ $REPO_NAME — clone failed"
    fi
done
echo ""

# ── Step 5: Verification report ─────────────────────────────────────────────
echo "═══════════════════════════════════════════════════"
echo "  Verification Report"
echo "═══════════════════════════════════════════════════"
echo ""

echo "Issues assigned to $GITHUB_ACCOUNT:"
for REPO in "${REPOS[@]}"; do
    ISSUES=$(gh issue list --repo "$REPO" --assignee "$GITHUB_ACCOUNT" --json number,title --jq '.[] | "#\(.number) \(.title)"' 2>/dev/null || echo "  (error)")
    if [ -n "$ISSUES" ]; then
        echo "  $REPO:"
        echo "$ISSUES" | sed 's/^/    /'
    else
        echo "  $REPO: (none assigned)"
    fi
done
echo ""

echo "All open issues (including unassigned):"
for REPO in "${REPOS[@]}"; do
    COUNT=$(gh issue list --repo "$REPO" --state open --json number --jq 'length' 2>/dev/null || echo "?")
    echo "  $REPO: $COUNT open issues"
done
echo ""

echo "═══════════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Expected E2E behavior:"
echo "    - Agent picks up: HealthTrack#1, ShopStream#1"
echo "    - Agent ignores:  HealthTrack#2-4, ShopStream#2-4"
echo "    - Agent ignores:  All GreenGrid issues (0 assigned)"
echo "    - Agent ignores:  All EduPath issues (0 assigned)"
echo "═══════════════════════════════════════════════════"
