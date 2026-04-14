# Changelog

All notable changes to SWE Squad will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **React WebUI** — full-featured management dashboard built with React, TypeScript, and Vite
  - Dashboard with real-time metrics, PR pipeline chart, and configurable refresh interval
  - Tickets page with Kanban drag-and-drop, bulk actions, and properties panel
  - Teams management with live status, inline editing, and VM connectivity indicators
  - Engines page with 4-step Add Engine wizard, health checks, and BYOK API key config
  - Visual pipeline editor using React Flow for workflow graph design
  - Integrations hub with configure dialog, test connection, and credential management
  - MCP Server management UI with full CRUD operations
  - Projects page with GitHub repo linking and inline editing
  - Activity page with filters, timeline chart, CSV export, and expandable rows
  - Settings page with governance, cycle, and memory configuration
  - Agents page with enable/disable toggle and inline description editing
  - RBAC page with roles, permissions matrix, and bypass warning
  - Organization Admin page with member management
  - Per-account and per-project secrets management with TTL support
  - Environment Config UI for per-project environment variables
  - Execution Mode section on Control page
  - Goal tree view toggle and sidebar notification badges
  - Budget warning banner and cost limits editor
  - Ticket activity feed with inline diffs and comments
  - GitHub label trigger CRUD endpoints and UI
  - Task templates and automations inbox (Scheduler)
  - Onboarding wizard for first-time setup
  - Mobile-responsive sidebar with slide-out drawer
  - Error boundaries on all routes to prevent blank pages
  - Keyboard shortcuts (j/k navigation, Enter to open, / to search, t to toggle theme)
  - Toast notifications for ticket state changes
  - SSE real-time updates with polling fallback
- **GitHub OAuth authentication** — email signup + GitHub OAuth login with auto-created personal accounts
- **Account isolation** — multi-tenant account schema with org-level scoping and team rail context
- **Approvals API** — `/api/approvals` backend endpoints and Approvals UI page
- **Rate limit lifecycle** — `RateLimitLifecycle` state machine with full cooldown/recovery cycle
- **Model probe endpoint** — `POST /api/models/probe` with fallback parsing for live model testing
- **Self-describing provider schemas** — dynamic config forms driven by provider parameter definitions
- **Connector catalog** — Slack, Vercel, and Supabase direct connectors with catalog API
- **Cloud VM providers** — sandbox providers for cloud platforms with instance creation methods
- **Graph executor** — workflow graph executor with team workflow loading and runner gating
- **Daily DB backup script** — Supabase + SQLite backup with 7-day retention
- **Comprehensive E2E test suite** — Playwright-based, 3100+ tests at 99.7% pass rate

### Changed
- Sidebar reorganized into 6 clear sections with proper hierarchy
- Pricing page uses shared config with inline editing and model management
- Dashboard caches `costs_extended` and governor data to eliminate 3.5s response time
- Test count: 3,766+ (up from 827 at v0.4.0)

### Fixed
- **Auth login bug** — normalized server auth response format; auto-create personal account on first OAuth login
- **XSS via Mermaid** — sanitized Mermaid diagram rendering with audit report and tests
- **Team rail context switching** — org scope, query invalidation, clear team on switch
- **Mobile sidebar** — hamburger toggle and slide-out drawer with backdrop
- **Blank pages** — ErrorBoundary on all routes; graph API payload normalization
- **PR pipeline chart** — uses real data with graceful empty state
- **Budget wiring** — cost tracker connected to dashboard server with API error handling
- **Embeddings timeout** — increased timeout for large model extraction
- **Internal references scrubbed** — removed hardcoded IPs, secrets, internal org names, and bot accounts from UI, SQL migrations, and CI/CD tooling for public repo readiness

### Security
- **Public repo scrub (Phase 5)** — removed hardcoded IPs and secrets, scrubbed internal org names from WebUI, sanitized SQL migrations, cleaned CI/CD tooling
- **XSS prevention** — Mermaid rendering sanitization with comprehensive test coverage
- **Time-limited secret exposure (TTL)** — setup-only secrets auto-expire after configured duration
- **Dead code removal** — unreachable code after RBAC fix removed to reduce attack surface

---

## [0.5.0] - 2026-03-29 (Multi-Team Fleet & Observability)

### Added
- **Fleet orchestrator** (`scripts/ops/swe_orchestrator.py`) — pipeline intelligence + auto-remediation across multiple SWE teams
- **VM health monitor** (`scripts/ops/swe_vm_monitor.sh`) — cron-based health monitoring for fleet infrastructure
- **Atomic task checkout** (`src/swe_team/atomic_checkout.py`) — prevents duplicate work across VMs; `CheckoutProvider` protocol with memory + Supabase backends
- **Fix verifier** (`src/swe_team/fix_verifier.py`) — post-merge fix verification (VERIFYING state); ensures deployed fixes actually work
- **Audit trail system** (`src/swe_team/audit_trail.py`) — structured audit trail with file + Supabase backends; `AuditProvider` protocol
- **Cost tracker** (`src/swe_team/cost_tracker.py`) — per-agent cost tracking with budget hard-stops; `CostTrackerProvider` protocol
- **Migration schemas** — Supabase migrations for atomic checkout, audit trail, and cost tracking
- **Gamma team config** — additional SWE team configuration for economy-tier operations

---

## [0.4.0] - 2026-03-27 (Architecture Hardening)

### Added
- **Provider-agnostic plugin architecture** — all external services behind Protocol interfaces; `EnvProvider`, `WorkspaceProvider`, `RepoMapProvider`, `SandboxProvider`, `NotificationProvider`, `IssueTracker`, `CodingEngine`, `AuthProvider` protocols with concrete adapters
- **`ClaudeCodeEngine`** — pluggable CodingEngine wrapping the Claude CLI; injected via constructor; `shutil.which("claude")` discovery replaces hardcoded paths
- **`AuthProvider` + `InMemoryAuthProvider`** — thread-safe per-provider auth state tracking with 3-failure circuit breaker, key rotation, and TTL expiry
- **Session lifecycle** — `SessionStore` persists named Claude sessions; supports resume/fork across daemon restarts
- **Projects/Repos management** — REST endpoints + CLI subcommands + dashboard UI
- **AgentRegistry routing** — CRITICAL/complex tickets dispatched to external agents via A2A
- **Unified GuardrailsCoordinator** — single entry point for all safety gates (circuit breaker, governor, stability, throttle)
- **TaskQueueProvider abstraction** — in-memory priority queue with dead-letter, auto-retry, and lease heartbeat
- **QueuedDispatcher bridge** — decouples ticket producers from consumers
- **RBAC middleware** — `@require_permission` and `@require_sandbox` decorators with structured audit logging
- **RepoRouter sandbox enforcement** — fail-closed routing of tickets to configured sandbox repos
- **GitHub Actions CI workflow** — `.github/workflows/test.yml` automates PR test runs
- **Dashboard features**: ticket detail modal, SVG donut chart, CSV export, keyboard shortcuts, skeleton screens, toast notifications, SSE real-time updates, cost trend chart, agent activity feed, responsive layout, ticket search/filter, similarity graph, auth status panel, settings tab, scheduler Gantt, RBAC viewer
- **Rich CLI output** — color-coded tables/panels with graceful fallback
- **Portfolio website** — static site for GitHub Pages with animated hero and feature cards
- **219 provider tests** across 6 previously untested provider domains

### Fixed
- Architecture violations resolved — core agents refactored to use `CodingEngine` and `IssueTracker` interfaces
- Claude CLI permission mode changed from `dangerously-skip` to secure mode with three options
- `claim_ticket()` returns `False` on error for graceful fallback
- Governor budget caps actively enforced at runtime
- GitHub comment idempotency via `find_comment_by_text()` + `update_github_comment()`
- Investigation template restructured with mandatory Fix Plan section
- Developer `--allowedTools` format corrected to comma-separated
- EDT-aware throttle scheduling with granular `time_bands` configuration

### Security
- Default `--dangerously-skip-permissions` changed to `False` — explicit opt-in required
- `permission_mode` configuration: `strict`, `auto`, `bypass` modes

### Changed
- Test count: **827 to 3,766+** across this release cycle

---

## [0.3.0] - 2026-03-21

### Added
- **Gemini CLI fallback chain** — automatic failover when Claude Code is rate-limited; sanitizes prompts before forwarding
- **Live model probing** — real API requests to validate each candidate model before committing
- **Per-cycle throttle config** — `severity_filter`, `max_new_tickets_per_cycle`, `max_investigations_per_cycle` tunable via YAML
- **Backlog pickup** — runner fetches all OPEN/TRIAGED tickets each cycle instead of skipping when no new logs detected
- **Repo-aware investigator** — `cwd` set to correct local clone based on `ticket.repo`
- **DeepWiki + Playwright MCP servers** — available in all agent subprocesses
- **A2A server/client** — full JSON-RPC 2.0 implementation
- **Rate limiter** — `ExponentialBackoff` and `RateLimitTracker`
- **mem0-style semantic memory** — fact extraction, dedup, and confidence lifecycle
- **Standalone Telegram module** — stdlib-only Bot API client, no external deps
- **CLI tools** (`swe-cli`) — 6 subcommands: `status`, `tickets`, `issues`, `repos`, `summary`, `report`
- **Cron support** with recommended schedules

### Fixed
- **False regression loop (CRITICAL)** — inverted guard caused every resolved ticket to re-file as regression
- Investigator eligibility now accepts `OPEN` status for backlog tickets
- Ralph Wiggum gate loosened from 0/3 to 20/50 critical/high thresholds

### Changed
- Default `EXTRACTION_MODEL`: `gemini-3-flash` to `gemini-2.5-flash-thinking`
- 511 unit tests (up from 327)

---

## [0.2.0] - 2026-03-17

### Added
- **Opus orchestrator pattern** — Opus acts as orchestrator only for CRITICAL tickets; launches sub-agents for implementation
- **Model tiers** (`ModelTiers` dataclass) — T1/T2/T3 with env var overrides
- **pgvector semantic memory** — bge-m3 embeddings via BASE_LLM proxy stored in Supabase
- **Monitor self-scan recursion fix** — defense-in-depth prevents agents from scanning their own logs
- **PreflightCheck gate** — validates git identity, repo accessibility, clean tree, and env vars
- **Closed-loop fix validation** — post-fix regression monitoring with re-investigation
- **HITL escalation** — Telegram alert after 3 failed fix attempts
- **Multi-repo support** — each ticket carries a `repo` field
- 243 unit tests (up from 132)

### Fixed
- Monitor agent scanning its own log file causing recursive ticket creation
- Preflight validation preventing agents from operating in wrong directory context

---

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
- Model routing: Haiku (cheap) to Sonnet (routine) to Opus (critical)
- Keep/discard fix loop with git branch isolation
- Deployment governor with complexity gates
- Creative agent for proactive improvement proposals
- Configurable via YAML and environment variables
- 132 unit tests
