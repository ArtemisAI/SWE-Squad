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

-- ---------------------------------------------------------------------------
-- 6. code_modules — Module registry for knowledge graph
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS code_modules (
    module_id   TEXT PRIMARY KEY,                    -- e.g. "security.py", "job_scraper.py"
    team_id     TEXT NOT NULL DEFAULT 'default',
    repo        TEXT NOT NULL DEFAULT '',             -- "ArtemisAI/LinkedAi"
    file_path   TEXT DEFAULT '',                      -- full path
    embedding   vector(1024),
    last_seen   TIMESTAMPTZ DEFAULT now(),
    metadata    JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_modules_team ON code_modules(team_id);
CREATE INDEX IF NOT EXISTS idx_modules_repo ON code_modules(repo);

-- ---------------------------------------------------------------------------
-- 7. knowledge_edges — Auto-discovered relationships between nodes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_edges (
    source_id       TEXT NOT NULL,                   -- ticket_id, module_id, pr_id, or gh_issue_id
    target_id       TEXT NOT NULL,
    edge_type       TEXT NOT NULL                    -- 'similar', 'touches_module', 'blocks',
                        CHECK (edge_type IN (        -- 'resolves', 'conflicts_with', 'caused_regression'
                            'similar', 'touches_module', 'blocks',
                            'resolves', 'conflicts_with', 'caused_regression'
                        )),
    team_id         TEXT NOT NULL DEFAULT 'default',
    confidence      FLOAT NOT NULL DEFAULT 0.0,      -- cosine similarity or LLM confidence
    discovered_at   TIMESTAMPTZ DEFAULT now(),
    discovered_by   TEXT DEFAULT '',                  -- 'embedding', 'fact_extraction', 'pr_sync', 'investigator'
    metadata        JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (source_id, target_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON knowledge_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON knowledge_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON knowledge_edges(edge_type);
CREATE INDEX IF NOT EXISTS idx_edges_team ON knowledge_edges(team_id);

-- ---------------------------------------------------------------------------
-- 8. resolution_clusters — Tickets sharing a root cause
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resolution_clusters (
    cluster_id      TEXT PRIMARY KEY,
    team_id         TEXT NOT NULL DEFAULT 'default',
    root_cause      TEXT DEFAULT '',                  -- LLM-extracted shared root cause
    primary_module  TEXT DEFAULT '',                  -- module most referenced
    ticket_ids      JSONB NOT NULL DEFAULT '[]',     -- all tickets in this cluster
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'investigating', 'resolved')),
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    metadata        JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_clusters_team ON resolution_clusters(team_id);
CREATE INDEX IF NOT EXISTS idx_clusters_status ON resolution_clusters(status);

-- ---------------------------------------------------------------------------
-- 9. pr_nodes — PR tracking synced from GitHub
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pr_nodes (
    pr_id           TEXT PRIMARY KEY,                -- "ArtemisAI/LinkedAi#142"
    team_id         TEXT NOT NULL DEFAULT 'default',
    repo            TEXT NOT NULL DEFAULT '',
    number          INTEGER NOT NULL DEFAULT 0,
    branch          TEXT DEFAULT '',
    title           TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'merged', 'closed')),
    author          TEXT DEFAULT '',
    files_changed   JSONB NOT NULL DEFAULT '[]',     -- ['src/application/security.py']
    ticket_ids      JSONB NOT NULL DEFAULT '[]',     -- tickets this PR claims to fix
    created_at      TIMESTAMPTZ DEFAULT now(),
    merged_at       TIMESTAMPTZ,
    review_status   TEXT DEFAULT 'pending'
                        CHECK (review_status IN ('pending', 'approved', 'changes_requested')),
    last_checked    TIMESTAMPTZ DEFAULT now(),
    metadata        JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_pr_status ON pr_nodes(status);
CREATE INDEX IF NOT EXISTS idx_pr_repo ON pr_nodes(repo);
CREATE INDEX IF NOT EXISTS idx_pr_team ON pr_nodes(team_id);

-- ---------------------------------------------------------------------------
-- 10. Row-Level Security — knowledge graph tables
-- ---------------------------------------------------------------------------
ALTER TABLE code_modules ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE resolution_clusters ENABLE ROW LEVEL SECURITY;
ALTER TABLE pr_nodes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS modules_all_access ON code_modules;
CREATE POLICY modules_all_access ON code_modules
    FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS edges_all_access ON knowledge_edges;
CREATE POLICY edges_all_access ON knowledge_edges
    FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS clusters_all_access ON resolution_clusters;
CREATE POLICY clusters_all_access ON resolution_clusters
    FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS pr_nodes_all_access ON pr_nodes;
CREATE POLICY pr_nodes_all_access ON pr_nodes
    FOR ALL USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- 11. Knowledge graph RPC functions
-- ---------------------------------------------------------------------------

-- Count edges of a given type for a node
CREATE OR REPLACE FUNCTION count_edges(
    p_node_id   TEXT,
    p_edge_type TEXT DEFAULT NULL,
    p_team      TEXT DEFAULT 'default'
)
RETURNS INTEGER
LANGUAGE sql STABLE AS $$
    SELECT count(*)::INTEGER
    FROM knowledge_edges
    WHERE team_id = p_team
      AND (source_id = p_node_id OR target_id = p_node_id)
      AND (p_edge_type IS NULL OR edge_type = p_edge_type);
$$;

-- Get edges for a node (outgoing + incoming)
CREATE OR REPLACE FUNCTION get_node_edges(
    p_node_id   TEXT,
    p_team      TEXT DEFAULT 'default',
    p_edge_type TEXT DEFAULT NULL,
    p_limit     INT DEFAULT 50
)
RETURNS TABLE (
    source_id       TEXT,
    target_id       TEXT,
    edge_type       TEXT,
    confidence      FLOAT,
    discovered_at   TIMESTAMPTZ,
    discovered_by   TEXT
)
LANGUAGE sql STABLE AS $$
    SELECT e.source_id, e.target_id, e.edge_type, e.confidence, e.discovered_at, e.discovered_by
    FROM knowledge_edges e
    WHERE e.team_id = p_team
      AND (e.source_id = p_node_id OR e.target_id = p_node_id)
      AND (p_edge_type IS NULL OR e.edge_type = p_edge_type)
    ORDER BY e.confidence DESC
    LIMIT p_limit;
$$;

-- Find cluster containing a ticket
CREATE OR REPLACE FUNCTION find_ticket_cluster(
    p_ticket_id TEXT,
    p_team      TEXT DEFAULT 'default'
)
RETURNS TABLE (
    cluster_id      TEXT,
    root_cause      TEXT,
    primary_module  TEXT,
    ticket_ids      JSONB,
    status          TEXT
)
LANGUAGE sql STABLE AS $$
    SELECT c.cluster_id, c.root_cause, c.primary_module, c.ticket_ids, c.status
    FROM resolution_clusters c
    WHERE c.team_id = p_team
      AND c.ticket_ids @> to_jsonb(p_ticket_id);
$$;

-- ---------------------------------------------------------------------------
-- 12. v_backlog_graph — Backlog view with knowledge graph edge counts
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_backlog_graph AS
SELECT t.*,
    CASE t.severity
        WHEN 'critical' THEN 1
        WHEN 'high'     THEN 2
        WHEN 'medium'   THEN 3
        WHEN 'low'      THEN 4
    END AS severity_rank,
    COALESCE(ec.edge_count, 0) AS edge_count
FROM swe_tickets t
LEFT JOIN (
    SELECT source_id AS node_id, count(*) AS edge_count FROM knowledge_edges GROUP BY source_id
    UNION ALL
    SELECT target_id AS node_id, count(*) AS edge_count FROM knowledge_edges GROUP BY target_id
) ec ON ec.node_id = t.ticket_id
WHERE t.status NOT IN ('resolved','closed','acknowledged')
ORDER BY severity_rank, edge_count DESC, t.created_at;
