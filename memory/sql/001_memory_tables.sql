-- =============================================================================
-- SWE-Squad Memory — Supabase Schema (claude-mem PostgreSQL migration)
--
-- Adapts claude-mem's SQLite schema to Supabase PostgreSQL with:
--   - team_id scoping on every table (multi-tenant)
--   - pgvector embeddings for semantic search
--   - FTS via tsvector for keyword search
--   - RLS policies for tenant isolation
--   - Matches SWE-Squad's existing team_id convention
--
-- Run after the base supabase_schema.sql (this extends, not replaces).
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- 1. memory_sessions — agent working sessions (maps to claude-mem sdk_sessions)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_sessions (
    id                  BIGSERIAL PRIMARY KEY,
    team_id             TEXT NOT NULL DEFAULT 'default',
    content_session_id  TEXT NOT NULL,
    memory_session_id   TEXT NOT NULL DEFAULT gen_random_uuid()::text,
    project             TEXT NOT NULL,
    platform_source     TEXT NOT NULL DEFAULT 'claude',
    agent_id            TEXT,                           -- SWE agent role (investigator, developer, etc.)
    user_prompt         TEXT,
    status              TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'completed', 'failed', 'orphaned')),
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at_epoch    BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT,
    completed_at        TIMESTAMPTZ,
    completed_at_epoch  BIGINT,
    failed_at_epoch     BIGINT,
    prompt_counter      INTEGER NOT NULL DEFAULT 0,
    custom_title        TEXT,
    worker_port         INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_memory_sessions_content UNIQUE (team_id, content_session_id),
    CONSTRAINT uq_memory_sessions_msid UNIQUE (memory_session_id)
);

CREATE INDEX IF NOT EXISTS idx_msess_team_project ON memory_sessions (team_id, project);
CREATE INDEX IF NOT EXISTS idx_msess_team_platform ON memory_sessions (team_id, platform_source);
CREATE INDEX IF NOT EXISTS idx_msess_agent ON memory_sessions (team_id, agent_id) WHERE agent_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. memory_observations — captured tool use events
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_observations (
    id                  BIGSERIAL PRIMARY KEY,
    team_id             TEXT NOT NULL DEFAULT 'default',
    memory_session_id   TEXT NOT NULL,
    project             TEXT NOT NULL,
    type                TEXT,                           -- bugfix, feature, decision, discovery, change
    title               TEXT,
    subtitle            TEXT,
    narrative           TEXT,
    text                TEXT,
    facts               TEXT,                           -- JSON array of fact strings
    concepts            TEXT,                           -- comma-separated
    files_read          TEXT,                           -- comma-separated
    files_modified      TEXT,                           -- comma-separated
    prompt_number       INTEGER DEFAULT 0,
    discovery_tokens    INTEGER DEFAULT 0,
    content_hash        TEXT,                           -- SHA256 for dedup
    generated_by_model  TEXT,
    relevance_count     INTEGER DEFAULT 0,
    embedding           vector(1024),                   -- pgvector, same dim as SWE-Squad (bge-m3)
    fts_vector          tsvector,                       -- auto-generated for full-text search
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at_epoch    BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_obs_session FOREIGN KEY (memory_session_id)
        REFERENCES memory_sessions (memory_session_id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_obs_team_project ON memory_observations (team_id, project);
CREATE INDEX IF NOT EXISTS idx_obs_session ON memory_observations (memory_session_id);
CREATE INDEX IF NOT EXISTS idx_obs_team_type ON memory_observations (team_id, type) WHERE type IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_obs_created ON memory_observations (team_id, created_at_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_obs_content_hash ON memory_observations (content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_obs_fts ON memory_observations USING gin(fts_vector);
CREATE INDEX IF NOT EXISTS idx_obs_embedding ON memory_observations
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Auto-generate FTS vector on insert/update
CREATE OR REPLACE FUNCTION memory_obs_fts_trigger()
RETURNS TRIGGER AS $$
BEGIN
    NEW.fts_vector := to_tsvector('english',
        COALESCE(NEW.title, '') || ' ' ||
        COALESCE(NEW.narrative, '') || ' ' ||
        COALESCE(NEW.text, '') || ' ' ||
        COALESCE(NEW.facts, '') || ' ' ||
        COALESCE(NEW.concepts, '')
    );
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_obs_fts ON memory_observations;
CREATE TRIGGER trg_obs_fts
    BEFORE INSERT OR UPDATE ON memory_observations
    FOR EACH ROW EXECUTE FUNCTION memory_obs_fts_trigger();

-- ---------------------------------------------------------------------------
-- 3. memory_summaries — session summaries
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_summaries (
    id                  BIGSERIAL PRIMARY KEY,
    team_id             TEXT NOT NULL DEFAULT 'default',
    memory_session_id   TEXT NOT NULL,
    project             TEXT NOT NULL,
    request             TEXT,
    investigated        TEXT,
    learned             TEXT,
    completed           TEXT,
    next_steps          TEXT,
    files_read          TEXT,
    files_edited        TEXT,
    notes               TEXT,
    prompt_number       INTEGER DEFAULT 0,
    discovery_tokens    INTEGER DEFAULT 0,
    embedding           vector(1024),
    fts_vector          tsvector,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at_epoch    BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_sum_session FOREIGN KEY (memory_session_id)
        REFERENCES memory_sessions (memory_session_id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sum_team_project ON memory_summaries (team_id, project);
CREATE INDEX IF NOT EXISTS idx_sum_session ON memory_summaries (memory_session_id);
CREATE INDEX IF NOT EXISTS idx_sum_fts ON memory_summaries USING gin(fts_vector);

-- FTS trigger for summaries
CREATE OR REPLACE FUNCTION memory_sum_fts_trigger()
RETURNS TRIGGER AS $$
BEGIN
    NEW.fts_vector := to_tsvector('english',
        COALESCE(NEW.request, '') || ' ' ||
        COALESCE(NEW.investigated, '') || ' ' ||
        COALESCE(NEW.learned, '') || ' ' ||
        COALESCE(NEW.completed, '') || ' ' ||
        COALESCE(NEW.next_steps, '')
    );
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sum_fts ON memory_summaries;
CREATE TRIGGER trg_sum_fts
    BEFORE INSERT OR UPDATE ON memory_summaries
    FOR EACH ROW EXECUTE FUNCTION memory_sum_fts_trigger();

-- ---------------------------------------------------------------------------
-- 4. memory_prompts — user prompts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_prompts (
    id                  BIGSERIAL PRIMARY KEY,
    team_id             TEXT NOT NULL DEFAULT 'default',
    content_session_id  TEXT NOT NULL,
    prompt_number       INTEGER NOT NULL DEFAULT 0,
    prompt_text         TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at_epoch    BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT
);

CREATE INDEX IF NOT EXISTS idx_prompts_team ON memory_prompts (team_id);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON memory_prompts (content_session_id);

-- ---------------------------------------------------------------------------
-- 5. memory_audit_trail — mutation log for compliance
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_audit_trail (
    id                  BIGSERIAL PRIMARY KEY,
    team_id             TEXT NOT NULL,
    action              TEXT NOT NULL,                  -- insert, update, delete, search, inject
    table_name          TEXT NOT NULL,
    record_id           BIGINT,
    agent_id            TEXT,
    platform_source     TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_team ON memory_audit_trail (team_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 6. Semantic search function (matches SWE-Squad's match_similar_tickets pattern)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION match_memory_observations(
    p_team_id       TEXT,
    p_embedding     vector(1024),
    p_top_k         INTEGER DEFAULT 10,
    p_threshold     FLOAT DEFAULT 0.70,
    p_project       TEXT DEFAULT NULL
)
RETURNS TABLE (
    id              BIGINT,
    project         TEXT,
    type            TEXT,
    title           TEXT,
    narrative       TEXT,
    facts           TEXT,
    concepts        TEXT,
    files_read      TEXT,
    files_modified  TEXT,
    similarity      FLOAT,
    created_at_epoch BIGINT
)
LANGUAGE sql STABLE
AS $$
    SELECT
        o.id,
        o.project,
        o.type,
        o.title,
        o.narrative,
        o.facts,
        o.concepts,
        o.files_read,
        o.files_modified,
        1 - (o.embedding <=> p_embedding) AS similarity,
        o.created_at_epoch
    FROM memory_observations o
    WHERE o.team_id = p_team_id
      AND o.embedding IS NOT NULL
      AND (p_project IS NULL OR o.project = p_project)
      AND 1 - (o.embedding <=> p_embedding) >= p_threshold
    ORDER BY o.embedding <=> p_embedding
    LIMIT p_top_k;
$$;

-- ---------------------------------------------------------------------------
-- 7. Full-text search function
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION search_memory_observations(
    p_team_id       TEXT,
    p_query         TEXT,
    p_limit         INTEGER DEFAULT 50,
    p_project       TEXT DEFAULT NULL,
    p_type          TEXT DEFAULT NULL
)
RETURNS TABLE (
    id              BIGINT,
    project         TEXT,
    type            TEXT,
    title           TEXT,
    narrative       TEXT,
    facts           TEXT,
    concepts        TEXT,
    files_read      TEXT,
    files_modified  TEXT,
    rank            FLOAT,
    created_at_epoch BIGINT
)
LANGUAGE sql STABLE
AS $$
    SELECT
        o.id,
        o.project,
        o.type,
        o.title,
        o.narrative,
        o.facts,
        o.concepts,
        o.files_read,
        o.files_modified,
        ts_rank(o.fts_vector, websearch_to_tsquery('english', p_query)) AS rank,
        o.created_at_epoch
    FROM memory_observations o
    WHERE o.team_id = p_team_id
      AND o.fts_vector @@ websearch_to_tsquery('english', p_query)
      AND (p_project IS NULL OR o.project = p_project)
      AND (p_type IS NULL OR o.type = p_type)
    ORDER BY rank DESC
    LIMIT p_limit;
$$;

-- ---------------------------------------------------------------------------
-- 8. Row-Level Security (enforced via team_id in JWT claims)
-- ---------------------------------------------------------------------------
-- Enable RLS on all memory tables
ALTER TABLE memory_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_summaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_prompts ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_audit_trail ENABLE ROW LEVEL SECURITY;

-- Service role bypasses RLS (for the memory worker service)
-- These permissive policies allow service-role keys full access.
-- For per-team isolation with anon keys, replace USING (true) with:
--   USING (team_id = current_setting('request.jwt.claims', true)::json->>'team_id')
CREATE POLICY memory_sessions_all ON memory_sessions FOR ALL USING (true);
CREATE POLICY memory_observations_all ON memory_observations FOR ALL USING (true);
CREATE POLICY memory_summaries_all ON memory_summaries FOR ALL USING (true);
CREATE POLICY memory_prompts_all ON memory_prompts FOR ALL USING (true);
CREATE POLICY memory_audit_all ON memory_audit_trail FOR ALL USING (true);

-- ---------------------------------------------------------------------------
-- 9. Team-scoped RLS policies (activate when ready for per-team JWT isolation)
-- ---------------------------------------------------------------------------
-- Uncomment these and DROP the permissive policies above when enabling
-- JWT-based team isolation:
--
-- CREATE POLICY team_sessions ON memory_sessions FOR ALL
--     USING (team_id = current_setting('request.jwt.claims', true)::json->>'team_id');
-- CREATE POLICY team_observations ON memory_observations FOR ALL
--     USING (team_id = current_setting('request.jwt.claims', true)::json->>'team_id');
-- CREATE POLICY team_summaries ON memory_summaries FOR ALL
--     USING (team_id = current_setting('request.jwt.claims', true)::json->>'team_id');
-- CREATE POLICY team_prompts ON memory_prompts FOR ALL
--     USING (team_id = current_setting('request.jwt.claims', true)::json->>'team_id');
-- CREATE POLICY team_audit ON memory_audit_trail FOR ALL
--     USING (team_id = current_setting('request.jwt.claims', true)::json->>'team_id');

-- ---------------------------------------------------------------------------
-- 10. Convenience views
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_memory_timeline AS
SELECT
    o.id,
    o.team_id,
    o.project,
    o.type,
    o.title,
    o.narrative,
    o.facts,
    o.concepts,
    o.files_read,
    o.files_modified,
    o.created_at_epoch,
    o.created_at,
    s.platform_source,
    s.agent_id,
    s.content_session_id
FROM memory_observations o
JOIN memory_sessions s ON o.memory_session_id = s.memory_session_id
ORDER BY o.created_at_epoch DESC;
