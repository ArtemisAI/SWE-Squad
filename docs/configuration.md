# Configuration

## Environment variables

All secrets and runtime settings are provided via environment variables (`.env` file or shell). Never hardcode credentials.

Copy `.env.example` to `.env` and fill in the required values:

```bash
cp .env.example .env
```

### Required variables

| Variable | Purpose |
|----------|---------|
| `SWE_TEAM_ENABLED` | Kill switch (`true`/`false`). Must be `true` to run. |
| `SWE_TEAM_CONFIG` | Path to `swe_team.yaml` (default: `config/swe_team.yaml`) |
| `SWE_TEAM_ID` | Unique team identifier for ticket scoping |

### GitHub integration

| Variable | Purpose |
|----------|---------|
| `SWE_GITHUB_ACCOUNT` | Bot GitHub account for issue assignment |
| `SWE_GITHUB_REPO` | Target repo (`owner/repo`) |
| `GH_TOKEN` | GitHub PAT for `gh` CLI |

### Notifications

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for alerts |
| `WEBHOOK_PORT` | GitHub webhook listener port (default: `9876`) |
| `WEBHOOK_SECRET` | HMAC secret for GitHub webhook signature validation |

### Semantic memory (optional)

| Variable | Purpose |
|----------|---------|
| `SUPABASE_URL` | Supabase project URL (enables Supabase store) |
| `SUPABASE_ANON_KEY` | Supabase anon or service-role key |
| `BASE_LLM_API_URL` | External OpenAI-compatible proxy (embeddings + extraction) |
| `BASE_LLM_API_KEY` | API key for BASE_LLM proxy |
| `EMBEDDING_MODEL` | Embedding model name (default: `bge-m3`) |
| `EXTRACTION_MODEL` | Fact-extraction model via BASE_LLM (default: `gemini-3-flash`) |

### Model tier overrides

| Variable | Purpose |
|----------|---------|
| `SWE_MODEL_T1` | Override T1 model (cheap tasks, default: `haiku`) |
| `SWE_MODEL_T2` | Override T2 model (routine fixes, default: `sonnet`) |
| `SWE_MODEL_T3` | Override T3 model (critical/orchestration, default: `opus`) |

### Remote worker access

| Variable | Purpose |
|----------|---------|
| `SWE_SSH_CONFIG` | Path to scoped SSH config (default: `config/ssh_workers.conf`) |
| `SWE_REMOTE_NODES` | JSON array of worker nodes for remote log collection |

## swe_team.yaml

The primary configuration file is `config/swe_team.yaml`. It controls:

- **Agent thresholds** — `max_open_critical`, `max_open_high`, stability gate limits.
- **Log directories** — local paths scanned by MonitorAgent each cycle.
- **Remote workers** — SSH aliases, log paths, and worker names.
- **Model tiers** — T1/T2/T3 model assignments (overridable via env vars).
- **Fallback agents** — ordered list of fallback CLI agents when Claude is rate-limited.
- **A2A hub URL** — `a2a_hub_url` for cross-agent coordination.
- **Scheduler jobs** — cron schedules, quotas, peak-hour windows.

### Remote worker configuration example

```yaml
monitor:
  remote_workers:
    - name: linkedai-browser-2
      ssh: "linkedai-browser-2"       # must match Host in ssh_workers.conf
      log_dir: "~/Projects/LinkedAi/logs"
```

### Fallback agent chain example

```yaml
fallback_agents:
  - name: gemini-cli
    type: gemini
    model: gemini-2.5-pro
  - name: opencode
    type: opencode
    model: claude-sonnet-4-5
```

## Cron scheduling

See `crontab.example` for recommended schedules:

```bash
# Run one full cycle every 5 minutes
*/5 * * * * /path/to/swe-squad/scripts/ops/swe_team_runner.py

# Daily summary at 09:00
0 9 * * * python3 scripts/ops/swe_cli.py summary
```

## Daemon mode

To run the runner as a long-lived daemon (useful for development):

```bash
python3 scripts/ops/swe_team_runner.py --daemon
```

The daemon writes its PID to `/tmp/swe_squad_daemon.pid` and logs to `logs/swe_team.log`.
