#!/usr/bin/env python3
"""Apply the ticket_type migration to a live Supabase project.

Usage
-----
    python scripts/migrations/run_migration.py

Requires SUPABASE_URL and SUPABASE_ANON_KEY (or SUPABASE_KEY) in the
environment (or .env file).

The script uses the Supabase PostgREST API to verify the column exists
after migration.  The actual ALTER TABLE must be run via the Supabase
SQL Editor (Dashboard > SQL Editor) or via the Management API.

This script:
  1. Prints the SQL that needs to be executed.
  2. Verifies connectivity to the Supabase project.
  3. Tests whether the ticket_type column already exists.
  4. If not, attempts to add it via the Supabase Management API
     (requires SUPABASE_ACCESS_TOKEN and project ref).
  5. Falls back to printing manual instructions if the API is unavailable.
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

MIGRATION_SQL = (
    "ALTER TABLE swe_tickets "
    "ADD COLUMN IF NOT EXISTS ticket_type TEXT NOT NULL DEFAULT 'unknown';"
)


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


def column_exists(url: str, key: str) -> bool:
    """Check if ticket_type column is queryable via PostgREST."""
    try:
        supabase_request(
            url, key, "GET", "/swe_tickets",
            params={"select": "ticket_type", "limit": "1"},
        )
        return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() if exc.fp else ""
        if "PGRST204" in body or exc.code == 400:
            return False
        raise


def try_management_api(sql: str) -> bool:
    """Try to execute SQL via Supabase Management API (requires access token)."""
    access_token = os.environ.get("SUPABASE_ACCESS_TOKEN", "")
    supabase_url = os.environ.get("SUPABASE_URL", "")

    if not access_token or not supabase_url:
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
            print(f"  Management API response: {resp.status}")
            return resp.status in (200, 201)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() if exc.fp else ""
        print(f"  Management API error ({exc.code}): {body[:200]}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"  Management API connection error: {exc}", file=sys.stderr)
        return False


def main() -> None:
    print("=" * 60)
    print("SWE-Squad: ticket_type column migration")
    print("=" * 60)
    print()
    print("SQL to execute:")
    print(f"  {MIGRATION_SQL}")
    print()

    url = get_env("SUPABASE_URL")
    key = get_env("SUPABASE_ANON_KEY", "SUPABASE_KEY")

    # Step 1: Check connectivity
    print("[1/3] Checking Supabase connectivity...")
    try:
        supabase_request(url, key, "GET", "/swe_tickets", params={"select": "ticket_id", "limit": "1"})
        print("  OK — Supabase is reachable.")
    except Exception as exc:
        print(f"  FAIL — Cannot reach Supabase: {exc}", file=sys.stderr)
        sys.exit(1)

    # Step 2: Check if column already exists
    print("[2/3] Checking if ticket_type column exists...")
    if column_exists(url, key):
        print("  OK — ticket_type column already exists. No migration needed.")
        sys.exit(0)
    print("  MISSING — ticket_type column not found. Attempting migration...")

    # Step 3: Try Management API
    print("[3/3] Attempting migration via Management API...")
    if try_management_api(MIGRATION_SQL):
        print("  Migration applied successfully via Management API.")
        # Verify
        if column_exists(url, key):
            print("  VERIFIED — ticket_type column is now queryable.")
        else:
            print("  WARNING — API returned success but column not yet visible.")
            print("  PostgREST schema cache may need a few seconds to refresh.")
        sys.exit(0)

    # Fallback: manual instructions
    print()
    print("=" * 60)
    print("MANUAL MIGRATION REQUIRED")
    print("=" * 60)
    print()
    print("The Management API is not available (missing SUPABASE_ACCESS_TOKEN")
    print("or project ref could not be determined).")
    print()
    print("Please run this SQL in the Supabase Dashboard SQL Editor:")
    print()
    print(f"    {MIGRATION_SQL}")
    print()
    print("Then reload the PostgREST schema cache:")
    print("  Dashboard > Settings > API > Click 'Reload schema cache'")
    print()
    sys.exit(2)


if __name__ == "__main__":
    main()
