# SWE-Squad Supabase Migrations

This directory contains SQL migrations that enhance the SWE-Squad Supabase schema with advanced features.

## Migrations

### Migration 002: Atomic Checkout
**File:** `002_atomic_checkout.sql`
**Issue:** #355
**Purpose:** Prevents duplicate work across multiple VMs by providing atomic ticket claiming with expiry.

**What it adds:**
- Checkout fields to `swe_tickets` table: `checkout_run_id`, `checkout_locked_at`, `checkout_locked_by`, `checkout_expires_at`
- Index for efficient checkout queries
- RPC functions for atomic operations:
  - `atomic_checkout()` — claim a ticket (idempotent, fails if already locked)
  - `release_checkout()` — unlock a ticket (only the holder can release)
  - `checkout_heartbeat()` — extend lock duration
  - `force_release_checkout()` — admin override to unlock a ticket
  - `cleanup_expired_checkouts()` — clean up stale locks

**Schema additions:**
- 4 new columns on `swe_tickets`
- 1 index on `(status, checkout_run_id)`
- 5 new RPCs

---

### Migration 003: Structured Audit Trail
**File:** `003_audit_trail.sql`
**Issue:** #350
**Purpose:** Creates an immutable log of all agent decisions for compliance, debugging, and analytics.

**What it adds:**
- New table `swe_audit_trail` with columns:
  - `id` (UUID primary key)
  - `team_id` (TEXT, scoping identifier)
  - `ticket_id` (TEXT, optional reference)
  - `actor` (TEXT, who/what triggered the action: 'team-alpha', 'orchestrator', 'human:username')
  - `action` (TEXT, what happened: 'triage', 'investigate_start', 'investigate_complete', 'develop_start', etc.)
  - `details` (JSONB, structured context)
  - `timestamp` (TIMESTAMPTZ, when it happened)
  - `created_at` (TIMESTAMPTZ, server timestamp)

- 3 indexes for efficient querying:
  - `idx_audit_team` on `(team_id, timestamp DESC)` — find all actions by a team
  - `idx_audit_ticket` on `(ticket_id, timestamp DESC)` — find all actions on a ticket
  - `idx_audit_action` on `(action, timestamp DESC)` — find all actions of a type

**Example entries:**
```json
{
  "team_id": "team-1",
  "actor": "team-alpha",
  "action": "investigate_start",
  "ticket_id": "TKT-001",
  "details": {"model": "sonnet", "severity": "high"},
  "timestamp": "2026-03-30T15:23:45.123Z"
}
```

---

### Migration 004: Per-Agent Cost Tracking
**File:** `004_cost_tracking.sql`
**Issue:** #371, #369
**Purpose:** Tracks LLM costs per agent with soft-alert and hard-stop budget controls.

**What it adds:**
- New table `swe_cost_events` for recording every LLM call:
  - `id` (UUID primary key)
  - `team_id` (TEXT)
  - `ticket_id` (TEXT, optional)
  - `model` (TEXT: 'haiku', 'sonnet', 'opus', etc.)
  - `input_tokens`, `output_tokens` (INT)
  - `cost_cents` (NUMERIC, dollar amount as cents)
  - `operation` (TEXT: 'investigate', 'develop', 'triage', 'embed')
  - `timestamp` (TIMESTAMPTZ)

- New table `swe_budget_policies` for budget configuration:
  - `id` (UUID primary key)
  - `team_id` (TEXT UNIQUE)
  - `daily_budget_cents` (INT, default 5000 = $50/day)
  - `monthly_budget_cents` (INT, default 100000 = $1000/month)
  - `alert_threshold_percent` (INT, default 80)
  - `hard_stop_enabled` (BOOLEAN)
  - `created_at`, `updated_at` (TIMESTAMPTZ)

- 3 indexes for efficient querying:
  - `idx_cost_team` on `(team_id, timestamp DESC)`
  - `idx_cost_ticket` on `(ticket_id)`
  - `idx_cost_operation` on `(team_id, operation, timestamp DESC)`

- 2 RPC functions for cost tracking:
  - `get_daily_spend_cents(p_team_id, p_date)` — returns total spend in cents for a team on a given day
  - `get_monthly_spend_cents(p_team_id, p_year, p_month)` — returns total spend in cents for a team in a given month

---

## How to Apply Migrations

### Option 1: Automated (Recommended)

If you have `SUPABASE_ACCESS_TOKEN` set in your environment:

```bash
cd /path/to/swe-squad
python3 scripts/ops/apply_migrations.py --verbose
```

This will:
1. Verify connectivity to Supabase
2. Execute each migration via the Supabase Management API
3. Report success or print fallback manual instructions

**Dry run (no-op):**
```bash
python3 scripts/ops/apply_migrations.py --dry-run --verbose
```

### Option 2: Manual via Supabase Dashboard

1. Go to your Supabase project: https://supabase.com
2. Navigate to **SQL Editor**
3. Create a new query for each migration:

#### Migration 002:
```sql
-- Paste contents of scripts/ops/migrations/002_atomic_checkout.sql
```

#### Migration 003:
```sql
-- Paste contents of scripts/ops/migrations/003_audit_trail.sql
```

#### Migration 004:
```sql
-- Paste contents of scripts/ops/migrations/004_cost_tracking.sql
```

4. Execute each migration in order (002 → 003 → 004)
5. After each migration, reload the PostgREST schema cache:
   - **Settings > API > Reload schema cache**

### Option 3: Supabase CLI (if installed)

```bash
# Link your Supabase project
supabase link --project-ref YOUR_PROJECT_REF

# Execute migrations
supabase db push scripts/ops/migrations/002_atomic_checkout.sql
supabase db push scripts/ops/migrations/003_audit_trail.sql
supabase db push scripts/ops/migrations/004_cost_tracking.sql
```

---

## Verification

After applying migrations, verify they were applied:

### Check tables exist:
```sql
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('swe_audit_trail', 'swe_cost_events', 'swe_budget_policies');
```

### Check indexes exist:
```sql
SELECT indexname FROM pg_indexes
WHERE tablename IN ('swe_tickets', 'swe_audit_trail', 'swe_cost_events');
```

### Check RPCs exist:
```sql
SELECT routine_name FROM information_schema.routines
WHERE routine_schema = 'public'
  AND routine_name LIKE 'atomic_checkout%'
  OR routine_name LIKE 'checkout_%'
  OR routine_name LIKE 'get_%_spend%';
```

---

## Rollback (if needed)

Migrations use `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`, so they are idempotent and safe to re-run.

To completely rollback:

```sql
-- Drop tables (reverse order)
DROP TABLE IF EXISTS swe_budget_policies CASCADE;
DROP TABLE IF EXISTS swe_cost_events CASCADE;
DROP TABLE IF EXISTS swe_audit_trail CASCADE;

-- Drop columns from swe_tickets
ALTER TABLE swe_tickets
  DROP COLUMN IF EXISTS checkout_run_id,
  DROP COLUMN IF EXISTS checkout_locked_at,
  DROP COLUMN IF EXISTS checkout_locked_by,
  DROP COLUMN IF EXISTS checkout_expires_at;

-- Drop RPCs
DROP FUNCTION IF EXISTS atomic_checkout(TEXT, UUID, TEXT, INT) CASCADE;
DROP FUNCTION IF EXISTS release_checkout(TEXT, UUID) CASCADE;
DROP FUNCTION IF EXISTS checkout_heartbeat(TEXT, UUID, INT) CASCADE;
DROP FUNCTION IF EXISTS force_release_checkout(TEXT) CASCADE;
DROP FUNCTION IF EXISTS cleanup_expired_checkouts() CASCADE;
DROP FUNCTION IF EXISTS get_daily_spend_cents(TEXT, DATE) CASCADE;
DROP FUNCTION IF EXISTS get_monthly_spend_cents(TEXT, INT, INT) CASCADE;
```

---

## Next Steps

After applying migrations:

1. **Test atomic checkout:** Run `python3 -m pytest tests/unit/test_atomic_checkout.py -v`
2. **Verify cost tracking:** Check that `cost_tracker.py` can insert events into `swe_cost_events`
3. **Test audit trail:** Check that audit events are being logged to `swe_audit_trail`
4. **Restart daemon:** Running daemons may need a restart to pick up the new schema:
   ```bash
   # Kill and restart the SWE runner daemon on your deployment host
   kill $(pgrep -f swe_team_runner) 2>/dev/null; sleep 3
   nohup python3 scripts/ops/swe_team_runner.py --daemon --interval 60 >> logs/swe_team.log 2>&1 &
   ```

---

## Notes

- All migrations use PostgreSQL 14+ features (gen_random_uuid, JSONB, etc.)
- Indexes use compound keys for multi-column filters
- RPC functions use standard PL/pgSQL for portability
- All tables include timestamp columns for audit and ordering
- The `IF NOT EXISTS` clauses make migrations idempotent (safe to re-run)

---

**Last updated:** 2026-03-30
**Status:** All three migrations ready for deployment
