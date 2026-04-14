-- Migration 004: Per-agent cost tracking with budget hard-stops
-- Issue: #347
--
-- Adds cost_events and budget_policies tables for dollar-denominated
-- spend tracking with soft-alert thresholds and hard-stop auto-pause.

-- Cost events — one row per LLM call
CREATE TABLE IF NOT EXISTS swe_cost_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id TEXT NOT NULL,
    ticket_id TEXT,
    model TEXT NOT NULL,
    input_tokens INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    cost_cents NUMERIC(10,4) NOT NULL DEFAULT 0,
    operation TEXT NOT NULL,  -- 'investigate', 'develop', 'triage', 'embed'
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cost_team ON swe_cost_events(team_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_cost_ticket ON swe_cost_events(ticket_id);
CREATE INDEX IF NOT EXISTS idx_cost_operation ON swe_cost_events(team_id, operation, timestamp DESC);

-- Budget policies — one row per team
CREATE TABLE IF NOT EXISTS swe_budget_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id TEXT NOT NULL UNIQUE,
    daily_budget_cents INT NOT NULL DEFAULT 5000,    -- $50/day default
    monthly_budget_cents INT NOT NULL DEFAULT 100000, -- $1000/month default
    alert_threshold_percent INT NOT NULL DEFAULT 80,
    hard_stop_enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_budget_team ON swe_budget_policies(team_id);

-- RPC: get daily spend in cents for a team
CREATE OR REPLACE FUNCTION get_daily_spend_cents(
    p_team_id TEXT,
    p_date DATE DEFAULT CURRENT_DATE
) RETURNS NUMERIC AS $$
BEGIN
    RETURN COALESCE(
        (SELECT SUM(cost_cents)
         FROM swe_cost_events
         WHERE team_id = p_team_id
           AND DATE(timestamp AT TIME ZONE 'UTC') = p_date),
        0
    );
END;
$$ LANGUAGE plpgsql;

-- RPC: get monthly spend in cents for a team
CREATE OR REPLACE FUNCTION get_monthly_spend_cents(
    p_team_id TEXT,
    p_year INT DEFAULT EXTRACT(YEAR FROM NOW())::INT,
    p_month INT DEFAULT EXTRACT(MONTH FROM NOW())::INT
) RETURNS NUMERIC AS $$
BEGIN
    RETURN COALESCE(
        (SELECT SUM(cost_cents)
         FROM swe_cost_events
         WHERE team_id = p_team_id
           AND EXTRACT(YEAR FROM timestamp AT TIME ZONE 'UTC') = p_year
           AND EXTRACT(MONTH FROM timestamp AT TIME ZONE 'UTC') = p_month),
        0
    );
END;
$$ LANGUAGE plpgsql;
