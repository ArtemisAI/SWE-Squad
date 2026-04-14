# SWE-Squad Memory Service — Architecture

## Overview

This module integrates [claude-mem](https://github.com/thedotmack/claude-mem)
(v12.1.0) as a persistent memory service for SWE-Squad's autonomous agent fleet.
The goal: agents learn from past investigations, share knowledge across sessions,
and build institutional memory — scoped by `team_id` for multi-tenant isolation.

## What is Claude-Mem?

Claude-mem is a TypeScript/Node.js project that provides persistent memory for
AI coding assistants. It captures observations from tool use, compresses them,
stores them in a database, and injects relevant context into future sessions.

**Key numbers**: ~40,000 lines of TypeScript, 80 test suites, 17+ DB migrations,
7 plugin skills, 6 IDE integrations.

**Original architecture**: SQLite (single file) + Chroma (vector search) + Express
HTTP API on port 37777. Single-user, localhost-only.

**Our adaptation**: Replace SQLite with Supabase PostgreSQL, replace Chroma with
pgvector, add auth middleware, add team_id scoping. Keep the HTTP API surface.

## Architecture Diagram

```
SWE-Squad Agents                         Memory Service
┌──────────────────────┐                ┌──────────────────────┐
│ investigator.py      │                │ Express :37777       │
│ developer.py         │  HTTP/JSON     │                      │
│ orchestrator.py      │ ──────────────→│ Auth Middleware       │
│ creative_agent.py    │                │   ↓                  │
│                      │                │ Team-ID Injection    │
│ Uses:                │                │   ↓                  │
│ memory.src.client.py │                │ Route Handlers       │
└──────────────────────┘                │   ↓                  │
                                        │ StorageAdapter       │
                                        │   ↓                  │
                                        │ SupabaseAdapter      │──→ Supabase PG
                                        │   ↓                  │    (pgvector +
                                        │ EmbeddingService     │──→  tsvector)
                                        │   (bge-m3 via        │
                                        │    BASE_LLM :8082)   │
                                        └──────────────────────┘
```

## Data Model

### Tables (all scoped by `team_id`)

```
memory_sessions         1──N  memory_observations
    │                              │
    │                              ├── embedding vector(1024)
    │                              └── fts_vector tsvector
    │
    ├── 1──N  memory_summaries
    │              │
    │              ├── embedding vector(1024)
    │              └── fts_vector tsvector
    │
    └── 1──N  memory_prompts

memory_audit_trail  (write-only log of all mutations)
```

### Key Relationships

- `memory_sessions.memory_session_id` → FK for observations and summaries
- `memory_sessions.content_session_id` → maps to the IDE/agent session ID
- `memory_observations.content_hash` → SHA256 for 30-second dedup window
- All tables have `team_id` with indexes on `(team_id, project)`

### Search Functions (PostgreSQL RPCs)

- `match_memory_observations(team_id, embedding, top_k, threshold, project)`
  — pgvector cosine similarity, returns ranked results
- `search_memory_observations(team_id, query, limit, project, type)`
  — tsvector FTS with `websearch_to_tsquery`

## Data Flow

### Recording (PostToolUse)

```
Agent runs a tool (Bash, Read, Write, etc.)
  → Python MemoryClient.record_observation()
    → POST /api/sessions/observations { contentSessionId, tool_name, tool_response, teamId }
      → Auth middleware validates API key, extracts teamId
        → SupabaseAdapter.storeObservation()
          → Compute content_hash for dedup
          → Generate embedding via EmbeddingService (bge-m3)
          → INSERT INTO memory_observations (team_id, ..., embedding)
          → INSERT INTO memory_audit_trail
```

### Context Injection (SessionStart)

```
Agent starts a new session
  → Python MemoryClient.get_context(project)
    → GET /api/context/inject?project=X&teamId=Y
      → SupabaseAdapter.getRecentObservations(teamId, project, limit=50)
      → Format as timeline text
      → Return plain text for system prompt injection
```

### Semantic Search

```
Agent searches memory for relevant past work
  → Python MemoryClient.search("auth bug fix")
    → GET /api/search?q=auth+bug+fix&teamId=Y
      → EmbeddingService.embed("auth bug fix") → 1024-dim vector
      → Supabase RPC: match_memory_observations(teamId, vector, 10, 0.70)
      → UNION with FTS: search_memory_observations(teamId, query, 50)
      → Merge, deduplicate, rank by combined score
      → Return JSON array of observations
```

## Integration Points with SWE-Squad

### Existing Components (no changes needed)

| Component | Why it's unchanged |
|-----------|-------------------|
| `src/swe_team/supabase_store.py` | Separate tables, same Supabase project |
| `src/swe_team/embeddings.py` | Memory uses same bge-m3 pipeline |
| `src/swe_team/agent_rbac.py` | Memory doesn't bypass RBAC |
| `scripts/ops/supabase_schema.sql` | Memory schema is additive (new tables) |

### Components to Modify

| Component | Change |
|-----------|--------|
| `src/swe_team/investigator.py` | Add `MemoryClient` for past investigation context |
| `src/swe_team/developer.py` | Record observations after Claude Code runs |
| `src/swe_team/orchestrator.py` | Session lifecycle management |
| `config/swe_team.yaml` | Add `memory:` config section |
| `.env.example` | Add `MEMORY_*` variables |

## Claude-Mem Source Reference

The claude-mem source is available at `memory/node_modules/claude-mem/` after
running `npm install` in the `memory/` directory. Key files to understand:

| File | Lines | What it does |
|------|-------|-------------|
| `src/services/worker-service.ts` | 1,253 | Main Express server + orchestrator |
| `src/services/sqlite/SessionStore.ts` | 2,674 | All CRUD — the interface to replicate |
| `src/services/sqlite/SessionSearch.ts` | 610 | FTS5 queries — replace with tsvector |
| `src/services/sqlite/types.ts` | 287 | TypeScript interfaces — reuse as-is |
| `src/services/worker/http/routes/SessionRoutes.ts` | 885 | Session lifecycle API |
| `src/services/worker/http/routes/SearchRoutes.ts` | 426 | Search API |
| `src/services/sync/ChromaSync.ts` | 843 | Chroma vector sync — replace with pgvector |
| `src/services/context/ContextBuilder.ts` | ~200 | Context formatting — keep as-is |
| `src/sdk/parser.ts` | 221 | Observation parser — keep as-is |
| `src/shared/platform-source.ts` | ~36 | Platform normalization — extend |

## Security Model

| Layer | Mechanism |
|-------|-----------|
| Network | Localhost by default; Tailscale for remote nodes |
| Authentication | API key header (simple) or JWT (for Supabase RLS) |
| Authorization | team_id scoping on every query; RBAC for agent permissions |
| Data isolation | Supabase RLS policies (permissive initially, JWT-tightened later) |
| Audit | All mutations logged to `memory_audit_trail` |

## Decisions & Trade-offs

1. **Fork vs dependency**: Using claude-mem as a git dependency first, with
   the option to fork into `memory/claude-mem-fork/` if deep modifications
   are needed. The implementation plan covers both paths.

2. **pgvector vs Chroma**: pgvector eliminates an external dependency and
   shares SWE-Squad's existing Supabase infrastructure. Chroma is disabled.

3. **Python client (stdlib only)**: Matches SWE-Squad's zero-extra-deps
   pattern in `supabase_store.py`. Uses `urllib` only.

4. **Permissive RLS first**: Start with `USING (true)` policies, tighten
   to JWT-based team isolation when auth is fully wired.

5. **Same embedding model**: Reuses bge-m3 (1024-dim) via the existing
   BASE_LLM proxy, ensuring embedding consistency with ticket similarity
   search.
