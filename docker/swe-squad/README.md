# SWE-Squad Docker Compose

Modular Docker Compose setup for the full SWE-Squad autonomous engineering system.

## Prerequisites

1. Copy `.env.example` to the project root as `.env` and fill in values:
   ```bash
   cp docker/swe-squad/.env.example .env
   ```
2. Ensure Docker and Docker Compose v2 are installed.

## Quick Start

```bash
# From the project root
docker compose up -d
```

Or directly:

```bash
docker compose -f docker/swe-squad/docker-compose.yml up -d
```

## Services

| Service | Description | Port |
|---|---|---|
| `swe-runner` | Main daemon — monitors, triages, investigates, fixes | — |
| `swe-dashboard` | Web dashboard for ticket and status visibility | 8888 |
| `swe-a2a-hub` | A2A inter-agent communication hub | 18791 |

## Common Operations

**View logs:**
```bash
docker compose -f docker/swe-squad/docker-compose.yml logs -f swe-runner
docker compose -f docker/swe-squad/docker-compose.yml logs -f swe-dashboard
```

**Scale workers (run multiple runner instances):**
```bash
docker compose -f docker/swe-squad/docker-compose.yml up -d --scale swe-runner=3
```

**Check runner health / status:**
```bash
docker compose -f docker/swe-squad/docker-compose.yml ps
# Status file written by daemon:
cat data/swe_team/status.json
```

**Restart a service:**
```bash
docker compose -f docker/swe-squad/docker-compose.yml restart swe-runner
```

**Stop all services:**
```bash
docker compose -f docker/swe-squad/docker-compose.yml down
```

## Required Environment Variables

| Variable | Purpose |
|---|---|
| `SWE_TEAM_ENABLED` | Kill switch — must be `true` to activate agents |
| `GH_TOKEN` | GitHub PAT (scopes: repo, read:org) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | Telegram chat/group ID for alerts |
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude Code CLI |
| `SWE_GITHUB_REPO` | Target repository (`owner/repo`) |
| `SWE_TEAM_ID` | Unique team ID to scope tickets |

## Optional Environment Variables

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` + `SUPABASE_KEY` | Enable Supabase ticket store (replaces JSON file) |
| `BASE_LLM_API_URL` + `BASE_LLM_API_KEY` | Enable semantic memory embeddings |
| `A2A_HUB_URL` | Connect to an external A2A hub instead of local |
| `WEBHOOK_SECRET` | HMAC secret for GitHub push webhook validation |
| `ANTHROPIC_BASE_URL` | Route Claude Code to an Anthropic-compatible proxy (test mode) |

## Test Mode: OAI Backend Via Proxy

Use this mode when Anthropic quota is exhausted and you want to validate SWE-Squad
flows using an OpenAI-compatible backend through `claude-code-proxy`.

1. Run proxy as a sidecar (outside this compose stack):
```bash
docker run -d --name claude-code-proxy \
   --restart unless-stopped \
   -e OPENAI_API_KEY=__SET_LATER__ \
   -e OPENAI_BASE_URL=https://your-proxy.example.com/v1 \
   -e PREFERRED_PROVIDER=openai \
   -p 8082:8082 \
   ghcr.io/1rgs/claude-code-proxy:latest
```

2. Configure SWE-Squad `.env` for proxy routing:
```dotenv
ANTHROPIC_BASE_URL=http://host.docker.internal:8082
ANTHROPIC_API_KEY=test-key
```

Notes:
- `ANTHROPIC_API_KEY` remains required by some client paths; use a non-empty test value.
- On Linux Docker hosts where `host.docker.internal` is unavailable, use the host IP.
- Do not commit real proxy/API keys; keep them in local `.env` only.

Stable CLI usage (recommended):
```bash
scripts/ops/claude_proxy_cli.sh
```
This wrapper always injects proxy env vars before launching `claude`, which
prevents fallback login prompts when your shell/session is missing exports.
It also performs a proxy preflight and exits early on failure (fail-closed),
so `claude` does not silently fall back to subscription auth.

### Clean Model Names (Short Aliases)

To keep Claude's model UX clean, `claudep` supports short names and resolves
them to provider-qualified backend IDs (for example `openai/qwen3-coder:...`)
before invoking Claude CLI.

List aliases:
```bash
scripts/ops/claude_proxy_cli.sh --list-models
```

Use a short name:
```bash
scripts/ops/claude_proxy_cli.sh --model qwen3
scripts/ops/claude_proxy_cli.sh --model deepseek-v3.2 --print "ping"
scripts/ops/claude_proxy_cli.sh --model glm-5 --print "ping"
```

Defaults:
- `CLAUDEP_DEFAULT_MODEL=qwen3`
- `CLAUDE_PROXY_MODEL_PROVIDER=openai`
- `kimi-k2-1t` benefits from `max_tokens >= 128` for reliable non-empty output.

Notes:
- `--list-models` is generated from `config/swe_team/proxy_model_policy.yaml`
   so all configured custom models are shown automatically.
- If Claude CLI shows `Auth conflict: token + ANTHROPIC_API_KEY`, run
   `claude /logout` when using API-key proxy mode, or unset `ANTHROPIC_API_KEY`
   when using claude.ai login mode.

Capability policy is tracked in:
- `config/swe_team/proxy_model_policy.yaml`

Runtime tier-routing integration:
- Investigator/Developer resolve tier model names through this policy when
   proxy mode is enabled.
- Auto-enabled when `ANTHROPIC_BASE_URL` is set.
- Override with `SWE_PROXY_POLICY_ENABLED=true|false`.

### Upstream Dependency (No Fork)

SWE-Squad can track the original `1rgs/claude-code-proxy` repository directly
as a vendored dependency to reduce maintenance overhead.

Sync/update upstream copy:
```bash
scripts/ops/claude_proxy_upstream.sh sync
```

Check current status:
```bash
scripts/ops/claude_proxy_upstream.sh status
```

This keeps proxy integration "baked in" as an easy alternative path while
avoiding divergence from upstream.

See `.env.example` for the full variable reference.

## Architecture Notes

- Source code is mounted read-only into containers — edit on the host, restart to pick up changes.
- `data/` and `logs/` are mounted read-write so the daemon can persist tickets and write logs.
- All services share the same image (`swe-squad:latest`) built from the project root Dockerfile.
- External services (Supabase, GitHub, Telegram) are accessed over the network — no containers needed for them.
