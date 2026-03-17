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

All tests must pass before committing. Tests live in `tests/unit/` and use only the standard library plus pytest (no external services required). Current baseline: **827+ tests**.

## Architecture Overview

```
scripts/ops/swe_team_runner.py   — Entry point. Runs one cycle or daemon loop.
scripts/ops/swe_cli.py           — CLI tool (status, tickets, issues, repos, summary, report)
src/swe_team/
    config.py          — Loads config/swe_team.yaml + env var overrides; ModelTiers dataclass
    models.py          — Dataclasses: SWETicket, TicketSeverity, TicketStatus, etc.
    monitor_agent.py   — Scans log directories for errors, deduplicates via fingerprints
    triage_agent.py    — Classifies severity, assigns tickets to agents
    investigator.py    — Root-cause analysis via Claude CLI; semantic memory context injection
    developer.py       — Attempts automated fixes, creates branches, keep/discard loop
    ralph_wiggum.py    — Stability gate (block/warn/pass)
    governance.py      — Deployment governor, complexity gates
    ticket_store.py    — JSON file-backed ticket persistence
    supabase_store.py  — Supabase PostgREST ticket persistence; semantic dedup; confidence
    embeddings.py      — bge-m3 embeddings + mem0-style fact extraction via BASE_LLM proxy
    telegram.py        — Standalone Telegram Bot API client (stdlib only, zero extra deps)
    notifier.py        — Telegram alerts, daily summaries, HITL escalation
    github_integration.py — GitHub issue creation and commenting (repo-aware)
    creative_agent.py  — Proactive improvement proposals (only when stable)
    distiller.py       — Trajectory distillation: caches successful fixes by fingerprint
    preflight.py       — PreflightCheck: validates git identity, clean tree, env vars
    events.py          — SWE event definitions for A2A dispatch
    remote_logs.py     — SSH/rsync remote log collection
src/a2a/               — A2A protocol: server, client, dispatch (hub + standalone), adapters
    src/a2a/server.py      — Lightweight A2A HTTP server (standalone mode)
    src/a2a/client.py      — A2A client (hub mode + direct agent mode)
    src/a2a/dispatch.py    — Event dispatcher: POSTs to centralized hub, fallback to standalone
    src/a2a/adapters/      — Agent adapters: gemini, opencode, generic CLI, swe_team
scripts/ops/a2a_hub.py     — Standalone A2A hub entry point
scripts/ops/a2a_request.py — CLI for sending A2A requests to any agent
scripts/ops/dashboard_data.py — Dashboard metrics generator
config/agent-card.json     — SWE Squad A2A agent card
config/swe_team.yaml   — Runtime configuration (agents, thresholds, log dirs, model tiers)
config/swe_team/programs/
    investigate.md     — Prompt template for Sonnet investigation pass
    orchestrate.md     — Prompt template for Opus orchestration (CRITICAL tickets)
    fix.md             — Prompt template for developer fix attempts
scripts/ops/supabase_schema.sql — Full Supabase DDL (tables, indexes, RLS, RPCs)
crontab.example        — Recommended cron schedules for runner + CLI tools
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
| `SUPABASE_URL` | Optional Supabase URL (enables Supabase store) |
| `SUPABASE_ANON_KEY` | Supabase anon/service-role key |
| `BASE_LLM_API_URL` | External OpenAI-compatible proxy (embeddings + extraction) |
| `BASE_LLM_API_KEY` | API key for BASE_LLM proxy |
| `EMBEDDING_MODEL` | Embedding model name (default: `bge-m3`) |
| `EXTRACTION_MODEL` | Fact-extraction model via BASE_LLM (default: `gemini-3-flash`) |
| `SWE_MODEL_T1` | Override T1 model (cheap tasks, default: `haiku`) |
| `SWE_MODEL_T2` | Override T2 model (routine fixes, default: `sonnet`) |
| `SWE_MODEL_T3` | Override T3 model (critical/orchestration, default: `opus`) |

See `.env.example` for the full list.

## Two-System Distinction (CRITICAL — read before touching embeddings.py or investigator.py)

There are two separate LLM systems. **Never confuse them.**

```
BASE_LLM proxy (BASE_LLM_API_URL)       Claude Code CLI (/usr/bin/claude)
────────────────────────────────────    ────────────────────────────────────
http://your-llm-proxy.example.com/v1/           subprocess.run(["claude", "--print"...])
OpenAI-compatible HTTP API              Used only by InvestigatorAgent / DeveloperAgent
43 models — gemini-3-flash, bge-m3      Models: haiku / sonnet / opus
T1 cheap tasks: gemini-3-flash          T3 orchestration: opus (never implementer)
Used in: embeddings.py, store           Used in: investigator.py, developer.py
Do NOT use claude-haiku here            Do NOT call subprocess from library code
```

## Model Routing

| Scenario | Tier | Model | Notes |
|----------|------|-------|-------|
| Embeddings, fact extraction | T1 | `gemini-3-flash` | Via BASE_LLM proxy |
| Routine HIGH bugs | T2 | `sonnet` | Claude CLI subprocess |
| CRITICAL bugs | T3 | `opus` | Claude CLI, orchestrates sub-agents |
| After 2 Sonnet failures | T3 | `opus` | Auto-escalation |
| Regression tickets | T3 | `opus` | Always heavy tier |
| Cached fix replay | — | None | TrajectoryDistiller, zero cost |

Opus is the **orchestrator only** — it must never launch a sub-agent with `model: opus`.

## Semantic Memory Pipeline

```
ticket resolved
      │
      ▼
extract_memory_facts()         ← gemini-3-flash via BASE_LLM proxy
      │                          structured: root cause, fix, module, tags
      ▼
embed_ticket()                 ← bge-m3 (1024-dim) via BASE_LLM proxy
      │
      ▼
store_embedding_with_dedup()   ← 0.92 cosine threshold
      │                          "stored" | "merged" | "skipped"
      ▼
Supabase pgvector              ← IVFFlat index, confidence-weighted ranking

On next investigation:
find_similar()                 ← top-5 at 0.75 floor, 180-day TTL
      │                          confidence-weighted score
      ▼
injected into investigation prompt as ## Semantic Memory context
      │
record_memory_hit()            ← increments memory_confidence (+0.1, max 2.0)
```

## Code Conventions

- Use **dataclasses** for all data models (no Pydantic, no attrs).
- Use **type hints** on all function signatures.
- Minimal dependencies: stdlib + pyyaml + python-dotenv. Optional extras only via `[embeddings]`.
- Imports from `src.swe_team.*` and `src.a2a.*` use dotted paths rooted at the project directory.
- Configuration is loaded once via `load_config()` and threaded through as arguments.
- Tests must not require network access, API keys, or running services.

## Divide-and-Conquer: Always Orchestrate Sub-Agents

**This is mandatory.** The main agent's context window is a finite shared resource.
Exhaust it and the entire session is lost.

### Rules for every agent in this system

1. **Never do large tasks alone.** Any task that touches > 2 files or requires > 3 research steps must be broken into parallel sub-agents.
2. **Always prefer parallel over sequential.** If two sub-tasks don't depend on each other, launch them at the same time.
3. **Keep the main context clean.** Read only what you need. Delegate broad search ("find all usages of X across the repo") to a sub-agent rather than running it yourself.
4. **Opus orchestrates, never implements.** Opus breaks problems into sub-tasks and synthesises results. Sonnet/Haiku do the actual work.
5. **Sub-agents report back summaries, not raw output.** A sub-agent that returns 5000 lines of grep output is useless. It must distil findings to < 200 lines before returning.

### How to break down a task

```
Big task → identify independent components
         → launch one sub-agent per component in parallel
         → collect summarised results
         → synthesise into final answer / commit
```

Example: investigating a LinkedAi scraping failure:
- Sub-agent A: read the failing module + recent git log
- Sub-agent B: search for similar past tickets in Supabase
- Sub-agent C: look up the third-party lib docs via DeepWiki
- Synthesise: root cause analysis from A + B + C combined

## DevOps / GitOps Practices

These rules apply to ALL agents (investigator, developer, orchestrator):

### Git hygiene
- **Feature branches only.** Never commit directly to `main` without a PR (except hot-fixes with explicit human approval).
- **One commit per logical change.** Squash noisy work-in-progress commits before opening a PR.
- **Descriptive commit messages.** Format: `type(scope): short summary` — types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
- **Always pull before pushing.** Run `git fetch && git rebase origin/main` to avoid unnecessary merge commits.
- **Never force-push to main.** Only force-push to personal feature branches.

### CI/CD gate
- **Tests must pass locally before pushing.** Run `python3 -m pytest tests/unit/ -q` — zero failures required.
- **Don't bypass hooks.** Never use `--no-verify`.
- **PR description must include a test plan.** The SWE developer agent's PRs must always list the test command and expected output.

### Secrets management
- All secrets via `.env` or environment variables — never hardcoded.
- Never commit `.env`, `*.key`, `*.pem`, or credentials files.
- Rotate any key that appears in a diff or log immediately.

### Observability
- Every cycle writes `data/swe_team/status.json` — check it before diagnosing a cycle failure.
- Telegram alerts on CRITICAL tickets, stability gate BLOCK, and HITL escalations.
- Supabase is the source of truth for ticket state — always query it, not local JSON, in multi-agent contexts.
- Emit A2A events for every state transition so the hub can correlate across agents.

### Safe deployment
- Developer agent creates a branch + PR, never merges directly.
- After merge, the deployer agent monitors for 30 minutes (check `status.json`).
- On regression: auto-rollback via `git revert` + Telegram alert.

## A2A Hub — Inter-Agent Communication

SWE Squad connects to a **centralized A2A Hub** for cross-agent coordination:

```
LinkedAi A2A Hub (Tailnet): http://100.110.176.73:18790
├── Registered: openclaw, gemini, llm_proxy, swe-squad
├── Protocol: JSON-RPC 2.0 over HTTP POST
├── Discovery: GET /health, GET /.well-known/agent-card.json
└── Events: POST /v1/events
```

- `src/a2a/dispatch.py` POSTs events to the hub (fallback: standalone log)
- `src/a2a/client.py` discovers agents and sends tasks through the hub
- `src/swe_team/agent_registry.py` registers with hub and discovers peers
- Config: `a2a_hub_url` in `swe_team.yaml` or `A2A_HUB_URL` env var
- **Standalone mode**: if hub is unreachable, the system runs `src/a2a/server.py` locally

### Fallback agent chain (when Claude Code is rate-limited)
```
Claude Code (primary) → Gemini CLI (T2 fallback) → OpenCode (T3 fallback) → fail gracefully
```

Configured in `swe_team.yaml` under `fallback_agents:`. Each fallback is tried in order via the agent registry.

## Multi-Agent Coordination Rules

- **Claim before working.** Before editing a file, check that no other agent has it in progress (check Supabase ticket assignments + git branches).
- **Heartbeat every 30 min.** Long-running investigations must update `ticket.metadata.last_heartbeat`. Stale tickets (>2h) get reassigned.
- **Emit A2A events.** Every state transition (triage, investigation start/complete, fix start/complete) must dispatch an event to the hub so other agents can react.
- **Don't duplicate work.** Before starting, query the ticket store for existing investigations on the same fingerprint.
- **Use the dashboard.** Check `data/swe_team/status.json` or run `swe_cli.py status` before starting work.

## What NOT To Do

- **No hardcoded paths.** Use `Path(__file__).resolve()` or environment variables.
- **No pushing to public repos.** Use `scripts/ops/sync_public.sh` for controlled sync.
- **No secrets in code.** All credentials come from `.env` or environment variables.
- **No new runtime dependencies** without discussion. Keep the core dependency footprint minimal.
- **No breaking the test suite.** Run `make test` before committing.
- **No calling claude CLI from library code** (`embeddings.py`, `supabase_store.py`, etc.).
- **No using `claude-haiku` via BASE_LLM proxy** — that model is not available there.
- **No Opus as implementer** — Opus orchestrates sub-agents; Sonnet/Haiku do the work.
- **No large tasks without sub-agents** — see Divide-and-Conquer rules above.
- **No raw grep/search output returned to main agent** — sub-agents must summarise.
