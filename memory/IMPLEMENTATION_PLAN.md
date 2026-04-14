# SWE-Squad Memory Service — Implementation Plan

> **Purpose**: Integrate claude-mem as a persistent, multi-tenant memory service
> for SWE-Squad's autonomous agent fleet. Agents (investigator, developer,
> creative, etc.) share memory within a team, scoped by `team_id`, backed by
> Supabase PostgreSQL with pgvector for semantic search.

---

## Table of Contents

1. [Current State](#1-current-state)
2. [Target Architecture](#2-target-architecture)
3. [Phase 1: Storage Layer Migration (SQLite → Supabase)](#3-phase-1-storage-layer-migration)
4. [Phase 2: Authentication & Multi-Tenant Scoping](#4-phase-2-authentication--multi-tenant-scoping)
5. [Phase 3: SWE-Squad Agent Integration](#5-phase-3-swe-squad-agent-integration)
6. [Phase 4: Embedding Pipeline Unification](#6-phase-4-embedding-pipeline-unification)
7. [Phase 5: Worker Service Adaptation](#7-phase-5-worker-service-adaptation)
8. [Phase 6: Docker & Deployment](#8-phase-6-docker--deployment)
9. [Phase 7: Testing & Validation](#9-phase-7-testing--validation)
10. [File Reference Matrix](#10-file-reference-matrix)
11. [Migration Checklist](#11-migration-checklist)
12. [Risk Register](#12-risk-register)

---

## 1. Current State

### Claude-mem (source: `memory/node_modules/claude-mem/` after `npm install`)

| Component | Technology | Location in claude-mem |
|-----------|-----------|----------------------|
| Database | SQLite (bun:sqlite, WAL mode) | `src/services/sqlite/Database.ts` |
| FTS | SQLite FTS5 | `src/services/sqlite/SessionSearch.ts` |
| Vector search | Chroma via MCP | `src/services/sync/ChromaSync.ts` |
| HTTP API | Express on :37777 | `src/services/worker-service.ts` |
| Auth | None (localhost only) | `src/services/worker/http/middleware.ts` |
| Multi-tenancy | None (`project` field, not enforced) | All tables have `project TEXT` |
| Scoping | `platform_source` per engine | `src/shared/platform-source.ts` |

### SWE-Squad (this repo)

| Component | Technology | Location |
|-----------|-----------|----------|
| Database | Supabase PostgreSQL | `src/swe_team/supabase_store.py` |
| Embeddings | bge-m3 (1024-dim) via BASE_LLM proxy | `src/swe_team/embeddings.py` |
| Vector search | pgvector (`match_similar_tickets()`) | `scripts/ops/supabase_schema.sql` |
| Multi-tenancy | `team_id` on all tables | `src/swe_team/supabase_store.py` |
| Auth | Service-role key + RBAC | `src/swe_team/agent_rbac.py` |
| RLS | Schema ready, policies permissive | `scripts/ops/supabase_schema.sql` |

### What exists in `memory/` (this directory)

| File | Purpose |
|------|---------|
| `package.json` | Node.js deps — claude-mem as git dependency |
| `tsconfig.json` | TypeScript build config |
| `sql/001_memory_tables.sql` | Supabase schema (sessions, observations, summaries, prompts, audit) |
| `src/client.py` | Python client for SWE agents |
| `docker/docker-compose.memory.yml` | Docker overlay for memory worker |
| `docker/Dockerfile.memory` | Worker container definition |
| `config/memory.env.example` | Environment config template |

---

## 2. Target Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    SWE-Squad Agent Fleet                        │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌─────────────┐ │
│  │ Investigator│ │ Developer  │ │ Creative   │ │ Orchestrator│ │
│  │ (Claude)   │ │ (Claude)   │ │ (Claude)   │ │ (Opus)      │ │
│  └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └──────┬──────┘ │
│        │              │              │               │         │
│        └──────────────┴──────────────┴───────────────┘         │
│                              │                                  │
│                    memory.src.client.MemoryClient               │
│                    (Python, team_id-scoped)                     │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTP (localhost or Tailscale)
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│              Memory Worker Service (Express :37777)              │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Auth Middleware (API key / JWT)                           │ │
│  │  Team-ID Injection (from auth → all queries)              │ │
│  ├────────────────────────────────────────────────────────────┤ │
│  │  Routes: /api/sessions/* /api/search /api/context/*       │ │
│  ├────────────────────────────────────────────────────────────┤ │
│  │  Storage Adapter (NEW — replaces SQLite layer)            │ │
│  │  ┌─────────────────────┐  ┌────────────────────────────┐ │ │
│  │  │ SupabaseStore       │  │ EmbeddingService           │ │ │
│  │  │ (PostgREST API)     │  │ (bge-m3 via BASE_LLM)     │ │ │
│  │  └─────────┬───────────┘  └─────────────┬──────────────┘ │ │
│  └────────────┼────────────────────────────┼────────────────┘ │
└───────────────┼────────────────────────────┼──────────────────┘
                │                            │
                ▼                            ▼
┌──────────────────────────────────────────────────────────────────┐
│              Supabase PostgreSQL                                 │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────┐ │
│  │ memory_sessions  │  │ memory_observations│ │ swe_tickets   │ │
│  │ memory_summaries │  │ memory_prompts    │  │ (existing)    │ │
│  │ memory_audit     │  │                   │  │               │ │
│  └──────────────────┘  └──────────────────┘  └───────────────┘ │
│  pgvector: match_memory_observations()                          │
│  FTS: search_memory_observations()                              │
│  RLS: team_id = jwt->>'team_id'                                 │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Phase 1: Storage Layer Migration

**Goal**: Replace claude-mem's SQLite layer with Supabase PostgreSQL while keeping the HTTP API surface identical.

### 3.1 Create the Storage Adapter Interface

Create a new TypeScript interface that both SQLite and Supabase backends implement:

**File to create**: `memory/src/storage/types.ts`

```typescript
export interface StorageAdapter {
  // Sessions
  createSession(params: CreateSessionParams): Promise<SessionRow>;
  getSession(contentSessionId: string, teamId: string): Promise<SessionRow | null>;
  updateMemorySessionId(sessionDbId: number, memorySessionId: string): Promise<void>;
  completeSession(contentSessionId: string, teamId: string): Promise<void>;

  // Observations
  storeObservation(params: StoreObservationParams): Promise<{ id: number; createdAtEpoch: number }>;
  getObservations(filters: ObservationFilters): Promise<ObservationRow[]>;
  getRecentObservations(teamId: string, project: string, limit: number): Promise<ObservationRow[]>;

  // Summaries
  storeSummary(params: StoreSummaryParams): Promise<{ id: number }>;
  getSummaries(teamId: string, project: string, limit: number): Promise<SummaryRow[]>;

  // Prompts
  storePrompt(params: StorePromptParams): Promise<{ id: number }>;

  // Search
  searchObservations(params: SearchParams): Promise<SearchResult[]>;
  searchSemantic(params: SemanticSearchParams): Promise<SearchResult[]>;

  // Lifecycle
  initialize(): Promise<void>;
  close(): Promise<void>;
}
```

### 3.2 Implement the Supabase Adapter

**File to create**: `memory/src/storage/supabase-adapter.ts`

This adapter calls Supabase via PostgREST (matching SWE-Squad's existing `supabase_store.py` pattern — stdlib `urllib` / `fetch`, no heavy SDK required).

**Key implementation details**:

1. **All queries include `team_id` filter**:
   ```typescript
   // Every SELECT includes team_id
   const url = `${supabaseUrl}/rest/v1/memory_observations?team_id=eq.${teamId}&project=eq.${project}`;
   ```

2. **Deduplication via content_hash** (30-second window, same as claude-mem):
   ```typescript
   const hash = crypto.createHash('sha256')
     .update(`${memorySessionId}|${title}|${narrative}`)
     .digest('hex');
   // Check: SELECT id FROM memory_observations WHERE content_hash = ? AND created_at_epoch > (now - 30s)
   ```

3. **FTS search via the `search_memory_observations()` function**:
   ```typescript
   const url = `${supabaseUrl}/rest/v1/rpc/search_memory_observations`;
   const body = { p_team_id: teamId, p_query: query, p_limit: limit };
   ```

4. **Semantic search via `match_memory_observations()`** (same as `match_similar_tickets`):
   ```typescript
   const url = `${supabaseUrl}/rest/v1/rpc/match_memory_observations`;
   const body = { p_team_id: teamId, p_embedding: vector, p_top_k: 10 };
   ```

### 3.3 Wire into Claude-mem's Worker Service

The worker service at `node_modules/claude-mem/src/services/worker-service.ts` creates a `DatabaseManager` that wraps `SessionStore` (SQLite). The migration path:

1. **Create a wrapper** that delegates to either SQLite or Supabase based on config:
   ```typescript
   // memory/src/storage/index.ts
   export function createStorageAdapter(config: MemoryConfig): StorageAdapter {
     if (config.backend === 'supabase') {
       return new SupabaseAdapter(config.supabaseUrl, config.supabaseKey, config.teamId);
     }
     // Fallback to original SQLite for local dev
     return new SqliteAdapter(config.dbPath);
   }
   ```

2. **Patch the worker service** routes to use the adapter instead of direct `SessionStore` calls. The key files to modify:
   - `SessionRoutes.ts` — replace `this.store.createSDKSession()` with `adapter.createSession()`
   - `SearchRoutes.ts` — replace `this.search.searchObservations()` with `adapter.searchObservations()`
   - `DataRoutes.ts` — replace direct DB queries with adapter methods

### 3.4 Claude-mem Files That Need Modification

| File (in node_modules/claude-mem/) | What to change | Why |
|-------------------------------------|----------------|-----|
| `src/services/worker-service.ts` | Accept StorageAdapter in constructor | Decouple from SQLite |
| `src/services/worker/http/routes/SessionRoutes.ts` | Use adapter for all DB calls | PostgreSQL backend |
| `src/services/worker/http/routes/SearchRoutes.ts` | Use adapter for search | pgvector + FTS |
| `src/services/worker/http/routes/DataRoutes.ts` | Use adapter for reads | PostgreSQL backend |
| `src/services/sqlite/Database.ts` | Keep as SQLite adapter | Backward compat |
| `src/services/sqlite/SessionStore.ts` | Extract to interface | Adapter pattern |
| `src/services/sync/ChromaSync.ts` | Disable when using pgvector | Avoid duplicate embeddings |

**Approach**: Rather than modifying `node_modules/` directly (fragile), create a **patched entry point** at `memory/src/server.ts` that:
1. Imports claude-mem's WorkerService
2. Monkey-patches or wraps the storage layer
3. Injects the Supabase adapter
4. Starts the modified service

If monkey-patching proves too brittle, **fork claude-mem into `memory/claude-mem-fork/`** and modify directly. The fork approach is more maintainable for deep changes.

---

## 4. Phase 2: Authentication & Multi-Tenant Scoping

### 4.1 Auth Middleware

**File to create**: `memory/src/middleware/auth.ts`

```typescript
import { Request, Response, NextFunction } from 'express';
import jwt from 'jsonwebtoken';

export function authMiddleware(req: Request, res: Response, next: NextFunction) {
  const apiKey = req.headers['authorization']?.replace('Bearer ', '');

  if (!apiKey) {
    // Allow localhost without auth (backward compat)
    const ip = req.ip || req.socket.remoteAddress;
    if (ip === '127.0.0.1' || ip === '::1') {
      req.teamId = process.env.SWE_TEAM_ID || 'default';
      return next();
    }
    return res.status(401).json({ error: 'Missing API key' });
  }

  // Option A: Simple API key lookup
  // Option B: JWT with team_id claim (for Supabase RLS)
  try {
    const decoded = jwt.verify(apiKey, process.env.JWT_SECRET!);
    req.teamId = (decoded as any).team_id;
    next();
  } catch {
    // Fallback to API key table lookup
    if (apiKey === process.env.MEMORY_API_KEY) {
      req.teamId = process.env.SWE_TEAM_ID || 'default';
      next();
    } else {
      res.status(403).json({ error: 'Invalid API key' });
    }
  }
}
```

### 4.2 Team-ID Injection

Every route handler must extract `teamId` from:
1. `req.teamId` (set by auth middleware)
2. `req.query.teamId` or `req.body.teamId` (explicit, for backward compat)
3. Environment `SWE_TEAM_ID` (fallback)

**All database queries MUST include `team_id`**. The Supabase adapter enforces this — if `teamId` is missing, the query throws.

### 4.3 CORS Update

**File to modify**: `memory/src/middleware/cors.ts`

Allow configurable origins via `MEMORY_CORS_ORIGINS` env var:
```typescript
const allowedOrigins = (process.env.MEMORY_CORS_ORIGINS || '')
  .split(',')
  .filter(Boolean)
  .concat(['http://localhost:37777', 'http://127.0.0.1:37777']);
```

---

## 5. Phase 3: SWE-Squad Agent Integration

### 5.1 Modify Investigator to Use Memory

**File to modify**: `src/swe_team/investigator.py`

The investigator already has `_semantic_memory_context()` using `match_similar_tickets()`. Add a parallel memory query:

```python
from memory.src.client import MemoryClient

class Investigator:
    def __init__(self, ..., memory_client: Optional[MemoryClient] = None):
        self._memory = memory_client or MemoryClient()

    def _semantic_memory_context(self, ticket: SWETicket) -> str:
        # Existing: search similar tickets via supabase_store
        ticket_context = self._existing_semantic_memory(ticket)

        # NEW: search observation memory for past investigation insights
        memory_context = self._memory.get_investigation_context(
            ticket.title, project=ticket.metadata.get("repo", ""),
        )

        return f"{ticket_context}\n\n{memory_context}" if memory_context else ticket_context
```

### 5.2 Modify Developer to Record Observations

**File to modify**: `src/swe_team/developer.py`

After each Claude Code CLI invocation, record the observation:

```python
# After subprocess.run(["claude", ...]) completes:
self._memory.record_observation(
    session_id=ticket.development_session_id,
    tool_name="claude-code",
    tool_input={"ticket_id": ticket.ticket_id, "prompt": prompt},
    tool_response=result.stdout[:2000],
    cwd=repo_path,
    project=repo_name,
)
```

### 5.3 Modify Orchestrator for Session Lifecycle

**File to modify**: `src/swe_team/orchestrator.py`

```python
# At start of orchestration:
memory.init_session(
    session_id=f"orch-{ticket.ticket_id}",
    project="SWE-Squad",
    agent_id="orchestrator",
)

# At end:
memory.record_summary(
    session_id=f"orch-{ticket.ticket_id}",
    summary=f"Orchestrated {ticket.ticket_id}: {ticket.status}",
)
memory.complete_session(session_id=f"orch-{ticket.ticket_id}")
```

### 5.4 Agent-Specific platform_source Values

| Agent | platform_source | project pattern |
|-------|----------------|-----------------|
| Investigator | `swe-investigator` | `swe-squad/{repo_name}` |
| Developer | `swe-developer` | `swe-squad/{repo_name}` |
| Creative | `swe-creative` | `swe-squad/{repo_name}` |
| Orchestrator | `swe-orchestrator` | `swe-squad/SWE-Squad` |
| Monitor | `swe-monitor` | `swe-squad/monitoring` |
| Triage | `swe-triage` | `swe-squad/triage` |

---

## 6. Phase 4: Embedding Pipeline Unification

### Goal
Use SWE-Squad's existing `bge-m3` pipeline (via BASE_LLM proxy at `:8082`) for memory observation embeddings, replacing claude-mem's Chroma dependency.

### 6.1 Create Embedding Service

**File to create**: `memory/src/embeddings/service.ts`

```typescript
export class EmbeddingService {
  private baseLlmUrl: string;
  private model: string;

  constructor(baseLlmUrl: string = 'http://localhost:8082/v1', model: string = 'bge-m3') {
    this.baseLlmUrl = baseLlmUrl;
    this.model = model;
  }

  async embed(text: string): Promise<number[]> {
    const response = await fetch(`${this.baseLlmUrl}/embeddings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: text, model: this.model }),
    });
    const data = await response.json();
    return data.data[0].embedding;  // 1024-dim vector
  }

  async embedBatch(texts: string[]): Promise<number[][]> {
    const response = await fetch(`${this.baseLlmUrl}/embeddings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: texts, model: this.model }),
    });
    const data = await response.json();
    return data.data.map((d: any) => d.embedding);
  }
}
```

### 6.2 Embed on Observation Storage

When storing an observation, generate and store the embedding in the same INSERT:

```typescript
// In SupabaseAdapter.storeObservation():
const text = `${params.title || ''} ${params.narrative || ''} ${params.facts || ''}`;
const embedding = await this.embeddingService.embed(text);

const body = {
  ...params,
  embedding: embedding,  // pgvector accepts JSON array
};
```

### 6.3 Disable Chroma

When using Supabase backend, set `CLAUDE_MEM_CHROMA_MODE=disabled` to prevent ChromaSync from running. All vector search goes through pgvector.

---

## 7. Phase 5: Worker Service Adaptation

### 7.1 Decision: Fork vs Wrap

**Recommended: Fork claude-mem** into `memory/claude-mem-fork/`.

Rationale:
- The SQLite → PostgreSQL change touches 6+ core files
- Auth middleware needs to wrap all route registrations
- team_id injection affects every query
- Monkey-patching `node_modules/` is fragile and breaks on updates

**Steps**:
1. Copy claude-mem source into `memory/claude-mem-fork/`
2. Remove SQLite-specific code from the fork
3. Add Supabase adapter
4. Add auth middleware
5. Add team_id to all route handlers
6. Update `package.json` to point to the fork instead of git dependency

### 7.2 Modified Entry Point

**File to create**: `memory/src/server.ts`

```typescript
import { WorkerService } from './claude-mem-fork/services/worker-service';
import { SupabaseAdapter } from './storage/supabase-adapter';
import { EmbeddingService } from './embeddings/service';
import { authMiddleware } from './middleware/auth';

const adapter = new SupabaseAdapter(
  process.env.SUPABASE_URL!,
  process.env.SUPABASE_KEY!,
  process.env.SWE_TEAM_ID || 'default',
);

const embeddings = new EmbeddingService(
  process.env.BASE_LLM_URL || 'http://localhost:8082/v1',
);

const worker = new WorkerService({
  storageAdapter: adapter,
  embeddingService: embeddings,
  middleware: [authMiddleware],
  host: process.env.CLAUDE_MEM_WORKER_HOST || '0.0.0.0',
  port: parseInt(process.env.CLAUDE_MEM_WORKER_PORT || '37777'),
});

worker.start();
```

### 7.3 Route Modifications Summary

| Route File | Changes Needed |
|-----------|----------------|
| `SessionRoutes.ts` | Replace `this.store.*` with `this.adapter.*`; add `req.teamId` to all calls |
| `SearchRoutes.ts` | Replace FTS5 with `search_memory_observations()` RPC; replace Chroma with `match_memory_observations()` RPC |
| `DataRoutes.ts` | Replace SQLite queries with adapter calls |
| `SettingsRoutes.ts` | Minimal changes — settings stay local or move to Supabase |
| `CorpusRoutes.ts` | Replace SQLite queries with adapter calls |

---

## 8. Phase 6: Docker & Deployment

### 8.1 Container Architecture

```
swe-runner (existing)          ←→  memory-worker (new)
  ├── investigator.py                 ├── Express :37777
  ├── developer.py                    ├── Supabase adapter
  └── memory.src.client.py ──HTTP──→  └── Embedding service ──→ BASE_LLM :8082
```

### 8.2 Deployment Steps

1. Run the Supabase migration:
   ```bash
   psql $SUPABASE_URL -f memory/sql/001_memory_tables.sql
   ```

2. Add memory service to docker-compose:
   ```bash
   docker compose -f docker/swe-squad/docker-compose.yml \
                  -f memory/docker/docker-compose.memory.yml up -d
   ```

3. Set environment variables in `.env`:
   ```bash
   MEMORY_WORKER_HOST=0.0.0.0
   MEMORY_WORKER_PORT=37777
   SUPABASE_URL=https://xyz.supabase.co
   SUPABASE_KEY=your-service-role-key
   SWE_TEAM_ID=alpha
   ```

### 8.3 Networking

For remote agent nodes (worker-1, worker-2, worker-3):
- Option A: Run memory-worker on each node (local SQLite fallback)
- Option B: Single memory-worker on worker-1, expose via Tailscale/Caddy
- Option C: Each node talks directly to Supabase (no worker, Python client calls PostgREST)

**Recommended**: Option B for simplicity. Single worker, Supabase backend, Tailscale for inter-node access.

---

## 9. Phase 7: Testing & Validation

### 9.1 Unit Tests

**File to create**: `memory/tests/test_client.py`

```python
def test_init_session():
    client = MemoryClient(team_id="test", host="localhost", port=37777)
    result = client.init_session("sess-1", "test-project", agent_id="investigator")
    assert "sessionDbId" in result

def test_search():
    client = MemoryClient(team_id="test")
    results = client.search("authentication bug", project="test-project")
    assert results.total >= 0

def test_team_isolation():
    client_a = MemoryClient(team_id="alpha")
    client_b = MemoryClient(team_id="beta")
    # Record in alpha
    client_a.init_session("sess-a", "shared-project", agent_id="dev")
    client_a.record_observation("sess-a", "Bash", tool_response="fixed auth bug")
    # Search in beta should NOT find alpha's data
    results = client_b.search("fixed auth bug", project="shared-project")
    assert results.total == 0
```

### 9.2 Integration Tests

```python
def test_supabase_schema():
    """Verify all memory tables exist in Supabase."""
    tables = ["memory_sessions", "memory_observations", "memory_summaries", "memory_prompts"]
    for table in tables:
        result = supabase_query(f"SELECT count(*) FROM {table} WHERE team_id = 'test'")
        assert result is not None

def test_semantic_search():
    """Verify pgvector similarity search works."""
    embedding = get_embedding("authentication fix")
    results = supabase_rpc("match_memory_observations", {
        "p_team_id": "test",
        "p_embedding": embedding,
        "p_top_k": 5,
    })
    assert isinstance(results, list)

def test_fts_search():
    """Verify full-text search works."""
    results = supabase_rpc("search_memory_observations", {
        "p_team_id": "test",
        "p_query": "authentication",
        "p_limit": 10,
    })
    assert isinstance(results, list)
```

### 9.3 End-to-End Test

1. Start memory worker
2. Init session via Python client
3. Record 3 observations
4. Search for observations
5. Get context injection text
6. Verify team isolation
7. Complete session
8. Verify audit trail

---

## 10. File Reference Matrix

### Files to CREATE (in `memory/`)

| File | Purpose | Phase |
|------|---------|-------|
| `src/storage/types.ts` | StorageAdapter interface | 1 |
| `src/storage/supabase-adapter.ts` | Supabase PostgREST adapter | 1 |
| `src/storage/index.ts` | Adapter factory | 1 |
| `src/middleware/auth.ts` | API key + JWT auth | 2 |
| `src/middleware/cors.ts` | Configurable CORS | 2 |
| `src/middleware/team-id.ts` | Team-ID extraction + injection | 2 |
| `src/embeddings/service.ts` | bge-m3 embedding via BASE_LLM | 4 |
| `src/server.ts` | Modified worker entry point | 5 |
| `tests/test_client.py` | Python client tests | 7 |
| `tests/test_storage.test.ts` | TypeScript adapter tests | 7 |
| `tests/test_integration.py` | End-to-end tests | 7 |

### Files to MODIFY (in SWE-Squad `src/`)

| File | Change | Phase |
|------|--------|-------|
| `src/swe_team/investigator.py` | Add `MemoryClient` for context enrichment | 3 |
| `src/swe_team/developer.py` | Record observations after Claude Code runs | 3 |
| `src/swe_team/orchestrator.py` | Session lifecycle (init/complete) | 3 |
| `src/swe_team/creative_agent.py` | Search memory for improvement candidates | 3 |
| `src/swe_team/config.py` | Add memory service config section | 3 |
| `docker/swe-squad/docker-compose.yml` | Reference memory overlay | 6 |
| `.env.example` | Add MEMORY_* variables | 6 |
| `scripts/ops/supabase_schema.sql` | Reference `memory/sql/001_memory_tables.sql` | 1 |

### Claude-mem Files to UNDERSTAND (read-only reference)

| File (in claude-mem source) | Why it matters |
|-----------------------------|----------------|
| `src/services/worker-service.ts` (1253 lines) | Main orchestrator — understand init flow |
| `src/services/worker/http/routes/SessionRoutes.ts` (885 lines) | Session API — must replicate in adapter |
| `src/services/worker/http/routes/SearchRoutes.ts` (426 lines) | Search API — replace FTS5 with pgvector |
| `src/services/sqlite/SessionStore.ts` (2674 lines) | All CRUD ops — extract interface from this |
| `src/services/sqlite/SessionSearch.ts` (610 lines) | FTS5 queries — replace with tsvector |
| `src/services/sqlite/types.ts` (287 lines) | TypeScript interfaces — reuse as-is |
| `src/services/sqlite/observations/store.ts` (104 lines) | Dedup logic — replicate in adapter |
| `src/services/sync/ChromaSync.ts` (843 lines) | Vector sync — replace with pgvector |
| `src/shared/platform-source.ts` | Platform normalization — extend for SWE agents |
| `src/services/context/ObservationCompiler.ts` | Context formatting — keep as-is |
| `src/services/context/ContextBuilder.ts` | Timeline builder — keep as-is |
| `src/sdk/parser.ts` (221 lines) | Observation XML parser — keep as-is |

---

## 11. Migration Checklist

### Phase 1: Storage Layer
- [ ] Create `StorageAdapter` interface in `memory/src/storage/types.ts`
- [ ] Implement `SupabaseAdapter` in `memory/src/storage/supabase-adapter.ts`
- [ ] Run `memory/sql/001_memory_tables.sql` on Supabase
- [ ] Test CRUD operations via Supabase adapter
- [ ] Test FTS via `search_memory_observations()`
- [ ] Test semantic search via `match_memory_observations()`

### Phase 2: Auth & Multi-Tenant
- [ ] Implement auth middleware
- [ ] Add team_id extraction to all routes
- [ ] Test team isolation (team A can't see team B's data)
- [ ] Update CORS configuration

### Phase 3: Agent Integration
- [ ] Wire `MemoryClient` into `investigator.py`
- [ ] Wire `MemoryClient` into `developer.py`
- [ ] Wire `MemoryClient` into `orchestrator.py`
- [ ] Add memory config to `config/swe_team.yaml`
- [ ] Test: investigation pulls relevant past observations
- [ ] Test: development records observations

### Phase 4: Embeddings
- [ ] Create `EmbeddingService` using BASE_LLM proxy
- [ ] Embed observations on storage
- [ ] Verify pgvector similarity search
- [ ] Disable Chroma sync

### Phase 5: Worker Adaptation
- [ ] Fork or wrap claude-mem worker
- [ ] Replace SQLite calls with adapter
- [ ] Create modified entry point `memory/src/server.ts`
- [ ] Test all API endpoints

### Phase 6: Deployment
- [ ] Build Docker image
- [ ] Test docker-compose overlay
- [ ] Configure networking (Tailscale or direct)
- [ ] Add to deployment playbook

### Phase 7: Validation
- [ ] Unit tests pass
- [ ] Integration tests pass
- [ ] End-to-end test passes
- [ ] Team isolation verified
- [ ] Performance: search < 500ms
- [ ] Memory: context injection < 2s

---

## 12. Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| Claude-mem API changes on update | Adapter breaks | Pin to specific git SHA in package.json; or fork |
| Supabase rate limits on high observation volume | Dropped observations | Batch inserts (100/request); queue with retry |
| bge-m3 proxy downtime | No semantic search | Graceful fallback to FTS-only; circuit breaker (matches embeddings.py pattern) |
| team_id not set | Cross-tenant data leak | Adapter throws on missing team_id; auth middleware enforces |
| SQLite → PostgreSQL query differences | Broken search | Test all query patterns; use Supabase RPC functions |
| Worker single-point-of-failure | All agents lose memory | Health check + auto-restart; agents degrade gracefully (memory is best-effort) |
| RLS policies too permissive | Security gap | Start permissive, tighten with JWT claims in Phase 2 |
