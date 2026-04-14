# Memory Service — Agent Instructions

You are working on the SWE-Squad Memory Service in this `memory/` directory.
This integrates claude-mem (a persistent memory system for AI assistants)
into SWE-Squad's multi-agent, multi-tenant architecture.

## Your Mission

Adapt claude-mem's storage layer from SQLite to Supabase PostgreSQL, add
authentication and team_id scoping, and wire the memory service into
SWE-Squad's agent fleet (investigator, developer, orchestrator).

## Read These First

1. `memory/IMPLEMENTATION_PLAN.md` — exhaustive phase-by-phase plan with file paths
2. `memory/ARCHITECTURE.md` — system architecture and data flow
3. `memory/sql/001_memory_tables.sql` — the Supabase schema (already written)
4. `memory/src/client.py` — the Python client for SWE agents (already written)

## Key Context

### What claude-mem is
- TypeScript/Node.js project providing persistent memory for AI coding assistants
- Express HTTP API on port 37777
- Captures tool use observations, compresses them, stores them, injects relevant
  context into future sessions
- Source available at `memory/node_modules/claude-mem/` after `npm install`

### What SWE-Squad is
- Python-based autonomous software engineering agent fleet
- Uses Supabase PostgreSQL with pgvector for data + semantic search
- Multi-tenant via `team_id` on all tables
- Agents: investigator (Claude Code CLI), developer (Claude Code CLI),
  orchestrator (Opus), creative, monitor, triage
- Config at `config/swe_team.yaml`, models at `src/swe_team/models.py`
- Existing Supabase integration at `src/swe_team/supabase_store.py`
- Embedding pipeline: bge-m3 (1024-dim) via BASE_LLM proxy at :8082

### The adaptation needed
- **Storage**: SQLite → Supabase PostgreSQL (same Supabase project as SWE-Squad)
- **Search**: FTS5 → tsvector; Chroma → pgvector
- **Auth**: None → API key middleware
- **Scoping**: `project` only → `team_id` + `project`
- **Embeddings**: Chroma MCP → bge-m3 via BASE_LLM proxy

## Implementation Order

Follow the phases in `IMPLEMENTATION_PLAN.md`:
1. Storage adapter (TypeScript interface + Supabase implementation)
2. Auth middleware + team_id injection
3. SWE agent integration (modify investigator.py, developer.py, orchestrator.py)
4. Embedding pipeline (bge-m3 via BASE_LLM)
5. Worker service entry point
6. Docker deployment
7. Testing

## File Locations

### This directory (`memory/`)
- `src/` — TypeScript source for the adapted worker service
- `src/client.py` — Python client for SWE agents (already done)
- `sql/` — Supabase migration scripts (already done)
- `docker/` — Container definitions (already done)
- `config/` — Environment templates (already done)
- `tests/` — Test files

### SWE-Squad (`../src/swe_team/`)
- `investigator.py` — Primary investigation agent (modify for memory context)
- `developer.py` — Code generation agent (modify to record observations)
- `orchestrator.py` — Planning agent (modify for session lifecycle)
- `supabase_store.py` — Reference for PostgREST API patterns
- `embeddings.py` — Reference for bge-m3 embedding patterns
- `agent_rbac.py` — Reference for RBAC patterns
- `models.py` — Data models (SWETicket, TicketStatus, etc.)

### Claude-mem source (`node_modules/claude-mem/` after npm install)
- `src/services/worker-service.ts` — Main orchestrator (1,253 lines)
- `src/services/sqlite/SessionStore.ts` — CRUD operations (2,674 lines) — THE KEY FILE
- `src/services/sqlite/types.ts` — TypeScript interfaces (287 lines) — REUSE THESE
- `src/services/worker/http/routes/SessionRoutes.ts` — Session API (885 lines)
- `src/services/worker/http/routes/SearchRoutes.ts` — Search API (426 lines)
- `src/services/sync/ChromaSync.ts` — Vector sync (843 lines) — REPLACE WITH PGVECTOR

## Critical Rules

1. **Every database query MUST include `team_id`**. No exceptions. If team_id
   is missing, throw an error — never silently query all tenants.

2. **The Python client uses stdlib only** (urllib). No `requests`, no `httpx`.
   This matches SWE-Squad's existing pattern in `supabase_store.py`.

3. **Memory is best-effort**. Agent operations must never fail because the
   memory service is down. Use fire-and-forget for observation recording,
   graceful fallbacks for context injection.

4. **Embeddings use the existing bge-m3 pipeline** via BASE_LLM proxy at :8082.
   Do NOT introduce a new embedding model or service. Dimension is 1024.

5. **Don't modify `scripts/ops/supabase_schema.sql`**. Memory tables are in
   `memory/sql/001_memory_tables.sql`. They coexist in the same Supabase project.

6. **RBAC applies**. Memory operations respect `src/swe_team/agent_rbac.py`.
   Don't bypass it.

7. **No secrets in code**. All credentials via environment variables.

8. **Match SWE-Squad's error patterns**. Use circuit breakers (see
   `src/swe_team/embeddings.py` for the pattern), logging via stdlib `logging`.

## Testing

Run Python tests: `python -m pytest memory/tests/`
Run TypeScript tests: `cd memory && npx tsx --test tests/**/*.test.ts`

## Getting Started

```bash
cd memory
npm install              # Installs claude-mem and dependencies
cat sql/001_memory_tables.sql  # Review the schema
cat src/client.py              # Review the Python client
cat IMPLEMENTATION_PLAN.md     # Read the full plan
```
