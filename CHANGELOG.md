# Changelog

All notable changes to SWE Squad will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Provider-agnostic plugin architecture** — 12 domain interfaces with 20+ built-in implementations; every external dependency is a swappable plugin registered via `swe_team.yaml`
- **Multi-team support** — assignee-based issue isolation enables alpha/beta squad coexistence on shared infrastructure without ticket collisions
- **Session lifecycle management** — `SessionStore` persists Claude Code sessions across daemon cycles; developer sessions fork from investigator sessions via `--fork-session`, carrying full context forward
- **Parallel executor with graceful timeout handling** — `ParallelExecutor` runs fix attempts in isolated git worktrees with configurable concurrency and per-attempt timeouts
- **Knowledge store with semantic similarity scoring** — `KnowledgeStore` wraps pgvector retrieval with graph-based relationship scoring for non-obvious cross-ticket connections
- **A2A inter-agent protocol** — JSON-RPC 2.0 event bus with server, client, hub dispatch, and adapters for Gemini CLI, OpenCode, and generic CLI agents
- **GitHub OAuth dashboard** — optional WebUI (`control_plane_api.py`) with live ticket metrics, session browser, project configuration editor, and role-based user management
- **Control plane API** — runtime configuration of model tiers, project priority weights, and sandbox paths without daemon restart
- **Circuit breaker** — rolling 80% development failure rate triggers a 30-minute pause; all LLM calls use capped exponential backoff
- **Credential scanner** — pre-commit and inline scanner detects secrets (API keys, tokens, private IPs) before they reach git history
- **Automated code review** — every fix PR is reviewed with a fail-closed merge policy; review failures block merge
- **Multi-repo scanning** — `github_multi_repo.py` aggregates issues across all configured sandbox repos with repo-scoped fingerprints
- **GitHub invite management** — `github_invites.py` handles bot account onboarding to new repos automatically
- **Orchestrator agent** — dedicated `orchestrator.py` coordinates sub-agent dispatch for CRITICAL tickets; Opus orchestrates, Sonnet/Haiku implement
- **Guardrails coordinator** — `guardrails.py` unifies circuit breaker, deployment governor, stability gate, and rate limiter into a single decision point
- **Log formatter** — structured log output with severity coloring and machine-readable JSON mode
- **Model probe** — runtime probe validates configured model tiers before the daemon starts
- **Rate limiter** — token-bucket rate limiter with per-model and per-team buckets
- **Proxy model policy** — declarative mapping of logical tier names to provider-specific model IDs
- **Batch resolution** — bulk-resolve stale or false-positive tickets via `swe-cli batch-resolve`
- **Scheduler** — cron-style task scheduler for recurring health checks and report generation
- **RBAC middleware** — `@require_permission` and `@require_sandbox` decorators for all privileged agent operations
- **Queued dispatcher** — `QueuedDispatcher` bridges `TaskQueueProvider` and `ParallelExecutor` for backpressure-safe task dispatch

## [0.3.0] - 2026-03-17

### Added
- **mem0-style semantic memory** — full extraction, dedup, and confidence lifecycle
  - `extract_memory_facts()`: distils resolved tickets into structured facts (root cause, fix, module, tags) via `gemini-3-flash` on BASE_LLM proxy before embedding — cleaner, denser embeddings
  - `store_embedding_with_dedup()`: 0.92 cosine-similarity threshold prevents duplicate memories; `_memory_detail_score()` tuple comparison chooses richer content on merge
  - Memory lifecycle: `memory_confidence` and `memory_accessed_at` columns; confidence increments (+0.1, cap 2.0) each time a memory is used; stale memories filtered by `max_age_days` (default 180)
  - `match_similar_tickets` RPC updated: confidence-weighted ranking, `raw_similarity` for transparency, TTL filter
  - `record_memory_hit()` called from investigator on every semantic context hit
- **Standalone Telegram module** (`src/swe_team/telegram.py`) — stdlib-only Bot API client, no external deps
- **CLI tools** (`scripts/ops/swe_cli.py`) — 6 subcommands: `status`, `tickets`, `issues`, `repos`, `summary`, `report`; all support `--json` for machine-readable output
- **Cron support** — `crontab.example` with recommended schedules for continuous monitoring and daily reports
- `--report daily|cycle|status` modes added to runner for cron integration
- Cost-tracking aggregation in daily summaries

### Changed
- `notifier.py` and `developer.py` rewired to use new `telegram.py` module
- `match_similar_tickets` Supabase RPC now returns `memory_confidence` and `raw_similarity` columns
- 327 unit tests (up from 243)

## [0.2.0] - 2026-03-17

### Added
- **Opus orchestrator pattern** — Opus acts as orchestrator only for CRITICAL tickets; launches Sonnet/Haiku sub-agents for all implementation work
- **Model tiers** (`ModelTiers` dataclass in `config.py`) — T1/T2/T3 with env var overrides (`SWE_MODEL_T1/T2/T3`); defaults: T1=haiku, T2=sonnet, T3=opus
- **pgvector semantic memory** — bge-m3 (1024-dim) embeddings via BASE_LLM proxy stored in Supabase; `find_similar()` retrieves top-k resolved tickets by cosine similarity at investigation time
- **Monitor self-scan recursion fix** — defense-in-depth: `exclude_patterns` config, hardcoded path guard, line-level `_SELF_LOG_RE` regex filter; prevents exponential ticket growth from agents scanning their own logs
- **PreflightCheck gate** — validates git identity, repo accessibility, clean working tree, and required env vars before DeveloperAgent commits
- **Closed-loop fix validation** — post-fix regression monitoring watches resolved tickets for recurrence within a configurable window; re-investigation path with parent context injection
- **HITL escalation** — after 3 failed fix attempts or regressions, fires alert to operator
- **Regression routing** — regression tickets always escalate to T3 (Opus) regardless of severity
- **`orchestrate.md` program** — generic orchestration prompt for Opus with CRITICAL RULES section enforcing anti-recursion
- **Multi-repo support** — each ticket carries a `repo` field; investigator and developer use it to set the correct working directory for coding engine invocations
- Supabase schema: pgvector extension, `embedding vector(1024)` column, IVFFlat index, `match_similar_tickets` RPC, `swe_ticket_events` audit trail
- 243 unit tests (up from 132)

### Fixed
- Monitor agent scanning its own log file causing recursive ticket creation
- Preflight validation preventing agents from operating in wrong directory context

## [0.1.0] - 2026-03-17

### Added
- Core agent loop: monitor, triage, investigate, develop, test
- Ralph Wiggum stability gate (bugs before features)
- Trajectory distillation for cached deterministic fixes
- Supabase ticket store with multi-team support and audit trail
- JSON ticket store as zero-dependency default
- A2A protocol adapter for inter-agent communication
- GitHub integration (issue creation, commenting, assignment)
- Telegram notifications (alerts, HITL escalation, daily summaries)
- Remote log collection via SSH/rsync
- Model routing: Haiku (cheap) → Sonnet (routine) → Opus (critical)
- Keep/discard fix loop with git branch isolation
- Deployment governor with complexity gates
- Creative agent for proactive improvement proposals
- Configurable via YAML and environment variables
- 132 unit tests
