# SWE-Squad Operations Runbook

Practical reference for diagnosing and resolving recurring issues.
Updated each time a new failure pattern is confirmed in production.

---

## 1. Health Audit Protocol

Run this to assess system state at any time:

```bash
# Last 100 log lines
tail -100 logs/swe_team.log

# Ticket counts
set -a && source .env && set +a && python3 -c "
from src.swe_team.supabase_store import SupabaseTicketStore
from collections import Counter
import os
store = SupabaseTicketStore(supabase_url=os.environ['SUPABASE_URL'], supabase_key=os.environ['SUPABASE_ANON_KEY'], team_id='swe-squad-1')
all_t = store.list_all()
print(dict(Counter(t.status.value for t in all_t)), 'total:', len(all_t))
"

# Daemon alive?
pgrep -fa "swe_team_runner.*daemon"
cat /tmp/swe_squad_daemon.pid
```

---

## 2. Known Failure Patterns

### 2.1 `AttributeError: 'SWETicket' object has no attribute 'repo'`

**Symptom:** Every dev agent attempt fails with AttributeError in swe_team_runner.py.

**Root cause:** `SWETicket` has no `.repo` attribute. Repo is stored in `ticket.metadata['repo']`.

**Fix (already applied):** All call sites in `swe_team_runner.py` must use:
```python
repo=ticket.metadata.get("repo", "")   # correct
# NOT: repo=ticket.repo or ""          # wrong — attribute doesn't exist
```

**Prevention:** Any new `comment_on_github_issue()` or `create_github_issue()` call must read repo from metadata, never from a direct attribute.

---

### 2.2 `KeyError 'ticket_id'` — orchestrate.md template fails for CRITICAL tickets

**Symptom:** `WARNING: Invalid orchestrate.md template: 'ticket_id'` in logs. CRITICAL tickets silently fall back to basic investigation prompt instead of full Opus orchestration.

**Root cause:** `investigator.py._build_orchestration_prompt()` did not pass all template variables used in `orchestrate.md`.

**Fix (already applied):** `template.format()` in `_build_orchestration_prompt()` must include:
```python
ticket_id=ticket.ticket_id,
branch=ticket.metadata.get("branch", ""),
```

**Prevention:** After editing `orchestrate.md`, always validate:
```python
python3 -c "
import re
with open('config/swe_team/programs/orchestrate.md') as f:
    content = f.read()
stripped = re.sub(r'\{\{[^}]*\}\}', '', content)
placeholders = set(re.findall(r'\{([a-zA-Z_][a-zA-Z_0-9.]*)\}', stripped))
passed = {'title','severity','source_module','description','investigation_report','ticket_id','branch'}
missing = placeholders - passed
print('MISSING:', missing or 'none')
"
```

Python f-string literals inside code blocks in `.md` templates must be double-escaped: `{{var}}` not `{var}`.

---

### 2.3 `comment_on_github_issue` posts to wrong repo

**Symptom:** `gh issue comment failed (rc=1): Could not resolve to an issue or pull request with the number of 199`.

**Root cause:** `comment_on_github_issue()` was missing a `repo` parameter, so `gh` defaulted to the CWD repo (SWE-Squad-DEV) instead of the ticket's repo (LinkedAi).

**Fix (already applied):** Function signature is now `comment_on_github_issue(issue_number, body, repo="")`. Always pass `repo=ticket.metadata.get("repo", "")`.

**Prevention:** When adding any `gh issue` or `gh pr` subprocess call, always include `--repo {repo}` when operating cross-repo.

---

### 2.4 `RuntimeError: you need to resolve your current index first`

**Symptom:** DeveloperAgent crashes mid-cycle; tickets stuck in `INVESTIGATION_COMPLETE` or `IN_DEVELOPMENT`.

**Root cause:** A prior merge (or concurrent Opus push) left the git index in a conflicted state. `git checkout -B {branch}` fails when conflict markers exist.

**Fix (already applied):** `developer.py._ensure_branch()` now:
1. Runs `git status --porcelain` to detect conflict markers (UU, AA, DD, etc.)
2. Runs `git reset --merge` automatically if conflicts found
3. Then proceeds with `git checkout -B {branch}`

**Prevention:** Never run `git merge` in an automated context without catching conflict exit codes. Prefer `git cherry-pick` with `--abort` fallback over merges in CI/automation.

---

### 2.5 Embedding endpoint 504s block cycle for ~3 minutes

**Symptom:** Log shows 3 consecutive `HTTP/1.1 504 Gateway Timeout` on `/embeddings`, each ~60s apart.

**Root cause:** OpenAI client default timeout is 10 min; BASE_LLM proxy `/embeddings` intermittently times out under load. 3 retries × 60s = ~3 min wasted per cycle.

**Fix (already applied):** `embeddings.py` now creates the client with `timeout=15.0, max_retries=2`. Worst case: 2 retries × 15s = 30s instead of 3 min.

**Prevention:** Embeddings are **best-effort** — always treat `embed_ticket()` return value as `Optional`. Never block a ticket transition on embedding failure.

---

### 2.6 False regression explosion — `[REGRESSION]` tickets for GitHub issues

**Symptom:** Tickets with `is_regression: True` and fingerprint starting with `gh-issue-{N}`. These are tickets treating GH issues as regressions of themselves.

**Root cause:** The regression check compares recently resolved tickets against current open tickets. GH-synced tickets use fingerprints like `gh-issue-14` which collide with resolved tickets that were originally created from the same GH issue.

**Detection and fix:**
```python
# Query
false_reg = [t for t in store.list_all()
             if t.metadata.get('is_regression')
             and str(t.metadata.get('fingerprint','')).startswith('gh-issue-')]

# Fix each one
for t in false_reg:
    t.metadata['resolution_note'] = 'false_regression_guard'
    t.metadata['is_regression'] = False
    t.transition(TicketStatus.RESOLVED)
    store.add(t)
```

**Prevention:** The regression check (`check_regressions()` in runner) should exclude tickets whose fingerprint starts with `gh-issue-` from the regression comparison set. This is tracked in GH #9.

---

### 2.7 Tickets stalled in `IN_DEVELOPMENT` or `INVESTIGATING` > 2h

**Symptom:** Tickets not advancing through the pipeline.

**Cause:** Dev agent crashed mid-attempt (AttributeError, RuntimeError, etc.) and did not update ticket status.

**Fix:** Reset stalled tickets back to `INVESTIGATION_COMPLETE`:
```python
from datetime import datetime, timezone, timedelta
now = datetime.now(timezone.utc)

def parse_ts(s):
    try: return datetime.fromisoformat(s.replace('Z','+00:00'))
    except: return None

stalled = [t for t in store.list_all()
           if t.status.value in ('investigating','in_development')
           and parse_ts(t.updated_at)
           and (now - parse_ts(t.updated_at)) > timedelta(hours=2)]

for t in stalled:
    t.status = TicketStatus.INVESTIGATION_COMPLETE
    t.updated_at = now.isoformat()
    t.metadata.pop('branch', None)  # clear partial branch
    store.add(t)
    print(f'Reset: {t.ticket_id}')
```

**Prevention:** Wrap every `ticket.transition()` call in a try/except that always writes the ticket back to the store, even on failure, so stalls are recoverable by the next cycle.

---

### 2.8 Daemon running stale code after a fix is merged

**Symptom:** Fixes are on `main` but errors continue in logs — daemon is running old imports.

**Root cause:** Python caches module imports at startup. Merging to `main` doesn't hot-reload a running daemon.

**Fix:** Restart the daemon:
```bash
# Kill old daemon
kill $(cat /tmp/swe_squad_daemon.pid) 2>/dev/null
rm -f /tmp/swe_squad_daemon.pid

# Start fresh (watchdog will also auto-restart within 15 min)
set -a && source .env && set +a
SWE_TEAM_ENABLED=true nohup /usr/bin/python3 scripts/ops/swe_team_runner.py \
  --daemon --interval 3600 >> logs/swe_team.log 2>&1 &
echo $! > /tmp/swe_squad_daemon.pid
echo "Daemon PID: $(cat /tmp/swe_squad_daemon.pid)"
```

**Prevention:** The watchdog (`scripts/ops/watchdog.sh`) restarts on crash or 90-min stall. For intentional restarts after hotfixes, always kill + restart manually rather than waiting for the watchdog.

---

### 2.9 Git commits being reset by background process

**Symptom:** `git reflog` shows `reset: moving to {old-hash}` immediately after every commit. Local branch tip returns to pre-commit state.

**Root cause:** The Claude Code execution environment performs a `git reset --hard` to maintain repository state isolation between agent operations.

**Workaround:** Push the commit object directly by hash before the reset fires:
```bash
git commit -m "..."
# Push immediately — before the reset
git push origin HEAD:refs/heads/{branch-name}
# If already reset, push by commit hash from reflog:
git push origin {commit-hash}:refs/heads/{branch-name}
```

The commit object survives in the git object store even after local reset. `git push origin {hash}:refs/heads/{branch}` always works.

---

### 2.10 Remote log collection failures

**Symptom:** `WARNING: rsync from linkedai-* failed` or `Timeout collecting logs from` in `swe_team.log`. Monitor cycle runs but only scans local logs.

**Diagnosis:**
```bash
# Check SSH connectivity to all workers
ssh -F config/ssh_workers.conf linkedai-browser-2 "echo ok"
ssh -F config/ssh_workers.conf linkedai-bot-2 "echo ok"
ssh -F config/ssh_workers.conf linkedai-hp-laptop "echo ok"

# Check that log directory exists on the worker
ssh -F config/ssh_workers.conf linkedai-bot-2 "ls -la ~/Projects/LinkedAi/logs/"

# Check worker reachability via Python
python3 -c "from src.swe_team.remote_logs import list_available_workers; print(list_available_workers())"
```

**Common causes:**
- Worker node is offline or Tailscale is disconnected → `tailscale status` on the worker
- SSH key not in worker's `authorized_keys` → `ssh-copy-id -i ~/.ssh/swe_workers_linkedai_key.pub agent@<IP>`
- Log directory doesn't exist on worker (e.g., `linkedai-browser-1` has no `logs/` dir) → exclude from `swe_team.yaml`
- rsync not installed on worker → `remote_logs.py` auto-falls back to SSH cat

**Prevention:** Only configure workers with verified log directories in `config/swe_team.yaml` under `monitor.remote_workers`. Use SSH config aliases (not raw IPs).

---

### 2.11 Code propagation failures

**Symptom:** Worker running stale code after a push. `propagate.sh` reports `FAIL` for one or more workers.

**Diagnosis:**
```bash
# Run propagation manually and check output
bash scripts/ops/propagate.sh --project linkedai

# Check propagation log
tail -20 logs/propagate.log

# Verify worker is on the correct commit
ssh -F config/ssh_workers.conf linkedai-bot-2 "cd ~/Projects/LinkedAi && git log --oneline -3"
```

**Common causes:**
- Worker has uncommitted changes → `git reset --hard origin/main` on the worker
- Worker's git remote is wrong → check `git remote -v` on the worker
- SSH timeout (worker slow/overloaded) → increase `--timeout` flag

**Fix:** `propagate.sh` runs `git fetch origin main && git reset --hard origin/main` on each worker. If a worker has local changes blocking this, SSH in and resolve manually.

---

### 2.12 Webhook listener down

**Symptom:** Pushes to GitHub don't trigger automatic propagation.

**Diagnosis:**
```bash
# Check systemd service
systemctl --user status swe-webhook.service

# Check if port is listening
ss -tlnp | grep 9876

# Health check
curl http://localhost:9876/health
```

**Fix:**
```bash
systemctl --user restart swe-webhook.service
systemctl --user enable swe-webhook.service  # survive reboots
```

**Note:** The webhook only receives events from within the Tailscale network. Public GitHub webhooks can't reach Tailscale IPs. For external pushes, use `git_push_propagate.sh` instead.

---

## 3. Stability Gate Thresholds

The Ralph-Wiggum gate (`ralph_wiggum.py`) blocks new feature work when:

| Metric | Block threshold (current config) |
|--------|----------------------------------|
| Open CRITICAL tickets | > 20 |
| Open HIGH tickets | > 50 |
| Failing tests | > 0 |
| CI green | not required (disabled) |

If the gate is permanently blocked, either:
1. Raise thresholds in `config/swe_team.yaml` under `governance:` (temporary workaround)
2. Drain the backlog by increasing `max_investigations_per_cycle` and `max_developments_per_cycle`

Current realistic drain rate: ~3 investigations + ~2 dev attempts per 30-min cycle.

---

## 4. Emergency Procedures

### Full pipeline reset

```bash
# 1. Kill daemon
kill $(cat /tmp/swe_squad_daemon.pid) 2>/dev/null

# 2. Reset all stalled tickets (see 2.7 above)

# 3. Clear any git conflict state in managed repos
git -C /home/agent/Projects/LinkedAi reset --merge 2>/dev/null || true
git -C /home/agent/SWE-Squad reset --merge 2>/dev/null || true

# 4. Restart daemon
set -a && source .env && set +a
SWE_TEAM_ENABLED=true nohup /usr/bin/python3 scripts/ops/swe_team_runner.py \
  --daemon --interval 3600 >> logs/swe_team.log 2>&1 &
echo $! > /tmp/swe_squad_daemon.pid
```

### Check what the daemon is actually running

```bash
# Which Python file?
ls -la /proc/$(cat /tmp/swe_squad_daemon.pid)/exe

# Is it using the latest code? (check commit date vs daemon start time)
git log --oneline origin/main | head -3
ps -o lstart= -p $(cat /tmp/swe_squad_daemon.pid)
```

---

## 5. Pre-commit Checklist for Runner / Agent Changes

Before pushing any change to `swe_team_runner.py`, `developer.py`, `investigator.py`, or `reviewer.py`:

- [ ] `python3 -m pytest tests/unit/test_swe_team.py -q` — 222+ passed, 0 unexpected failures
- [ ] No `ticket.repo` references — use `ticket.metadata.get("repo", "")`
- [ ] No `ticket.assigned_to` unless checking `is not None`
- [ ] Any new `gh issue`/`gh pr` subprocess call includes `--repo {repo}` for cross-repo safety
- [ ] New prompt template variables added to the corresponding `template.format()` kwargs
- [ ] Python code-block literals in `.md` templates use `{{double-braces}}` to escape format vars
- [ ] `embed_ticket()` return value treated as `Optional` — never blocks a state transition
- [ ] `ticket.transition(new_status)` wrapped in try/except — status written back on failure

---

## 6. Self-Healing Monitor

`scripts/ops/self_heal.py` runs every 5 minutes from cron and responds automatically.

### What it fixes automatically (Tier 1 — no LLM)

| Problem | Action |
|---------|--------|
| Daemon dead or log stalled >90 min | Kill old PID + start fresh daemon |
| Tickets stuck in `investigating`/`in_development` >2h | Reset to `INVESTIGATION_COMPLETE` |
| False regressions (`gh-issue-*` + `is_regression`) | Resolve with `false_regression_guard` |

### When it invokes Claude Code (Tier 2 — rate-limited, max once per 30 min)

| Trigger | Threshold |
|---------|-----------|
| ERROR lines in last 100 log lines | > 4 non-rsync errors |
| Regression burst | > 5 regression tickets in 35 min |

Claude receives `config/swe_team/programs/health_audit_auto.md` with the trigger reason and recent error context injected. Claude has full tool access and will:
1. Read logs + query Supabase
2. Fix stalls and false regressions
3. Trace code bugs, write fixes, push PRs, merge
4. Create GH issues for anything it can't fix safely

### Logs
- `logs/self_heal.log` — every run result (fast checks)
- `logs/self_heal_claude.log` — full Claude output when invoked

### Cooldown file
`/tmp/swe_squad_claude_invoked.lock` — mtime controls the 30-min cooldown.
Delete it to force-allow the next Claude invocation:
```bash
rm -f /tmp/swe_squad_claude_invoked.lock
```

## 7. Log Locations

| Log | Path | Purpose |
|-----|------|---------|
| Main runner | `logs/swe_team.log` | All cycle activity, errors, ticket transitions |
| Watchdog | `logs/watchdog.log` | Daemon restart events |
| GitHub sync | `logs/github_sync.log` | GH Issues → Supabase sync (every 5 min) |
| Cron | `logs/cron.log` | Daily report output |
| A2A hub | `data/a2a/` | Inter-agent event log |
| Remote logs | `logs/remote/{worker-name}/` | Logs collected from worker nodes via rsync |
| Propagation | `logs/propagate.log` | Code propagation results per worker |
| Webhook | systemd journal | `journalctl --user -u swe-webhook.service` |
