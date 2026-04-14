# SWE-Squad Dashboard Server API Reference

Base URL: `http://<host>:8080` (default port, configurable via `--port`)

All API endpoints return JSON unless otherwise noted. Authentication is required for most endpoints when GitHub OAuth is configured (`GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` env vars). When OAuth is not configured, auth is bypassed and all endpoints are accessible.

**Common error shape:**

```json
{ "error": "Human-readable message", "status": 400 }
```

**Common status codes across all endpoints:**

| Code | Meaning |
|------|---------|
| 200  | Success |
| 201  | Created |
| 400  | Bad request / validation error |
| 401  | Unauthorized (missing or invalid session) |
| 403  | Forbidden (insufficient role) |
| 404  | Resource not found |
| 500  | Internal server error |
| 503  | Dependency unavailable (e.g. UserStore not initialised) |

---

## Table of Contents

1. [Health and Auth](#1-health--auth)
2. [Tickets](#2-tickets)
3. [Projects](#3-projects)
4. [Teams](#4-teams)
5. [Agents and Engines](#5-agents--engines)
6. [Costs and Budget](#6-costs--budget)
7. [Scheduler and Routines](#7-scheduler--routines)
8. [Goals and Approvals](#8-goals--approvals)
9. [Settings and Instance](#9-settings--instance)
10. [Pipeline and Control](#10-pipeline--control)
11. [GitHub Integration](#11-github-integration)
12. [MCP Servers](#12-mcp-servers)
13. [Data Import/Export](#13-data-importexport)
14. [Users and Secrets](#14-users--secrets)
15. [Accounts](#15-accounts)
16. [Governor](#16-governor)
17. [Integrations](#17-integrations)
18. [Suggestions](#18-suggestions)
19. [Real-time Events](#19-real-time-events)

---

## 1. Health and Auth

### `GET /health`

Health check endpoint. No auth required.

**Response:**
```json
{ "status": "ok" }
```

---

### `GET /api/auth/status`

Return authentication state for all providers and the current OAuth session. No auth required.

**Response:**
```json
{
  "providers": [
    {
      "name": "github",
      "is_authenticated": true,
      "is_healthy": true,
      "consecutive_failures": 0,
      "last_error": null
    }
  ],
  "session": {
    "authenticated": true,
    "login": "octocat",
    "name": "The Octocat",
    "orgs": ["my-org"],
    "avatar_url": "https://...",
    "role": "admin"
  },
  "oauth_enabled": true
}
```

---

### `GET /auth/login`

Initiate GitHub OAuth flow. Redirects to GitHub authorization page.

**Query parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `return_to` | string | URL to redirect to after login (optional) |

---

### `GET /auth/callback`

GitHub OAuth callback. Exchanges code for token, creates session, redirects to dashboard.

**Query parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `code` | string | OAuth authorization code from GitHub |
| `state` | string | CSRF state token |

---

### `GET /auth/logout`

Destroy session and redirect to login page.

---

### `GET /api/onboarding/status`

Check whether first-time setup is complete. No auth required.

**Response:**
```json
{
  "completed": true,
  "team_id": "swe-squad-alpha",
  "repos": ["owner/repo"]
}
```

---

### `POST /api/onboarding/complete`

Complete first-time setup. Auth required.

**Request body:** Configuration values for initial setup (team_id, repos, etc.).

**Response:**
```json
{ "ok": true }
```

---

## 2. Tickets

### `GET /api/tickets/<id>`

Return full ticket detail.

**Auth:** Yes

**Response:** Full ticket object with all fields (ticket_id, title, description, status, severity, source_module, investigation_report, metadata, comments, labels, etc.).

**Status codes:** 200, 404

---

### `GET /api/tickets/<id>/activity`

Return activity timeline for a ticket (status changes, comments, assignments).

**Auth:** Yes

**Response:** Array of activity events with timestamps.

---

### `GET /api/tickets/<id>/feed`

Return unified activity feed for a ticket.

**Auth:** Yes

**Response:** Feed entries with comments, status changes, and agent actions.

---

### `POST /api/tickets`

Create a new ticket.

**Auth:** Yes

**Request body:**
```json
{
  "title": "Bug in module X",
  "description": "Detailed description of the issue",
  "severity": "HIGH",
  "source_module": "module_name",
  "project_id": "my-project",
  "labels": ["bug", "frontend"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | Yes | Ticket title |
| `description` | string | Yes | Detailed description |
| `severity` | string | No | `CRITICAL`, `HIGH`, `MEDIUM`, `LOW` |
| `source_module` | string | No | Source module name |
| `project_id` | string | No | Associated project ID |
| `labels` | string[] | No | Labels/tags |

**Response:** Created ticket object (status 201).

---

### `POST /api/tickets/<id>/assign`

Assign a ticket to an agent.

**Auth:** Yes

**Request body:**
```json
{ "assignee": "swe-squad-alpha" }
```

**Status codes:** 200, 400 (missing assignee), 404

---

### `POST /api/tickets/<id>/investigate`

Trigger investigation on a ticket.

**Auth:** Yes

**Request body:** Empty or optional parameters.

**Status codes:** 200, 404

---

### `POST /api/tickets/<id>/develop`

Trigger developer agent on a ticket.

**Auth:** Yes

**Request body:** Empty or optional parameters.

**Status codes:** 200, 404

---

### `POST /api/tickets/<id>/trigger`

Alias for `/api/tickets/<id>/investigate`. Used by legacy UI.

---

### `POST /api/tickets/<id>/comment`

Add a comment to a ticket.

**Auth:** Yes

**Request body:**
```json
{ "comment": "This looks like a race condition." }
```

**Status codes:** 200, 400, 404

---

### `POST /api/tickets/<id>/feed/comment`

Add a comment to the ticket's activity feed.

**Auth:** Yes

**Request body:**
```json
{ "comment": "Investigation update..." }
```

---

### `POST /api/tickets/<id>/label`

Update labels on a ticket.

**Auth:** Yes

**Request body:**
```json
{ "labels": ["bug", "high-priority"] }
```

---

### `PATCH /api/tickets/<id>/status`

Update ticket status.

**Auth:** Yes

**Request body:**
```json
{ "status": "resolved" }
```

---

### `PATCH /api/tickets/<id>/severity`

Update ticket severity.

**Auth:** Yes

**Request body:**
```json
{ "severity": "CRITICAL" }
```

---

### `PATCH /api/tickets/<id>/title`

Update ticket title.

**Auth:** Yes

**Request body:**
```json
{ "title": "New title" }
```

---

### `PATCH /api/tickets/<id>/description`

Update ticket description.

**Auth:** Yes

**Request body:**
```json
{ "description": "Updated description" }
```

---

### `DELETE /api/tickets/<id>/comment/<index>`

Delete a comment by index.

**Auth:** Yes

**Status codes:** 200, 404

---

## 3. Projects

### `GET /api/projects`

List all configured projects.

**Auth:** Yes

**Response:**
```json
[
  {
    "name": "my-project",
    "description": "A sample project",
    "local_path": "/home/user/Projects/my-project",
    "priority": "high",
    "enabled": true,
    "github_repo": "owner/repo"
  }
]
```

---

### `GET /api/projects/<name>`

Get a single project by name.

**Auth:** Yes

**Status codes:** 200, 404

---

### `GET /api/projects/<name>/tickets`

List tickets associated with a project.

**Auth:** Yes

---

### `GET /api/projects/<name>/stats`

Get statistics for a project (ticket counts by status, severity breakdown).

**Auth:** Yes

---

### `GET /api/projects/<name>/secrets`

List secret metadata for a project (names only, never values).

**Auth:** Yes

**Response:**
```json
{ "secrets": ["API_KEY", "DB_PASSWORD"] }
```

---

### `GET /api/projects/<name>/env`

List environment variables for a project (secret values masked).

**Auth:** Yes

**Response:**
```json
{
  "env_vars": [
    { "key": "NODE_ENV", "value": "production", "secret": false },
    { "key": "API_KEY", "value": "********", "secret": true }
  ]
}
```

---

### `POST /api/projects`

Create a new project.

**Auth:** Yes

**Request body:**
```json
{
  "name": "new-project",
  "description": "Project description",
  "local_path": "/path/to/repo",
  "priority": "medium",
  "enabled": true,
  "github_repo": "owner/repo"
}
```

| Field | Type | Required |
|-------|------|----------|
| `name` | string | Yes |
| `description` | string | No |
| `local_path` | string | No |
| `priority` | string | No (default: `medium`) |
| `enabled` | boolean | No (default: `true`) |
| `github_repo` | string | No |

**Status codes:** 201, 400

---

### `POST /api/projects/<name>/secrets`

Store an encrypted secret for a project.

**Auth:** Yes

**Request body:**
```json
{
  "name": "API_KEY",
  "value": "secret_value",
  "ttl_minutes": 60
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Secret key name |
| `value` | string | Yes | Secret value (stored encrypted) |
| `ttl_minutes` | integer | No | Auto-expire after this many minutes |

**Status codes:** 201, 400, 503

---

### `POST /api/projects/<name>/env`

Set or update an environment variable for a project.

**Auth:** Yes

**Request body:**
```json
{
  "key": "NODE_ENV",
  "value": "production",
  "secret": false
}
```

**Status codes:** 201, 400

---

### `PATCH /api/projects/<name>/name`

Rename a project.

**Auth:** Yes

**Request body:** `{ "name": "new-name" }`

---

### `PATCH /api/projects/<name>/description`

Update project description.

**Auth:** Yes

**Request body:** `{ "description": "Updated description" }`

---

### `PATCH /api/projects/<name>/priority`

Update project priority.

**Auth:** Yes

**Request body:** `{ "priority": "high" }`

---

### `PATCH /api/projects/<name>/enabled`

Enable or disable a project.

**Auth:** Yes

**Request body:** `{ "enabled": false }`

---

### `PATCH /api/projects/<name>/local_path`

Update project local path.

**Auth:** Yes

**Request body:** `{ "local_path": "/new/path" }`

---

### `PATCH /api/projects/<name>/github_repo`

Update project GitHub repository.

**Auth:** Yes

**Request body:** `{ "github_repo": "owner/repo" }`

---

### `DELETE /api/projects/<name>`

Delete a project.

**Auth:** Yes

**Status codes:** 200, 404

---

### `DELETE /api/projects/<name>/secrets/<secret_name>`

Delete a project secret.

**Auth:** Yes

**Status codes:** 200, 404

---

### `DELETE /api/projects/<name>/env/<key>`

Delete a project environment variable.

**Auth:** Yes

**Status codes:** 200, 404

---

## 4. Teams

### `GET /api/teams`

List all configured teams with live status, burn rate, and VM status.

**Auth:** Yes

**Response:**
```json
{
  "alpha": {
    "github_account": "your-bot-alpha",
    "vm_address": "192.0.2.1",
    "live_status": {
      "total_tickets": 12,
      "active_tickets": 3,
      "active_ticket_ids": ["abc123"],
      "active_sessions": 2,
      "open_count": 5,
      "in_progress_count": 3,
      "closed_count": 4
    },
    "burn_rate": {
      "daily_spent_usd": 0.45,
      "hourly_rate_usd": 0.03,
      "budget_percent": 12.5
    },
    "vm_status": { "state": "running" }
  }
}
```

---

### `GET /api/teams/<name>/health`

VM health check for a specific team.

**Auth:** Yes

**Response:** Health check results including connectivity and system metrics.

---

### `POST /api/teams`

Create a new team.

**Auth:** Yes

**Request body:**
```json
{
  "name": "delta",
  "github_account": "swe-squad-delta",
  "vm_address": "100.1.2.3",
  "tier": "Standard",
  "engine": "claude",
  "concurrency": 2,
  "budget": 10.0,
  "role": "developer",
  "specializations": ["frontend"]
}
```

---

### `POST /api/teams/<name>/start`

Start a team's VM/agent process.

**Auth:** Yes

---

### `POST /api/teams/<name>/stop`

Stop a team's VM/agent process.

**Auth:** Yes

---

### `POST /api/teams/<name>/restart`

Restart a team's VM/agent process.

**Auth:** Yes

---

### `PATCH /api/teams/<name>/name`

Rename a team. **Auth:** Yes. Body: `{ "name": "new-name" }`

### `PATCH /api/teams/<name>/vm_address`

Update VM address. **Auth:** Yes. Body: `{ "vm_address": "100.1.2.3" }`

### `PATCH /api/teams/<name>/github_account`

Update GitHub account. **Auth:** Yes. Body: `{ "github_account": "new-account" }`

### `PATCH /api/teams/<name>/tier`

Update tier. **Auth:** Yes. Body: `{ "tier": "Senior" }`

### `PATCH /api/teams/<name>/concurrency`

Update concurrency limit. **Auth:** Yes. Body: `{ "concurrency": 3 }`

### `PATCH /api/teams/<name>/budget`

Update daily budget. **Auth:** Yes. Body: `{ "budget": 15.0 }`

### `PATCH /api/teams/<name>/role`

Update team role. **Auth:** Yes. Body: `{ "role": "developer" }`

### `PATCH /api/teams/<name>/engine`

Update coding engine. **Auth:** Yes. Body: `{ "engine": "claudez" }`

### `PATCH /api/teams/<name>/specializations`

Update team specializations. **Auth:** Yes. Body: `{ "specializations": ["frontend", "backend"] }`

### `DELETE /api/teams/<name>`

Delete a team. **Auth:** Yes.

---

## 5. Agents and Engines

### `GET /api/agents`

List all configured agents.

**Auth:** Yes

**Response:** Array of agent objects with name, role, engine, model tier, enabled status, etc.

---

### `GET /api/agents/<name>`

Get a single agent by name.

**Auth:** Yes

---

### `GET /api/agents/<name>/runs`

Get recent run history for an agent.

**Auth:** Yes

**Query parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `limit` | integer | Max runs to return |

---

### `GET /api/agents/<name>/stats`

Get performance statistics for an agent (success rate, avg duration, etc.).

**Auth:** Yes

---

### `GET /api/agents/<name>/keys`

Get API key metadata for an agent.

**Auth:** Yes

---

### `GET /api/agents/models`

List available models across all configured engines.

**Auth:** Yes

---

### `POST /api/agents`

Create a new agent.

**Auth:** Yes

**Request body:**
```json
{
  "name": "investigator-2",
  "role": "investigator",
  "engine": "claude",
  "model_tier": "T2",
  "max_tasks": 3,
  "enabled": true,
  "description": "Secondary investigator agent"
}
```

---

### `POST /api/agents/<name>/environment-test`

Test an agent's environment (verify CLI, keys, connectivity).

**Auth:** Yes

---

### `PUT /api/agents/<name>`

Full update of an agent configuration.

**Auth:** Yes

**Request body:** Complete agent object.

---

### `PATCH /api/agents/<name>/role`

Update agent role. **Auth:** Yes. Body: `{ "role": "developer" }`

### `PATCH /api/agents/<name>/engine`

Update agent engine. **Auth:** Yes. Body: `{ "engine": "claudez" }`

### `PATCH /api/agents/<name>/model_tier`

Update agent model tier. **Auth:** Yes. Body: `{ "model_tier": "T3" }`

### `PATCH /api/agents/<name>/max_tasks`

Update agent max concurrent tasks. **Auth:** Yes. Body: `{ "max_tasks": 5 }`

### `PATCH /api/agents/<name>/tools`

Update agent tool list. **Auth:** Yes. Body: `{ "tools": ["bash", "read", "write"] }`

### `PATCH /api/agents/<name>/enabled`

Enable or disable an agent. **Auth:** Yes. Body: `{ "enabled": false }`

### `PATCH /api/agents/<name>/description`

Update agent description. **Auth:** Yes. Body: `{ "description": "..." }`

### `DELETE /api/agents/<name>`

Delete an agent. **Auth:** Yes.

---

### `GET /api/engines`

List all coding engines, their health status, and fallback agent configuration.

**Auth:** Yes

**Response:**
```json
{
  "engine_routing": { "default": "claude" },
  "fallback_agents": [
    {
      "name": "gemini",
      "command": "/usr/bin/gemini",
      "api_key_masked": "sk-...***abc"
    }
  ],
  "models": {},
  "registry": [
    { "name": "claude", "available": true, "model": "sonnet" }
  ],
  "registry_count": 3
}
```

---

### `POST /api/engines/install`

Install a new coding engine.

**Auth:** Yes

**Request body:**
```json
{ "engine": "gemini-cli", "version": "latest" }
```

---

### `POST /api/engines/health-check`

Run a health check on a specific engine.

**Auth:** Yes

**Request body:**
```json
{ "engine": "claude" }
```

---

### `POST /api/models/probe`

Probe a model endpoint for availability. Lists models and optionally tests a completion.

**Auth:** Yes

**Request body:**
```json
{
  "url": "http://proxy.example.com/v1",
  "api_key": "sk-...",
  "model": "gpt-4",
  "timeout": 10
}
```

---

### `PATCH /api/engines/routing`

Update engine routing configuration.

**Auth:** Yes

**Request body:**
```json
{ "default": "claude", "fallback": ["gemini", "opencode"] }
```

---

### `PATCH /api/engines/<name>/model`

Update default model for an engine. **Auth:** Yes. Body: `{ "model": "sonnet-4" }`

### `PATCH /api/engines/<name>/timeout`

Update timeout for an engine. **Auth:** Yes. Body: `{ "timeout": 120 }`

### `PATCH /api/engines/<name>/binary`

Update command/binary path for an engine. **Auth:** Yes. Body: `{ "binary": "/usr/bin/claude" }`

### `PATCH /api/engines/<name>/enabled`

Enable or disable an engine. **Auth:** Yes. Body: `{ "enabled": false }`

### `PATCH /api/engines/<name>/team`

Assign an engine to a team. **Auth:** Yes. Body: `{ "team": "alpha" }`

### `PATCH /api/engines/<name>/api_key`

Set BYOK API key for an engine. **Auth:** Yes. Body: `{ "api_key": "sk-..." }`

### `DELETE /api/engines/<name>`

Remove a fallback agent / engine. **Auth:** Yes.

---

## 6. Costs and Budget

### `GET /api/cost`

Get budget status overview.

**Auth:** Yes

**Query parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `team_id` | string | Filter by team ID |

---

### `GET /api/costs/by_hour`

Token usage aggregated by hour (last 48 hours).

**Auth:** Yes

**Response:** Array of hourly cost objects.

---

### `GET /api/costs/by_day`

Token usage aggregated by day (last 30 days).

**Auth:** Yes

---

### `GET /api/costs/by_week`

Token usage aggregated by week (last 12 weeks).

**Auth:** Yes

---

### `GET /api/costs/by_month`

Token usage aggregated by month (last 6 months).

**Auth:** Yes

---

### `GET /api/costs/by_agent`

Token usage broken down by agent (last 7 days).

**Auth:** Yes

---

### `GET /api/costs/by_ticket`

Token usage broken down by ticket (last 7 days).

**Auth:** Yes

---

### `GET /api/costs/by_model`

Token usage broken down by model (last 7 days).

**Auth:** Yes

---

### `GET /api/costs/by_range`

Token usage for a custom date range.

**Auth:** Yes

**Query parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `start` | ISO 8601 string | Yes | Range start (e.g. `2026-04-01T00:00:00`) |
| `end` | ISO 8601 string | Yes | Range end |

**Status codes:** 200, 400 (missing or invalid dates)

---

### `GET /api/costs/roi`

Return on investment metrics.

**Auth:** Yes

---

### `GET /api/costs/cache_efficiency`

Cache hit/miss efficiency metrics.

**Auth:** Yes

---

### `GET /api/pricing`

Get current model pricing configuration.

**Auth:** Yes

---

### `POST /api/pricing`

Save updated pricing configuration.

**Auth:** Yes

**Request body:** Pricing config object (model name to input/output cost per token).

---

### `POST /api/pricing/reset`

Reset pricing to built-in defaults.

**Auth:** Yes

---

### `GET /api/budget/policies`

List budget policies.

**Auth:** Yes

---

### `POST /api/budget/policies`

Create or update a budget policy.

**Auth:** Yes

---

### `GET /api/budget/incidents`

List budget incidents (threshold breaches).

**Auth:** Yes

---

### `POST /api/budget/incidents`

Create a budget incident.

**Auth:** Yes

---

### `GET /api/budget/incidents/<id>/resolve`

Resolve a budget incident.

**Auth:** Yes

---

### `GET /api/budget/provider-quotas`

Get provider-level quota information.

**Auth:** Yes

---

### `GET /api/budget/spend-window`

Get spend data for a time window.

**Auth:** Yes

---

### `GET /api/budget/subscriptions`

List provider subscription details.

**Auth:** Yes

---

### `GET /api/budget/accounting-models`

List available accounting models (token-based, session-based, etc.).

**Auth:** Yes

---

## 7. Scheduler and Routines

### `GET /api/scheduler`

List all scheduled jobs (raw jobs.json data).

**Auth:** Yes

---

### `GET /api/scheduler/history`

Get scheduler run history.

**Auth:** Yes

---

### `GET /api/scheduler/templates`

List available job templates.

**Auth:** Yes

**Response:**
```json
{ "templates": [...] }
```

---

### `GET /api/jobs`

List all jobs with enriched metadata.

**Auth:** Yes

---

### `GET /api/jobs/<id>/history`

Get run history for a specific job.

**Auth:** Yes

---

### `POST /api/jobs`

Create a new scheduled job.

**Auth:** Yes

---

### `POST /api/jobs/<id>/pause`

Pause a running job. **Auth:** Yes.

### `POST /api/jobs/<id>/resume`

Resume a paused job. **Auth:** Yes.

### `POST /api/jobs/<id>/cancel`

Cancel a job. **Auth:** Yes.

### `POST /api/jobs/<id>/trigger`

Manually trigger a job run. **Auth:** Yes.

### `POST /api/jobs/<id>/delete`

Delete a job. **Auth:** Yes.

---

### `POST /api/scheduler/templates/<id>/apply`

Create a new job from a template.

**Auth:** Yes

---

### `GET /api/routines`

List all routines (recurring automation tasks).

**Auth:** Yes

---

### `GET /api/routines/<id>`

Get a single routine by ID.

**Auth:** Yes

---

### `GET /api/routines/<id>/runs`

Get run history for a routine.

**Auth:** Yes

---

### `GET /api/routines/<id>/activity`

Get activity log for a routine.

**Auth:** Yes

---

### `POST /api/routines`

Create a new routine.

**Auth:** Yes

---

### `POST /api/routines/<id>/run`

Manually trigger a routine run. **Auth:** Yes.

### `POST /api/routines/<id>/pause`

Pause a routine. **Auth:** Yes.

### `POST /api/routines/<id>/resume`

Resume a paused routine. **Auth:** Yes.

### `POST /api/routines/<id>/archive`

Archive a routine. **Auth:** Yes.

### `PATCH /api/routines/<id>`

Update routine configuration. **Auth:** Yes.

---

## 8. Goals and Approvals

### `GET /api/goals`

List all goals (hierarchical project objectives).

**Auth:** Yes

---

### `GET /api/goals/<id>`

Get a single goal by ID.

**Auth:** Yes

---

### `GET /api/goals/<id>/stats`

Get statistics for a goal (child ticket counts, completion percentage).

**Auth:** Yes

---

### `POST /api/goals`

Create a new goal.

**Auth:** Yes

**Request body:**
```json
{
  "title": "Launch mobile app",
  "description": "Ship iOS and Android apps",
  "project_id": "mobile-launch",
  "parent_goal_id": null
}
```

---

### `GET /api/approvals`

List pending approvals.

**Auth:** Yes

---

### `GET /api/approvals/<id>`

Get a single approval by ticket ID.

**Auth:** Yes

---

### `GET /api/approvals/<id>/comments`

List comments on an approval.

**Auth:** Yes

---

### `POST /api/approvals/<id>/approve`

Approve a pending ticket.

**Auth:** Yes

**Request body:**
```json
{ "comment": "Looks good, approved." }
```

---

### `POST /api/approvals/<id>/reject`

Reject a pending ticket.

**Auth:** Yes

**Request body:**
```json
{ "comment": "Needs rework." }
```

---

### `POST /api/approvals/<id>/request-revision`

Request revision on a pending ticket.

**Auth:** Yes

**Request body:**
```json
{ "comment": "Please address the edge case." }
```

---

### `POST /api/approvals/<id>/comments`

Add a comment to an approval thread.

**Auth:** Yes

**Request body:**
```json
{ "comment": "Additional context..." }
```

---

## 9. Settings and Instance

### `GET /api/settings`

Get dashboard display settings.

**Auth:** Yes

**Response:**
```json
{
  "theme": "dark",
  "refresh_interval": 30,
  "tickets_per_page": 25,
  "default_tab": "overview",
  "notifications_enabled": true,
  "notification_level": "errors"
}
```

---

### `GET /api/settings/full`

Get full settings including governance, cycle, memory, and monitor configuration.

**Auth:** Yes

---

### `POST /api/settings`

Save dashboard display settings.

**Auth:** Yes

**Request body:** Settings object (same shape as GET response).

---

### `PATCH /api/settings/governance`

Update governance settings section. **Auth:** Yes.

### `PATCH /api/settings/cycle`

Update cycle settings section. **Auth:** Yes.

### `PATCH /api/settings/memory`

Update memory settings section. **Auth:** Yes.

### `PATCH /api/settings/monitor`

Update monitor settings section. **Auth:** Yes.

### `PATCH /api/settings/display`

Update individual dashboard display settings. **Auth:** Yes.

---

### `GET /api/instance/settings`

Get instance-level settings (connection methods, deployment config).

**Auth:** Yes

---

### `GET /api/instance/connections`

Get configured connection methods (SSH endpoints).

**Auth:** Yes

**Response:**
```json
{ "connection_methods": [...] }
```

---

### `GET /api/instance/heartbeat`

Get instance heartbeat data (uptime, version, last activity).

**Auth:** Yes

---

### `POST /api/instance/settings`

Save instance-level settings.

**Auth:** Yes

---

### `POST /api/instance/connections`

Save connection methods.

**Auth:** Yes

**Request body:**
```json
{ "connection_methods": [...] }
```

---

### `POST /api/instance/connections/ssh/generate`

Generate an ed25519 SSH keypair and store the private key as an encrypted secret.

**Auth:** Yes

**Request body:**
```json
{
  "secret_name": "my-ssh-key",
  "comment": "user@swe-squad"
}
```

**Response (201):**
```json
{
  "ok": true,
  "secret_name": "my-ssh-key",
  "public_key": "ssh-ed25519 AAAA...",
  "fingerprint": "SHA256:..."
}
```

---

### `POST /api/instance/connections/ssh/import`

Import an existing SSH private key into the encrypted secret store.

**Auth:** Yes

**Request body:**
```json
{
  "secret_name": "imported-key",
  "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n..."
}
```

**Response (201):** Same shape as `/ssh/generate`.

---

### `POST /api/instance/connections/test`

Test SSH connectivity using a stored key secret.

**Auth:** Yes

**Request body:**
```json
{
  "host": "worker-1",
  "username": "deploy",
  "secret_name": "my-ssh-key",
  "port": 22
}
```

**Response:**
```json
{
  "ok": true,
  "exit_code": 0,
  "stdout": "swe-ssh-ok",
  "stderr": ""
}
```

---

### `GET /api/rbac`

Get RBAC roles configuration.

**Auth:** Yes

**Response:**
```json
{
  "roles": [
    {
      "role": "admin",
      "permissions": ["*"],
      "deny": [],
      "description": "Full access",
      "enabled": true,
      "models": []
    }
  ],
  "overrides": [],
  "bypass_mode": true
}
```

---

### `GET /api/roles`

Get RBAC roles matrix (enriched view).

**Auth:** Yes

---

### `GET /api/providers/schemas`

Get parameter schemas for all provider types.

**Auth:** Yes

---

## 10. Pipeline and Control

### `GET /api/pipeline/config`

Get pipeline stage configuration.

**Auth:** Yes

**Response:**
```json
{
  "stages": {
    "monitor": { "enabled": true, "timeout_minutes": 5, "max_retries": 1, "model_tier": "T1" },
    "triage": { "enabled": true, "timeout_minutes": 5, "max_retries": 1, "model_tier": "T1" },
    "investigate": { "enabled": true, "timeout_minutes": 30, "max_retries": 3, "model_tier": "T2" },
    "develop": { "enabled": true, "timeout_minutes": 60, "max_retries": 3, "model_tier": "T2" },
    "review": { "enabled": true, "timeout_minutes": 15, "max_retries": 1, "model_tier": "T2" },
    "verify": { "enabled": true, "timeout_minutes": 10, "max_retries": 2, "model_tier": "T1" }
  },
  "execution_profile": "base"
}
```

---

### `POST /api/pipeline/trigger`

Trigger a full pipeline cycle.

**Auth:** Yes

---

### `PATCH /api/pipeline/stages/<stage_name>`

Update a single pipeline stage configuration.

**Auth:** Yes

**URL parameter:** `stage_name` must be one of: `monitor`, `triage`, `investigate`, `develop`, `review`, `verify`.

**Request body:**
```json
{
  "enabled": true,
  "timeout_minutes": 45,
  "max_retries": 2,
  "model_tier": "T2"
}
```

---

### `PATCH /api/pipeline/profile`

Switch execution profile.

**Auth:** Yes

**Request body:**
```json
{ "profile": "aggressive" }
```

---

### `GET /api/execution/mode`

Get current execution mode.

**Auth:** Yes

**Response:**
```json
{
  "mode": "start",
  "available_modes": ["plan", "review", "start"],
  "description": "Fully autonomous -- execute without pauses"
}
```

---

### `GET /api/execution/checkpoints`

List pending review checkpoints (used in `review` execution mode).

**Auth:** Yes

---

### `PATCH /api/execution/mode`

Change execution mode.

**Auth:** Yes

**Request body:**
```json
{ "mode": "review" }
```

Valid modes: `plan`, `review`, `start`.

---

### `POST /api/execution/checkpoints/<id>/approve`

Approve a review checkpoint.

**Auth:** Yes

---

### `POST /api/execution/checkpoints/<id>/reject`

Reject a review checkpoint.

**Auth:** Yes

---

### `GET /api/workflows`

Return the active workflow pipeline definition.

**Auth:** Yes

---

### `GET /api/status`

Get system status (from `status.json`).

**Auth:** Yes

---

### `GET /api/rate-limits`

Get current rate limit lifecycle state per provider.

**Auth:** Yes

---

### `GET /api/heartbeats`

Get agent heartbeat data.

**Auth:** Yes

**Query parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `team_id` | string | Filter by team ID |

---

## 11. GitHub Integration

### `GET /api/github/repos`

List connected GitHub repositories.

**Auth:** Yes

**Query parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `org` | string | Filter by organization |

---

### `POST /api/github/repos/connect`

Connect a GitHub repository to the system.

**Auth:** Yes

**Request body:**
```json
{ "repo": "owner/repo-name" }
```

---

### `GET /api/github/label-triggers`

List configured label triggers (automation rules based on GitHub labels).

**Auth:** Yes

---

### `POST /api/github/label-triggers`

Create or update a label trigger.

**Auth:** Yes

**Request body:**
```json
{
  "label": "swe-squad",
  "action": "investigate",
  "priority": "HIGH",
  "auto_assign": true
}
```

---

### `POST /api/github/label-triggers/test`

Test a label trigger against live GitHub issues.

**Auth:** Yes

---

### `DELETE /api/github/label-triggers/<label>`

Remove a label trigger.

**Auth:** Yes

---

## 12. MCP Servers

### `GET /api/mcp/servers`

List configured MCP (Model Context Protocol) servers.

**Auth:** Yes

---

### `POST /api/mcp/servers`

Add an MCP server configuration.

**Auth:** Yes

**Request body:**
```json
{
  "name": "my-mcp-server",
  "url": "http://localhost:3000",
  "enabled": true
}
```

---

### `PATCH /api/mcp/servers/<name>`

Toggle enable/disable for an MCP server.

**Auth:** Yes

**Request body:**
```json
{ "enabled": false }
```

---

### `DELETE /api/mcp/servers/<name>`

Remove an MCP server configuration.

**Auth:** Yes

---

## 13. Data Import/Export

### `GET /api/tickets/export`

Export tickets as CSV, JSON, or ZIP.

**Auth:** Yes

**Query parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `format` | string | `csv` | Output format: `csv`, `json`, or `zip` |
| `ticket_ids` | string | (all) | Comma-separated ticket IDs to export |
| `status` | string | (all) | Filter by status |
| `severity` | string | (all) | Filter by severity |
| `source_module` | string | (all) | Filter by source module |
| `include_full` | string | `false` | Include full ticket data (description, investigation report) |

**Response:** File download with appropriate Content-Type header.

---

### `POST /api/tickets/import`

Import tickets from an uploaded file (CSV, JSON, or ZIP).

**Auth:** Yes

**Request body:** Multipart form data or JSON array of ticket objects.

---

### `GET /data`

Raw dashboard data as JSON (legacy endpoint).

**Auth:** Yes

---

## 14. Users and Secrets

### `GET /api/users/me`

Get the current user's profile.

**Auth:** Yes

**Response:**
```json
{
  "github_login": "octocat",
  "name": "The Octocat",
  "orgs": ["my-org"],
  "role": "admin",
  "avatar_url": "https://...",
  "settings": {}
}
```

---

### `PATCH /api/users/me/settings`

Update the current user's settings.

**Auth:** Yes

**Request body:** Settings key-value pairs.

---

### `GET /api/users`

List all users. Admin only.

**Auth:** Yes (admin role required)

**Status codes:** 200, 403

---

### `GET /api/secrets`

List secret names for the current user (never values).

**Auth:** Yes

**Response:**
```json
{ "secrets": ["GH_TOKEN", "API_KEY"] }
```

---

### `POST /api/secrets`

Store an encrypted secret for the current user.

**Auth:** Yes

**Request body:**
```json
{ "name": "GH_TOKEN", "value": "ghp_..." }
```

**Status codes:** 201, 400

---

### `POST /api/secrets/purge`

Delete all expired secrets across all users.

**Auth:** Yes

**Response:**
```json
{ "ok": true, "deleted": 5 }
```

---

### `DELETE /api/secrets/<name>`

Delete a user secret.

**Auth:** Yes

**Status codes:** 200, 404

---

## 15. Accounts

### `GET /api/accounts`

List accounts the current user belongs to.

**Auth:** Yes

---

### `GET /api/accounts/<id>`

Get account details.

**Auth:** Yes

---

### `GET /api/accounts/<id>/members`

List members of an account.

**Auth:** Yes

---

### `GET /api/accounts/<id>/secrets`

List secret metadata for an account.

**Auth:** Yes

---

### `POST /api/accounts`

Create a new account (multi-tenant organization).

**Auth:** Yes

**Request body:**
```json
{
  "name": "My Team Account",
  "slug": "my-team",
  "description": "Shared account for the team"
}
```

---

### `POST /api/accounts/<id>/secrets`

Store an encrypted secret for an account.

**Auth:** Yes

**Request body:**
```json
{
  "name": "DEPLOY_KEY",
  "value": "secret_value",
  "ttl_minutes": 1440
}
```

**Status codes:** 201, 400, 503

---

### `POST /api/accounts/<id>/members`

Invite a member to an account.

**Auth:** Yes

**Request body:**
```json
{
  "github_login": "octocat",
  "role": "member"
}
```

---

### `PATCH /api/accounts/<id>/members/<login>`

Update a member's role in an account.

**Auth:** Yes

**Request body:**
```json
{ "role": "admin" }
```

---

### `DELETE /api/accounts/<id>/secrets/<name>`

Delete an account secret.

**Auth:** Yes

---

### `DELETE /api/accounts/<id>/members/<login>`

Remove a member from an account.

**Auth:** Yes

---

## 16. Governor

### `GET /api/governor/status`

Get full governor status (quota, concurrency decision, schedule, bonus info, alerts).

**Auth:** Yes

**Response:**
```json
{
  "quota": { "daily_limit": 1000, "used": 250, "remaining": 750, "percent": 25.0 },
  "decision": { "allowed": true, "max_concurrent": 3 },
  "schedule": { "current_window": "default", "concurrency_multiplier": 1.0, "is_peak": false, "is_weekend": false },
  "bonus": { "active": false, "multiplier": 1.0 },
  "alerts": []
}
```

Returns `{"error": "Governor not configured", "configured": false}` when the governor is not set up.

---

### `GET /api/governor/quota`

Get quota status only.

**Auth:** Yes

---

### `GET /api/governor/decision`

Get current concurrency decision.

**Auth:** Yes

---

### `GET /api/governor/alerts`

Get active governor alerts.

**Auth:** Yes

---

### `GET /api/governor/summary`

Get a summary of governor state.

**Auth:** Yes

---

## 17. Integrations

### `GET /api/integrations`

List available integration connectors and their manifests.

**Auth:** Yes

**Response:**
```json
{
  "connectors": [
    {
      "connector_type": "github",
      "name": "GitHub",
      "category": "code",
      "description": "GitHub issue tracker integration",
      "icon": "github",
      "auth_type": "oauth",
      "actions": ["create_issue", "list_issues"],
      "triggers": ["on_push", "on_issue"],
      "config_schema": {},
      "credential_schema": [
        { "key": "token", "label": "Token", "field_type": "password", "required": true, "secret": true, "description": "GitHub PAT" }
      ]
    }
  ],
  "categories": ["code", "notification"]
}
```

---

### `POST /api/integrations/configure`

Save connector credentials.

**Auth:** Yes

---

### `POST /api/integrations/test`

Test connector credentials.

**Auth:** Yes

---

## 18. Suggestions

### `GET /api/suggestions`

List creative agent suggestions (proactive improvement proposals).

**Auth:** Yes

---

### `POST /api/suggestions/<id>/accept`

Accept a suggestion (creates a ticket from it).

**Auth:** Yes

---

### `POST /api/suggestions/<id>/dismiss`

Dismiss a suggestion.

**Auth:** Yes

---

## 19. Real-time Events

### `GET /api/stream`

Server-Sent Events (SSE) endpoint for real-time dashboard updates.

**Auth:** Yes

**Response:** SSE stream with `data:` frames containing JSON objects. Events are emitted on ticket changes, pipeline progress, and system status updates.

**Example event:**
```
data: {"type": "status_update", "data": {...}}
```

---

### `GET /api/activity`

Get recent system activity log.

**Auth:** Yes

**Query parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `limit` | integer | Max entries to return |

---

### `GET /api/graph`

Get ticket similarity graph data (nodes and edges for visualization).

**Auth:** Yes

---

## Inbox

### `POST /api/inbox/alerts/archive`

Archive a governor alert.

**Auth:** Yes

---

### `POST /api/inbox/failed-runs/archive`

Archive a failed routine run notification.

**Auth:** Yes

---

## Endpoint Count

**Total API endpoints documented: 156**

| Category | Count |
|----------|-------|
| Health and Auth | 7 |
| Tickets | 15 |
| Projects | 16 |
| Teams | 15 |
| Agents and Engines | 24 |
| Costs and Budget | 18 |
| Scheduler and Routines | 16 |
| Goals and Approvals | 10 |
| Settings and Instance | 14 |
| Pipeline and Control | 11 |
| GitHub Integration | 5 |
| MCP Servers | 4 |
| Data Import/Export | 3 |
| Users and Secrets | 7 |
| Accounts | 7 |
| Governor | 5 |
| Integrations | 3 |
| Suggestions | 3 |
| Real-time / Activity / Inbox | 5 |
