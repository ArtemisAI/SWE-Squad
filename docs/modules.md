# Module Reference

All modules live under `src/swe_team/`. Each is a focused Python module with a single responsibility. The system has no mandatory runtime dependencies beyond `pyyaml` and `python-dotenv`.

## Core pipeline modules

| Module | Description |
|--------|-------------|
| `monitor_agent.py` | Scans configured log directories for error patterns, de-duplicates against recently filed tickets via fingerprints, and creates `SWETicket` items for new issues. |
| `triage_agent.py` | Receives `ISSUE_DETECTED` events or raw `SWETicket` objects, classifies by severity and module, and assigns to the appropriate agent tier. |
| `investigator.py` | Runs root-cause analysis via the Claude Code CLI and attaches the resulting report to the ticket. Injects semantic memory context from past similar tickets. |
| `developer.py` | Uses a keep/discard loop with git as the state machine to attempt fixes and only keep changes that pass tests and complexity gates. |
| `ralph_wiggum.py` | Implements the stability-first governance pattern — fix bugs before building new features. Evaluates error rate, open ticket backlog, and regression risk to return `PASS`, `WARN`, or `BLOCK`. |
| `governance.py` | Provides deployment rules, rollback logic, and integration checks. Works alongside Ralph Wiggum to enforce CI/CD gates. |

## Storage modules

| Module | Description |
|--------|-------------|
| `ticket_store.py` | JSON file-backed persistent ticket store. Lightweight default with fingerprint dedup tracking. Drop-in interface compatible with `supabase_store.py`. |
| `supabase_store.py` | Supabase PostgREST-backed ticket store. Drop-in replacement with semantic dedup (cosine similarity), confidence weighting, and pgvector search. Zero extra dependencies — uses only `urllib`. |

## Intelligence modules

| Module | Description |
|--------|-------------|
| `embeddings.py` | Embedding helper for semantic memory. Calls the BASE_LLM proxy (bge-m3 by default) and returns `None` on failure so callers treat semantic memory as best-effort. |
| `distiller.py` | Trajectory distiller — stores deterministic fix automations keyed by error fingerprint so repeated issues are resolved without re-running LLM investigations. |
| `creative_agent.py` | Analyzes resolved ticket patterns and proposes low-severity improvements. Only runs when the stability gate is passing. |

## Infrastructure modules

| Module | Description |
|--------|-------------|
| `config.py` | Loads `config/swe_team.yaml` plus environment variable overrides. Provides the `ModelTiers` dataclass for T1/T2/T3 model routing. |
| `models.py` | Core dataclasses: `SWETicket`, `TicketSeverity`, `TicketStatus`, `IssueType`, and related enums. |
| `events.py` | SWE-team-specific pipeline events extending the core A2A `EventType` vocabulary. |
| `preflight.py` | `PreflightCheck` validates git identity, clean tree, and required env vars before any developer or investigator action. |
| `scheduler.py` | Cron-based, quota-aware, peak-hour-aware job runner. Supports 5-field cron expressions and per-job quota limits. |
| `rate_limiter.py` | Rate-limit detection and exponential backoff for Claude Code CLI calls. Provides `ExponentialBackoff` retry wrapper. |
| `session.py` | Session tagging for end-to-end tracing across logs, GitHub comments, and Supabase records. |

## Security & RBAC modules

| Module | Description |
|--------|-------------|
| `agent_rbac.py` | Role-Based Access Control for SWE-Squad agents. Loads role definitions from `config/swe_team/roles.yaml`. Deny-by-default: any permission not explicitly granted is denied. |
| `model_boundary.py` | SEC-68 model boundary enforcement. Ensures only approved Claude models are used for code generation tasks. Blocks unauthorized model substitution. |

## Notification & integration modules

| Module | Description |
|--------|-------------|
| `notifier.py` | Sends Telegram alerts for new high/critical tickets, stability gate blocks, and daily summaries. |
| `telegram.py` | Standalone Telegram Bot API client using only stdlib (`urllib`). Reads credentials from environment variables. Zero external dependencies. |
| `github_integration.py` | Creates and manages GitHub issues from SWE tickets using the `gh` CLI. Repo-aware: reads repo from `ticket.metadata['repo']`. |
| `remote_logs.py` | Collects logs from remote worker machines via SSH/rsync. Maps source modules to workers for targeted log fetching during investigation. |

## A2A protocol (`src/a2a/`)

| Module | Description |
|--------|-------------|
| `src/a2a/server.py` | Lightweight standalone A2A HTTP server for when the hub is unreachable. |
| `src/a2a/client.py` | A2A client supporting both hub mode (centralized) and direct agent mode. |
| `src/a2a/dispatch.py` | Event dispatcher — POSTs to the centralized hub with fallback to standalone log. |
| `src/a2a/adapters/` | Agent adapters for Gemini CLI, OpenCode, generic CLI, and SWE-Team itself. |

## Ops scripts

Additional operational scripts live under `src/swe_team/ops/` and `scripts/ops/`:

- `swe_team_runner.py` — Main entry point; runs one cycle or daemon loop.
- `swe_cli.py` — CLI tool: `status`, `tickets`, `issues`, `repos`, `summary`, `report`.
- `dashboard_data.py` — Dashboard metrics generator writing `data/swe_team/status.json`.
- `a2a_hub.py` — Standalone A2A hub entry point.
- `webhook_listener.py` — GitHub webhook listener (port 9876) for push-triggered propagation.
- `propagate.sh` — Parallel SSH code propagation to all worker nodes (~4 seconds).
