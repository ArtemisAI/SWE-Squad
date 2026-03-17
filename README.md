# SWE Squad — Autonomous Software Engineering Team

Self-healing, self-diagnosing development agents that monitor production systems, detect errors, investigate root causes, implement fixes, and learn from successes.

Built on [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and the [A2A protocol](https://github.com/google/A2A) for inter-agent coordination.

## How It Works

```
Every 30 min:
│
├─ COLLECT ─── rsync logs from remote workers via SSH
├─ FETCH ───── pick up GitHub issues assigned to the team's bot account
├─ MONITOR ─── scan logs for ERROR, CRITICAL, Traceback patterns
├─ TRIAGE ──── route by severity and module to specialist agents
├─ NOTIFY ──── Telegram alerts for HIGH/CRITICAL issues
├─ DISTILL ─── check for cached deterministic fixes (zero LLM cost)
├─ INVESTIGATE  Claude Code CLI diagnosis (Sonnet or Opus)
├─ FIX ─────── keep/discard loop with git branches, auto-revert on failure
├─ GATE ────── Ralph Wiggum stability gate (bugs before features)
├─ CREATIVE ── weekly improvement proposals from ticket patterns
└─ DISPATCH ── A2A events for cross-agent coordination
```

## Model Routing

| Scenario | Model | Why |
|----------|-------|-----|
| Routine HIGH bugs | **Sonnet** | Fast, cost-efficient |
| CRITICAL bugs | **Opus** | Orchestrates sub-agents |
| After 2 failed Sonnet attempts | **Opus** | Automatic escalation |
| Issue scanning, docs | **Haiku** | Cheap, fast |
| Deterministic replay | **None** | Cached fix, zero LLM cost |

## Ralph Wiggum Loop

The developer agent feeds failures forward into the next attempt:

```
Attempt 1 (Sonnet): try fix → tests fail → capture error
Attempt 2 (Sonnet): try fix WITH previous error context → tests fail → capture
Attempt 3 (Opus):   escalate, orchestrate sub-agents → tests pass → KEEP
```

If all 3 attempts fail → HITL escalation via Telegram.

## Multi-Team Support

Each SWE Squad instance is scoped by `team_id` and a dedicated GitHub account. Multiple teams can share the same Supabase backend without overlap.

Configure in `.env`:
```bash
SWE_TEAM_ID=my-team
SWE_GITHUB_ACCOUNT=my-bot-account
SWE_GITHUB_REPO=owner/repo
```

## Ticket Store

Two backends available:

- **JSON** (default) — file-backed, zero dependencies, good for single-machine setups
- **Supabase** — PostgreSQL-backed, multi-agent, real-time capable, audit trail

Set `SUPABASE_URL` and `SUPABASE_ANON_KEY` in `.env` to activate Supabase. Run `scripts/ops/supabase_schema.sql` to create tables.

## Components

| File | Purpose |
|------|---------|
| `src/swe_team/monitor_agent.py` | Log scanning, error detection, fingerprint dedup |
| `src/swe_team/triage_agent.py` | Severity routing, module specialist assignment |
| `src/swe_team/investigator.py` | Claude Code CLI diagnosis with model routing |
| `src/swe_team/developer.py` | Keep/discard fix loop with git branches |
| `src/swe_team/ralph_wiggum.py` | Stability gate — bugs before features |
| `src/swe_team/governance.py` | Deployment governor, complexity gate |
| `src/swe_team/creative_agent.py` | Proactive improvement proposals |
| `src/swe_team/distiller.py` | Trajectory distillation — cache successful fixes |
| `src/swe_team/supabase_store.py` | Supabase ticket store (zero-dep, stdlib urllib) |
| `src/swe_team/ticket_store.py` | JSON ticket store with fingerprint dedup |
| `src/a2a/adapters/swe_team.py` | A2A protocol adapter |
| `scripts/ops/swe_team_runner.py` | Entry point — cron, daemon, bootstrap modes |
| `scripts/ops/supabase_schema.sql` | Supabase schema migration |
| `config/swe_team/programs/` | Markdown agent programs (investigate, fix, orchestrate) |

## Quick Start

```bash
# Clone
git clone https://github.com/ArtemisAI/SWE-Squad.git
cd SWE-Squad

# Install dependencies
pip install python-dotenv pyyaml

# Configure
cp .env.example .env
# Edit .env with your GitHub token, Telegram bot, etc.

# Bootstrap (first run — acknowledge existing errors)
SWE_TEAM_ENABLED=true python scripts/ops/swe_team_runner.py --bootstrap -v

# Run a single scan cycle
SWE_TEAM_ENABLED=true python scripts/ops/swe_team_runner.py -v

# Daemon mode (persistent loop, 30-minute cycles)
SWE_TEAM_ENABLED=true python scripts/ops/swe_team_runner.py --daemon -v

# Daily summary via Telegram
SWE_TEAM_ENABLED=true python scripts/ops/swe_team_runner.py --summary

# Run tests
python -m pytest tests/unit/test_swe_team.py -v
```

## Requirements

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`)
- [`gh` CLI](https://cli.github.com/) (authenticated)
- SSH access to worker machines (optional, for remote log collection)
- Telegram bot token (optional, for notifications)

## License

MIT
