# SWE-Squad WebUI — Page Reference

## Navigation Structure

The application uses a persistent left sidebar (`Sidebar.tsx`) with grouped navigation sections, a collapsible team rail on the far left (`TeamRail.tsx`), and an optional right-side properties panel. The sidebar is togglable on desktop (`Cmd+B`) and slides over on mobile. A `BreadcrumbBar` appears above page content on nested routes.

**Sidebar sections (top to bottom):**

1. **Main** — Dashboard, Inbox (badge: unread count), Create
2. **Work** — Tickets (badge: open count), Goals, Projects, Workspaces
3. **Agents** — Teams, Agents, Engines
4. **Operations** — Control, Scheduler, Routines
5. **Observability** — Activity, Costs, RBAC, Approvals (badge: pending count), Graph
6. **Config** — Settings, Instance, Organization, Integrations, Data Export, Data Import

All authenticated routes are wrapped in `AuthGuard` → `Layout`. Unauthenticated routes (`/welcome`, `/login`, `/onboarding`) render outside the layout shell.

**Global keyboard shortcuts:**

| Shortcut | Action |
|---|---|
| `Cmd+K` | Open command palette |
| `Cmd+I` | Open new ticket dialog |
| `Cmd+.` | Toggle properties/governor panel |
| `Cmd+B` | Toggle sidebar |

---

## Pages

### Dashboard (`/`)

**Purpose:** Central overview of system health, live agent activity, and recent ticket state. Auto-refreshes at a configurable interval (default 30 s).

**Features:**
- Eight configurable metric cards arranged in two rows: Total Tickets, Critical, Fix Success Rate, PRs Merged (24 h), Memory Hits (24 h), Gate Verdict, Cache Efficiency, Rate Limits. Cards can be shown/hidden via an "Edit Cards" popover; preferences are persisted in `localStorage`.
- Dismissible budget warning banner that turns red when daily spend exceeds 95 % or a `hard_stop` budget status is returned.
- **Live Runs** section that polls at half the main refresh interval (min 15 s), showing currently active agent runs with status, severity, assignee, and age. Each row links to the ticket detail page.
- Status and Severity donut/pie charts (`StatusChart`, `SeverityChart`).
- PR Pipeline chart showing creation vs. merge rates and verification pass rate.
- **Suggested Tasks** panel: AI-generated proactive improvement cards (reliability, testing, performance, security, refactor). Each card has Accept and Dismiss actions.
- Recent Activity feed (last 10 events) and Recent Tickets list (open + in-progress, sorted by update time).
- GitHub Summary box (shown only when GitHub integration is enabled): open issues, linked, and orphaned counts.
- `GovernorPanel` component showing deployment governor status.

**API Endpoints:**
- `GET /api/dashboard` — full dashboard payload (ticket counts, agent performance, PR lifecycle, memory stats, costs, recent activity)
- `GET /api/heartbeats/live` — live agent run statuses
- `GET /api/settings/display` — refresh interval and display preferences
- `GET /api/budget/status` — daily budget usage percentage
- `GET /api/suggestions` — pending proactive task suggestions
- `POST /api/suggestions/:id/accept` — accept a suggestion
- `POST /api/suggestions/:id/dismiss` — dismiss a suggestion

---

### Tickets (`/tickets`)

**Purpose:** Full ticket management console — browse, filter, bulk-edit, and triage all SWE tickets in list or kanban board view.

**Features:**
- Toggle between **List view** (tabbed: Open / In Progress / Closed) and **Kanban board view** (drag-and-drop columns).
- Filter bar: Severity (ALL / CRITICAL / HIGH / MEDIUM / LOW), Status (18 statuses), Assignee (dynamic from ticket data), Sort by (Updated / Created / Severity with ASC/DESC toggle), full-text search across title, ID, and assignee.
- Board view supports grouping by: Status, Severity, Assignee, or Project. Dragging a card between status columns triggers a status update.
- **Bulk action bar** (floats at bottom when tickets are selected): select tickets with checkbox, shift-click for range selection, Escape to clear. Bulk operations: Change Status, Change Severity, Assign — all applied concurrently with toast feedback.
- URL-driven state: all filter, sort, view, and tab parameters are synced to query string for shareable deep-links.
- Inline severity editing directly from the list row.

**API Endpoints:**
- `GET /api/dashboard` — ticket data (tickets_by_state: open/in_progress/closed)
- `PATCH /api/tickets/:id/status` — update single ticket status
- `PATCH /api/tickets/:id/severity` — update single ticket severity
- `POST /api/tickets/:id/assign` — assign ticket to an agent

---

### Ticket Detail (`/tickets/*`)

**Purpose:** Full detail view for a single ticket with investigation controls, inline editing, activity feed, and semantic memory info.

**Features:**
- Inline-editable title (click to edit, Enter to save).
- Inline-editable description (multiline).
- **Investigate** and **Develop** action buttons trigger agent runs on-demand.
- Status dropdown changer and severity/priority icon changer with optimistic toast feedback.
- Inline assignee editor with text input (supports keyboard shortcuts).
- Two-tab layout: **Detail** (default) and **Activity Feed**.
- Detail tab shows: Live Run Widget (when active), description, Investigation Report, Proposed Fix, Test Results, Comment Thread.
- Sidebar (Detail tab): WorkspaceCard, Details panel (ID, type, investigation/dev attempt counters, fix confidence, source module, fingerprint), Labels, Semantic Memory status, Dependencies (blocked-by / blocking links), Activity Timeline.
- Activity Feed tab shows a streaming `TicketFeed` component.
- Links to GitHub issue (`meta.github_url`) and PR (`meta.pr_url`) with external link buttons.
- Breadcrumb: Tickets → ticket_id.

**API Endpoints:**
- `GET /api/dashboard` — source of ticket data (resolved by ticket_id client-side)
- `GET /api/tickets/:id/activity` — audit trail / activity events
- `PATCH /api/tickets/:id/status` — change status
- `PATCH /api/tickets/:id/severity` — change severity
- `POST /api/tickets/:id/assign` — change assignee
- `PATCH /api/tickets/:id/title` — update title
- `PATCH /api/tickets/:id/description` — update description
- `POST /api/tickets/:id/investigate` — trigger investigation agent
- `POST /api/tickets/:id/develop` — trigger development agent

---

### Projects (`/projects`)

**Purpose:** Manage the set of repositories SWE-Squad monitors and works on — create, search, enable/disable, and configure projects.

**Features:**
- Responsive card grid (1→2→3 columns) sorted by priority (critical first) then alphabetically.
- Search bar filters by name, description, or local path.
- **Connect GitHub Repo** button opens `RepoPickerDialog` to browse and connect repos from the authenticated GitHub account.
- **Add Project** dialog: name, local path, description, GitHub repo (optional), priority selector, enabled toggle.
- Each card: inline-editable name, description, priority badge (via click-to-edit select), GitHub repo link (editable if unset), local path (editable), ticket count, total cost, enabled/disabled toggle with green/grey dot indicator.
- Delete button (hover reveal) with confirmation dialog.
- Priority sorting: critical → high → medium → low.

**API Endpoints:**
- `GET /api/projects` — list all projects with stats (ticket_count, total_cost_usd)
- `POST /api/projects` — create new project
- `DELETE /api/projects/:name` — delete project
- `PATCH /api/projects/:name/enabled` — toggle enabled flag
- `PATCH /api/projects/:name/name` — rename
- `PATCH /api/projects/:name/description` — update description
- `PATCH /api/projects/:name/priority` — update priority
- `PATCH /api/projects/:name/local_path` — update local path
- `PATCH /api/projects/:name/github_repo` — link GitHub repo
- `GET /api/github/repos` — (via RepoPickerDialog) list connectable GitHub repos

---

### Project Detail (`/projects/*`)

**Purpose:** Deep-dive view for a single project — tickets, environment variables, and configuration.

**Features:**
- Inline-editable project name, description, and priority.
- Ticket list scoped to the project, with status badges and links to ticket detail.
- Environment variable manager: add/edit/delete key-value pairs (secrets masked with show/hide toggle).
- Project metadata: GitHub repo link, local path, enabled status, priority badge.
- Import/export of environment variables (Upload / Download buttons).
- Delete project action with confirmation.
- Back navigation to Projects list.

**API Endpoints:**
- `GET /api/projects/:name` — project detail with tickets
- `PATCH /api/projects/:name/*` — field-level updates (same set as Projects list)
- `DELETE /api/projects/:name` — delete
- `GET /api/projects/:name/env` — environment variables
- `POST /api/projects/:name/env` — set env variable
- `DELETE /api/projects/:name/env/:key` — remove env variable

---

### Agents (`/agents`)

**Purpose:** View and manage all configured SWE agents inline — role, model tier, engine, tools, and concurrency limits are all editable in the table.

**Features:**
- Editable table with columns: Status (enabled/disabled dot — click to toggle), Agent name + description, Role (editable select: monitor/triage/investigator/developer/reviewer/creative/governor), Model tier (editable select: haiku/sonnet/opus), Engine alias (editable text), Tools (comma-separated tag editor), Max concurrent tasks (editable number), Node (display only).
- Role filter tab bar above the table (All + one tab per distinct role).
- **Add Agent** dialog: name, description, role, model, engine, max tasks, tools. Saves to `config/swe_team.yaml` via API.
- Delete agent with confirmation modal (warns that the change modifies `swe_team.yaml`).
- Footer counts: total agents shown, enabled count (green), disabled count (red).
- All edits are in-place with optimistic save + "Save failed" error indicator per row.

**API Endpoints:**
- `GET /api/agents` — list all agents
- `POST /api/agents` — create agent
- `DELETE /api/agents/:name` — remove agent
- `PATCH /api/agents/:name/enabled` — toggle enabled
- `PATCH /api/agents/:name/description` — update description
- `PATCH /api/agents/:name/role` — update role
- `PATCH /api/agents/:name/model_tier` — update model tier
- `PATCH /api/agents/:name/engine` — update engine alias
- `PATCH /api/agents/:name/tools` — update tools list
- `PATCH /api/agents/:name/max_tasks` — update concurrency limit

---

### Agent Detail (`/agents/:id`)

**Purpose:** Deep-dive view for a single agent — configuration, run history, live heartbeat, and run transcripts.

**Features:**
- Agent header: name, role badge, model badge, enabled status indicator.
- Tab bar: Overview, Runs, Config, API Keys.
- Overview tab: current live run info (from heartbeat), recent run stats (success rate, total runs).
- Runs tab: paginated run history with status, duration, ticket linkage. Click a run to open the transcript viewer (`RunTranscriptView`).
- Config tab: inline-editable agent settings (model, engine, max tasks, tools, description).
- API Keys tab: masked key display with show/hide toggle and rotate action.
- Refresh button to re-fetch latest heartbeat.

**API Endpoints:**
- `GET /api/agents/:id` — agent config
- `GET /api/agents/:id/runs` — run history
- `GET /api/heartbeats` — agent heartbeat data
- `GET /api/agents/:id/runs/:runId/transcript` — run transcript
- `PATCH /api/agents/:id/*` — config updates
- `POST /api/agents/:id/api-key/rotate` — rotate API key

---

### Teams (`/teams`)

**Purpose:** Manage multi-VM SWE teams — view team configs, health, cost budgets, and GitHub account assignments.

**Features:**
- Team cards showing: VM, GitHub account, role (alpha/beta/gamma), tier, engine alias, max concurrent tasks, daily cost budget, specializations.
- Inline editing of all fields (same click-to-edit pattern as Agents page).
- Start / Stop / Restart controls per team.
- Activity indicator: shows whether the team's daemon is running.
- Cost budget display with daily spend if available.
- **Add Team** dialog for creating new team configurations.
- Delete team with confirmation.

**API Endpoints:**
- `GET /api/teams` — list all teams
- `POST /api/teams` — create team
- `DELETE /api/teams/:id` — remove team
- `PATCH /api/teams/:id/*` — field-level updates
- `POST /api/teams/:id/start` — start team daemon
- `POST /api/teams/:id/stop` — stop team daemon
- `POST /api/teams/:id/restart` — restart team daemon

---

### Engines (`/engines`)

**Purpose:** Configure and inspect the coding engine registry — fallback agents, model routing, and engine health.

**Features:**
- **Engine Registry** table: each registered engine with name, availability (green/red badge), and model.
- **Fallback Agent** list: priority-ordered fallback chain. Each entry shows name, command, default model, timeout, enabled toggle, skills, team assignment, masked API key. Full inline editing.
- **Model Routing** table: maps model tier aliases (T1/T2/T3) to concrete model names — editable.
- **Add Fallback Agent** dialog with all fields.
- Per-engine health check / ping action.
- API key fields have show/hide toggle.
- Pagination on fallback agents list.
- Search/filter on fallback agent list.

**API Endpoints:**
- `GET /api/engines` — engine registry, fallback agents, model routing
- `POST /api/engines/fallback` — add fallback agent
- `DELETE /api/engines/fallback/:name` — remove fallback agent
- `PATCH /api/engines/fallback/:name` — update fallback agent
- `PATCH /api/engines/routing` — update model routing table
- `POST /api/engines/:name/ping` — health check an engine

---

### Jules Engine (`/engines/jules`)

**Purpose:** Placeholder page for the Google Jules coding engine connector.

**Features:**
- Informational card explaining that runtime config lives in `config/swe_team.yaml` under `provider: jules`.
- "Coming soon: engine-specific controls" badge.
- Link back to `/engines`.

**API Endpoints:** None (static placeholder).

---

### Control (`/control`)

**Purpose:** Operator control plane for the SWE pipeline — pause/resume the daemon, trigger manual cycles, manage pipeline stages, and monitor the task queue.

**Features:**
- **Pipeline Status** card: running/paused state indicator, cycle interval display, Pause / Resume / Trigger Cycle buttons.
- **Pipeline Editor** (lazy-loaded): visual drag-and-drop editor for pipeline stage ordering and configuration.
- **Task Queue** section: shows queued, in-progress, and dead-letter tasks with counts.
- **Checklist** section: preflight checks with pass/warn/fail status for git identity, env vars, connectivity.
- **Audit Log** section: recent governance and control events.
- Links to sub-pages (Graph, RBAC) from breadcrumb area.

**API Endpoints:**
- `GET /api/control/pipeline` — pipeline status (state, cycle_interval, paused)
- `POST /api/control/pause` — pause the pipeline
- `POST /api/control/resume` — resume the pipeline
- `POST /api/control/trigger` — fire one pipeline cycle immediately
- `GET /api/control/queue` — task queue stats
- `GET /api/control/checklist` — preflight check results

---

### Costs (`/costs`)

**Purpose:** Comprehensive cost analytics — track API spend by time, agent, ticket, and model, with budget policy management and ROI calculation.

**Features:**
- Five tabs: **Time**, **Agent**, **Ticket**, **Model**, **Budget**.
- **Time tab**: date-range picker (presets: hour/day/week/month), clickable bar chart of cost over time (`CostBarChart`), summary metric cards (Total Cost, Avg per Ticket, Cache Efficiency, Total Tokens).
- **Agent tab**: cost breakdown table per agent (input tokens, output tokens, cache tokens, total cost USD).
- **Ticket tab**: per-ticket cost table with ticket ID, title, and cost.
- **Model tab**: per-model breakdown (input/output/cache tokens, count, cost).
- **Budget tab**: Budget Policy card (daily limits, hard-stop thresholds), Budget Incidents card, Biller Spend card, Provider Quota card, Finance Timeline chart, Accounting Model card. ROI calculator with configurable team cost input.
- Pricing editor: inline-editable per-model pricing table (input/output/cache cost per 1k tokens) with save button.

**API Endpoints:**
- `GET /api/dashboard` — base cost data
- `GET /api/costs` — time-bucketed cost data (with `?start=&end=` params)
- `GET /api/costs/by_agent` — per-agent breakdown
- `GET /api/costs/by_ticket` — per-ticket breakdown
- `GET /api/costs/by_model` — per-model breakdown
- `GET /api/costs/roi` — ROI calculation
- `GET /api/pricing` — model pricing table
- `PUT /api/pricing` — save pricing table
- `GET /api/budget/status` — budget usage
- `GET /api/budget/incidents` — budget incidents
- `GET /api/budget/provider-quotas` — provider quota data
- `GET /api/budget/spend-window` — 7-day spend history
- `GET /api/budget/subscriptions` — subscription billing data
- `GET /api/budget/policies` — budget policies

---

### Activity (`/activity`)

**Purpose:** Structured audit trail and event log — searchable, filterable, exportable history of all SWE pipeline actions.

**Features:**
- Filter bar: Action type (investigation_started, triage_complete, dev_started, dev_complete, status_changed, stability_check, other), Severity (CRITICAL/HIGH/MEDIUM/LOW), Date range (All Time / Today / Last 7 Days / Last 30 Days).
- Full-text search field.
- Bar chart (Recharts) showing event volume over time.
- Paginated event list: each row shows timestamp, action type badge, ticket ID (link), severity indicator, and collapsible detail.
- Export button (downloads as JSON).
- URL-driven filters via query parameters.

**API Endpoints:**
- `GET /api/activity` — activity events (with filter params: `action_type`, `severity`, `date_range`, `search`, `page`)

---

### Scheduler (`/scheduler`)

**Purpose:** Manage cron-scheduled automation jobs — view, create, pause/resume, trigger, and review run history.

**Features:**
- Three tabs: **Jobs**, **Templates**, **History**.
- **Jobs tab**: list of scheduled jobs with name, human-readable cron description, next run time, status (active/paused), category badge. Per-job: Pause/Resume toggle, Run Now (immediate trigger), Delete with confirmation.
- **Add Job** form: name, cron schedule (via `ScheduleEditor` component), description.
- **Templates tab**: pre-built job templates (maintenance, monitoring, reporting categories). One-click "Use Template" to instantiate a job from a template.
- **History tab**: run records with job ID, run time, status (success/failure), duration, and error message. Searchable by job ID.
- Cron expressions are rendered as human-readable strings ("Daily at 02:00 UTC", "Every hour", etc.).

**API Endpoints:**
- `GET /api/jobs` — list scheduled jobs
- `POST /api/jobs` — create new job
- `DELETE /api/jobs/:id` — delete job
- `POST /api/jobs/:id/pause` — pause job
- `POST /api/jobs/:id/resume` — resume job
- `POST /api/jobs/:id/run` — run immediately
- `GET /api/scheduler/history` — run records
- `GET /api/scheduler/templates` — available job templates

---

### Routines (`/routines`)

**Purpose:** Manage higher-level agent routines — recurring tasks with named schedules, run history, and webhook triggers.

**Features:**
- List of routines with status pill (active/paused/archived), schedule display, description.
- Per-routine actions: Run Now, Pause/Resume, Archive.
- **Create Routine** dialog: name, description, cron schedule (via `ScheduleEditor`).
- Links to individual routine detail pages.

**API Endpoints:**
- `GET /api/routines` — list routines
- `POST /api/routines` — create routine
- `POST /api/routines/:id/run` — trigger immediately
- `POST /api/routines/:id/pause` — pause
- `POST /api/routines/:id/resume` — resume
- `POST /api/routines/:id/archive` — archive

---

### Routine Detail (`/routines/:id`)

**Purpose:** Deep-dive view for a single routine — schedule editing, run history, activity log, and webhook configuration.

**Features:**
- Tabs: **Overview**, **Runs**, **Activity**.
- Schedule editor (`ScheduleEditor` component) with save button.
- Webhook URL field for trigger configuration.
- Run history table: run timestamp, status, duration.
- Activity log: timestamped event stream for the routine.
- Back link to Routines list.

**API Endpoints:**
- `GET /api/routines/:id` — routine config
- `GET /api/routines/:id/runs` — run history
- `GET /api/routines/:id/activity` — activity events
- `PUT /api/routines/:id` — update schedule and trigger config

---

### Goals (`/goals`)

**Purpose:** Track progress on projects and initiatives using goal hierarchies linked to ticket groups.

**Features:**
- Toggle between **Grid view** (cards) and **Tree view** (hierarchical).
- Search by goal ID or display name.
- **Grid view**: `GoalCard` components sorted by completion progress (descending) then name. Each card shows progress bar, ticket count, completed count.
- **Tree view**: `GoalTree` component rendering the full parent/child ticket hierarchy across all goals.
- **Create Goal** dialog: goal ID (required, slug-style), optional display name.
- Empty state with Create Goal CTA.

**API Endpoints:**
- `GET /api/goals` — list all goals with stats
- `POST /api/goals` — create goal
- `GET /api/goals/:id` — goal tree nodes (for tree view)

---

### Goal Detail (`/goals/:id`)

**Purpose:** Detail view for a single goal — hierarchy tree, flat ticket list, and completion statistics.

**Features:**
- Three tabs: **Hierarchy** (tree view of all tickets in the goal), **Tickets** (flat list with status badges and severity), **Stats** (completion percentage, open/closed breakdown).
- `GoalProgress` component: visual progress bar.
- `GoalTree` component for the hierarchy tab.
- Ticket rows link to their ticket detail pages.
- **Add Ticket to Goal** button (opens new ticket dialog pre-linked to this goal).
- Back link to Goals list.

**API Endpoints:**
- `GET /api/goals/:id` — goal nodes/hierarchy
- `GET /api/goals/:id/stats` — completion stats

---

### Graph (`/graph`)

**Purpose:** Visualize ticket dependency relationships as a directed acyclic graph (DAG) and view module interaction heat.

**Features:**
- Two tabs: **DAG** and **Heatmap**.
- **DAG tab**: topological-layout SVG graph. Nodes are tickets colored by severity (red = critical, orange = high, yellow = medium, grey = low). Edges represent block/dependency relationships with arrowheads. Clicking a node navigates to the ticket detail page. Falls back to a dependency table below the graph if SVG is empty.
- Dependency table: ticket ID (link), title, severity, blocked-by list, blocking list.
- **Heatmap tab**: `ModuleHeatmap` — N×N grid of source modules, cell color intensity proportional to co-occurrence frequency. Hover tooltip shows module pair and count. Legend with 5-step gradient scale.
- Summary line: "N tickets, M dependencies".

**API Endpoints:**
- `GET /api/graph` — graph data (`{ nodes, edges, heatmap }`)

---

### Approvals (`/approvals`)

**Purpose:** Human-in-the-loop review queue — approve, reject, or request revisions on tickets awaiting human sign-off before proceeding.

**Features:**
- List of tickets in `PENDING_HUMAN` or escalated states.
- Collapsible `ApprovalRow` per ticket: expand to reveal action forms.
- Four actions per approval: **Approve** (with optional note), **Reject** (requires reason), **Request Revision** (with feedback text), **Comment** (free-form note).
- Each action has its own text area and confirm button; all are disabled while a mutation is pending.
- Status badge and ticket metadata shown in collapsed state.
- Click on ticket title navigates to ticket detail.
- On action success: toast notification with new status, cache invalidation of both approvals and inbox queries.

**API Endpoints:**
- `GET /api/approvals` — list pending approvals
- `POST /api/approvals/:id/approve` — approve with optional note
- `POST /api/approvals/:id/reject` — reject with reason
- `POST /api/approvals/:id/revision` — request revision with feedback
- `POST /api/approvals/:id/comment` — add comment

---

### Inbox (`/inbox`)

**Purpose:** Unified notification and action centre — system alerts, failed agent runs, and pending approvals in one place.

**Features:**
- Three tabs with counts: **Alerts**, **Failed Runs**, **Approvals**.
- **Alerts tab**: `AlertCard` components for system alerts (severity: critical/warning/info). Swipe-to-archive gesture (`SwipeToArchive` component). Archive all button. Severity icons and color coding.
- **Failed Runs tab**: `FailedRunCard` for each failed agent run. Shows error summary, ticket ID link, and archive action.
- **Approvals tab**: `TicketCard` components for tickets needing human approval. Click navigates to ticket detail or approvals page.
- Archive actions for individual items.
- Empty states per tab when all items are cleared.

**API Endpoints:**
- `GET /api/inbox/alerts` — system alerts
- `POST /api/inbox/alerts/:id/archive` — archive alert
- `GET /api/inbox/failed-runs` — failed agent runs
- `POST /api/inbox/failed-runs/:id/archive` — archive failed run
- `GET /api/approvals?status=pending` — pending approvals (via `queryKeys.inbox.pendingApprovals`)

---

### Settings (`/settings`)

**Purpose:** SWE team runtime configuration — 9-tab settings editor covering display, governance, cycle, memory, monitoring, pricing, auth status, secrets, and GitHub triggers.

**Features (tabs):**
- **Display**: refresh interval, theme preferences.
- **Governance**: max open critical/high tickets, regression thresholds, circuit breaker settings.
- **Cycle**: daemon cycle interval, max retries, investigation/dev attempt caps.
- **Memory**: embedding model, similarity thresholds, TTL, memory hit confidence weights.
- **Monitor**: log directories, remote worker configuration.
- **Pricing**: per-model cost rates (displayed from Settings page; full edit is on Costs page).
- **Auth Status**: OAuth provider status, current token scopes, connected accounts.
- **Secrets**: list of registered secret keys with masked values, add/delete secret entries.
- **GitHub Triggers**: label-trigger rules — maps GitHub issue labels to automatic triage actions. CRUD for trigger entries with test capability.
- All numeric and string fields use inline click-to-edit pattern.

**API Endpoints:**
- `GET /api/settings` — full settings object
- `PATCH /api/settings` — update settings fields
- `GET /api/settings/display` — display-specific settings
- `GET /api/accounts` — connected accounts
- `GET /api/projects` — for project selector in some settings
- `GET /api/label-triggers` — GitHub label triggers
- `POST /api/label-triggers` — create trigger
- `DELETE /api/label-triggers/:id` — delete trigger
- `POST /api/label-triggers/:id/test` — test a trigger

---

### Instance Settings (`/instance`)

**Purpose:** Instance-level infrastructure settings — general config, sandbox provisioning methods, experimental flags, heartbeat monitoring, and MCP server connections.

**Features (tabs):**
- **General**: instance name, base URL, log level, max workers. (Rendered by `InstanceGeneralSettings` component.)
- **Provisioning**: sandbox creation methods — configure available VM/container providers, default isolation mode. (Rendered by `InstanceCreationMethods` component.)
- **Experimental**: feature flags for in-development capabilities. Toggle switches with descriptions. (Rendered by `InstanceExperimentalSettings` component.)
- **Heartbeat**: live instance health status — uptime display, component health breakdown, last heartbeat timestamp, pulse indicator. Auto-polls every 15 s.
- **MCP Servers**: list of connected Model Context Protocol servers. Add new server (URL, name, auth), delete, enable/disable per server.

**API Endpoints:**
- `GET /api/instance/settings` — general instance settings
- `PATCH /api/instance/settings` — update settings
- `GET /api/instance/heartbeat` — health/uptime data
- `GET /api/instance/mcp-servers` — MCP server list
- `POST /api/instance/mcp-servers` — add MCP server
- `DELETE /api/instance/mcp-servers/:id` — remove MCP server

---

### Integrations (`/integrations`)

**Purpose:** Configure third-party service connectors — authenticate and test integrations for notifications, issue trackers, CI systems, and more.

**Features:**
- Connector grid grouped by category (source control, CI/CD, monitoring, notifications, etc.).
- Search/filter connectors by name.
- Each connector card: name, description, category badge, supported actions and triggers, connected/disconnected status indicator.
- **Configure** button opens a dialog with the connector's credential schema: field labels, types (text, password, URL), required indicators, and show/hide toggle for secret fields.
- **Test Connection** button validates credentials before saving.
- Save writes credentials to the backend.

**API Endpoints:**
- `GET /api/integrations` — connector list and categories
- `POST /api/integrations/:connector_type/configure` — save credentials
- `POST /api/integrations/:connector_type/test` — test connection

---

### RBAC (`/rbac`)

**Purpose:** Read-only view of the role-based access control configuration — roles, permissions, overrides, and bypass mode status.

**Features:**
- Bypass mode banner: shown when RBAC engine is not configured (all permissions granted — known gap).
- Roles table: each role row shows allowed permissions (checkmark), denied permissions (X), and neutral permissions (dash). Column headers are all unique permissions across all roles.
- Overrides list: per-agent or per-user permission overrides with allow/deny indicators.
- Color-coded icons: `CheckCircle2` (allowed), `XCircle` (denied), `MinusCircle` (not set).
- Breadcrumb: Operations → RBAC.

**API Endpoints:**
- `GET /api/rbac` — roles, permissions, overrides, and bypass_mode flag

---

### Organization (`/organization`)

**Purpose:** Org admin panel — manage organization members, roles, and billing plan.

**Features:**
- Organization header: name, plan badge (free/pro/enterprise), member count, bot count.
- Member table: GitHub avatar, login, display name, role badge (owner/admin/developer/viewer), join date.
- Inline role editor per member (click the badge to get a select dropdown).
- **Invite Member** button: enter GitHub login, select role, submit.
- Delete member with confirmation.
- Edit organization display name (inline).
- Current user's own role is highlighted; owners cannot be removed.

**API Endpoints:**
- `GET /api/org` — org info, members, plan
- `POST /api/org/members` — invite member
- `DELETE /api/org/members/:login` — remove member
- `PATCH /api/org/members/:login/role` — change member role
- `PATCH /api/org` — update org name

---

### Workspaces (`/workspaces`)

**Purpose:** Manage isolated git worktree workspaces used by agents for development tasks.

**Features:**
- Filter bar: Mode (All / Isolated / Shared / Operator Branch), Status (All / running / provisioning / provisioned / stopped / error).
- `IssueWorkspaceCard` grid showing each workspace: ID, mode badge, status badge, associated ticket link, branch name, created time.
- **Create Workspace** button: opens dialog for new workspace with mode selection.
- Refresh button.
- Links to workspace detail pages.

**API Endpoints:**
- `GET /api/workspaces` — list workspaces
- `POST /api/workspaces` — create workspace
- (individual workspace operations on detail page)

---

### Workspace Detail (`/workspaces/:id`)

**Purpose:** Manage a single project-level workspace — view metadata, change mode, start/stop, and delete.

**Features:**
- Workspace metadata: ID, mode, status badge, branch, created time, associated ticket.
- Mode selector: Isolated / Shared / Operator Branch with descriptions.
- Start / Stop control buttons.
- Delete workspace with confirmation dialog.
- Git branch display.
- Filesystem tree viewer panel.
- Back navigation to Workspaces list.

**API Endpoints:**
- `GET /api/workspaces/:id` — workspace details
- `PATCH /api/workspaces/:id/mode` — change mode
- `POST /api/workspaces/:id/start` — start workspace
- `POST /api/workspaces/:id/stop` — stop workspace
- `DELETE /api/workspaces/:id` — delete workspace

---

### Execution Workspace Detail (`/workspaces/:id/execution`)

**Purpose:** Live execution console for a running workspace — real-time log streaming, execution status, and run controls.

**Features:**
- Execution status panel: running/stopped indicator, current step, progress percentage, start/completion times, error display.
- **Terminal-style log viewer**: scrolling log output with color-coded levels (info = blue, warn = amber, error = red, debug = muted). Auto-scrolls to latest entry.
- Start / Stop execution buttons.
- Refresh button to re-fetch status and logs.
- Download logs button.
- Maximize toggle for the log panel.
- Back link to parent workspace detail.

**API Endpoints:**
- `GET /api/workspaces/:id/execution` — execution status
- `GET /api/workspaces/:id/logs` — execution log entries
- `POST /api/workspaces/:id/execution/start` — start
- `POST /api/workspaces/:id/execution/stop` — stop

---

### Data Export (`/data/export`)

**Purpose:** Export ticket data from the system in CSV, JSON, or ZIP format with filtering options.

**Features:**
- Format selector: CSV / JSON / ZIP (ZIP includes full metadata).
- Filter panel: Status filter, Severity filter, Source Module filter.
- Optional: "Include full metadata" checkbox (adds investigation report, proposed fix, test results).
- Ticket selector table: shows all tickets matching filters with checkboxes for selective export. Select All support.
- Selected count indicator.
- Download button generates URL with filter params and triggers browser download.
- Preview of selected ticket IDs before export.

**API Endpoints:**
- `GET /api/dashboard` — ticket list for preview/selection
- `GET /api/export?format=csv|json|zip&status=...&severity=...&include_full=...` — export download

---

### Data Import (`/data/import`)

**Purpose:** Import tickets from JSON or CSV files into the system with collision handling.

**Features:**
- Step-by-step wizard (4 steps: Upload, Strategy, Preview, Complete).
- **Upload step**: file drag-and-drop or click-to-browse (accepts `.json`, `.csv`). Parses file client-side for preview.
- **Strategy step**: collision strategy selector — Skip (keep existing), Overwrite (replace), Merge (combine fields).
- **Preview step**: table of parsed tickets (ticket_id, title, severity, status, assignee, source_module) with count summary.
- **Complete step**: import result summary — imported, skipped, updated counts, and per-row error list.
- Back/Next navigation between steps; Submit triggers the import POST.
- Status badges on preview rows.

**API Endpoints:**
- `POST /api/import` — import tickets JSON/CSV payload with `{ tickets, strategy }` body

---

### Login (`/login`)

**Purpose:** Authentication entry point — GitHub OAuth sign-in or anonymous mode pass-through.

**Features:**
- Split panel layout on desktop (left: SWE-Squad branding/description; right: sign-in form).
- "Sign in with GitHub" button triggers OAuth flow via `authApi.signIn(next)`.
- When OAuth is disabled (anonymous mode): shows informational message and "Continue to Dashboard" button.
- Redirects already-authenticated users to the `?next=` param destination (default `/`).
- Loading spinner while auth state resolves.

**API Endpoints:**
- `GET /api/auth/oauth-url?next=...` — initiates GitHub OAuth (via `authApi.signIn`)

---

### Landing (`/welcome`)

**Purpose:** Public marketing/welcome page for new users — describes SWE-Squad capabilities and provides onboarding entry points.

**Features:**
- Hero section: tagline, brief description, "Get Started" CTA button.
- Feature cards: Monitor, Investigate, Fix & Ship — each with icon, title, and description.
- "How it works" steps: Connect repos → Configure agents → Watch it work → Ship faster.
- Architecture diagram: shows Monitor → Triage → Investigate → Fix → Verify pipeline.
- Tech stack badges (GitHub, Claude, pgvector, etc.).
- Links to `/onboarding` (Get Started) and `/login` (Sign In).

**API Endpoints:** None (static marketing page).

---

### Onboarding (`/onboarding`)

**Purpose:** First-run setup wizard for new instances — 4-step guided configuration of team ID and repositories.

**Features:**
- Step indicator: 4 steps with active/complete state.
- **Step 1 — Team ID**: text input for unique team identifier with validation.
- **Step 2 — Add Repos**: form to add repositories (name, optional local path, optional branches). Multiple repos supported; at least one required.
- **Step 3 — Review**: displays configured team ID and repo list for confirmation.
- **Step 4 — Complete**: success state after `POST /api/onboarding/complete`, with "Go to Dashboard" button.
- Form validation with inline error messages.
- Back/Next navigation; no skip option.

**API Endpoints:**
- `POST /api/onboarding/complete` — submit team_id and repos to finalize setup

---

### Create (`/create`)

**Purpose:** Transient route that opens the global new-ticket dialog and immediately redirects back.

**Features:**
- Triggers the `newTicket` dialog context event on mount.
- Redirects to the previous page after 100 ms.
- Shows a brief loading spinner while the redirect occurs.
- No permanent UI of its own — the ticket creation experience is the modal dialog.

**API Endpoints:** None directly (dialog handles `POST /api/tickets`).

---

### Not Found (`*`)

**Purpose:** 404 catch-all page for any route that does not match a registered path.

**Features:**
- Error card with `AlertTriangle` icon, "Page not found" heading, and human-readable message.
- Displays the requested path in a monospace code block.
- "Go to Dashboard" button linking to `/`.
- Rendered inside the authenticated Layout (so sidebar remains visible).

**API Endpoints:** None.
