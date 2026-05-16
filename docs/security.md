# Security

## Model boundary enforcement

SWE-Squad enforces strict boundaries on which models may perform code generation tasks.

**SEC-68 pattern**: The `model_boundary.py` module prevents unauthorized model substitution. Only approved Claude models are permitted for code generation. Any attempt to use a non-Claude model for code generation is blocked at the boundary check.

This protects against scenarios where a connected agent (via A2A) could route code generation tasks to an unapproved model without operator awareness.

The boundary is enforced in `developer.py` and `investigator.py` before every Claude CLI call.

## Role-Based Access Control (RBAC)

SWE-Squad uses a deny-by-default RBAC system defined in `config/swe_team/roles.yaml`.

- **Deny-by-default**: any permission not explicitly granted is denied.
- **Role definitions**: each agent role lists the operations it is allowed to perform (e.g., `create_branch`, `push_commit`, `create_github_issue`).
- **Enforcement point**: `agent_rbac.py` is called at every pipeline stage transition.

The RBAC system prevents privilege escalation — a triage agent cannot perform developer actions, and a developer agent cannot modify governance thresholds.

## SSH security model

Remote worker access uses a scoped SSH configuration:

- `config/ssh_workers.conf` — `IdentitiesOnly yes`, Host entries for each worker.
- `~/.ssh/swe_workers_linkedai_key` — Dedicated ed25519 key for workers only.

Only workers listed in `ssh_workers.conf` are reachable via the dedicated key. The primary orchestrator node is explicitly excluded from `authorized_keys` on workers — agents cannot reach "up" the hierarchy.

## Secrets management

- All secrets are provided via `.env` or environment variables — never hardcoded.
- The files `.env`, `*.key`, `*.pem`, and any credentials files are `.gitignore`d.
- Any key that appears in a diff or log must be rotated immediately.
- `GH_TOKEN` is stripped from subprocess environments before passing to worker processes.

## GitHub webhook validation

The webhook listener (`scripts/ops/webhook_listener.py`) validates all incoming GitHub webhooks using HMAC-SHA256 signature verification against `WEBHOOK_SECRET`. Requests with invalid or missing signatures are rejected with HTTP 403.

## Preflight checks

Before any investigator or developer action, `preflight.py` validates:

- **Git identity**: the configured committer name and email match expected values.
- **Clean working tree**: no uncommitted changes that could corrupt the patch/discard loop.
- **Required env vars**: all necessary variables are present before the agent proceeds.

Failing preflight causes the ticket to be held rather than proceeding with a potentially unsafe action.

## Audit trail

- Every cycle writes `data/swe_team/status.json` for observability.
- Every A2A event is logged to the hub and local fallback.
- Every investigation and fix attempt is attached to the ticket record in Supabase.
- Session tags (from `session.py`) provide end-to-end tracing across logs, GitHub comments, and Supabase records.
