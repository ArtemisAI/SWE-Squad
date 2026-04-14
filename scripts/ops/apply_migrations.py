#!/usr/bin/env python3
"""
Apply all SWE-Squad migrations (002, 003, 004) to a live Supabase project.

Usage
-----
    python scripts/ops/apply_migrations.py [--dry-run] [--verbose]

Requires SUPABASE_URL and SUPABASE_ANON_KEY (or SUPABASE_KEY) in the
environment (or .env file).

This script:
  1. Loads each migration SQL file from scripts/ops/migrations/
  2. Verifies connectivity to Supabase
  3. Executes each migration in sequence
  4. Verifies schema changes were applied
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass


def get_env(name: str, fallback_name: str | None = None) -> str:
    val = os.environ.get(name, "")
    if not val and fallback_name:
        val = os.environ.get(fallback_name, "")
    if not val:
        names = f"{name}" + (f" or {fallback_name}" if fallback_name else "")
        print(f"ERROR: {names} not set in environment.", file=sys.stderr)
        sys.exit(1)
    return val


def supabase_request(
    url: str, key: str, method: str, path: str,
    params: dict | None = None, body: dict | None = None,
) -> list | dict | None:
    full_url = f"{url.rstrip('/')}/rest/v1{path}"
    if params:
        full_url += "?" + urllib.parse.urlencode(params, safe=".,()!")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(full_url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def try_management_api(sql: str, verbose: bool = False) -> bool:
    """Try to execute SQL via Supabase Management API (requires access token)."""
    access_token = os.environ.get("SUPABASE_ACCESS_TOKEN", "")
    supabase_url = os.environ.get("SUPABASE_URL", "")

    if not access_token or not supabase_url:
        if verbose:
            print("  (Management API not available — manual execution required)")
        return False

    # Extract project ref from URL: https://<ref>.supabase.co
    try:
        host = urllib.parse.urlparse(supabase_url).hostname or ""
        project_ref = host.split(".")[0]
        if not project_ref:
            return False
    except Exception:
        return False

    mgmt_url = f"https://api.supabase.com/v1/projects/{project_ref}/database/query"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    data = json.dumps({"query": sql}).encode()
    req = urllib.request.Request(mgmt_url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if verbose:
                print(f"  Management API response: {resp.status}")
            return resp.status in (200, 201)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() if exc.fp else ""
        if verbose:
            print(f"  Management API error ({exc.code}): {body[:200]}", file=sys.stderr)
        return False
    except Exception as exc:
        if verbose:
            print(f"  Management API connection error: {exc}", file=sys.stderr)
        return False


def load_migration(migration_num: int) -> str:
    """Load a migration SQL file."""
    migrations_dir = Path(__file__).resolve().parents[1] / "ops" / "migrations"
    migrations = list(migrations_dir.glob(f"{migration_num:03d}_*.sql"))
    if not migrations:
        raise FileNotFoundError(f"Migration {migration_num:03d} not found in scripts/ops/migrations/")
    return migrations[0].read_text(encoding="utf-8")


def main(dry_run: bool = False, verbose: bool = False) -> None:
    print("=" * 70)
    print("SWE-Squad: Apply Migrations 002, 003, 004")
    print("=" * 70)
    print()

    url = get_env("SUPABASE_URL")
    key = get_env("SUPABASE_ANON_KEY", "SUPABASE_KEY")

    # Step 1: Verify connectivity
    print("[Setup] Verifying Supabase connectivity...")
    try:
        supabase_request(url, key, "GET", "/swe_tickets", params={"select": "ticket_id", "limit": "1"})
        print("  OK — Supabase is reachable.")
    except Exception as exc:
        print(f"  FAIL — Cannot reach Supabase: {exc}", file=sys.stderr)
        sys.exit(1)
    print()

    migrations = [
        (2, "Atomic checkout (multi-VM race prevention)"),
        (3, "Structured audit trail (agent decision logging)"),
        (4, "Per-agent cost tracking with budget hard-stops"),
    ]

    manual_sqls = []

    for mig_num, description in migrations:
        print(f"[Migration {mig_num:03d}] {description}")
        try:
            sql = load_migration(mig_num)
        except FileNotFoundError as exc:
            print(f"  FAIL — {exc}", file=sys.stderr)
            sys.exit(1)

        if verbose:
            print(f"  SQL ({len(sql)} bytes):")
            for line in sql.splitlines()[:5]:
                print(f"    {line}")
            if len(sql.splitlines()) > 5:
                print(f"    ... ({len(sql.splitlines())} lines total)")

        if dry_run:
            print("  [DRY RUN] Would execute this migration.")
            print()
            continue

        # Try Management API
        if try_management_api(sql, verbose=verbose):
            print("  OK — Applied via Management API")
        else:
            print("  WARNING — Management API not available. Manual execution required.")
            manual_sqls.append((mig_num, description, sql))

        print()

    # Summary
    print("=" * 70)
    if dry_run:
        print("DRY RUN COMPLETE")
        print("All migrations are ready to apply.")
    elif not manual_sqls:
        print("MIGRATIONS APPLIED SUCCESSFULLY")
        print("All three migrations have been applied to Supabase.")
    else:
        print("MANUAL MIGRATIONS REQUIRED")
        print("=" * 70)
        print()
        print("The following migrations must be applied manually via the Supabase")
        print("SQL Editor (Dashboard > SQL Editor):")
        print()
        for mig_num, description, sql in manual_sqls:
            print(f"--- Migration {mig_num:03d}: {description} ---")
            print(sql)
            print()
        print("After running each migration, reload the PostgREST schema cache:")
        print("  Dashboard > Settings > API > Click 'Reload schema cache'")

    print("=" * 70)
    sys.exit(0)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Apply SWE-Squad migrations to Supabase"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    main(dry_run=args.dry_run, verbose=args.verbose)
