-- ============================================================================
-- 008_memory_service.sql
--
-- Multi-tenant memory service for SWE-Squad swarm agents.
-- Supports: tenant isolation (RLS), per-project scoping, vector similarity
-- search, ACL, and TTL-based expiry.
--
-- Requires: pgvector extension (CREATE EXTENSION IF NOT EXISTS vector;)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. swarm_memory table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS swarm_memory (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  agent_id TEXT,
  engine TEXT,
  type TEXT NOT NULL CHECK (type IN (
    'investigation', 'fix_pattern', 'root_cause', 'knowledge', 'config'
  )),
  content TEXT NOT NULL,
  embedding vector(1024),
  tags TEXT[] DEFAULT '{}',
  confidence NUMERIC(3,1) DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 2.0),
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Tenant isolation index (ALL queries MUST filter by tenant_id)
CREATE INDEX IF NOT EXISTS idx_swarm_memory_tenant
  ON swarm_memory(tenant_id);

-- Composite indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_swarm_memory_tenant_project
  ON swarm_memory(tenant_id, project_id);

CREATE INDEX IF NOT EXISTS idx_swarm_memory_tenant_type
  ON swarm_memory(tenant_id, type);

CREATE INDEX IF NOT EXISTS idx_swarm_memory_tenant_project_type
  ON swarm_memory(tenant_id, project_id, type);

-- GIN index for tag array containment queries
CREATE INDEX IF NOT EXISTS idx_swarm_memory_tags
  ON swarm_memory USING GIN(tags);

-- Expiry index for TTL pruning
CREATE INDEX IF NOT EXISTS idx_swarm_memory_expires
  ON swarm_memory(expires_at)
  WHERE expires_at IS NOT NULL;

-- Vector similarity index (IVFFlat, scoped to tenant via partial index not possible,
-- but RLS + tenant_id filter ensures only tenant rows are scanned).
CREATE INDEX IF NOT EXISTS idx_swarm_memory_embedding
  ON swarm_memory USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);

-- ---------------------------------------------------------------------------
-- 2. Row-Level Security (RLS) — tenant isolation at database level
-- ---------------------------------------------------------------------------

ALTER TABLE swarm_memory ENABLE ROW LEVEL SECURITY;

-- Drop existing policy if re-running migration
DROP POLICY IF EXISTS swarm_memory_tenant_isolation ON swarm_memory;

CREATE POLICY swarm_memory_tenant_isolation ON swarm_memory
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- ---------------------------------------------------------------------------
-- 3. swarm_memory_acl table — per-agent, per-project access control
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS swarm_memory_acl (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  can_read BOOLEAN DEFAULT true,
  can_write BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(tenant_id, project_id, agent_id)
);

ALTER TABLE swarm_memory_acl ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS swarm_memory_acl_tenant ON swarm_memory_acl;

CREATE POLICY swarm_memory_acl_tenant ON swarm_memory_acl
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE INDEX IF NOT EXISTS idx_swarm_memory_acl_lookup
  ON swarm_memory_acl(tenant_id, project_id, agent_id);

-- ---------------------------------------------------------------------------
-- 4. Vector similarity search RPC
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION match_swarm_memory(
  query_embedding vector(1024),
  query_tenant_id TEXT,
  match_count INT DEFAULT 10,
  similarity_floor FLOAT DEFAULT 0.75,
  max_age_days INT DEFAULT 180,
  query_project_id TEXT DEFAULT NULL
)
RETURNS TABLE (
  id UUID,
  tenant_id TEXT,
  project_id TEXT,
  agent_id TEXT,
  engine TEXT,
  type TEXT,
  content TEXT,
  embedding vector(1024),
  tags TEXT[],
  confidence NUMERIC(3,1),
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ,
  similarity FLOAT
)
LANGUAGE sql STABLE
AS $$
  SELECT
    m.id,
    m.tenant_id,
    m.project_id,
    m.agent_id,
    m.engine,
    m.type,
    m.content,
    m.embedding,
    m.tags,
    m.confidence,
    m.expires_at,
    m.created_at,
    m.updated_at,
    (1 - (m.embedding <=> query_embedding)) * (m.confidence / 1.0) AS similarity
  FROM swarm_memory m
  WHERE
    m.tenant_id = query_tenant_id
    AND m.embedding IS NOT NULL
    AND m.created_at >= NOW() - (max_age_days || ' days')::INTERVAL
    AND (m.expires_at IS NULL OR m.expires_at > NOW())
    AND (query_project_id IS NULL OR m.project_id = query_project_id)
    AND (1 - (m.embedding <=> query_embedding)) >= similarity_floor
  ORDER BY similarity DESC
  LIMIT match_count;
$$;

-- ---------------------------------------------------------------------------
-- 5. Prune expired entries RPC
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION prune_expired_swarm_memory(
  query_tenant_id TEXT
)
RETURNS TABLE (deleted_count BIGINT)
LANGUAGE sql VOLATILE
AS $$
  WITH deleted AS (
    DELETE FROM swarm_memory
    WHERE tenant_id = query_tenant_id
      AND expires_at IS NOT NULL
      AND expires_at < NOW()
    RETURNING id
  )
  SELECT COUNT(*) AS deleted_count FROM deleted;
$$;

-- ---------------------------------------------------------------------------
-- 6. Updated_at trigger
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION update_swarm_memory_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_swarm_memory_updated_at ON swarm_memory;

CREATE TRIGGER trg_swarm_memory_updated_at
  BEFORE UPDATE ON swarm_memory
  FOR EACH ROW
  EXECUTE FUNCTION update_swarm_memory_updated_at();
