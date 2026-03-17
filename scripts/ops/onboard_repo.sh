#!/bin/bash
# =============================================================================
# onboard_repo.sh — Interactive onboarding for a new SWE-Squad repository
# =============================================================================

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

REPO=""
TEAM_ID=""
BOT_ACCOUNT=""

usage() {
    cat <<USAGE
Usage: $0 [--repo owner/repo] [--team-id id] [--bot-account account]

Interactive by default. If a value is omitted, you'll be prompted.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)
            REPO="$2"
            shift 2
            ;;
        --team-id)
            TEAM_ID="$2"
            shift 2
            ;;
        --bot-account)
            BOT_ACCOUNT="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown argument: $1${NC}"
            usage
            exit 1
            ;;
    esac
done

prompt_if_empty() {
    local value="$1"
    local prompt="$2"
    if [[ -n "$value" ]]; then
        echo "$value"
        return
    fi

    local input=""
    while [[ -z "$input" ]]; do
        read -r -p "$prompt" input
    done
    echo "$input"
}

require_cmd() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo -e "${RED}Missing required command: $cmd${NC}"
        exit 1
    fi
}

update_env_value() {
    local key="$1"
    local value="$2"
    python3 - "$PROJECT_ROOT/.env" "$key" "$value" <<'PY'
from pathlib import Path
import sys

env_path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]

lines = env_path.read_text(encoding="utf-8").splitlines()
prefix = f"{key}="
updated = False
for idx, line in enumerate(lines):
    if line.startswith(prefix):
        lines[idx] = f"{key}={value}"
        updated = True
        break

if not updated:
    lines.append(f"{key}={value}")

env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

ensure_label() {
    local repo="$1"
    local name="$2"
    local color="$3"
    local description="$4"

    if gh label list --repo "$repo" --limit 200 --search "$name" --json name --jq '.[].name' | grep -Fxq "$name"; then
        echo "  - ${name} (exists)"
    else
        gh label create "$name" --repo "$repo" --color "$color" --description "$description" >/dev/null
        echo "  - ${name} (created)"
    fi
}

verify_collaborator_permission() {
    local repo="$1"
    local account="$2"

    local permission
    permission=$(gh api "repos/${repo}/collaborators/${account}/permission" --jq '.permission' 2>/dev/null || true)

    if [[ -z "$permission" ]]; then
        echo -e "${RED}Could not verify collaborator permission for ${account} on ${repo}.${NC}"
        return 1
    fi

    case "$permission" in
        write|admin|maintain)
            echo "${permission}"
            return 0
            ;;
        *)
            echo -e "${RED}${account} permission is '${permission}' (requires write or above).${NC}"
            return 1
            ;;
    esac
}

send_test_telegram() {
    local project_root="$1"
    python3 - "$project_root" <<'PY'
import asyncio
from pathlib import Path
import sys
from dotenv import load_dotenv

project_root = Path(sys.argv[1])
load_dotenv(project_root / '.env', override=True)

from src.notifications.telegram import send_telegram_alert

ok = asyncio.run(send_telegram_alert("✅ SWE-Squad onboarding test notification"))
raise SystemExit(0 if ok else 1)
PY
}

require_cmd gh
require_cmd git
require_cmd python3

REPO=$(prompt_if_empty "$REPO" "Target repository (owner/repo): ")
TEAM_ID=$(prompt_if_empty "$TEAM_ID" "SWE team ID: ")
BOT_ACCOUNT=$(prompt_if_empty "$BOT_ACCOUNT" "GitHub bot account: ")

if [[ ! "$REPO" =~ ^[^/]+/[^/]+$ ]]; then
    echo -e "${RED}Invalid repo format. Expected owner/repo${NC}"
    exit 1
fi

if [[ ! -f "$PROJECT_ROOT/.env.example" ]]; then
    echo -e "${RED}Missing .env.example in ${PROJECT_ROOT}${NC}"
    exit 1
fi

echo -e "${BOLD}== SWE-Squad Repo Onboarding ==${NC}"
echo "Project root: $PROJECT_ROOT"
echo "Repo: $REPO"
echo "Team ID: $TEAM_ID"
echo "Bot account: $BOT_ACCOUNT"
echo ""

gh auth status >/dev/null
AUTH_LOGIN=$(gh api user --jq '.login')
if [[ "$AUTH_LOGIN" != "$BOT_ACCOUNT" ]]; then
    echo -e "${RED}gh is authenticated as '${AUTH_LOGIN}', but onboarding requires bot account '${BOT_ACCOUNT}'.${NC}"
    echo -e "${YELLOW}Run: gh auth login (using ${BOT_ACCOUNT})${NC}"
    exit 1
fi

echo -n "Verifying collaborator access... "
BOT_PERMISSION=$(verify_collaborator_permission "$REPO" "$BOT_ACCOUNT")
echo -e "${GREEN}${BOT_PERMISSION}${NC}"

echo "Creating required labels..."
LABELS_OK=true
for spec in \
    "swe-team|1D76DB|SWE-Squad managed issue" \
    "auto-detected|5319E7|Detected automatically by SWE-Squad" \
    "severity: critical|B60205|Critical severity" \
    "severity: high|D93F0B|High severity" \
    "severity: medium|FBCA04|Medium severity" \
    "severity: low|0E8A16|Low severity" \
    "module:*|C2E0C6|Module scope label (replace * with module name)"; do
    IFS='|' read -r name color desc <<< "$spec"
    if ! ensure_label "$REPO" "$name" "$color" "$desc"; then
        LABELS_OK=false
    fi
done

if [[ -f "$PROJECT_ROOT/.env" ]]; then
    read -r -p ".env already exists. Overwrite and create backup? [y/N]: " OVERWRITE_ENV
    if [[ ! "$OVERWRITE_ENV" =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}Aborted to avoid overwriting existing .env${NC}"
        exit 1
    fi
    ENV_BACKUP="$PROJECT_ROOT/.env.bak.$(date -u +%Y%m%d%H%M%SZ)"
    cp "$PROJECT_ROOT/.env" "$ENV_BACKUP"
    echo -e "${YELLOW}Existing .env backed up to ${ENV_BACKUP}${NC}"
fi

cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
update_env_value "SWE_TEAM_ID" "$TEAM_ID"
update_env_value "SWE_GITHUB_ACCOUNT" "$BOT_ACCOUNT"
update_env_value "SWE_GITHUB_REPO" "$REPO"

echo -e "${GREEN}Generated .env from .env.example${NC}"

CLONE_DIR="${PWD}/${REPO##*/}"
CLONE_OK=true
if [[ -d "$CLONE_DIR/.git" ]]; then
    echo -e "${YELLOW}Repo already cloned at ${CLONE_DIR} — skipping clone${NC}"
else
    if git clone "https://github.com/${REPO}.git" "$CLONE_DIR"; then
        echo -e "${GREEN}Cloned repository to ${CLONE_DIR}${NC}"
    else
        echo -e "${RED}Failed to clone ${REPO}${NC}"
        CLONE_OK=false
    fi
fi

BOOTSTRAP_OK=true
if (cd "$PROJECT_ROOT" && python3 scripts/ops/swe_team_runner.py --bootstrap); then
    echo -e "${GREEN}Bootstrap scan completed${NC}"
else
    echo -e "${RED}Bootstrap scan failed${NC}"
    BOOTSTRAP_OK=false
fi

TELEGRAM_OK=true
if (cd "$PROJECT_ROOT" && send_test_telegram "$PROJECT_ROOT"); then
    echo -e "${GREEN}Test Telegram notification sent${NC}"
else
    echo -e "${YELLOW}Telegram test notification failed (check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)${NC}"
    TELEGRAM_OK=false
fi

echo ""
echo -e "${BOLD}Onboarding Summary${NC}"
[[ -n "$BOT_PERMISSION" ]] && echo "[x] Bot write access verified (${BOT_PERMISSION})" || echo "[ ] Bot write access verified"
[[ "$LABELS_OK" == true ]] && echo "[x] Required labels created/verified" || echo "[ ] Required labels created/verified"
[[ -f "$PROJECT_ROOT/.env" ]] && echo "[x] .env generated from template" || echo "[ ] .env generated from template"
[[ "$CLONE_OK" == true ]] && echo "[x] Repository clone in sandbox working directory" || echo "[ ] Repository clone in sandbox working directory"
[[ "$BOOTSTRAP_OK" == true ]] && echo "[x] Bootstrap scan executed" || echo "[ ] Bootstrap scan executed"
[[ "$TELEGRAM_OK" == true ]] && echo "[x] Telegram test notification verified" || echo "[ ] Telegram test notification verified"

if [[ "$LABELS_OK" == true && "$CLONE_OK" == true && "$BOOTSTRAP_OK" == true && "$TELEGRAM_OK" == true ]]; then
    echo -e "${GREEN}Onboarding complete.${NC}"
    exit 0
fi

echo -e "${YELLOW}Onboarding finished with warnings. Review checklist above.${NC}"
exit 1
