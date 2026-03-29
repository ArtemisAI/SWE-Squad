# Deployment Notes — SWE-Squad Alpha

## Multi-Team Configuration

Each SWE-Squad team instance requires:
1. A dedicated GitHub account (e.g., `my-squad-bot`, `my-squad-beta`)
2. Assignee-based issue pickup — teams only process issues assigned to their account
3. Separate `SWE_TEAM_ID` for Supabase ticket scoping
4. Independent daemon process with its own `.env`

## Verified Deployment Checklist

- [x] Claude Code CLI installed and authenticated
- [x] GitHub PAT with `repo` scope configured
- [x] Supabase connection verified (tickets read/write)
- [x] BASE_LLM proxy reachable (embeddings + extraction)
- [x] Sandbox repos cloned to `~/Projects/`
- [x] Session store initialized at `data/swe_team/sessions.json`
- [x] Daemon running: `python3 scripts/ops/swe_team_runner.py --daemon --interval 120`

## Session Management

The daemon persists Claude Code sessions across cycles:
- Investigation sessions are stored with ticket ID linkage
- Developer sessions fork from investigator sessions via `--fork-session`
- Suspended sessions are automatically resumed on the next cycle
- Session IDs tracked in both local store and Supabase metadata
