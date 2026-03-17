# LinkedAi — Integration Plan

**Date:** 2026-03-16
**Status:** Active
**Objective:** Safely bring all viable open work into main, close dead PRs, prevent future conflicts, and establish a sustainable multi-agent development workflow.

---

## Current State

- **Active nodes:** Primary (orchestrator) + Bot-2 (Google Jobs scraping)
- **Decommissioned:** Worker (incident #250, 2026-03-16), ai-gpu (2026-03-15)
- **Open PRs:** 16 (11 abandoned, 5 plausibly active)
- **Open Issues:** ~60 (18 in Application alone, 11 P0 critical)
- **Sync:** Git only (Seafile abandoned after corruption incident)

---

## Phase 0: Housekeeping (Day 1)

**Goal:** Clear dead weight, preserve ideas, unblock merge paths.

### 0.1 Close 11 Abandoned PRs

All 143+ commits behind main. Branches retained for reference. Ideas preserved in linked issues.

| PR# | Title | Linked Issue | Action |
|-----|-------|-------------|--------|
| 86 | Goose Scraper Supervisor | Roadmap | Close with comment |
| 87 | KO system pattern loading | #69 | Close with comment |
| 89 | Event protocol + dispatch | Roadmap | Close with comment |
| 116 | A2A CV tailoring | N/A (archived) | Close with comment |
| 146 | CV Vector Similarity | #144 | Close with comment |
| 147 | Pydantic validation (Codex) | #208 | Close — duplicate of #150 |
| 148 | Portfolio (Claude) | #64 | Close — empty diff |
| 149 | Portfolio (Codex) | #64 | Close with comment |
| 150 | Pydantic validation (Copilot) | #208 | Close — duplicate of #147 |
| 158 | Browser monitoring | #157, #194 | Close with comment |
| 159 | Super Smart Easy Apply | #208 | Close with comment |

**Closing comment template:**
> Closing — branch is 143+ commits behind main after the March restructuring and cannot be reconciled. The feature idea is preserved in Issue #XXX. Branch retained for reference; can be re-implemented on a fresh branch from current main if still needed.

### 0.2 Resolve Competing Implementations

**SWE Team (#203 vs #204):**
- **Decision:** Merge #203 first (67/67 tests passing, clean merge verified, feature flag `enabled: false`)
- **Then:** Close #204 or have Copilot open a fresh PR to add the memory module on top of #203's foundation
- **Rationale:** #203 is tested and ready; #204 has broader scope but untested and overlapping base

**Pydantic Validation (#150 vs #147):**
- Both stale (143 behind). Close both.
- If still wanted: open fresh PR from main, tracked under Issue #208

---

## Phase 1: Isolated Module Merges (Day 1-2)

**Rule:** Only merge PRs that touch files no other PR touches. Test before merge.

### 1.1 PR #203 — SWE Team Module

| Attribute | Value |
|-----------|-------|
| Branch | `claude/feature-autonomous-development-automation` |
| Files | `src/swe_team/*`, `config/swe_team.yaml`, `src/database/migrations/022_*`, docs, tests |
| Conflicts | None (verified clean merge) |
| Tests | 67/67 passing |
| Feature flag | `enabled: false` (opt-in only) |
| Production coupling | Zero (grep verified) |

**Steps:**
1. Rebase onto current main: `git checkout claude/feature-autonomous-development-automation && git rebase origin/main`
2. Run tests: `.venv/bin/python3 -m pytest tests/unit/test_swe_team.py -v`
3. Verify no conflicts: `git merge-tree $(git merge-base HEAD origin/main) origin/main HEAD`
4. Merge via GitHub (user action): `gh pr merge 203 --squash`
5. Close #204 with comment pointing to #203

### 1.2 PR #229 — Gemini Proxy

| Attribute | Value |
|-----------|-------|
| Branch | `claude/multi-gemini-containerization` |
| Files | `services/gemini-proxy/*`, `docs/` |
| Conflicts | None (isolated directory) |
| Tests | Syntax validation only (no pytest tests for this module) |
| Behind main | 48 commits |

**Steps:**
1. Rebase onto current main (resolve any doc conflicts)
2. Verify only `services/gemini-proxy/` and `docs/` are changed — reject any changes to `src/`, `scripts/`, or root files
3. Syntax check all `.py` files
4. Merge via GitHub

**WARNING:** The branch currently contains SWE team commits mixed in (see report). Must verify the diff after rebase contains ONLY gemini proxy files. If contaminated, cherry-pick only the gemini proxy commits.

---

## Phase 2: Auth & Browser Fixes (Day 2-3)

**Context:** 5 P0 security issues in auth. Worker decommissioned (incident #250). This is the highest-priority area after housekeeping.

### 2.1 PR #205 — Browser Automation Violations

| Attribute | Value |
|-----------|-------|
| Branch | `claude/fix-browser-automation-violations` |
| Files | `src/auth/*`, `src/scraping/recipes/*`, `scripts/ops/*` |
| Conflicts | Only with #158 (closed in Phase 0) |
| Behind main | 78 commits |

**Steps:**
1. Close #158 first (Phase 0)
2. Rebase #205 onto current main
3. **CRITICAL:** Do NOT modify `scripts/ops/authenticate.py` — another agent owns it
4. Run auth-related tests
5. Manual review of all auth changes (security-critical)
6. Merge via GitHub

### 2.2 Address Open Auth Issues (separate PRs)

After #205 merges, create focused single-issue PRs for:
- Issue #238: SEC P0 — Chrome launched with default personal profile
- Issue #240: SEC P0 — SMART_APPLY_PROFILE=test in .env
- Issue #250: SEC P0 — Applications from morpheus account

Each fix should be:
- 1 issue per PR
- Tested on `bot2` profile first (per README rule)
- Only touching files in `src/auth/` or `src/application/`
- Never crossing module boundaries

---

## Phase 3: WebUI & Cross-area (Day 3-4)

### 3.1 PR #235 — WebUI noVNC Monitors

| Attribute | Value |
|-----------|-------|
| Branch | `copilot/update-webui-dashboard-issues` |
| Files | `webui/*`, `src/application/recipes/*`, `scripts/ops/*`, `config/*` |
| Conflicts | With #150, #147 (closed in Phase 0) |
| Behind main | 38 commits |
| Risk | **Crosses WebUI → Application boundary** (modifies recipe YAMLs) |

**Steps:**
1. Ensure #150 and #147 are closed first
2. Rebase onto current main
3. **REVIEW carefully:** Separate webui changes from recipe changes. If recipe changes are unrelated to the WebUI feature, they should be split into a separate PR
4. Run full test suite
5. Merge via GitHub

---

## Phase 4: Fresh Work on Remaining Issues (Ongoing)

After all active PRs are merged, prioritize new work by severity:

### Priority 1: Application P0 Bugs (11 issues)

These are the most critical — applications failing, wrong accounts, data truncation.

| Issue | Focus Area | Module Boundary |
|-------|-----------|-----------------|
| #250 | Account safety in apply | `src/application/applicant.py` |
| #248 | Easy apply account check | `src/easy_apply/` |
| #221 | Behavioral questions skipped | `src/application/answer_resolver.py` |
| #220 | Work experience truncation | `src/application/validation.py` |
| #218 | Duplicate application dedup | `src/application/applicant.py`, `src/database/` |
| #187 | EEO fields without HITL | `src/application/recipes/*.yaml` |
| #188 | ATS form truncation | `src/application/recipes/*.yaml` |
| #190 | ATS sign-in recovery | `src/application/recipe_runner.py` |

**Rule:** Each bug fix = 1 PR, 1 branch, tested, stays within module boundary.

### Priority 2: Infrastructure Stability

| Issue | Focus |
|-------|-------|
| #237 | LLM proxy API keys returning 401 |
| #226 | API 429 rate limit optimization |
| #231 | Bot-2 VM provisioning |
| #228 | Deploy Gemini proxy to IO VM |

### Priority 3: Pipeline Improvements

| Issue | Focus |
|-------|-------|
| #209 | Scraper enhancements (UMBRELLA) |
| #210 | Pipeline observability (UMBRELLA) |
| #207 | WebUI dashboard (UMBRELLA) |

### Priority 4: New Features (Backlog)

| Issue | Focus |
|-------|-------|
| #201 | Autonomous SWE team (enable after #203 merges) |
| #191 | Email A2A Agent |
| #194 | Agent Activity Monitor |

---

## Development Rules (Going Forward)

### 1. One Issue = One PR = One Module

- Every PR addresses exactly one issue
- Every PR stays within one module boundary (see ownership table below)
- Cross-module changes must be split into per-module PRs
- PR title format: `fix(module): description` or `feat(module): description`

### 2. Module Ownership — Exclusive Boundaries

| Area | Allowed Files | Do NOT Touch |
|------|--------------|--------------|
| **Application** | `src/application/`, `src/easy_apply/` | `src/scraping/`, `src/auth/` |
| **Scraping** | `src/scraping/`, `src/pages/` | `src/application/` |
| **Auth** | `src/auth/` | `scripts/ops/authenticate.py` (reserved) |
| **Evaluation** | `src/evaluation/`, `src/embeddings/` | `src/application/` |
| **A2A** | `src/a2a/`, `src/scheduler/` | All other `src/` |
| **Database** | `src/database/`, `src/core/`, `src/data/` | `src/application/` |
| **SWE Team** | `src/swe_team/`, `config/swe_team.yaml` | All production pipeline |
| **Gemini Proxy** | `services/gemini-proxy/` | All other directories |
| **WebUI** | `webui/` | `src/` (use API only) |
| **Infra/Ops** | `scripts/ops/`, `config/` | `src/` modules |

### 3. Branch Naming

| Agent | Prefix |
|-------|--------|
| Copilot SWE | `copilot/*` |
| Claude Code | `claude/*` |
| Codex | `codex/*` |

Never ask one agent to push to another's branch. Never manually create `copilot/*` branches.

### 4. Testing Requirements

Before any PR is merge-ready:
- [ ] All new/modified code has unit tests
- [ ] `pytest tests/unit/test_<module>.py -v` passes 100%
- [ ] No imports from outside the module boundary (grep verified)
- [ ] Feature flags default to `false` for new modules
- [ ] No hardcoded secrets, no direct LLM API calls
- [ ] SQL uses parameterized queries (`$1`, `$2`)

### 5. Rebase Policy

- Branches must be rebased onto main before merge (no merge commits)
- If a branch is >50 commits behind, rebase before review
- If a branch is >100 commits behind, consider closing and re-implementing

### 6. PR Lifecycle

```
Create branch → Implement → Test → Open PR (draft) →
CR review loop (@copilot or manual) → All tests pass →
Rebase onto main → Mark ready → Human merges
```

- Only humans merge PRs (never agents)
- Agents post CRs but never close/merge PRs
- Stale PRs (>2 weeks without activity, >50 behind) flagged for review

---

## Preventing Future Drift

### Automated Guards

1. **Branch health check** (weekly via `/babysit-prs`):
   - Flag any PR >30 commits behind main
   - Flag any PR with no activity for 7+ days
   - Flag any PR that touches files another open PR also touches

2. **Module boundary check** (per PR):
   - Script to verify no file in the diff crosses ownership boundaries
   - Can be a pre-merge hook or manual check

3. **Test regression check** (per PR):
   - Run `pytest tests/unit/ -q` on the PR branch
   - Must not introduce new failures (pre-existing segfaults excluded)

### Knowledge Preservation

When closing stale PRs:
- Comment with the linked issue number
- Keep the branch (don't delete)
- If the PR had good CR feedback, add it to the issue as implementation notes
- Update the issue with "Previous attempt in PR #XXX — closed due to drift, re-implement from main"

---

## Timeline

| Phase | What | When | Who |
|-------|------|------|-----|
| **0** | Close 11 abandoned PRs | Day 1 | Claude (with user approval) |
| **0** | Resolve SWE team competition | Day 1 | User decision |
| **1** | Merge #203 (SWE Team) | Day 1 | User merges |
| **1** | Merge #229 (Gemini Proxy) | Day 1-2 | User merges after rebase |
| **2** | Merge #205 (Auth fixes) | Day 2-3 | User merges after rebase + review |
| **3** | Merge #235 (WebUI) | Day 3-4 | User merges after rebase + review |
| **4** | Fresh PRs for P0 app bugs | Day 4+ | Per-issue, per-module |

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Merge introduces regression | Medium | High | Test on branch before merge, feature flags for new modules |
| Agent crosses module boundary | High | Medium | Enforce ownership table, boundary check script |
| PR drift (branches fall behind) | High | Medium | Weekly branch health check, rebase policy |
| pytest segfaults block testing | Certain | High | File issue for env fix (numpy + pytest-asyncio upgrade) |
| Account mixup after Worker decommission | Medium | Critical | Verify SMART_APPLY_PROFILE on every node before any apply run |
| Git sync overwrites local work | Low | High | Primary uses `pull` mode (not reset); workers use `reset` mode |

---

*Plan created 2026-03-16 by Claude Code (Opus 4.6). Aligned with README.md v4.0.0 and CLAUDE.md.*
