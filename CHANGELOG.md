# Changelog

All notable changes to SWE Squad will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.3.0] - 2026-03-17

### Added
- **Gemini CLI fallback chain** — `GeminiCLIAdapter` wraps `/usr/bin/gemini` as a drop-in `_FallbackAgent`; activated automatically when Claude Code returns `RateLimitExhausted` after exponential backoff. Sanitises prompts before forwarding (blocks credential keywords). Fallback chain: Claude CLI → backoff (30s→300s) → Gemini CLI → Telegram HITL.
- **Live model probing** — `ModelProbe` now sends real API requests (`probe_embedding_model`, `probe_chat_model`) to each candidate before committing to it. Detects models that are listed but return empty responses (e.g. `gemini-3-flash`). Falls through the fallback list until a candidate actually responds.
- **Richer BASE_LLM fallback lists** — `kimi-k2.5:cloud` (1M context), `deepseek-r1:14b`, `qwen3-coder:30b` added. `gemini-2.5-flash-thinking` promoted to first slot for extraction. `gemini-3-flash` demoted to last-resort.
- **Per-cycle throttle config** (`CycleConfig`) — `severity_filter`, `max_new_tickets_per_cycle`, `max_investigations_per_cycle`, `max_developments_per_cycle`, `max_open_investigating` tunable via `swe_team.yaml` under `cycle:`.
- **Backlog pickup** — runner fetches all `OPEN`/`TRIAGED` tickets from store each cycle and merges into the investigation queue; previously the squad skipped the backlog when no new logs were detected.
- **`--max-cycles N`** CLI arg — daemon stops after N cycles.
- **Repo-aware investigator cwd** — `InvestigatorAgent` maps `ticket.repo` → local clone path and passes `cwd=` to the Claude CLI subprocess.
- **Repo-aware GitHub comments** — `comment_on_github_issue()` passes `--repo` to `gh`; LinkedAi comments go to `ArtemisAI/LinkedAi`, not `SWE-Squad-DEV`.
- **DeepWiki + Playwright MCP servers** — added to `.mcp.json` and `~/.claude.json`; investigation prompt updated to document when to use each.
- **Model card** — `config/swe_team/model_card.md` documents all agents, models, MCP servers, routing rules, and cost estimates.
- **`src/apply/` module** — `char_guard.py` (ATS field char-count limits), `field_classifier.py` (EEO field detection), `hitl_gate.py`.
- **A2A server/client** — `src/a2a/server.py`, `src/a2a/client.py`, full JSON-RPC 2.0 implementation.
- **Rate limiter** — `src/swe_team/rate_limiter.py`: `ExponentialBackoff`, `RateLimitTracker`.
- **Agent registry** — `src/swe_team/agent_registry.py`.
- **Observability dashboard** — `templates/dashboard.html`, `config/grafana/`, `scripts/ops/dashboard_data.py`.
- **A2A hub URL** — corrected to `100.110.176.73:18790` (Tailscale); agents: `openclaw`, `gemini`, `llm_proxy`.
- **Divide-and-conquer + DevOps/GitOps rules in CLAUDE.md** — mandatory sub-agent orchestration, feature branch workflow, rebase policy, secrets management.
- Investigation telemetry: `model`, `repo_cwd`, `report_chars`, `duration_s` written to ticket metadata.

### Fixed
- **False regression loop (CRITICAL)** — `check_regressions()` had inverted guard causing every resolved ticket to re-file as a regression every cycle. Removed inverted guard; also skips `gh-issue-*` fingerprints.
- **Investigator eligibility** — `_eligible()` now accepts `TicketStatus.OPEN`; backlog tickets were silently skipped.
- **Developer agent git index crash** — crashed with `RuntimeError: error: you need to resolve your current index first` on dirty index. Filed as #22.
- **Ralph Wiggum gate** — loosened from `0/3` to `20/50` critical/high; `require_ci_green=false`.

### Changed
- Default `EXTRACTION_MODEL`: `gemini-3-flash` → `gemini-2.5-flash-thinking` (former returns empty responses).
- 511 unit tests (up from 327)

### Added
- **mem0-style semantic memory** — full extraction, dedup, and confidence lifecycle
  - `extract_memory_facts()`: distils resolved tickets into structured facts (root cause, fix, module, tags) via `gemini-3-flash` on BASE_LLM proxy before embedding — cleaner, denser embeddings
  - `store_embedding_with_dedup()`: 0.92 cosine-similarity threshold prevents duplicate memories; `_memory_detail_score()` tuple comparison chooses richer content on merge
  - Memory lifecycle: `memory_confidence` and `memory_accessed_at` columns; confidence increments (+0.1, cap 2.0) each time a memory is used; stale memories filtered by `max_age_days` (default 180)
  - `match_similar_tickets` RPC updated: confidence-weighted ranking, `raw_similarity` for transparency, TTL filter
  - `record_memory_hit()` called from investigator on every semantic context hit
- **Standalone Telegram module** (`src/swe_team/telegram.py`) — stdlib-only Bot API client, no external deps; replaces broken LinkedAI import
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
- **Opus orchestrator pattern** — Opus 4.6 acts as orchestrator only for CRITICAL tickets; launches Sonnet/Haiku sub-agents for all implementation work; never implements directly
- **Model tiers** (`ModelTiers` dataclass in `config.py`) — T1/T2/T3 with env var overrides (`SWE_MODEL_T1/T2/T3`); T1=haiku, T2=sonnet, T3=opus
- **pgvector semantic memory** — bge-m3 (1024-dim) embeddings via BASE_LLM proxy stored in Supabase; `find_similar()` retrieves top-k resolved tickets by cosine similarity at investigation time
- **Monitor self-scan recursion fix** — defense-in-depth: `exclude_patterns` config, hardcoded `swe_team` path guard, line-level `_SELF_LOG_RE` regex filter; prevents exponential ticket growth from agents scanning their own logs
- **PreflightCheck gate** — validates git identity, repo accessibility, clean working tree, and required env vars before DeveloperAgent commits; failures surface as clear error messages rather than silent corruption
- **Closed-loop fix validation** — post-fix regression monitoring watches resolved tickets for recurrence within a configurable window; re-investigation path with parent context injection
- **HITL escalation** — after 3 failed fix attempts or regressions, fires Telegram alert to operator
- **Regression routing** — regression tickets always escalate to T3 (Opus) regardless of severity
- **`orchestrate.md` program** — generic orchestration prompt for Opus; uses `{repo_root}`, `{test_command}`, `{github_repo}` placeholders; CRITICAL RULES section enforces anti-recursion
- **Multi-repo support** — each ticket carries a `repo` field; investigator and developer use it to set the correct `cwd` for Claude CLI invocations
- Supabase schema: pgvector extension, `embedding vector(1024)` column, IVFFlat index, `match_similar_tickets` RPC, `swe_ticket_events` audit trail
- 243 unit tests (up from 132)

### Fixed
- Monitor agent scanning its own log file causing recursive ticket creation (#8, #9)
- Preflight validation preventing agents from operating in wrong directory context (#10)

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
