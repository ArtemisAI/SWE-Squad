-- Teams stored in database (not just YAML)
-- Enables: add/remove teams from UI, per-account team scoping, scaling

CREATE TABLE IF NOT EXISTS teams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,                    -- "alpha", "beta", "gamma"
    display_name TEXT DEFAULT '',          -- "SWE-Squad Alpha"
    vm_address TEXT DEFAULT '',            -- SSH address or hostname
    github_account TEXT NOT NULL,          -- "swe-squad-alpha"
    role TEXT DEFAULT 'developer' CHECK (role IN ('full', 'developer', 'investigator', 'reviewer')),
    engine TEXT DEFAULT 'claude',          -- default coding engine
    tier TEXT DEFAULT 'standard' CHECK (tier IN ('senior', 'standard', 'economy')),
    max_concurrent INTEGER DEFAULT 5,
    cost_budget_daily NUMERIC(10,2) DEFAULT 50.00,
    specializations TEXT[] DEFAULT '{}',   -- array of specialization tags
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'paused', 'stopped', 'error')),
    config_json JSONB DEFAULT '{}',       -- additional config (flexible)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(account_id, name)
);

CREATE INDEX IF NOT EXISTS idx_teams_account ON teams(account_id);
CREATE INDEX IF NOT EXISTS idx_teams_github ON teams(github_account);

-- Engine installations per team/instance
CREATE TABLE IF NOT EXISTS team_engines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    engine_name TEXT NOT NULL,
    binary_path TEXT DEFAULT '',
    installed BOOLEAN DEFAULT false,
    health_status TEXT DEFAULT 'unknown' CHECK (health_status IN ('healthy', 'unhealthy', 'unknown', 'installing')),
    api_key_secret_id UUID REFERENCES account_secrets(id),
    config_json JSONB DEFAULT '{}',
    last_health_check TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(team_id, engine_name)
);

CREATE INDEX IF NOT EXISTS idx_team_engines_team ON team_engines(team_id);
