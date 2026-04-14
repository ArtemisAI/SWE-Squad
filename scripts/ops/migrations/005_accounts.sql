-- Account isolation schema (Phase 1)
-- Enables multi-tenancy: each user belongs to one or more accounts,
-- and all data (tickets, projects, secrets) is scoped to an account.

-- Core account entity
CREATE TABLE IF NOT EXISTS accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,  -- URL-friendly identifier
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deleted')),
    created_by TEXT NOT NULL,  -- github_login of founder
    plan TEXT DEFAULT 'free' CHECK (plan IN ('free', 'pro', 'enterprise')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- User-to-account membership
CREATE TABLE IF NOT EXISTS account_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    github_login TEXT NOT NULL,
    role TEXT DEFAULT 'developer' CHECK (role IN ('owner', 'admin', 'developer', 'viewer')),
    invited_by TEXT,
    joined_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(account_id, github_login)
);

-- Account-scoped secrets
CREATE TABLE IF NOT EXISTS account_secrets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    encrypted_value TEXT NOT NULL,
    created_by TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(account_id, name)
);

-- Account budget policies
CREATE TABLE IF NOT EXISTS account_budgets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    monthly_budget_cents BIGINT DEFAULT 0,
    alert_threshold_pct INTEGER DEFAULT 80,
    enforce_hard_limit BOOLEAN DEFAULT false,
    current_spend_cents BIGINT DEFAULT 0,
    period_start TIMESTAMPTZ DEFAULT date_trunc('month', NOW()),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_account_members_login ON account_members(github_login);
CREATE INDEX IF NOT EXISTS idx_account_members_account ON account_members(account_id);
CREATE INDEX IF NOT EXISTS idx_account_secrets_account ON account_secrets(account_id);

-- Add account_id to swe_tickets (nullable for migration — existing tickets get default)
DO $$ BEGIN
    ALTER TABLE swe_tickets ADD COLUMN IF NOT EXISTS account_id UUID REFERENCES accounts(id);
EXCEPTION WHEN others THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_tickets_account ON swe_tickets(account_id, team_id);
