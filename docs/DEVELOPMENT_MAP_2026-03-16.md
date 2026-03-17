# LinkedAi Development Map & Conflict Analysis

**Date:** 2026-03-16
**Author:** Claude Code (Opus 4.6)
**Purpose:** Full inventory of modules, development areas, open PRs, issues, conflict zones, branch health, and recommended merge strategy.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Module Inventory](#2-module-inventory)
3. [Development Areas](#3-development-areas)
4. [Open PRs — Status & Classification](#4-open-prs--status--classification)
5. [Open Issues by Area](#5-open-issues-by-area)
6. [PR-to-Module Conflict Matrix](#6-pr-to-module-conflict-matrix)
7. [Conflict Zones — Danger Files](#7-conflict-zones--danger-files)
8. [Branch Health & Divergence](#8-branch-health--divergence)
9. [Competing Implementations](#9-competing-implementations)
10. [Recommended Merge Strategy](#10-recommended-merge-strategy)
11. [Housekeeping — Abandoned PRs](#11-housekeeping--abandoned-prs)
12. [GH013 Branch Naming Rule](#12-gh013-branch-naming-rule)
13. [Environment Issues — pytest Segfaults](#13-environment-issues--pytest-segfaults)
14. [Autoresearch-Inspired Improvements](#14-autoresearch-inspired-improvements)

---

## 1. Executive Summary

LinkedAi has **16 open PRs** and **~60 open issues** across 14 development areas. The repo has accumulated significant technical overhead:

- **11 of 16 PRs are abandoned** (143+ commits behind main, never rebased after a major restructuring)
- **4 files are touched by 3-4 PRs simultaneously** — merging any one will break the others
- **2 pairs of competing implementations** exist (SWE Team, Pydantic Validation) where different agents built the same feature independently
- **Only 5 PRs are plausibly active**, and only 1 is non-draft

**Immediate recommendations:**
1. Close 11 abandoned PRs (ideas preserved in issues, branches retained)
2. Resolve 2 competing implementations (pick one, close the other)
3. Merge 4 remaining PRs in isolation order: Gemini Proxy → SWE Team → Auth Fixes → WebUI

---

## 2. Module Inventory

### Source Modules (`src/`)

| Module | Path | Purpose | Status |
|--------|------|---------|--------|
| **A2A** | `src/a2a/` | Agent-to-Agent protocol — event hub, dispatch, inter-agent communication | Active |
| **Application** | `src/application/` | Job application pipeline — applicant orchestrator, artifact aggregation, Goose recipes, validation | Active (most critical bugs here) |
| **Auth** | `src/auth/` | LinkedIn session management, automated auth, cookie handling | Active (5 P0 security issues) |
| **Config** | `src/config/` | Shared configuration utilities | Active |
| **Core** | `src/core/` | Core abstractions — job database interface, shared models | Active |
| **CV Tailoring** | `src/cv_tailoring/` | LLM-based CV generation | **ARCHIVED** — using static CVs |
| **Data** | `src/data/` | Data layer utilities | Active |
| **Database** | `src/database/` | Supabase/asyncpg access, SQL migrations | Active |
| **Easy Apply** | `src/easy_apply/` | LinkedIn Easy Apply automation, CV selector | Active |
| **Embeddings** | `src/embeddings/` | SBERT/vector embedding utilities for scoring | Active |
| **Evaluation** | `src/evaluation/` | Job evaluation — KO system, scoring, shadow runner | Active |
| **Experimental** | `src/experimental/` | Prototype code | Low activity |
| **Integrations** | `src/integrations/` | External service connectors | Low activity |
| **Notifications** | `src/notifications/` | Telegram alerts, notification dispatch | Active |
| **Observability** | `src/observability/` | Monitoring, logging, metrics | Active |
| **Pages** | `src/pages/` | Page object models for browser automation | Active |
| **Profile Builder** | `src/profile_builder/` | LinkedIn profile creation/update automation | Backlog |
| **Scheduler** | `src/scheduler/` | Task scheduling utilities | Active |
| **Scraping** | `src/scraping/` | Job scraping — Playwright CDP, recipes, platform adapters | Active |
| **Skills** | `src/skills/` | LinkedIn skill management | Low activity |
| **SWE Team** | `src/swe_team/` | Autonomous SWE agents — triage, monitor, governance | **NEW** (PR #203) |
| **Utils** | `src/utils/` | Shared utilities (trace manager, etc.) | Active |

### Services & Supporting Directories

| Path | Purpose | Status |
|------|---------|--------|
| `services/gemini-proxy/` | Multi-container Gemini CLI proxy with rate isolation | Active (PR #229) |
| `webui/` | Vue 3 dashboard + Express backend | Active (PR #235) |
| `portfolio/` | Architecture showcase webpage (Vue + Tailwind) | Backlog |
| `scripts/ops/` | Operational scripts — health checks, enricher, evaluate, orchestrator | Active |
| `scripts/db/` | Database maintenance scripts | Active |
| `scripts/linkedin/` | LinkedIn-specific operational scripts | Active |
| `scripts/benchmark/` | Performance benchmarking | Low activity |
| `scripts/reports/` | Reporting/analytics scripts | Active |
| `config/` | Cron schedules, node config, search profiles | Active |
| `docs/` | Architecture docs, PRDs, roadmaps | Active |
| `tests/` | Unit, integration, functional, agentic tests | Active |
| `archive/` | Legacy/deprecated code | Deprecated |

---

## 3. Development Areas

### Area 1: Application & ATS (HIGHEST BUG DENSITY)
- **Modules:** `src/application/`, `src/easy_apply/`
- **Owners:** Application & ATS Agent (Primary node only)
- **Active PRs:** #235, #150, #147, #159
- **Open Issues:** 18 (11 P0 critical)
- **Key files:** `applicant.py`, `artifact_aggregator.py`, recipe YAMLs, `validation.py`
- **Risk:** Highest conflict zone — 4 PRs touch `artifact_aggregator.py`

### Area 2: Scraping & Browser Automation
- **Modules:** `src/scraping/`, `src/pages/`, `src/auth/`
- **Owners:** Scraping & Browser Agent (Worker + Bot-2 only)
- **Active PRs:** #86, #158, #205, #235
- **Open Issues:** 11 (5 P0 security in auth)
- **Key files:** `job_scraper.py`, `automated_auth.py`, scraping recipes
- **Risk:** Auth issues are security-critical (wrong account = wrong person's LinkedIn)

### Area 3: Evaluation & Scoring
- **Modules:** `src/evaluation/`, `src/embeddings/`
- **Owners:** Evaluation & Scoring Agent (Primary only)
- **Active PRs:** #87
- **Open Issues:** 2
- **Key files:** KO system, scoring configs
- **Risk:** Low — mostly isolated

### Area 4: A2A & Orchestration
- **Modules:** `src/a2a/`, `src/scheduler/`
- **Owners:** Orchestration Agent (Primary only)
- **Active PRs:** #89, #116
- **Open Issues:** 2
- **Key files:** `config/a2a_hub.yaml`, event protocol
- **Risk:** Medium — both PRs modify shared config

### Area 5: SWE Team (NEW MODULE)
- **Modules:** `src/swe_team/`
- **Active PRs:** #203, #204 (competing implementations)
- **Open Issues:** 1 (#201)
- **Key files:** All new — `config.py`, `models.py`, `monitor_agent.py`, `triage_agent.py`, `governance.py`, `ralph_wiggum.py`
- **Risk:** Two PRs create the same module independently — must pick one

### Area 6: Gemini Proxy
- **Modules:** `services/gemini-proxy/`
- **Active PRs:** #229
- **Open Issues:** 3
- **Key files:** `Dockerfile`, `docker-compose.yml`, `proxy/main.py`, `proxy/gemini_runner.py`
- **Risk:** Low — fully isolated from core pipeline

### Area 7: WebUI Dashboard
- **Modules:** `webui/`
- **Active PRs:** #235
- **Open Issues:** 5
- **Key files:** Vue components, Express API
- **Risk:** Medium — PR #235 also touches `src/application/` recipes (cross-area)

### Area 8: Infrastructure & Ops
- **Modules:** `scripts/ops/`, `config/`, `src/observability/`, `src/notifications/`
- **Active PRs:** #205, #235
- **Open Issues:** 6
- **Key files:** Cron configs, health checks, `scripts/ops/authenticate.py`
- **Risk:** `authenticate.py` has active work — do not touch

### Area 9: Database & Migrations
- **Modules:** `src/database/`, `src/core/`, `src/data/`
- **Active PRs:** #86, #146, #159 (all stale)
- **Open Issues:** 4
- **Risk:** Migration number collisions possible if multiple PRs add migrations

### Area 10: CV System (ARCHIVED)
- **Modules:** `src/cv_tailoring/` (archived), `src/easy_apply/` (active CV selector)
- **Active PRs:** #116, #146 (both stale)
- **Risk:** None — archived, using static CVs

### Area 11: Portfolio
- **Modules:** `portfolio/`
- **Active PRs:** #148, #149 (both stale)
- **Open Issues:** 1
- **Risk:** None — fully isolated

### Area 12: Profile Builder
- **Modules:** `src/profile_builder/`
- **Active PRs:** None
- **Open Issues:** 2
- **Risk:** None — backlog

### Area 13: Notifications & Email
- **Modules:** `src/notifications/`
- **Active PRs:** None
- **Open Issues:** 2
- **Risk:** None

### Area 14: Integrations
- **Modules:** `src/integrations/`
- **Active PRs:** None
- **Risk:** None

---

## 4. Open PRs — Status & Classification

### Active PRs (plausibly mergeable)

| PR# | Title | Branch | Agent | Behind Main | Draft | Area |
|-----|-------|--------|-------|-------------|-------|------|
| **235** | Embed noVNC viewer in WebUI Monitors | `copilot/update-webui-dashboard-issues` | Copilot | 38 | No | WebUI + Application |
| **229** | Gemini proxy with NewAPI + hybrid containers | `claude/multi-gemini-containerization` | Claude | 48 | Yes | Gemini Proxy |
| **205** | Fix browser automation violations | `claude/fix-browser-automation-violations` | Claude | 78 | Yes | Auth + Scraping |
| **204** | PRD/ROADMAP + memory module | `copilot/swe-iterate-prd-and-roadmap` | Copilot | 11 | Yes | SWE Team |
| **203** | Autonomous SWE Team architecture | `claude/feature-autonomous-development-automation` | Claude | 86 | Yes | SWE Team |

### Abandoned PRs (143+ commits behind — recommend closing)

| PR# | Title | Branch | Agent | Behind Main | Area |
|-----|-------|--------|-------|-------------|------|
| **159** | Super Smart Easy Apply cascade | `copilot/add-super-smart-easy-apply-module` | Copilot | 143 | Application |
| **158** | Browser agent monitoring + Playwright traces | `claude/add-browser-agent-monitoring-dashboard` | Claude | 143 | Scraping + Auth |
| **150** | Pre-submission Pydantic validation | `copilot/add-pydantic-validation-layer` | Copilot | 143 | Application |
| **149** | Portfolio showcase (Codex) | `codex/create-portfolio-showcase-webpage` | Codex | 143 | Portfolio |
| **148** | Portfolio showcase (Claude) | `claude/create-portfolio-architecture-webpage` | Claude | 143 | Portfolio (empty diff) |
| **147** | Pre-submit validation (Codex) | `codex/add-pydantic-validation-layer` | Codex | 143 | Application |
| **146** | CV Selection via Vector Similarity | `copilot/replace-cv-tailoring-with-selection` | Copilot | 143 | CV/Easy Apply |
| **116** | A2A-native CV tailoring system | `copilot/cr-19-add-cv-tailoring-system` | Copilot | 143 | CV Tailoring |
| **89** | Event protocol + dispatch + Review Gate | `copilot/pr-88-swarm-orchestration-implementation` | Copilot | 143 | A2A |
| **87** | KO system DB pattern loading | `copilot/fix-ko-system-pattern-loading` | Copilot | 143 | Evaluation |
| **86** | Goose Scraper Supervisor | `copilot/add-ai-supervised-job-scraper` | Copilot | 143 | Scraping + DB |

---

## 5. Open Issues by Area

### Application/Apply — 18 issues (MOST CRITICAL)

**P0 Critical (11):**
| # | Title |
|---|-------|
| 250 | SEC P0: Applications sent from morpheus scraping account |
| 248 | BUG P0: Easy apply running with morpheus account |
| 242 | BUG P0: Google OAuth fails — Chrome profile not logged into Google |
| 240 | SEC P0: SMART_APPLY_PROFILE=test in .env on primary |
| 221 | BUG P0: Behavioral questions on ATS form completely skipped |
| 220 | BUG P0: Work experience truncated + stale candidate data |
| 218 | BUG P0: Duplicate/repeated applications — no dedup guard |
| 217 | BUG P0: Application bot on blank Chrome profile |
| 190 | BUG P0: Agent cannot recover when ATS requires sign-in |
| 188 | BUG P0: ATS form truncation audit |
| 187 | BUG P0: Agent fills EEO/demographic fields without HITL |

**P1 High (6):**
| # | Title |
|---|-------|
| 249 | BUG P1: Stale ATS passwords — no password reset automation |
| 245 | BUG P1: Email verification (OTP) fails during apply |
| 244 | BUG P1: CAPTCHA blocking hard apply |
| 243 | BUG P1: Salary field blocks applications |
| 239 | AUTO: 61% application failure rate |
| 219 | BUG P1: Address field hallucinations |

**P2 (2):**
| # | Title |
|---|-------|
| 247 | BUG P2: Metrics failure_reason polluted by Goose banner text |
| 246 | BUG P2: External platform account creation blocks apps |

### Auth/Sessions — 5 issues (ALL CRITICAL)

| # | Title | Priority |
|---|-------|----------|
| 250 | SEC P0: Applications from morpheus account | CRITICAL |
| 240 | SEC P0: Wrong SMART_APPLY_PROFILE in .env | CRITICAL |
| 238 | SEC P0: Chrome launched with default personal profile on 3 nodes | CRITICAL |
| 217 | BUG P0: Blank Chrome profile | CRITICAL |
| 216 | BUG P0: Session/profile sync conflict — NFS symlinks | CRITICAL |

### Browser Automation — 2 umbrella issues

| # | Title |
|---|-------|
| 206 | UMBRELLA: Browser Automation & Session Management |
| 199 | P0: Browser automation hardening |

### Scraping — 6 issues

| # | Title |
|---|-------|
| 209 | UMBRELLA: Scraper Enhancements & Job Discovery |
| 156 | PIPELINE-012: Apply within 6h of posting |
| 155 | SCRAPER-011: Ingest Google Job Alerts emails |
| 154 | SCRAPER-010: Parse LinkedIn job recommendation emails |
| 153 | SCRAPER-009: Scrape LinkedIn Recommended Jobs |
| 152 | SCRAPER-008: Collect 'See more jobs like this' |

### WebUI — 7 issues

| # | Title |
|---|-------|
| 207 | UMBRELLA: WebUI Dashboard |
| 168 | CR-031: Next-Gen WebUI Implementation (Epic) |
| 167 | CR-030: Next-Gen Dashboard Architecture |
| 166 | CR-029: Advanced Data Controls & Performance |
| 165 | CR-028: Telemetry, State Handling & Edge Cases |
| 164 | CR-027: Data Integrity & API Robustness |
| 163 | CR-026: UI/UX Framework Upgrade |

### Observability/Ops — 3 issues

| # | Title |
|---|-------|
| 210 | UMBRELLA: Pipeline Observability & Operations |
| 194 | EPIC: Agent Activity Monitor |
| 157 | OPS-013: Browser Agent Monitoring Dashboard |

### HITL/Compliance — 3 issues

| # | Title |
|---|-------|
| 208 | UMBRELLA: Application Quality, HITL & Compliance |
| 193 | AUDIT P1: Systematic review of hallucination risk |
| 189 | FEAT P1: Verbose live log streaming |

### Infrastructure/DevOps — 6 issues

| # | Title |
|---|-------|
| 211 | UMBRELLA: Infrastructure, Security & Tech Debt |
| 237 | BUG: LLM proxy API keys all returning 401 |
| 231 | Convert IO Bot-2 to LinkedAi scraping worker |
| 228 | Deploy Gemini Proxy to IO VM |
| 226 | API 429 Rate Limit Diagnosis |
| 222 | Pipeline recovery |

### Gemini Proxy — 3 issues

| # | Title |
|---|-------|
| 76 | Multi-Gemini Containerization |
| 227 | Central Proxy open concerns |
| 228 | Deploy Gemini Proxy Containers |

### SWE Team — 1 issue

| # | Title |
|---|-------|
| 201 | Autonomous development and automation |

### Other / Backlog

| # | Title | Area |
|---|-------|------|
| 223 | Refactor auth system | Auth |
| 212 | UMBRELLA: Future Features | Backlog |
| 191 | Email A2A Agent | Notifications |
| 176 | Root directory bloat audit | Infra |
| 142 | Mark as top choice when score >= 95% | Evaluation |
| 141 | Easy Apply — select CV from LinkedIn templates | Easy Apply |
| 144 | CV Selection via Vector Similarity | CV |
| 122 | Bug: llm_usage_metrics always 0 tokens | Database |
| 92 | Review OpenClaw ACP gateway | A2A |
| 75 | Sandbox OpenClaw in Docker | Infra |
| 72 | Tech Debt: Project Folder Cleanup | Infra |
| 71 | Review Cycle Optimization | Evaluation |
| 70 | User Profile Management | Profile Builder |
| 69 | Evaluation System Review | Evaluation |
| 68 | Human-like Behavior in Scraper | Scraping |
| 65 | Pi Agent for GitHub Ticket Triaging | SWE Team |
| 64 | Portfolio Architecture Showcase | Portfolio |
| 62 | A2A Alerting System | A2A |
| 49 | Email Notification System polling | Notifications |
| 29 | Create Comprehensive Sample Data | Database |
| 28 | Data Modeling Optimization | Database |
| 24 | AI-Powered Content Creation | Content |
| 23 | Open Source Preparation | Security |
| 19 | Advanced LinkedIn Automation | Networking |
| 8 | Profile Builder | Profile Builder |

---

## 6. PR-to-Module Conflict Matrix

Files changed by each active PR, grouped by module. **Bold** = conflict with another PR.

| Module / File | #235 | #229 | #205 | #204 | #203 | #159 | #158 | #150 | #149 | #147 | #146 | #116 | #89 | #87 | #86 |
|---------------|-------|------|------|------|------|------|------|------|------|------|------|------|-----|-----|-----|
| **src/application/applicant.py** | **X** | | | | | | | **X** | | **X** | | | | | |
| **src/application/artifact_aggregator.py** | **X** | | | | | | | **X** | | **X** | **X** | | | | |
| **src/application/recipes/*.yaml** | **X** | | | | | | | **X** | | **X** | | | | | |
| **src/application/validation.py** | | | | | | | | **X** | | **X** | | | | | |
| src/easy_apply/ | | | | | | X | | | | | X | | | | |
| **src/auth/automated_auth.py** | | | **X** | | | | **X** | | | | | | | | |
| src/scraping/recipes/ | | | X | | | | X | | | | | | | | |
| **src/swe_team/*** | | | | **X** | **X** | | | | | | | | | | |
| **config/swe_team.yaml** | | | | **X** | **X** | | | | | | | | | | |
| services/gemini-proxy/ | | X | | | | | | | | | | | | | |
| webui/ | X | | | | | | | | | | | | | | |
| portfolio/ | | | | | | | | | X | | | | | | |
| src/cv_tailoring/ | | | | | | | | | | | | X | | | |
| **config/a2a_hub.yaml** | | | | | | | | | | | | **X** | **X** | | |
| **job_scraper.py** | | | | | | | | | | | | | **X** | | **X** |
| **scripts/ops/smart_evaluate.py** | | | | | | | | | | | **X** | | | **X** | |
| **scripts/ops/job_enricher.py** | | | **X** | | | | **X** | | | | | | | | |
| **scripts/ops/validate_submission.py** | **X** | | | | | | | **X** | | | | | | | |
| src/core/ | | | | | | | | | | | | | | | X |
| src/evaluation/ | | | | | | | | | | | | | | X | |
| docs/ | | X | | X | X | | X | | | | | | | | |

---

## 7. Conflict Zones — Danger Files

### CRITICAL (4 PRs touch the same file)

| File | PRs | Risk |
|------|-----|------|
| `src/application/artifact_aggregator.py` | #235, #150, #147, #146 | Merging any one will conflict with the other 3 |

### HIGH (3 PRs touch the same file)

| File | PRs | Risk |
|------|-----|------|
| `src/application/applicant.py` | #235, #150, #147 | Application orchestrator — heart of apply pipeline |
| `src/application/recipes/easy_apply.yaml` | #235, #150, #147 | Easy Apply recipe template |
| `src/application/recipes/hard_apply.yaml` | #235, #150, #147 | Hard Apply recipe template |
| `src/application/recipes/hard_apply_generic.yaml` | #235, #150, #147 | Generic ATS recipe |

### MEDIUM (2 PRs touch the same file)

| File | PRs | Risk |
|------|-----|------|
| `src/application/validation.py` | #150, #147 | Both add the same validation module |
| `src/auth/automated_auth.py` | #205, #158 | Auth module |
| `src/swe_team/*` (all files) | #204, #203 | Competing SWE team implementations |
| `config/swe_team.yaml` | #204, #203 | SWE team config |
| `config/a2a_hub.yaml` | #116, #89 | A2A protocol config |
| `job_scraper.py` | #89, #86 | Root scraper file |
| `scripts/ops/smart_evaluate.py` | #146, #87 | Evaluation entry point |
| `scripts/ops/job_enricher.py` | #205, #158 | Enrichment script |
| `scripts/ops/validate_submission.py` | #235, #150 | Submission validator |
| `scripts/ops/authenticate.py` | — | **ACTIVE WORK by another agent — DO NOT TOUCH** |

### Conflict Clusters (groups that must be resolved together)

1. **Application Validation Cluster:** PRs #235, #150, #147 — all modify applicant, aggregator, and recipes. #150 and #147 are competing implementations of Pydantic validation by different agents (Copilot vs Codex). Must pick one and close the other before merging #235.

2. **SWE Team Cluster:** PRs #203, #204 — both create `src/swe_team/` from scratch with different designs. Only one can merge. #203 has tested fixes (67/67 passing); #204 is healthier (11 commits behind vs 86).

3. **Auth/Browser Cluster:** PRs #205, #158 — both modify `automated_auth.py` and `job_enricher.py`. #158 is stale (143 behind); close it, then #205 merges cleanly.

4. **A2A Config Cluster:** PRs #116, #89 — both modify `config/a2a_hub.yaml`. Both stale (143 behind); close both, re-implement if needed.

5. **Scraper Root Cluster:** PRs #89, #86 — both modify `job_scraper.py`. Both stale; close both.

---

## 8. Branch Health & Divergence

| PR# | Branch | Ahead | Behind | Health | Assessment |
|-----|--------|-------|--------|--------|------------|
| 204 | `copilot/swe-iterate-prd-and-roadmap` | 20 | 11 | Green | Most current — light rebase needed |
| 235 | `copilot/update-webui-dashboard-issues` | 6 | 38 | Yellow | Moderate rebase needed |
| 229 | `claude/multi-gemini-containerization` | 6 | 48 | Yellow | Moderate rebase needed |
| 205 | `claude/fix-browser-automation-violations` | 2 | 78 | Orange | Significant rebase needed |
| 203 | `claude/feature-autonomous-development-automation` | 10 | 86 | Orange | Significant rebase but clean merge (tested) |
| 159 | `copilot/add-super-smart-easy-apply-module` | 286 | 143 | Red | **ABANDONED** |
| 158 | `claude/add-browser-agent-monitoring-dashboard` | 287 | 143 | Red | **ABANDONED** |
| 150 | `copilot/add-pydantic-validation-layer` | 287 | 143 | Red | **ABANDONED** |
| 149 | `codex/create-portfolio-showcase-webpage` | 285 | 143 | Red | **ABANDONED** |
| 148 | `claude/create-portfolio-architecture-webpage` | 284 | 143 | Red | **ABANDONED** (empty diff) |
| 147 | `codex/add-pydantic-validation-layer` | 286 | 143 | Red | **ABANDONED** |
| 146 | `copilot/replace-cv-tailoring-with-selection` | 286 | 143 | Red | **ABANDONED** |
| 116 | `copilot/cr-19-add-cv-tailoring-system` | 247 | 143 | Red | **ABANDONED** |
| 89 | `copilot/pr-88-swarm-orchestration-implementation` | 183 | 143 | Red | **ABANDONED** |
| 87 | `copilot/fix-ko-system-pattern-loading` | 139 | 143 | Red | **ABANDONED** |
| 86 | `copilot/add-ai-supervised-job-scraper` | 127 | 143 | Red | **ABANDONED** |

**All 11 "Red" PRs diverged at the same point** — likely a major repo restructuring ~143 commits ago. None were rebased after that event. They contain good ideas but the code is irreconcilable with current main.

---

## 9. Competing Implementations

### Competition 1: SWE Team Module

| Aspect | PR #203 (Claude) | PR #204 (Copilot) |
|--------|-----------------|-------------------|
| Branch | `claude/feature-autonomous-development-automation` | `copilot/swe-iterate-prd-and-roadmap` |
| Behind main | 86 | 11 |
| Tests | 67/67 passing (segfault fixes applied) | Untested |
| Merge conflicts | None (tested clean merge) | Unknown |
| Scope | Core module: models, events, config, monitor, triage, governance, Ralph Wiggum gate, ticket store | Extended: PRD/roadmap + memory module (6 types, maturation pipeline, access control) |
| Feature flag | `enabled: false` (verified) | Unknown |

**Recommendation:** PR #203 is tested and verified. PR #204 has a broader scope (memory module) but is untested. Options:
- **Option A:** Merge #203 first (proven), then have Copilot build #204's memory module on top
- **Option B:** Rebase #204 onto main, test it, potentially absorb #203's fixes

### Competition 2: Pydantic Validation

| Aspect | PR #150 (Copilot) | PR #147 (Codex) |
|--------|-------------------|-----------------|
| Branch | `copilot/add-pydantic-validation-layer` | `codex/add-pydantic-validation-layer` |
| Behind main | 143 | 143 |
| Status | ABANDONED | ABANDONED |

**Recommendation:** Close both. If Pydantic validation is still wanted, start a fresh PR from current main. The feature is tracked in Issue #208 (UMBRELLA: Application Quality).

---

## 10. Recommended Merge Strategy

### Phase 0: Housekeeping
Close 11 abandoned PRs. Preserve branches for reference. Comment on each with reason.

### Phase 1: Isolated Modules (zero cross-conflicts)

**1st: PR #229 — Gemini Proxy**
- Touches: `services/gemini-proxy/` only
- Conflicts with: Nothing
- Needs: Rebase (48 behind), syntax check on `authenticate.py` (avoid)
- Note: Branch name misleading — verify only gemini proxy files are included

**2nd: PR #203 — SWE Team**
- Touches: `src/swe_team/`, `config/swe_team.yaml`, migrations, docs, tests
- Conflicts with: Only #204 (which should be closed or deferred)
- Status: 67/67 tests passing, clean merge verified, feature flag defaults `false`
- Note: Close #204 first, or absorb its memory module as a follow-up PR

### Phase 2: Moderate Complexity

**3rd: PR #205 — Auth/Browser Fixes**
- Touches: `src/auth/`, `src/scraping/recipes/`, `scripts/ops/`
- Conflicts with: Only #158 (closed in Phase 0)
- Needs: Rebase (78 behind), careful review of auth changes (security-critical)
- Note: Do NOT modify `scripts/ops/authenticate.py` — another agent is working on it

### Phase 3: Complex / Cross-area

**4th: PR #235 — WebUI + noVNC**
- Touches: `webui/`, `src/application/recipes/`, `scripts/ops/`, `config/`
- Conflicts with: #150, #147 (closed in Phase 0)
- Needs: Rebase (38 behind), review of recipe changes (crosses into Application area)
- Note: This is the only non-draft PR — may have user priority

### Post-merge: New work on fresh branches
- **Pydantic validation** — new PR from main if still wanted
- **Memory module** — new PR extending `src/swe_team/` from #204's design
- **CV vector selection** — new PR from main if still wanted
- **KO pattern loading** — new PR from main

---

## 11. Housekeeping — Abandoned PRs

### PRs to close (11 total)

| PR# | Title | Reason | Issue preserved? |
|-----|-------|--------|-----------------|
| 86 | Goose Scraper Supervisor | 143 behind, irreconcilable | Tracked in roadmap |
| 87 | KO system pattern loading | 143 behind | Tracked in #69 |
| 89 | Event protocol + dispatch | 143 behind | Tracked in roadmap |
| 116 | A2A CV tailoring system | 143 behind, CV archived | Not needed |
| 146 | CV Vector Similarity | 143 behind | Tracked in #144 |
| 147 | Pydantic validation (Codex) | 143 behind, duplicate of #150 | Tracked in #208 |
| 148 | Portfolio (Claude) | 143 behind, empty diff | Tracked in #64 |
| 149 | Portfolio (Codex) | 143 behind | Tracked in #64 |
| 150 | Pydantic validation (Copilot) | 143 behind, duplicate of #147 | Tracked in #208 |
| 158 | Browser monitoring | 143 behind | Tracked in #157, #194 |
| 159 | Super Smart Easy Apply | 143 behind | Tracked in #208 |

**Closing comment template:**
> Closing — branch is 143 commits behind main and cannot be reconciled. The feature idea is preserved in Issue #XXX and can be re-implemented on a fresh branch from current main. Branch retained for reference.

---

## 12. GH013 Branch Naming Rule

**Root cause identified and documented in CLAUDE.md (commit `a07230d`).**

Each coding agent can only push to branches with its own prefix:

| Agent | Prefix | Enforced by |
|-------|--------|------------|
| Copilot SWE | `copilot/*` | GitHub platform token scoping |
| Claude Code | `claude/*` | GitHub platform token scoping |
| Codex | `codex/*` | GitHub platform token scoping |

**Rule:** Never ask one agent to iterate on another agent's branch. If Agent A needs to pick up Agent B's work, Agent A must create its own prefixed branch.

This explains why Copilot couldn't push to PR #203 (`claude/*` branch) and PR #229 (`claude/*` branch). We resolved both by pushing directly as Claude.

---

## 13. Environment Issues — pytest Segfaults

**Two pre-existing segfault issues affect the test suite on all branches (including main):**

### Segfault 1: Python 3.12 + logging + asyncio
- **Trigger:** Any `logger.warning()`/`logger.error()` call during pytest
- **Root cause:** Python 3.12 added `logging.logAsyncioTasks = True` by default. The `anyio` pytest plugin loads `asyncio` into `sys.modules` in a broken state, causing `asyncio.current_task()` to segfault inside `LogRecord.__init__`
- **Fix:** Add `import logging; logging.logAsyncioTasks = False` at the top of test files
- **Status:** Fixed in PR #203's test file. Not yet applied globally.

### Segfault 2: pytest-asyncio 1.3.0 + Python 3.12
- **Trigger:** Any `@pytest.mark.asyncio` test
- **Root cause:** `pytest-asyncio 1.3.0` is incompatible with Python 3.12. `_get_event_loop_no_warn()` segfaults
- **Fix options:**
  - Upgrade `pytest-asyncio` to ≥0.21 (may break other tests)
  - Convert async tests to sync where possible (done in PR #203)
  - Use `asyncio.run()` instead of `@pytest.mark.asyncio`

### Segfault 3: numpy/ctypes
- **Trigger:** Tests that import numpy-dependent modules
- **Root cause:** numpy/ctypes incompatibility with current Python 3.12.12
- **Impact:** Prevents running the full unit test suite — ~12 test files crash
- **Fix:** Upgrade numpy or pin to compatible version

**Recommendation:** File a dedicated issue for pytest environment stabilization. This blocks CI/CD for the entire project.

---

## 14. Autoresearch-Inspired Improvements

From reviewing [karpathy/autoresearch](https://github.com/karpathy/autoresearch), 5 ideas were identified and posted to Issue #201:

| # | Idea | Effort | Impact |
|---|------|--------|--------|
| 1 | Agent behavior as markdown programs (`config/swe_team/programs/*.md`) | Low | Medium |
| 2 | **Time-boxed keep/discard experiment loop** — every fix gets a budget, tests as metric, git reset on failure | Medium | **High** |
| 3 | Persistent loop with crash recovery — isolate failures per ticket | Medium | High |
| 4 | **Git as state machine** — branches for state, commits for progress, reverts for failure | Low | **High** |
| 5 | Automated simplicity criterion — reject disproportionately complex fixes | Low | Medium |

**Priority:** Start with #2 (keep/discard loop) and #4 (git-as-state-machine) — highest impact, lowest effort, directly address the "abandoned fixes" problem.

---

## Appendix: File Ownership Boundaries

To prevent agents from stepping on each other, enforce these ownership rules:

| Agent / Developer | Exclusive Ownership | Do NOT Touch |
|-------------------|--------------------|--------------|
| **Application Agent** | `src/application/`, `src/easy_apply/` | `src/scraping/`, `src/auth/` |
| **Scraping Agent** | `src/scraping/`, `src/pages/` | `src/application/` |
| **Auth Agent** | `src/auth/` | `src/application/`, `scripts/ops/authenticate.py` (reserved) |
| **Evaluation Agent** | `src/evaluation/`, `src/embeddings/` | `src/application/` |
| **Orchestration Agent** | `src/a2a/`, `src/scheduler/` | Everything else |
| **Database Agent** | `src/database/`, `src/core/`, `src/data/` | `src/application/` |
| **SWE Team Agent** | `src/swe_team/` | Everything in production pipeline |
| **Gemini Proxy Agent** | `services/gemini-proxy/` | Everything else |
| **WebUI Agent** | `webui/` | `src/` (should use API only) |
| **Infra/Ops Agent** | `scripts/ops/`, `config/` | `src/` modules |

**Cross-area PRs** (like #235 touching both `webui/` and `src/application/recipes/`) are the primary source of conflicts. These should be split into per-area PRs where possible.

---

*Report generated 2026-03-16 by Claude Code (Opus 4.6). Data sourced from git history, GitHub API, and local test runs.*
