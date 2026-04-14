#!/usr/bin/env bash
# Pre-push hook: block direct pushes to main/master.
# Install: cp scripts/ops/pre-push-hook.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push
#
# Bot accounts must NEVER push directly to main.
# All changes must go through a PR reviewed by a human maintainer.
#
# Configuration (optional env vars):
#   SWE_HUMAN_EMAIL — git email of the human maintainer allowed to bypass this check
#                     (default: read from git config user.email; bypass disabled if unset)

protected_branches="^(main|master)$"

HUMAN_EMAIL="${SWE_HUMAN_EMAIL:-}"

while read local_ref local_sha remote_ref remote_sha; do
    branch=$(echo "$remote_ref" | sed 's|refs/heads/||')
    if echo "$branch" | grep -qE "$protected_branches"; then
        current_email=$(git config user.email)
        if [ -n "$HUMAN_EMAIL" ] && [ "$current_email" = "$HUMAN_EMAIL" ]; then
            # Human account: warn but allow (human may need to hotfix)
            echo "WARNING: Direct push to $branch by human account ($current_email)."
            echo "Prefer opening a PR. Proceeding only because this is the human account."
        else
            echo "ERROR: Direct push to $branch is blocked ($current_email)."
            echo "Create a feature branch and open a PR instead."
            echo "  git checkout -b fix/your-description"
            echo "  git push origin fix/your-description"
            echo "  gh pr create --base main"
            exit 1
        fi
    fi
done

exit 0
