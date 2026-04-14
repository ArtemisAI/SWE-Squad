#!/usr/bin/env python3
"""
Supabase Migration Toolkit — SWE-Squad
=======================================

Handles three operations for migrating from Supabase Cloud to self-hosted:

  1. schema   — Apply full DDL (tables, indexes, RPCs, views, RLS) to a new instance
  2. backup   — Export all table data from a Supabase instance via PostgREST
  3. restore  — Import previously backed-up data into a new Supabase instance

Usage:
  # Apply schema to new self-hosted instance (via psql)
  python3 scripts/ops/migrate_supabase.py schema --db-url "postgresql://postgres:PASSWORD@HOST:5432/postgres"

  # Backup all data from cloud (or any instance) via PostgREST API
  python3 scripts/ops/migrate_supabase.py backup \
      --supabase-url "https://xyz.supabase.co" \
      --supabase-key "eyJ..." \
      --output-dir backups/supabase-20260331

  # Restore backed-up data into new instance via PostgREST API
  python3 scripts/ops/migrate_supabase.py restore \
      --supabase-url "http://NEW_HOST:8000" \
      --supabase-key "NEW_ANON_KEY" \
      --input-dir backups/supabase-20260331

  # Validate new instance: health, schema, CRUD, pgvector
  python3 scripts/ops/migrate_supabase.py verify \
      --supabase-url "http://NEW_HOST:8000" \
      --supabase-key "NEW_ANON_KEY"

Environment variables (fallback if flags not provided):
  SUPABASE_URL, SUPABASE_ANON_KEY, DATABASE_URL
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Tables in dependency order (foreign keys respected)
TABLES_ORDERED = [
    "swe_tickets",
    "swe_ticket_events",
    "code_modules",
    "knowledge_edges",
    "resolution_clusters",
    "pr_nodes",
    "swe_audit_trail",
    "swe_cost_events",
    "swe_budget_policies",
]

SQL_FILES_ORDERED = [
    PROJECT_ROOT / "scripts" / "ops" / "supabase_schema.sql",
    PROJECT_ROOT / "scripts" / "ops" / "migrations" / "002_atomic_checkout.sql",
    PROJECT_ROOT / "scripts" / "ops" / "migrations" / "003_audit_trail.sql",
    PROJECT_ROOT / "scripts" / "ops" / "migrations" / "004_cost_tracking.sql",
]


# ---------------------------------------------------------------------------
# PostgREST helpers
# ---------------------------------------------------------------------------

def _postgrest_request(
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    data: bytes | None = None,
    extra_headers: dict | None = None,
    params: dict | None = None,
) -> tuple[int, bytes]:
    """Make a PostgREST API request. Returns (status_code, body)."""
    url = f"{base_url.rstrip('/')}/rest/v1{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"

    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _postgrest_get_all(base_url: str, api_key: str, table: str) -> list[dict]:
    """Fetch all rows from a table, paginating in batches of 1000."""
    all_rows: list[dict] = []
    offset = 0
    batch_size = 1000
    while True:
        params = {"select": "*", "limit": str(batch_size), "offset": str(offset)}
        status, body = _postgrest_request(base_url, api_key, "GET", f"/{table}", params=params)
        if status != 200:
            err_msg = body.decode("utf-8", errors="replace")
            print(f"  ERROR fetching {table} (HTTP {status}): {err_msg[:200]}")
            return all_rows
        rows = json.loads(body)
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < batch_size:
            break
        offset += batch_size
    return all_rows


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_schema(args: argparse.Namespace) -> int:
    """Apply the full schema to a PostgreSQL database via psql."""
    db_url = args.db_url or os.environ.get("DATABASE_URL")

    # Concatenate all SQL files in order
    combined_sql = []
    for sql_file in SQL_FILES_ORDERED:
        if not sql_file.exists():
            print(f"WARNING: {sql_file} not found, skipping")
            continue
        combined_sql.append(f"-- === {sql_file.name} ===")
        combined_sql.append(sql_file.read_text())
        combined_sql.append("")

    full_sql = "\n".join(combined_sql)

    if args.dry_run:
        print("=== DRY RUN: SQL that would be executed ===")
        print(full_sql)
        return 0

    if not db_url:
        print("ERROR: --db-url or DATABASE_URL required")
        return 1

    # Write to temp file and execute via psql
    tmp_file = PROJECT_ROOT / "backups" / "_migration_combined.sql"
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file.write_text(full_sql)

    print(f"Applying {len(SQL_FILES_ORDERED)} SQL files to {db_url.split('@')[1] if '@' in db_url else db_url}...")
    try:
        result = subprocess.run(
            ["psql", db_url, "-f", str(tmp_file), "--set", "ON_ERROR_STOP=on"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"PSQL STDERR:\n{result.stderr}")
            print(f"PSQL STDOUT:\n{result.stdout}")
            return 1
        print("Schema applied successfully.")
        print(result.stdout[-500:] if len(result.stdout) > 500 else result.stdout)
        return 0
    except FileNotFoundError:
        print("ERROR: psql not found. Install PostgreSQL client tools.")
        print(f"  Alternatively, run the SQL manually:\n  psql $DATABASE_URL -f {tmp_file}")
        return 1
    finally:
        tmp_file.unlink(missing_ok=True)


def cmd_backup(args: argparse.Namespace) -> int:
    """Export all table data from a Supabase instance via PostgREST."""
    base_url = args.supabase_url or os.environ.get("SUPABASE_URL")
    api_key = args.supabase_key or os.environ.get("SUPABASE_ANON_KEY")
    if not base_url or not api_key:
        print("ERROR: --supabase-url and --supabase-key required (or env vars)")
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "backup_time": datetime.now(timezone.utc).isoformat(),
        "source_url": base_url,
        "tables": {},
    }

    total_rows = 0
    errors = 0
    for table in TABLES_ORDERED:
        print(f"  Backing up {table}...", end=" ", flush=True)
        rows = _postgrest_get_all(base_url, api_key, table)
        out_file = output_dir / f"{table}.json"
        out_file.write_text(json.dumps(rows, indent=2, default=str))
        manifest["tables"][table] = {"rows": len(rows), "file": out_file.name}
        total_rows += len(rows)
        if rows:
            print(f"{len(rows)} rows")
        else:
            print("0 rows (or error)")
            errors += 1

    manifest_file = output_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2))
    print(f"\nBackup complete: {total_rows} total rows across {len(TABLES_ORDERED)} tables")
    print(f"Manifest: {manifest_file}")
    if errors:
        print(f"WARNING: {errors} table(s) returned 0 rows (check for API errors above)")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Import backed-up data into a new Supabase instance via PostgREST."""
    base_url = args.supabase_url or os.environ.get("SUPABASE_URL")
    api_key = args.supabase_key or os.environ.get("SUPABASE_ANON_KEY")
    if not base_url or not api_key:
        print("ERROR: --supabase-url and --supabase-key required (or env vars)")
        return 1

    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        print(f"ERROR: {input_dir} does not exist")
        return 1

    total_inserted = 0
    errors = 0
    for table in TABLES_ORDERED:
        data_file = input_dir / f"{table}.json"
        if not data_file.exists():
            print(f"  SKIP {table} — no backup file")
            continue

        rows = json.loads(data_file.read_text())
        if not rows:
            print(f"  SKIP {table} — 0 rows")
            continue

        print(f"  Restoring {table} ({len(rows)} rows)...", end=" ", flush=True)

        # Insert in batches of 100
        batch_size = 100
        inserted = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            body = json.dumps(batch, default=str).encode("utf-8")
            # Use upsert (Prefer: resolution=merge-duplicates) to handle re-runs
            status, resp_body = _postgrest_request(
                base_url, api_key, "POST", f"/{table}",
                data=body,
                extra_headers={"Prefer": "return=minimal,resolution=merge-duplicates"},
            )
            if status in (200, 201):
                inserted += len(batch)
            else:
                err = resp_body.decode("utf-8", errors="replace")[:200]
                print(f"\n    BATCH ERROR (HTTP {status}): {err}")
                errors += 1

        total_inserted += inserted
        print(f"{inserted} inserted")

    print(f"\nRestore complete: {total_inserted} total rows inserted")
    if errors:
        print(f"WARNING: {errors} batch error(s) — check output above")
    return 0 if errors == 0 else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Validate a Supabase instance: health, tables, CRUD, pgvector."""
    base_url = args.supabase_url or os.environ.get("SUPABASE_URL")
    api_key = args.supabase_key or os.environ.get("SUPABASE_ANON_KEY")
    if not base_url or not api_key:
        print("ERROR: --supabase-url and --supabase-key required (or env vars)")
        return 1

    checks_passed = 0
    checks_failed = 0

    def check(name: str, passed: bool, detail: str = ""):
        nonlocal checks_passed, checks_failed
        status = "PASS" if passed else "FAIL"
        msg = f"  [{status}] {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        if passed:
            checks_passed += 1
        else:
            checks_failed += 1

    print(f"Verifying {base_url}...\n")

    # 1. Health check
    try:
        health_req = urllib.request.Request(f"{base_url.rstrip('/')}/rest/v1/", headers={
            "apikey": api_key, "Authorization": f"Bearer {api_key}",
        })
        with urllib.request.urlopen(health_req, timeout=10) as resp:
            check("PostgREST health", resp.status == 200, f"HTTP {resp.status}")
    except Exception as e:
        check("PostgREST health", False, str(e))

    # 2. Table existence
    for table in TABLES_ORDERED:
        status, body = _postgrest_request(base_url, api_key, "GET", f"/{table}", params={"limit": "1"})
        check(f"Table '{table}' exists", status == 200, f"HTTP {status}")

    # 3. CRUD test — insert + read + delete a test ticket
    test_id = f"__migration_test_{int(datetime.now(timezone.utc).timestamp())}"
    test_row = {
        "ticket_id": test_id,
        "team_id": "__test__",
        "title": "Migration verification test",
        "description": "Auto-created by migrate_supabase.py verify — safe to delete",
        "severity": "low",
        "status": "open",
    }
    status, _ = _postgrest_request(
        base_url, api_key, "POST", "/swe_tickets",
        data=json.dumps(test_row).encode("utf-8"),
    )
    check("CRUD: INSERT test ticket", status in (200, 201), f"HTTP {status}")

    # Read it back
    status, body = _postgrest_request(
        base_url, api_key, "GET", "/swe_tickets",
        params={"ticket_id": f"eq.{test_id}", "select": "ticket_id,title"},
    )
    if status == 200:
        rows = json.loads(body)
        check("CRUD: SELECT test ticket", len(rows) == 1, f"got {len(rows)} row(s)")
    else:
        check("CRUD: SELECT test ticket", False, f"HTTP {status}")

    # Delete it
    status, _ = _postgrest_request(
        base_url, api_key, "DELETE", "/swe_tickets",
        params={"ticket_id": f"eq.{test_id}"},
    )
    check("CRUD: DELETE test ticket", status in (200, 204), f"HTTP {status}")

    # 4. RPC: match_similar_tickets (verifies pgvector extension)
    rpc_body = json.dumps({
        "team": "__test__",
        "match_count": 1,
        "query_embedding": [0.0] * 1024,
    }).encode("utf-8")
    status, body = _postgrest_request(base_url, api_key, "POST", "/rpc/match_similar_tickets", data=rpc_body)
    check("RPC: match_similar_tickets (pgvector)", status == 200, f"HTTP {status}")

    # 5. RPC: atomic_checkout
    import uuid
    rpc_body = json.dumps({
        "p_ticket_id": "__nonexistent__",
        "p_run_id": str(uuid.uuid4()),
        "p_locked_by": "__test__",
    }).encode("utf-8")
    status, _ = _postgrest_request(base_url, api_key, "POST", "/rpc/atomic_checkout", data=rpc_body)
    check("RPC: atomic_checkout", status == 200, f"HTTP {status}")

    # 6. RPC: get_daily_spend_cents
    rpc_body = json.dumps({"p_team_id": "__test__"}).encode("utf-8")
    status, _ = _postgrest_request(base_url, api_key, "POST", "/rpc/get_daily_spend_cents", data=rpc_body)
    check("RPC: get_daily_spend_cents", status == 200, f"HTTP {status}")

    # 7. Views
    for view in ["v_backlog", "v_stability", "v_queue_critical"]:
        status, _ = _postgrest_request(base_url, api_key, "GET", f"/{view}", params={"limit": "1"})
        check(f"View '{view}' accessible", status == 200, f"HTTP {status}")

    print(f"\n{'='*50}")
    print(f"Results: {checks_passed} passed, {checks_failed} failed")
    return 0 if checks_failed == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="SWE-Squad Supabase Migration Toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # schema
    p_schema = sub.add_parser("schema", help="Apply full DDL to a PostgreSQL database via psql")
    p_schema.add_argument("--db-url", help="PostgreSQL connection string (or DATABASE_URL env)")
    p_schema.add_argument("--dry-run", action="store_true", help="Print SQL without executing")

    # backup
    p_backup = sub.add_parser("backup", help="Export all data from a Supabase instance")
    p_backup.add_argument("--supabase-url", help="Supabase REST URL")
    p_backup.add_argument("--supabase-key", help="Supabase anon/service key")
    p_backup.add_argument("--output-dir", default=f"backups/supabase-{datetime.now().strftime('%Y%m%d')}")

    # restore
    p_restore = sub.add_parser("restore", help="Import backed-up data into a new Supabase instance")
    p_restore.add_argument("--supabase-url", help="Target Supabase REST URL")
    p_restore.add_argument("--supabase-key", help="Target Supabase anon/service key")
    p_restore.add_argument("--input-dir", required=True, help="Directory with backup JSON files")

    # verify
    p_verify = sub.add_parser("verify", help="Validate new instance: health, schema, CRUD, pgvector")
    p_verify.add_argument("--supabase-url", help="Supabase REST URL to verify")
    p_verify.add_argument("--supabase-key", help="Supabase anon/service key")

    args = parser.parse_args()
    commands = {
        "schema": cmd_schema,
        "backup": cmd_backup,
        "restore": cmd_restore,
        "verify": cmd_verify,
    }
    sys.exit(commands[args.command](args))


if __name__ == "__main__":
    main()
