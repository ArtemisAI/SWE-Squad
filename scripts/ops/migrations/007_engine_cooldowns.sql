-- Per-engine cooldown lifecycle state (cross-VM coordination + visibility)
CREATE TABLE IF NOT EXISTS engine_cooldowns (
    team_id TEXT NOT NULL,
    engine_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'healthy'
        CHECK (status IN ('healthy', 'rate_limited', 'monthly_exhausted', 'down', 'recovering')),
    cooldown_until TIMESTAMPTZ,
    reset_at TIMESTAMPTZ,
    next_probe_at TIMESTAMPTZ,
    last_error TEXT DEFAULT '',
    fallback_engine TEXT DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (team_id, engine_name)
);

CREATE INDEX IF NOT EXISTS idx_engine_cooldowns_team_status
    ON engine_cooldowns(team_id, status);

CREATE INDEX IF NOT EXISTS idx_engine_cooldowns_probe
    ON engine_cooldowns(next_probe_at);
