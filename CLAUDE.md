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

All tests must pass before committing. Current baselines:
- **Python**: `python3 -m pytest tests/ -v --tb=short` — **5900+ tests** (stdlib + pytest, no external services)
- **TypeScript**: `cd control-plane && pnpm test` — **900+ tests** (vitest)
- **TypeScript types**: `cd control-plane && pnpm typecheck`

## Architecture Overview

The system has two layers: a **TypeScript control plane** (pi-agent daemon with 16 custom tools)
and a **Python agent library** (specialized agents, ticket store, embeddings).

```
control-plane/                     — TypeScript V2 control plane (primary entry point)
  src/main.ts                      — Daemon: persistent pi-agent session + heartbeat loop
  src/tools/                       — 16 custom tools (ticket CRUD, delegation, safety gates)
  src/providers/                   — Supabase, notification, engine, memory providers
  src/services/                    — Memory service, workspace manager
  src/safety/                      — Circuit breaker, outcome tracker
  src/config/                      — Zod schemas + YAML/env loader
  tests/                           — 900+ vitest tests

scripts/ops/swe_team_runner.py   — Legacy Python runner (cron/daemon mode)
scripts/ops/swe_cli.py           — CLI tool (status, tickets, issues, repos, summary, report)
scripts/ops/swe_orchestrator.py  — Fleet orchestrator: pipeline intelligence + auto-remediation
src/swe_team/
    config.py          — Loads config/swe_team.yaml + env var overrides; ModelTiers dataclass
    models.py          — Dataclasses: SWETicket, TicketSeverity, TicketStatus, etc.
    monitor_agent.py   — Scans log directories for errors, deduplicates via fingerprints
    triage_agent.py    — Classifies severity, assigns tickets to agents
    investigator.py    — Root-cause analysis via CodingEngine; semantic memory context injection
    developer.py       — Attempts automated fixes, creates branches, keep/discard loop
    ralph_wiggum.py    — Stability gate (block/warn/pass)
    governance.py      — Deployment governor, complexity gates
    ticket_store.py    — JSON file-backed ticket persistence
    supabase_store.py  — Supabase PostgREST ticket persistence; semantic dedup; confidence
    embeddings.py      — bge-m3 embeddings + mem0-style fact extraction via LLM proxy
    telegram.py        — Standalone Telegram Bot API client (stdlib only, zero extra deps)
    notifier.py        — Telegram alerts, daily summaries, HITL escalation
    github_integration.py — GitHub issue creation and commenting (repo-aware)
    github_scanner.py     — Label-based GitHub issue discovery (autonomous backlog pickup)
    github_multi_repo.py  — Multi-repo issue aggregation across configured repos
    creative_agent.py  — Proactive improvement proposals (only when stable)
    distiller.py       — Trajectory distillation: caches successful fixes by fingerprint
    preflight.py       — PreflightCheck: validates git identity, clean tree, env vars
    repo_router.py        — Ticket → repo routing (fail-closed: unknown repo = ValueError)
    events.py          — SWE event definitions for A2A dispatch
    remote_logs.py     — SSH/rsync remote log collection + on-demand worker log fetch
    guardrails.py      — Unified safety gate coordinator (circuit breaker + governor + stability + throttle)
    queued_dispatcher.py — Queue-backed task dispatch bridge (TaskQueueProvider <> ParallelExecutor)
    rbac_middleware.py  — RBAC decorators: @require_permission, @require_sandbox, RBACContext
    atomic_checkout.py  — Atomic task checkout manager (prevents duplicate work across VMs)
    fix_verifier.py     — Post-merge fix verification (VERIFYING state)
    audit_trail.py      — Structured audit trail (file + Supabase backends)
    cost_tracker.py     — Per-agent cost tracking with budget hard-stops
    providers/task_queue/base.py    — TaskQueueProvider protocol (enqueue/claim/complete/fail/heartbeat)
    providers/task_queue/memory.py  — Thread-safe in-memory queue (priority heap, dead-letter, auto-retry)
    providers/checkout/base.py      — CheckoutProvider protocol (memory + supabase backends)
    providers/audit/base.py         — AuditProvider protocol
    providers/cost/base.py          — CostTrackerProvider protocol
src/a2a/               — A2A protocol: server, client, dispatch (hub + standalone), adapters
    src/a2a/server.py      — Lightweight A2A HTTP server (standalone mode)
    src/a2a/client.py      — A2A client (hub mode + direct agent mode)
    src/a2a/dispatch.py    — Event dispatcher: POSTs to centralized hub, fallback to standalone
    src/a2a/adapters/      — Agent adapters: gemini, opencode, generic CLI, swe_team
scripts/ops/a2a_hub.py     — Standalone A2A hub entry point
scripts/ops/a2a_request.py — CLI for sending A2A requests to any agent
scripts/ops/dashboard_data.py — Dashboard metrics generator
scripts/ops/supabase_schema.sql — Full Supabase DDL (includes goal hierarchy fields)
config/agent-card.json     — SWE Squad A2A agent card
config/swe_team.yaml   — Runtime configuration (agents, thresholds, log dirs, model tiers)
config/swe_team/programs/
    investigate.md     — Prompt template for investigation pass
    orchestrate.md     — Prompt template for orchestration (CRITICAL tickets)
    fix.md             — Prompt template for developer fix attempts
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
| `SUPABASE_URL` | Supabase PostgREST URL (enables Supabase ticket store) |
| `SUPABASE_ANON_KEY` | Supabase anon or service-role key |
| `SUPABASE_DB_PW` | PostgreSQL password for direct DB access |
| `SUPABASE_CONNECTION_STRING` | Direct PostgreSQL connection string |
| `BASE_LLM_API_URL` | External OpenAI-compatible proxy (embeddings + extraction) |
| `BASE_LLM_API_KEY` | API key for BASE_LLM proxy |
| `EMBEDDING_MODEL` | Embedding model name (default: `bge-m3`) |
| `EXTRACTION_MODEL` | Fact-extraction model via BASE_LLM (default: `gemini-3-flash`) |
| `SWE_MODEL_T1` | Override T1 model (cheap tasks, default: `haiku`) |
| `SWE_MODEL_T2` | Override T2 model (routine fixes, default: `sonnet`) |
| `SWE_MODEL_T3` | Override T3 model (critical/orchestration, default: `opus`) |
| `SWE_SSH_CONFIG` | Path to scoped SSH config for worker access |
| `SWE_REMOTE_NODES` | JSON array of worker nodes for remote log collection |

See `.env.example` for the full list.

## Two-System Distinction (CRITICAL — read before touching embeddings.py or investigator.py)

There are two separate LLM systems. **Never confuse them.**

```
BASE_LLM proxy (BASE_LLM_API_URL)       Claude Code CLI (/usr/bin/claude)
────────────────────────────────────    ────────────────────────────────────
http://your-llm-proxy.example.com/v1/   subprocess.run(["claude", "--print"...])
OpenAI-compatible HTTP API               Used only by InvestigatorAgent / DeveloperAgent
Cheap models: gemini-3-flash, bge-m3     Models: haiku / sonnet / opus
T1 cheap tasks: gemini-3-flash           T3 orchestration: opus (never implementer)
Used in: embeddings.py, store            Used in: investigator.py, developer.py
Do NOT use claude-haiku here             Do NOT call subprocess from library code
```

## Model Routing

| Scenario | Tier | Model | Notes |
|----------|------|-------|-------|
| Embeddings, fact extraction | T1 | `gemini-3-flash` | Via BASE_LLM proxy |
| Routine HIGH bugs | T2 | `sonnet` | CodingEngine subprocess |
| CRITICAL bugs | T3 | `opus` | CodingEngine, orchestrates sub-agents |
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

## Goal Hierarchy — Project Context for Tickets

Tickets can be organized into hierarchical projects with shared goals, enabling sub-task tracking and strategic context. This is optional; flat tickets still work as before.

### Fields

- `project_id` (Optional[str]): Unique project/initiative identifier. Groups related tickets under a shared goal.
- `parent_ticket_id` (Optional[str]): Ticket ID of the parent issue. Enables sub-task relationships.
- `goal` (Optional[str]): Short goal description (e.g. "Implement offline sync", "Launch mobile v1").

### Query Methods

**TicketStore** and **SupabaseTicketStore** both provide:
- `list_by_project_id(project_id)` — all tickets in a project
- `list_by_parent_ticket_id(parent_ticket_id)` — all sub-tasks of a ticket
- `get_project_root_tickets(project_id)` — root-level (no parent) tickets in a project

### Supabase Schema

Three new nullable columns on `swe_tickets`:
- `project_id TEXT` — indexed with team_id for fast filtering
- `parent_ticket_id TEXT` — indexed with team_id for sub-task queries
- `goal TEXT` — no index (descriptive only)

Run `scripts/ops/supabase_schema.sql` to apply the schema migration.

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

Example: investigating a failing test suite:
- Sub-agent A: read the failing module + recent git log
- Sub-agent B: search for similar past tickets in Supabase
- Sub-agent C: look up the third-party lib docs
- Synthesise: root cause analysis from A + B + C combined

## DevOps / GitOps Practices

These rules apply to ALL agents (investigator, developer, orchestrator):

### Git hygiene
- **Commit early and often.** Every agent must commit after every logical unit of work. Dirty git status in conversation headers wastes massive amounts of tokens — keep the working tree clean.
- **Feature branches only.** Never commit directly to `main` without a PR (except hot-fixes with explicit human approval).
- **One commit per logical change.** Squash noisy work-in-progress commits before opening a PR.
- **Descriptive commit messages.** Format: `type(scope): short summary` — types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
- **Always pull before pushing.** Run `git fetch && git rebase origin/main` to avoid unnecessary merge commits.
- **Never force-push to main.** Only force-push to personal feature branches.

### CI/CD gate
- **Tests must pass locally before pushing.** Run `python3 -m pytest tests/unit/ -q` — zero failures required.
- **Don't bypass hooks.** Never use `--no-verify`.
- **PR description must include a test plan.** PRs must always list the test command and expected output.

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

## A2A — Inter-Agent Communication

SWE Squad supports agent-to-agent communication via an optional A2A Hub:

- `src/a2a/dispatch.py` POSTs events to the hub (fallback: standalone log)
- `src/a2a/client.py` discovers agents and sends tasks through the hub
- `src/a2a/server.py` provides a lightweight standalone A2A server
- Config: `a2a_hub_url` in `swe_team.yaml` or `A2A_HUB_URL` env var
- **Standalone mode**: if no hub is configured, the system runs `src/a2a/server.py` locally

### Fallback agent chain (when primary engine is rate-limited)
```
Claude Code (primary) → Gemini CLI (fallback) → OpenCode (fallback) → fail gracefully
```

Configured in `swe_team.yaml` under `fallback_agents:`. Each fallback is tried in order via the agent registry.

## Multi-Agent Coordination Rules

- **Claim before working.** Before editing a file, check that no other agent has it in progress (check Supabase ticket assignments + git branches).
- **Heartbeat every 30 min.** Long-running investigations must update `ticket.metadata.last_heartbeat`. Stale tickets (>2h) get reassigned.
- **Emit A2A events.** Every state transition (triage, investigation start/complete, fix start/complete) must dispatch an event so other agents can react.
- **Don't duplicate work.** Before starting, query the ticket store for existing investigations on the same fingerprint.

## Provider-Agnostic Plugin Architecture (CORE DESIGN LAW)

SWE-Squad is **provider-agnostic in all non-core components**. Every external service,
tool, or platform is a swappable plugin. The core never imports a provider directly.

### The Rule

> If it can be replaced by a competing product, it must be behind an interface.

| Component | Interface | Current default | Alternatives |
|---|---|---|---|
| Coding agent | `CodingEngine` | Claude Code CLI | Gemini CLI, OpenCode, OpenHands, GitHub Copilot |
| Notification | `NotificationProvider` | Telegram | Slack, PagerDuty, email, webhook |
| Issue tracker | `IssueTracker` | GitHub Issues | Jira, Linear, GitLab |
| Dev sandbox | `SandboxProvider` | Docker | Local subprocess, GitHub Codespaces, cloud VMs |
| UI/Dashboard | `DashboardProvider` | Built-in HTTP server | Grafana, custom React app |
| Embeddings | `EmbeddingProvider` | bge-m3 via BASE_LLM | OpenAI, local sentence-transformers |
| Vector store | `VectorStore` | Supabase pgvector | Qdrant, Weaviate, Chroma |
| Env/Secrets | `EnvProvider` | dotenv flat-file | HashiCorp Vault, AWS Secrets Manager, K8s Secrets |
| Workspace | `WorkspaceProvider` | git-worktree | Docker volume, cloud VM, noop |
| Repo map | `RepoMapProvider` | ctags | tree-sitter, none (file listing fallback) |
| Task queue | `TaskQueueProvider` | In-memory (heapq) | Redis, RabbitMQ, SQS |
| Guardrails | `GuardrailsCoordinator` | Built-in | Custom orchestrator |

### Plugin Contract

Every plugin must:
1. Implement the interface defined in `src/swe_team/providers/<domain>/base.py`
2. Be registered in `config/swe_team.yaml` under the relevant `providers:` key
3. Receive all config via constructor — no `os.environ` reads inside plugin classes
4. Be loadable by name via `ProviderRegistry` without changing any core code

### What This Means in Practice

- **Never `import telegram` in core code.** Import `NotificationProvider` from the interface.
- **Never call `/usr/bin/claude` directly in core.** Call `self._engine.run(prompt)`.
- **Never hardcode `gh issue create`.** Call `self._tracker.create_issue(...)`.
- **New provider = new file in `providers/<domain>/` + entry in `swe_team.yaml`.** Nothing else changes.

### Violation Policy

Any hardcoded provider reference in core code is a bug. When found:
1. Log a GitHub issue with label `architecture-violation`
2. The violation is tracked until refactored
3. PRs introducing new violations will be rejected by the architecture lint check (`make lint-providers`)

## RBAC Middleware

All agent methods that perform privileged operations must be decorated:
- `@require_permission("task_name")` — checks `self._rbac_engine` for authorization
- `@require_sandbox` — verifies cwd is inside configured sandbox paths
- Both decorators are backward compatible: skipped if no RBAC engine is configured.

> **WARNING:** RBAC currently runs in **bypass mode** by default (no RBAC engine configured = all permissions granted). This is a known gap. Configure an RBAC engine in `swe_team.yaml` for production use.

## Development Safety & Idempotency

### GitHub Interaction
- **Idempotent Comments**: **NEVER** post a new GitHub comment without searching for an existing one with a `Ticket ID` marker first. Use `find_comment_by_text` and `update_github_comment` from `src/swe_team/github_integration.py`.
- **Multi-Repo Support**: `fetch_github_tickets()` iterates all repos from `config.repos` with repo-scoped fingerprints.
- **Bot Identity**: Configure `SWE_GITHUB_ACCOUNT` to your dedicated bot account. Each deployment should use its own account.

### Runaway Prevention
- **Session Caps**: A hard limit of 3 investigation attempts and 3 development attempts per ticket is enforced. Upon exhaustion, mark the ticket as `FAILED` and escalate to HITL.
- **Circuit Breaker**: The `CircuitBreaker` (`src/swe_team/circuit_breaker.py`) tracks the rolling failure rate of development attempts. If failures exceed 80%, the daemon pauses for 30 minutes.

## What NOT To Do

- **No hardcoded paths.** Use `Path(__file__).resolve()` or environment variables.
- **No secrets in code.** All credentials come from `.env` or environment variables.
- **No new runtime dependencies** without discussion. Keep the core dependency footprint minimal.
- **No breaking the test suite.** Run `make test` before committing.
- **No calling claude CLI from library code** (`embeddings.py`, `supabase_store.py`, etc.).
- **No Opus as implementer** — Opus orchestrates sub-agents; Sonnet/Haiku do the work.
- **No large tasks without sub-agents** — see Divide-and-Conquer rules above.
- **No raw grep/search output returned to main agent** — sub-agents must summarise.
- **No direct pushes to main** — always open a PR, wait for review.
