# Repository Onboarding Guide (SWE-Squad DEV)

This guide covers both:

1. Automated onboarding (`scripts/ops/onboard_repo.sh`)
2. Manual onboarding (for edge cases and custom setups)

Use this in the private DEV repo (`ArtemisAI/SWE-Squad-DEV`) first, then sync a simplified public version using `scripts/ops/sync_public.sh`.

## Prerequisites

- Access to `ArtemisAI/SWE-Squad-DEV`
- A dedicated GitHub bot account (example: `ArtemisArchitect`)
- `gh` CLI installed and authenticated as the bot account
- Python 3.10+
- VM/sandbox with SSH keys configured (if cloning private repos over SSH)
- Telegram bot token + target chat ID
- (Optional) Supabase project URL/key for ticket store backend

Install local dependencies:

```bash
pip install python-dotenv pyyaml pytest
```

## Bot account + PAT setup

1. Sign in as the bot account (not your personal owner/admin account).
2. Generate a fine-grained or classic token with at least:
   - `repo`
   - `read:org`
3. Export token for CLI use:

```bash
export GH_TOKEN="<bot-token>"
gh auth login
```

Verify active account:

```bash
gh api user --jq '.login'
```

Expected: your bot username.

## Repository permission setup

Grant the bot account collaborator access on the target repository with **Write** (or higher) permission.

Verify permission:

```bash
gh api repos/OWNER/REPO/collaborators/BOT_ACCOUNT/permission --jq '.permission'
```

Accepted values for onboarding: `write`, `maintain`, `admin`.

## Automated onboarding (recommended)

Run from this repo root:

```bash
./scripts/ops/onboard_repo.sh
```

The script will:

- Prompt for:
  - target repo (`owner/repo`)
  - team ID
  - bot account
- Verify bot collaborator permission
- Ensure labels exist:
  - `swe-team`
  - `auto-detected`
  - `severity: critical`
  - `severity: high`
  - `severity: medium`
  - `severity: low`
  - `module:*`
- Generate `.env` from `.env.example`
- Clone target repository into the current sandbox working directory
- Run bootstrap scan:

```bash
python3 scripts/ops/swe_team_runner.py --bootstrap
```

- Send a Telegram test alert
- Print a final checklist summary

Non-interactive usage:

```bash
./scripts/ops/onboard_repo.sh \
  --repo owner/repo \
  --team-id swe-squad-2 \
  --bot-account ArtemisArchitect
```

## `.env` configuration reference

Primary required values per team:

```dotenv
SWE_TEAM_ID=swe-squad-1
SWE_GITHUB_ACCOUNT=ArtemisArchitect
SWE_GITHUB_REPO=owner/repo
GH_TOKEN=<bot-pat>
TELEGRAM_BOT_TOKEN=<telegram-bot-token>
TELEGRAM_CHAT_ID=<telegram-chat-id>
```

Optional:

```dotenv
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_KEY=<service-or-anon-key>
```

## VM sandbox deployment notes

- Keep the sandbox isolated from Git remotes for agent execution flows.
- All runtime configuration must come from environment variables (`.env`), not hardcoded values.
- Do not run SWE agents with `ArtemisAI` credentials.

## Bootstrap + first-run verification

1. Bootstrap baseline:

```bash
python3 scripts/ops/swe_team_runner.py --bootstrap -v
```

2. Run one operational cycle:

```bash
python3 scripts/ops/swe_team_runner.py -v
```

3. Confirm:
   - no startup exceptions
   - expected labels are available in target repo
   - Telegram test + runtime alerts are delivered
   - tickets are scoped to the configured `SWE_TEAM_ID`

## Multi-team / second repo onboarding

To onboard another repository, run onboarding again with a **different** `SWE_TEAM_ID` and target `SWE_GITHUB_REPO`.

This preserves team scoping and prevents cross-team ticket collision.

## Troubleshooting

### `gh` account mismatch

If the script says authenticated user does not match bot account:

```bash
gh auth logout
gh auth login
gh api user --jq '.login'
```

### Missing collaborator permission

Ensure the bot is added to the target repo with write access, then re-run onboarding.

### Bootstrap scan fails

- Check `.env` and `config/swe_team.yaml`
- Ensure dependencies are installed
- Re-run with verbose logging:

```bash
python3 scripts/ops/swe_team_runner.py --bootstrap -v
```

### Telegram test fails

Validate bot token and chat ID:

```bash
python3 - <<'PY'
import asyncio
from src.notifications.telegram import send_telegram_alert
print(asyncio.run(send_telegram_alert('SWE-Squad telegram connectivity test')))
PY
```

### Public repo sync

After DEV onboarding docs are finalized, sync a simplified public-safe version with:

```bash
./scripts/ops/sync_public.sh --push -m "docs: onboarding guide update"
```
