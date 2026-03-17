-- =============================================================================
-- SWE-Squad — Supabase Schema
-- Run once to initialise the ticket store and audit trail.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- 1. swe_tickets — main work queue
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS swe_tickets (
    ticket_id       TEXT PRIMARY KEY,
    team_id         TEXT NOT NULL DEFAULT 'default',
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    severity        TEXT NOT NULL DEFAULT 'medium'
                        CHECK (severity IN ('critical','high','medium','low')),
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN (
                            'open','triaged','acknowledged','investigating',
                            'investigation_complete','in_development','in_review',
                            'testing','deploying','monitoring','resolved',
                            'rolled_back','closed'
                        )),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    assigned_to     TEXT,
    labels          JSONB NOT NULL DEFAULT '[]',
    source_module   TEXT,
    error_log       TEXT,
    related_tickets JSONB NOT NULL DEFAULT '[]',
    metadata        JSONB NOT NULL DEFAULT '{}',

    -- Lifecycle fields
    investigation_report TEXT,
    proposed_fix         TEXT,
    test_results         JSONB,
    deployment_id        TEXT,
    rollback_reason      TEXT,
    embedding            vector(1024)
);

ALTER TABLE swe_tickets
    -- Keep this for existing deployments where table predates embeddings.
    ADD COLUMN IF NOT EXISTS embedding vector(1024);

ALTER TABLE swe_tickets
    ADD COLUMN IF NOT EXISTS memory_confidence FLOAT DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS memory_accessed_at TIMESTAMPTZ;

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_tickets_team_status
    ON swe_tickets (team_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_team_severity
    ON swe_tickets (team_id, severity);
CREATE INDEX IF NOT EXISTS idx_tickets_fingerprint
    ON swe_tickets ((metadata->>'fingerprint'))
    WHERE metadata->>'fingerprint' IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tickets_assigned
    ON swe_tickets (assigned_to)
    WHERE assigned_to IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tickets_embedding
    ON swe_tickets
    USING ivfflat (embedding vector_cosine_ops)
    -- Lists tuned for moderate ticket volume; increase with table growth.
    WITH (lists = 100);

-- Auto-update updated_at on row changes
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tickets_updated_at ON swe_tickets;
CREATE TRIGGER trg_tickets_updated_at
    BEFORE UPDATE ON swe_tickets
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ---------------------------------------------------------------------------
-- 2. swe_ticket_events — immutable audit trail
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS swe_ticket_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_id   TEXT NOT NULL REFERENCES swe_tickets(ticket_id) ON DELETE CASCADE,
    team_id     TEXT NOT NULL DEFAULT 'default',
    from_status TEXT,
    to_status   TEXT NOT NULL,
    agent       TEXT,
    note        TEXT DEFAULT '',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_ticket
    ON swe_ticket_events (ticket_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_team
    ON swe_ticket_events (team_id, occurred_at);

-- ---------------------------------------------------------------------------
-- 3. Views — work queues
-- ---------------------------------------------------------------------------

-- All open tickets ranked by severity then age
CREATE OR REPLACE VIEW v_backlog AS
SELECT *,
    CASE severity
        WHEN 'critical' THEN 1
        WHEN 'high'     THEN 2
        WHEN 'medium'   THEN 3
        WHEN 'low'      THEN 4
    END AS severity_rank
FROM swe_tickets
WHERE status NOT IN ('resolved','closed','acknowledged')
ORDER BY severity_rank, created_at;

-- Critical tickets (for dashboards / alerts)
CREATE OR REPLACE VIEW v_queue_critical AS
SELECT * FROM swe_tickets
WHERE severity = 'critical'
  AND status NOT IN ('resolved','closed','acknowledged')
ORDER BY created_at;

-- Per-agent backlog
CREATE OR REPLACE VIEW v_queue_by_agent AS
SELECT assigned_to, team_id, severity, status, count(*) AS ticket_count
FROM swe_tickets
WHERE status NOT IN ('resolved','closed','acknowledged')
GROUP BY assigned_to, team_id, severity, status
ORDER BY assigned_to, team_id;

-- Stability gate summary (used by Ralph Wiggum)
CREATE OR REPLACE VIEW v_stability AS
SELECT
    team_id,
    count(*) FILTER (WHERE severity = 'critical' AND status NOT IN ('resolved','closed','acknowledged')) AS open_critical,
    count(*) FILTER (WHERE severity = 'high' AND status NOT IN ('resolved','closed','acknowledged')) AS open_high,
    count(*) FILTER (WHERE status NOT IN ('resolved','closed','acknowledged')) AS total_open,
    count(*) FILTER (WHERE status IN ('resolved','closed')) AS total_resolved
FROM swe_tickets
GROUP BY team_id;

-- ---------------------------------------------------------------------------
-- 4. Row-Level Security — scope by team_id
-- ---------------------------------------------------------------------------
ALTER TABLE swe_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE swe_ticket_events ENABLE ROW LEVEL SECURITY;

-- Allow full access via service role / anon key (RLS policy is permissive
-- for now; tighten per-team once JWT claims carry team_id).
DROP POLICY IF EXISTS tickets_all_access ON swe_tickets;
CREATE POLICY tickets_all_access ON swe_tickets
    FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS events_all_access ON swe_ticket_events;
CREATE POLICY events_all_access ON swe_ticket_events
    FOR ALL USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- 5. Semantic memory retrieval (pgvector similarity)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION match_similar_tickets(
    query_embedding  vector(1024),
    team             TEXT,
    match_count      INT     DEFAULT 5,
    similarity_floor FLOAT   DEFAULT 0.75,
    max_age_days     INT     DEFAULT 180
)
RETURNS TABLE (
    ticket_id            TEXT,
    title                TEXT,
    source_module        TEXT,
    error_log            TEXT,
    investigation_report TEXT,
    proposed_fix         TEXT,
    similarity           FLOAT,
    raw_similarity       FLOAT,
    memory_confidence    FLOAT
)
LANGUAGE sql STABLE AS $$
    SELECT
        t.ticket_id,
        t.title,
        t.source_module,
        t.error_log,
        t.investigation_report,
        t.proposed_fix,
        -- Final ranking score: semantic similarity weighted by confidence (1.0-2.0).
        ((1 - (t.embedding <=> query_embedding)) * COALESCE(t.memory_confidence, 1.0)) AS similarity,
        1 - (t.embedding <=> query_embedding) AS raw_similarity,
        COALESCE(t.memory_confidence, 1.0) AS memory_confidence
    FROM swe_tickets t
    WHERE t.team_id = team
      AND t.status IN ('resolved', 'closed')
      AND t.embedding IS NOT NULL
      AND COALESCE(t.memory_accessed_at, t.updated_at, t.created_at)
          >= now() - make_interval(days => GREATEST(max_age_days, 1))
      AND ((1 - (t.embedding <=> query_embedding)) * COALESCE(t.memory_confidence, 1.0)) >= similarity_floor
    ORDER BY similarity DESC
    LIMIT match_count;
$$;

CREATE OR REPLACE FUNCTION increment_memory_confidence(p_ticket_id TEXT, p_team TEXT)
RETURNS void LANGUAGE sql AS $$
    -- Fixed increment/cap follows issue #6 memory lifecycle policy.
    UPDATE swe_tickets
    SET memory_confidence = LEAST(COALESCE(memory_confidence, 1.0) + 0.1, 2.0),
        memory_accessed_at = now()
    WHERE ticket_id = p_ticket_id AND team_id = p_team;
$$;
