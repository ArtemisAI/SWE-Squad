# Repository Onboarding Guide

This guide covers setting up SWE-Squad to monitor and fix issues in your repositories.

## Prerequisites

- A dedicated GitHub bot account for the SWE agents
- `gh` CLI installed and authenticated as the bot account
- Python 3.10+
- Telegram bot token + target chat ID (for notifications)
- (Optional) Supabase project URL/key for ticket store backend

Install dependencies:

```bash
pip install python-dotenv pyyaml pytest
```

## Bot Account + PAT Setup

1. Sign in as the bot account (not your personal account).
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

## Repository Permission Setup

Grant the bot account collaborator access on the target repository with **Write** (or higher) permission.

Verify permission:

```bash
gh api repos/OWNER/REPO/collaborators/BOT_ACCOUNT/permission --jq '.permission'
```

Accepted values for onboarding: `write`, `maintain`, `admin`.

## Automated Onboarding

Run from this repo root:

```bash
./scripts/ops/onboard_repo.sh
```

The script will:

- Prompt for target repo, team ID, and bot account
- Verify bot collaborator permission
- Ensure required labels exist (`swe-team`, `auto-detected`, severity labels)
- Generate `.env` from `.env.example`
- Clone target repository into the working directory
- Run bootstrap scan
- Send a Telegram test alert

Non-interactive usage:

```bash
./scripts/ops/onboard_repo.sh \
  --repo owner/repo \
  --team-id my-team \
  --bot-account my-bot
```

## `.env` Configuration Reference

Primary required values:

```dotenv
SWE_TEAM_ID=my-team
SWE_GITHUB_ACCOUNT=my-bot
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

## Bootstrap + First-Run Verification

1. Bootstrap baseline:

```bash
python3 scripts/ops/swe_team_runner.py --bootstrap -v
```

2. Run one operational cycle:

```bash
python3 scripts/ops/swe_team_runner.py -v
```

3. Confirm:
   - No startup exceptions
   - Expected labels are available in target repo
   - Telegram test + runtime alerts are delivered
   - Tickets are scoped to the configured `SWE_TEAM_ID`

## Multi-Team / Multi-Repo Onboarding

To onboard another repository, run onboarding again with a **different** `SWE_TEAM_ID` and target `SWE_GITHUB_REPO`. This preserves team scoping and prevents cross-team ticket collision.

## Troubleshooting

### `gh` account mismatch

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
- Re-run with verbose logging: `python3 scripts/ops/swe_team_runner.py --bootstrap -v`

### Telegram test fails

Validate bot token and chat ID in your `.env` file and ensure the bot has been added to the target chat.
