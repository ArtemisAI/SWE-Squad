-- Migration 003: Structured audit trail for agent decisions
-- Issue: #350
--
-- Creates an immutable audit trail table recording every mutating action
-- with actor, action type, timestamp, and structured context.
-- Design: append-only log of every mutating action for compliance + debugging.

CREATE TABLE IF NOT EXISTS swe_audit_trail (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id TEXT NOT NULL,
    ticket_id TEXT,
    actor TEXT NOT NULL,  -- 'team-alpha', 'orchestrator', 'human:username'
    action TEXT NOT NULL,  -- 'triage', 'investigate_start', 'investigate_complete',
                           -- 'develop_start', 'develop_complete', 'pr_created',
                           -- 'deploy', 'resolve', 'fail', 'escalate',
                           -- 'cb_reset', 'config_change'
    details JSONB DEFAULT '{}',
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_team ON swe_audit_trail(team_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_ticket ON swe_audit_trail(ticket_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON swe_audit_trail(action, timestamp DESC);
