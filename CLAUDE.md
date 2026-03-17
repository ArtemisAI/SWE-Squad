# CLAUDE.md — Project Instructions for Claude Code Agents

## Project Purpose

SWE-Squad is an autonomous software engineering team: a set of AI agents that monitor logs, triage issues, investigate root causes, propose fixes, and enforce a stability gate before new work proceeds.

## Running Tests

```bash
python3 -m pytest tests/ -v --tb=short
```

Or via the Makefile:

```bash
make test
```

All tests must pass before committing. Tests live in `tests/unit/` and use only the standard library plus pytest (no external services required).

## Architecture Overview

```
scripts/ops/swe_team_runner.py   — Entry point. Runs one cycle or daemon loop.
src/swe_team/
    config.py          — Loads config/swe_team.yaml + env var overrides
    models.py          — Dataclasses: SWETicket, TicketSeverity, TicketStatus, etc.
    monitor_agent.py   — Scans log directories for errors, deduplicates via fingerprints
    triage_agent.py    — Classifies severity, assigns tickets to agents
    investigator.py    — Root-cause analysis on triaged tickets
    developer.py       — Attempts automated fixes, creates branches
    ralph_wiggum.py    — Stability gate (block/warn/pass)
    governance.py      — Deployment governance checks
    ticket_store.py    — JSON file-backed ticket persistence
    supabase_store.py  — Optional Supabase-backed ticket persistence
    embeddings.py      — Semantic memory via embeddings
    notifier.py        — Telegram notifications for alerts and summaries
    github_integration.py — GitHub issue creation and lookup
    creative_agent.py  — Proposes low-severity improvements
    distiller.py       — Trajectory distillation for deterministic fixes
    events.py          — SWE event definitions
    remote_logs.py     — SSH/rsync remote log collection
src/a2a/               — Agent-to-Agent protocol stubs (dispatch, models, events)
config/swe_team.yaml   — Runtime configuration (agents, thresholds, log dirs)
```

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `SWE_TEAM_ENABLED` | Kill switch (`true`/`false`). Must be `true` to run. |
| `SWE_TEAM_CONFIG` | Path to `swe_team.yaml` (default: `config/swe_team.yaml`) |
| `SWE_TEAM_ID` | Unique team identifier for ticket scoping |
| `SWE_GITHUB_ACCOUNT` | Bot GitHub account for issue assignment |
| `SWE_GITHUB_REPO` | Target repo (`owner/repo`) |
| `GH_TOKEN` | GitHub PAT for `gh` CLI |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | Telegram chat for alerts |
| `SUPABASE_URL` | Optional Supabase URL (replaces JSON store) |
| `SUPABASE_KEY` | Optional Supabase key |

See `.env.example` for the full list.

## Code Conventions

- Use **dataclasses** for all data models (no Pydantic, no attrs).
- Use **type hints** on all function signatures.
- Minimal dependencies: stdlib + pyyaml + python-dotenv. Optional extras only via `[embeddings]`.
- Imports from `src.swe_team.*` and `src.a2a.*` use dotted paths rooted at the project directory.
- Configuration is loaded once via `load_config()` and threaded through as arguments.
- Tests must not require network access, API keys, or running services.

## What NOT To Do

- **No hardcoded paths.** Use `Path(__file__).resolve()` or environment variables.
- **No pushing to public repos.** Use `scripts/ops/sync_public.sh` for controlled sync.
- **No secrets in code.** All credentials come from `.env` or environment variables.
- **No new runtime dependencies** without discussion. Keep the core dependency footprint minimal.
- **No breaking the test suite.** Run `make test` before committing.
