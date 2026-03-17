# Autonomous Software Engineering Team Architecture

**Version:** 1.0
**Status:** Design Phase
**Created:** 2026-03-13
**Authors:** Claude Sonnet 4.5 (AI Product Management)

## Executive Summary

This document outlines the architecture for a fully autonomous Software Engineering (SWE) team within LinkedAi. The system leverages the existing A2A (Agent-to-Agent) protocol, event-driven orchestration, and multi-agent coordination to create a self-healing, self-improving development loop that can detect issues, investigate problems, propose solutions, implement fixes, test changes, and deploy updates — all with minimal human intervention.

## Table of Contents

1. [Vision & Objectives](#vision--objectives)
2. [Current State Analysis](#current-state-analysis)
3. [Autonomous SWE Team Roles](#autonomous-swe-team-roles)
4. [Architecture Overview](#architecture-overview)
5. [Core Components](#core-components)
6. [Workflows & Use Cases](#workflows--use-cases)
7. [Implementation Phases](#implementation-phases)
8. [Token Economy & Cost Management](#token-economy--cost-management)
9. [Security & Governance](#security--governance)
10. [Success Metrics](#success-metrics)

---

## Vision & Objectives

### Vision Statement

Create a self-sustaining development ecosystem where autonomous agents continuously monitor, diagnose, fix, test, and improve the LinkedAi platform — transforming from reactive debugging to proactive system evolution.

### Core Objectives

1. **Zero Abandoned Issues**: Every detected problem is tracked, investigated, and resolved or escalated
2. **Rapid Response**: Issues detected in production trigger immediate investigation within 2 minutes
3. **Validated Changes**: All code changes pass comprehensive testing before deployment
4. **Knowledge Accumulation**: Solutions to common problems are stored and reused
5. **Cost Efficiency**: Optimize LLM usage (free/cheap for routine, expensive for critical)
6. **Safe Evolution**: Parallel sandbox testing ensures zero production disruption
7. **Ralph Wiggum Loop**: Fix bugs before building new features (technical debt prevention)

### Non-Goals

- Replace human oversight entirely (humans retain veto power on critical changes)
- Achieve AGI or general-purpose reasoning beyond software engineering
- Support arbitrary programming languages (Python-first)

---

## Current State Analysis

### Strengths

The LinkedAi codebase already has robust infrastructure for autonomous operations:

| Component | Status | Capability |
|-----------|--------|------------|
| **A2A Hub** | ✅ Production | JSON-RPC 2.0 message routing, task persistence, adapter registry |
| **Event System** | ✅ Production | 8 event types, pipeline orchestration, cooldown management |
| **Error Handling** | ✅ Production | 3-tier supervision (heuristic → self-heal → LLM escalation) |
| **Monitoring** | ✅ Production | Real-time GUI, metrics collection, Langfuse traces, health checks |
| **Testing** | ✅ Production | 22+ unit tests, integration tests, pytest with asyncio |
| **CI/CD** | ✅ Production | GitHub Actions with Python 3.9-3.11 matrix, security scans |
| **Database** | ✅ Production | PostgreSQL with asyncpg, state transitions, JSONB metadata |
| **Configuration** | ✅ Production | YAML-driven with env var interpolation |

### Gaps Identified

| Gap | Impact | Priority |
|-----|--------|----------|
| GitHub API integration | Cannot auto-create issues/PRs | **HIGH** |
| Problem analysis agent | No root cause diagnosis | **HIGH** |
| Knowledge base | Repeated investigation of known issues | **MEDIUM** |
| CI/CD feedback loop | No auto-rollback on test failures | **HIGH** |
| Code verification | Changes committed without local testing | **HIGH** |
| Self-diagnostic | Limited introspection of failure causes | **MEDIUM** |
| Incident response | No escalation beyond Telegram alerts | **MEDIUM** |

---

## Autonomous SWE Team Roles

The autonomous SWE team consists of **7 specialized agent roles**, each with clear boundaries and responsibilities:

### 1. **Monitoring Agent** (Sentinel)

**Role:** Continuous system health surveillance

**Responsibilities:**
- Scan logs for errors/warnings every 5 minutes
- Monitor CI/CD pipeline failures
- Track performance regressions
- Detect anomalies in metrics (job scrape rates, application success, LLM latency)
- Trigger alerts when thresholds exceeded

**Skills Required:**
- Log parsing and pattern matching
- Metric threshold evaluation
- GitHub Actions API access
- Telegram/email notification

**Adapter:** `monitor_agent` (CLI-based Python script)

**Cost:** FREE (heuristic-based, zero LLM)

---

### 2. **Triage Agent** (Dispatcher)

**Role:** Issue classification and assignment

**Responsibilities:**
- Receive alerts from Monitoring Agent
- Classify severity (LOW/MEDIUM/HIGH/CRITICAL)
- Determine issue type (bug, performance, security, infra)
- Assign to appropriate specialized agent
- Estimate complexity (XS/S/M/L/XL)
- Create GitHub issue if doesn't exist

**Skills Required:**
- Issue classification (LLM-based)
- Severity assessment
- GitHub API (create/update issues)
- A2A message routing

**Adapter:** `triage_agent` (LLM-backed)

**Cost:** ~$0.10 per triage (uses kimi-k2.5:cloud)

---

### 3. **Investigation Team** (Diagnosticians)

**Role:** Root cause analysis and diagnostic reporting

**Team Composition:**
- **Orchestrator** (coordinates sub-agents)
- **Database Investigator** (queries DB, checks data integrity)
- **Code Investigator** (searches codebase, traces execution)
- **Browser Investigator** (reproduces browser-related issues)
- **External Investigator** (checks dependencies, API status)

**Responsibilities:**
- Deep dive into error context (stack traces, logs, DB state)
- Reproduce issue in isolated environment
- Identify root cause with evidence
- Propose 2-3 solution approaches
- Document findings in technical report
- Estimate fix complexity

**Skills Required:**
- Code search (grep, LSP)
- Database queries (SQL)
- Log analysis
- Playwright browser automation
- API testing
- LLM reasoning (synthesis)

**Adapter:** `investigation_orchestrator` (spawns subagents via A2A)

**Cost:** $0.50-$2.00 per investigation (uses mix of cheap/mid-tier models)

---

### 4. **Development Team** (Implementers)

**Role:** Code changes, bug fixes, feature implementation

**Team Composition:**
- **Orchestrator** (coordinates multiple devs)
- **Developer Agents** (Sonnet 4.5 for complex, Haiku for simple)
- **Documentation Agent** (Haiku for updating docs/comments)
- **Test Writer Agent** (writes unit/integration tests)

**Responsibilities:**
- Implement fix based on investigation report
- Write/update tests to cover the change
- Update documentation if needed
- Follow coding conventions (type hints, PEP 8)
- Use ecosystem tools (black, mypy, pytest)
- Commit changes to feature branch

**Skills Required:**
- Code generation (LLM)
- File editing
- Git operations
- Test writing
- Documentation

**Adapter:** `claude_code_adapter` (via acpx CLI), `copilot_adapter` (GitHub Copilot API)

**Cost:** $1.00-$5.00 per fix (uses Claude Sonnet 4.5 or Opus 4.5 for complex)

---

### 5. **Testing & QA Team** (Validators)

**Role:** Verify changes don't break existing functionality

**Responsibilities:**
- Run unit tests in isolated environment
- Run integration tests if needed
- Check code coverage delta
- Validate linting/formatting (black, flake8)
- Run security scans (bandit, safety)
- Execute smoke tests for critical paths
- Report pass/fail with detailed logs
- Suggest additional tests if coverage gap detected

**Skills Required:**
- Pytest execution
- Coverage analysis
- Linting/formatting
- Security scanning
- Test log parsing

**Adapter:** `test_runner_agent` (CLI-based)

**Cost:** FREE (subprocess execution only, zero LLM)

---

### 6. **Deployment Team** (Integrators)

**Role:** Merge changes and deploy to production

**Responsibilities:**
- Create PR from feature branch
- Run final validation in CI
- Monitor CI pipeline status
- Auto-merge if all checks pass
- Deploy to production (via GitHub Actions)
- Monitor health post-deployment
- Rollback if errors detected within 5 minutes
- Close related GitHub issue

**Skills Required:**
- GitHub API (PR creation, merge)
- CI/CD monitoring
- Health checks
- Rollback automation

**Adapter:** `deployment_agent` (GitHub API + subprocess)

**Cost:** FREE (API calls only, zero LLM)

---

### 7. **Creative/Improvement Agent** (Innovator)

**Role:** Proactive system optimization and feature proposals

**Responsibilities:**
- Weekly scan of codebase for improvement opportunities
- Analyze metrics for bottlenecks (performance, cost, reliability)
- Propose optimizations (caching, indexing, query optimization)
- Suggest new features based on usage patterns
- Consult with Deep Wiki / external research
- Draft RFC (Request for Comments) document
- Submit to "Council" (multi-agent debate) for refinement
- Send to human for approval

**Skills Required:**
- Code analysis
- Performance profiling
- Research (web, documentation)
- RFC writing
- Multi-agent coordination

**Adapter:** `creative_agent` (LLM-backed, Opus 4.5 for deep thinking)

**Cost:** $5.00-$20.00 per proposal (uses Opus 4.5 for strategic thinking)

**Frequency:** Weekly (low-frequency, high-quality)

---

## Architecture Overview

### High-Level Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    Autonomous SWE Orchestrator                   │
│                  (A2A Hub + Event Handler)                       │
└──────────────────────────────┬──────────────────────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                    │
          ▼                    ▼                    ▼
   ┌─────────────┐      ┌─────────────┐     ┌─────────────┐
   │  Monitoring │      │   Triage    │     │  Creative   │
   │   Agent     │─────▶│   Agent     │     │   Agent     │
   │  (Sentinel) │      │ (Dispatcher)│     │ (Innovator) │
   └─────────────┘      └──────┬──────┘     └─────────────┘
                               │
                   ┌───────────┼───────────┐
                   │           │           │
                   ▼           ▼           ▼
            ┌────────────┬────────────┬────────────┐
            │Investigation│Development │ Testing    │
            │   Team     │   Team     │  & QA Team │
            │(Diagnostics)│(Implementer)│(Validators)│
            └────────────┴─────┬──────┴────────────┘
                               │
                               ▼
                        ┌────────────┐
                        │ Deployment │
                        │   Team     │
                        │(Integrators)│
                        └────────────┘
                               │
                               ▼
                        ┌────────────┐
                        │  Knowledge │
                        │    Base    │
                        │  (PostgreSQL│
                        │   + Vector) │
                        └────────────┘
```

### Data Flow

```
1. Error Detected (logs, CI failure, metrics)
   ↓
2. Monitoring Agent → creates alert event
   ↓
3. Triage Agent → classifies, creates GitHub issue, routes to Investigation
   ↓
4. Investigation Team → diagnoses, creates technical report
   ↓
5. Development Team → implements fix, writes tests, commits to branch
   ↓
6. Testing Team → validates in sandbox (unit + integration)
   ├─ FAIL → back to Development (step 5)
   └─ PASS → forward to Deployment
   ↓
7. Deployment Team → creates PR, monitors CI, merges
   ↓
8. Post-Deploy Monitoring → health check for 5 minutes
   ├─ FAIL → automatic rollback
   └─ PASS → close issue, update knowledge base
   ↓
9. Knowledge Base Updated → solution stored for future reuse
```

### Integration with Existing System

The autonomous SWE system runs **parallel** to the core LinkedIn automation pipeline:

```
┌──────────────────────────────────────────────────────────────┐
│              Core LinkedIn Automation Pipeline                │
│  Scrape → Evaluate → Enrich → Tailor → Review → Apply       │
└────────────────────────┬─────────────────────────────────────┘
                         │
              (on errors, CI failures, anomalies)
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│            Autonomous SWE Team (Parallel System)              │
│  Monitor → Triage → Investigate → Develop → Test → Deploy   │
└──────────────────────────────────────────────────────────────┘
                         │
              (after validation & testing)
                         │
                         ▼
           Inject changes back into core system
           (smooth integration, monitored rollback)
```

**Key Principle:** Autonomous SWE never touches production directly. All changes go through:
1. Sandbox testing
2. CI validation
3. Staged deployment
4. Post-deploy health check
5. Automatic rollback on failure

---

## Core Components

### Component 1: GitHub Integration Adapter

**File:** `src/a2a/adapters/github_adapter.py`

**Purpose:** Enable A2A agents to interact with GitHub API

**Capabilities:**
- Create issues with labels and assignees
- Update issue status and comments
- Create PRs from branches
- Merge PRs with validation
- Fetch workflow runs and logs
- List open issues by label/milestone
- Search code across repository

**Skills:**
```yaml
- id: create_issue
  name: "Create GitHub Issue"
  description: "Create a new issue with labels, assignees, and milestone"
  tags: [github, issue, create]

- id: create_pr
  name: "Create Pull Request"
  description: "Create PR from feature branch with description and reviewers"
  tags: [github, pr, create]

- id: merge_pr
  name: "Merge Pull Request"
  description: "Merge a PR after validation checks pass"
  tags: [github, pr, merge]

- id: get_workflow_logs
  name: "Get CI Workflow Logs"
  description: "Fetch logs from failed GitHub Actions workflow"
  tags: [github, ci, logs]

- id: search_code
  name: "Search Code"
  description: "Search across repository for code patterns"
  tags: [github, search, code]
```

**Configuration:**
```yaml
# Add to config/a2a_hub.yaml
adapters:
  github:
    enabled: true
    type: "http"
    endpoint: "https://api.github.com"
    api_key: "${GITHUB_TOKEN:}"
    timeout: 30
    skills: [create_issue, create_pr, merge_pr, get_workflow_logs, search_code]
```

---

### Component 2: Problem Analysis Agent

**File:** `src/swe_agents/problem_analyzer.py`

**Purpose:** Perform root cause analysis on detected issues

**Algorithm:**
1. Parse error logs/stack traces
2. Query database for related state
3. Search codebase for error location
4. Analyze recent git history (what changed?)
5. Check external dependencies (API status, network)
6. Synthesize findings into diagnostic report

**Output Format (Diagnostic Report):**
```json
{
  "issue_id": "gh-123",
  "severity": "HIGH",
  "category": "runtime_error",
  "root_cause": {
    "primary": "NoneType error in cv_tailoring/pipeline.py:470",
    "secondary": "Missing null check for run_research() return value"
  },
  "evidence": [
    "Stack trace shows AttributeError on line 470",
    "run_research() can return None (line 384)",
    "No null guard exists before accessing result"
  ],
  "proposed_solutions": [
    {
      "approach": "Add null guard with fallback to CrewAI",
      "complexity": "S",
      "risk": "LOW",
      "estimated_time": "30min"
    },
    {
      "approach": "Make run_research() never return None",
      "complexity": "M",
      "risk": "MEDIUM",
      "estimated_time": "2h"
    }
  ],
  "affected_files": [
    "src/cv_tailoring/pipeline.py",
    "src/cv_tailoring/acp/runner.py"
  ],
  "test_coverage": "67% (missing null case)",
  "related_issues": ["gh-89", "gh-102"]
}
```

---

### Component 3: Knowledge Base

**Database Schema:**

```sql
-- Table: swe_knowledge_base
CREATE TABLE swe_knowledge_base (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    issue_id VARCHAR(50) NOT NULL,  -- GitHub issue number
    problem_signature TEXT NOT NULL,  -- Hash of error pattern
    error_category VARCHAR(50),  -- runtime_error, import_error, type_error, etc.
    root_cause TEXT,
    solution TEXT,
    files_modified JSONB,  -- List of files changed
    git_commit_sha VARCHAR(40),  -- Commit that fixed it
    success_rate FLOAT DEFAULT 1.0,  -- 1.0 = worked every time
    times_reused INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    last_used_at TIMESTAMP,
    embedding VECTOR(768),  -- SBERT embedding for semantic search
    metadata JSONB  -- Arbitrary context (Python version, dependencies, etc.)
);

CREATE INDEX idx_knowledge_problem_sig ON swe_knowledge_base(problem_signature);
CREATE INDEX idx_knowledge_category ON swe_knowledge_base(error_category);
CREATE INDEX idx_knowledge_embedding ON swe_knowledge_base USING ivfflat (embedding vector_cosine_ops);
```

**Usage:**
- Before investigating, query knowledge base for similar errors (vector similarity)
- If match found with success_rate > 0.8, reuse solution directly
- If match found with success_rate < 0.8, use as starting point
- After fix deployed successfully, add to knowledge base

---

### Component 4: Sandbox Testing Environment

**Purpose:** Isolated environment for testing code changes before production

**Implementation:**
- Docker container with identical dependencies
- Copy of database schema (no production data)
- Mocked external APIs (LinkedIn, LLM proxy)
- Separate git branch (e.g., `swe-sandbox-fix-123`)

**Validation Steps:**
1. Create sandbox container
2. Apply code changes
3. Run unit tests (`pytest tests/unit/`)
4. Run integration tests (`pytest tests/integration/`)
5. Run smoke tests (critical path validation)
6. Check code coverage delta
7. Validate security (bandit, safety)
8. Destroy sandbox container

**Cost:** FREE (uses GitHub Actions free tier or local Docker)

---

### Component 5: Ralph Wiggum Loop (Bug-First Policy)

**Principle:** Fix bugs before building new features

**Implementation:**
- Triage Agent assigns priority scores:
  - **CRITICAL** (prod down): Priority 1, immediate
  - **HIGH** (broken feature): Priority 2, within 1 hour
  - **MEDIUM** (degraded UX): Priority 3, within 24 hours
  - **LOW** (minor issue): Priority 4, weekly batch
- Development Team pulls from priority queue
- New features blocked if P1/P2 bugs exist
- Weekly "bug bash" to clear P3/P4 backlog

**Governance:**
- Human can override (promote feature to P1)
- Creative Agent proposals go through approval flow (not auto-implemented)

---

### Component 6: CI/CD Feedback Loop

**Purpose:** Automatically rollback failed deployments

**Workflow:**
```
1. Deployment Team merges PR
   ↓
2. GitHub Actions CI runs
   ├─ Linting (flake8, black)
   ├─ Type checking (mypy)
   ├─ Unit tests (pytest)
   ├─ Integration tests
   ├─ Security scans (bandit, safety)
   └─ Build validation
   ↓
3. If ANY check fails:
   ├─ Trigger rollback
   ├─ Revert commit
   ├─ Re-open GitHub issue
   ├─ Send alert to human
   └─ Create incident report
   ↓
4. If all checks pass:
   └─ Deploy to production
```

**Monitoring Window:** 5 minutes post-deploy
- Track error rate (logs, Sentry)
- Track performance (latency p95)
- Track success metrics (scraper job count, application success rate)
- If degradation > 20%, trigger automatic rollback

---

## Workflows & Use Cases

### Use Case 1: Job Scraper Encounters CAPTCHA

**Scenario:** Job scraper hits CAPTCHA after scraping 20 jobs

**Workflow:**

```
1. [09:15 AM] Scraper Supervisor detects anomaly (CAPTCHA detected)
   - Tier 1 self-healing fails (no automatic solution for CAPTCHA)
   - Tier 2 Goose escalation triggered
   - Goose attempts recovery (fails after 3 minutes)
   ↓
2. [09:18 AM] Monitoring Agent receives failure signal
   - Scans logs, detects pattern: "CAPTCHA challenge"
   - Triggers alert event via A2A Hub
   ↓
3. [09:18 AM] Triage Agent receives alert
   - Classification: HIGH severity, category: "scraping_challenge"
   - Estimates complexity: M (medium)
   - Creates GitHub issue #234: "CAPTCHA blocking scraper"
   - Routes to Investigation Team
   ↓
4. [09:19 AM] Investigation Team starts
   - Orchestrator spawns Browser Investigator
   - Browser Investigator reproduces issue (navigates to search page)
   - Confirms CAPTCHA appears after ~15 pages
   - Checks knowledge base: finds similar issue #189 (solved 30 days ago)
   - Solution: "Add random delay + human-like behavior"
   - Verifies solution still works (success_rate: 0.9)
   - Creates diagnostic report (reuses #189 solution)
   ↓
5. [09:25 AM] Development Team starts
   - Orchestrator assigns to Haiku (simple fix)
   - Haiku applies fix: adds jitter to pagination delay
   - Updates test: adds CAPTCHA mock test case
   - Commits to branch: `swe-fix-234-captcha-delay`
   ↓
6. [09:30 AM] Testing Team validates
   - Runs unit tests: PASS (28/28)
   - Runs integration tests: PASS (12/12)
   - Runs scraper smoke test with mocked CAPTCHA: PASS
   - Coverage delta: +2% (new test added)
   ↓
7. [09:35 AM] Deployment Team executes
   - Creates PR #456: "Fix CAPTCHA by adding randomized delays"
   - CI checks: ALL PASS
   - Auto-merges PR
   - Deploys to production
   - Monitors for 5 minutes: scraper running, no CAPTCHA
   - Closes issue #234
   ↓
8. [09:45 AM] Knowledge Base updated
   - Updates entry for "CAPTCHA challenge"
   - Increments times_reused: 1 → 2
   - Updates last_used_at timestamp
   - Success!
```

**Total Time:** 30 minutes (detection → resolution → deployment)

**Cost:**
- Triage: $0.10
- Investigation: $0.50 (found existing solution quickly)
- Development: $0.50 (Haiku)
- Testing: $0 (subprocess)
- Deployment: $0 (API calls)
- **Total: $1.10**

---

### Use Case 2: CI Pipeline Failure (Broken Tests)

**Scenario:** PR merged breaks 5 tests in `tests/unit/test_cv_tailoring.py`

**Workflow:**

```
1. [11:45 AM] GitHub Actions CI fails
   - 5 tests fail in test_cv_tailoring.py
   - Error: AttributeError: 'NoneType' object has no attribute 'content'
   ↓
2. [11:47 AM] Monitoring Agent detects CI failure
   - Fetches workflow logs via GitHub API
   - Parses failure: test_cv_generation_with_acp failed
   - Triggers alert event
   ↓
3. [11:47 AM] Triage Agent classifies
   - Severity: CRITICAL (CI broken, blocks all PRs)
   - Category: "test_failure"
   - Creates issue #235: "CI broken: test_cv_tailoring.py failures"
   - Routes to Investigation Team
   ↓
4. [11:48 AM] Investigation Team analyzes
   - Code Investigator checks recent commits (last 24h)
   - Finds commit d4f3a21: "Refactor ACP pipeline error handling"
   - Suspects: run_research() now returns None in some cases
   - Confirms: tests assume run_research() never returns None
   - Diagnostic: "Missing null guard in pipeline.py:470"
   ↓
5. [11:55 AM] Development Team fixes
   - Orchestrator assigns to Sonnet 4.5 (moderate complexity)
   - Sonnet adds null guard: if result is None, fallback to CrewAI
   - Updates 5 tests to cover None case
   - Commits to branch: `swe-fix-235-null-guard`
   ↓
6. [12:05 PM] Testing Team validates
   - Runs all unit tests: PASS (28/28)
   - Runs integration tests: PASS
   - Coverage: 100% (null case now covered)
   ↓
7. [12:10 PM] Deployment Team merges
   - Creates PR #457: "Fix NoneType error in CV tailoring"
   - CI checks: ALL PASS (previously failing tests now pass)
   - Auto-merges PR
   - Closes issue #235
   ↓
8. [12:15 PM] Knowledge Base updated
   - Stores solution: "Add null guard for run_research()"
   - Tags: ["cv_tailoring", "null_check", "acp"]
   - Embedding generated for future semantic search
   - Success!
```

**Total Time:** 30 minutes

**Cost:**
- Triage: $0.10
- Investigation: $0.80 (code analysis)
- Development: $1.50 (Sonnet 4.5)
- Testing: $0
- Deployment: $0
- **Total: $2.40**

---

### Use Case 3: Creative Agent Proposes Optimization

**Scenario:** Weekly scan identifies performance bottleneck in job evaluation

**Workflow:**

```
1. [Monday 8:00 AM] Creative Agent starts weekly scan
   - Analyzes metrics: evaluation stage takes 3 minutes per job
   - Checks database: 10,000+ duplicate embedding calculations
   - Researches: finds caching pattern in SBERT docs
   - Hypothesis: "Cache embeddings to reduce LLM calls by 70%"
   ↓
2. [Monday 8:30 AM] Creative Agent drafts RFC
   - Title: "RFC-001: Cache Job Embeddings for Faster Evaluation"
   - Problem: Duplicate embedding calculations waste $500/month
   - Proposal: Store embeddings in jobs.embedding column (VECTOR type)
   - Benefits: 70% faster evaluation, $350/month savings
   - Risks: Schema migration required, cache invalidation strategy
   - Alternatives: Use Redis cache (ephemeral), use external service
   ↓
3. [Monday 9:00 AM] Council Meeting (multi-agent debate)
   - Participants: Creative Agent, Investigation Agent, Dev Agent
   - Discussion:
     - Dev: "Schema migration is low-risk, we have rollback"
     - Investigation: "Need cache invalidation when job description changes"
     - Creative: "Add updated_at trigger to invalidate cache"
   - Consensus: Approve with cache invalidation strategy
   ↓
4. [Monday 9:30 AM] Send to human for approval
   - Sends RFC to human via Telegram
   - Human responds: "Approved. Implement in Q2."
   - Creative Agent creates GitHub issue #236: "RFC-001: Cache embeddings"
   - Assigns to Development Team, milestone: Q2-2026
   ↓
5. [Later in Q2] Development Team implements
   - (Standard workflow: Investigate → Develop → Test → Deploy)
   - Success!
```

**Total Time:** 1.5 hours for proposal (weekly cadence)

**Cost:**
- Creative Agent: $15.00 (Opus 4.5 for deep thinking)
- Council Meeting: $5.00 (3 agents × 5 min each)
- **Total: $20.00** (monthly cost: ~$80)

---

## Implementation Phases

### Phase 1: Core Infrastructure (Weeks 1-2)

**Goal:** Build foundational components for autonomous operation

**Deliverables:**
1. GitHub Integration Adapter (`src/a2a/adapters/github_adapter.py`)
2. Problem Analysis Agent (`src/swe_agents/problem_analyzer.py`)
3. Knowledge Base schema and repository (`src/database/models/knowledge_base.py`)
4. Monitoring Agent (`src/swe_agents/monitor_agent.py`)
5. Triage Agent (`src/swe_agents/triage_agent.py`)

**Tests:**
- Unit tests for each component (>80% coverage)
- Integration test: Monitoring → Triage → GitHub issue creation
- Mock GitHub API responses

**Success Criteria:**
- Monitoring Agent detects errors from logs
- Triage Agent creates GitHub issues automatically
- Knowledge Base stores and retrieves solutions

**Estimated Effort:** 40 hours (human supervision)

---

### Phase 2: Development & Testing Pipeline (Weeks 3-4)

**Goal:** Enable autonomous code changes with validation

**Deliverables:**
1. Investigation Team Orchestrator (`src/swe_agents/investigation_orchestrator.py`)
2. Development Team Orchestrator (`src/swe_agents/dev_orchestrator.py`)
3. Claude Code Adapter (via acpx CLI)
4. Test Runner Agent (`src/swe_agents/test_runner.py`)
5. Sandbox environment setup (Docker)

**Tests:**
- End-to-end test: Issue → Investigation → Fix → Test → Report
- Sandbox isolation validation
- Test coverage tracking

**Success Criteria:**
- Investigation Team produces diagnostic reports
- Development Team generates code fixes
- Test Runner validates changes in sandbox
- No production data touched during testing

**Estimated Effort:** 60 hours

---

### Phase 3: Deployment & Rollback (Weeks 5-6)

**Goal:** Safe deployment with automatic rollback

**Deliverables:**
1. Deployment Agent (`src/swe_agents/deployment_agent.py`)
2. PR creation and merge automation
3. CI/CD monitoring integration
4. Automatic rollback system
5. Post-deploy health checks

**Tests:**
- Rollback trigger on test failure
- Health monitoring alerts
- PR merge workflow

**Success Criteria:**
- Deployment Agent creates PRs
- CI failures trigger rollback
- Health degradation triggers rollback within 5 minutes
- Zero production outages during testing

**Estimated Effort:** 40 hours

---

### Phase 4: Creative & Improvement Loop (Weeks 7-8)

**Goal:** Proactive optimization proposals

**Deliverables:**
1. Creative Agent (`src/swe_agents/creative_agent.py`)
2. Council system (multi-agent debate)
3. RFC template and workflow
4. Human approval integration (Telegram)

**Tests:**
- Weekly scan execution
- RFC generation
- Council debate simulation

**Success Criteria:**
- Creative Agent runs weekly
- Generates at least 1 RFC per week
- Human can approve/reject proposals

**Estimated Effort:** 30 hours

---

### Phase 5: Ralph Wiggum Loop & Governance (Week 9)

**Goal:** Enforce bug-first policy and governance

**Deliverables:**
1. Priority queue system
2. Bug-first policy enforcement
3. Human veto mechanism
4. Audit log for all autonomous actions

**Tests:**
- Priority queue ordering
- Feature blocking when P1/P2 bugs exist

**Success Criteria:**
- Bugs fixed before features
- Human can override decisions
- Full audit trail of all actions

**Estimated Effort:** 20 hours

---

### Phase 6: Optimization & Monitoring (Week 10-12)

**Goal:** Cost optimization and comprehensive observability

**Deliverables:**
1. Token usage dashboard (Grafana)
2. Model selection optimization (cheap for simple, expensive for complex)
3. Metrics: MTTR (Mean Time To Resolution), fix success rate
4. Documentation and runbooks

**Tests:**
- Cost tracking accuracy
- Metrics collection

**Success Criteria:**
- Token costs < $100/month (95% of fixes)
- MTTR < 1 hour for HIGH severity
- Fix success rate > 90%
- Full documentation published

**Estimated Effort:** 30 hours

---

**Total Estimated Effort:** 220 hours (~6 weeks with 2 developers)

---

## Token Economy & Cost Management

### Cost Optimization Strategy

| Task | Model | Cost per Call | Frequency | Monthly Cost |
|------|-------|---------------|-----------|--------------|
| Monitoring | FREE (heuristic) | $0 | 8,640/month | $0 |
| Triage | kimi-k2.5 | $0.10 | ~50/month | $5 |
| Investigation | kimi-k2.5 | $0.80 | ~50/month | $40 |
| Development (simple) | Claude Haiku | $0.50 | ~30/month | $15 |
| Development (moderate) | Claude Sonnet 4.5 | $1.50 | ~15/month | $22.50 |
| Development (complex) | Claude Opus 4.5 | $5.00 | ~5/month | $25 |
| Testing | FREE (subprocess) | $0 | ~50/month | $0 |
| Deployment | FREE (API calls) | $0 | ~50/month | $0 |
| Creative | Claude Opus 4.5 | $15.00 | ~4/month | $60 |
| **Total** | | | | **$167.50/month** |

### Free Tier Usage

- **GitHub Actions**: 2,000 minutes/month (free for public repos)
- **Supabase**: 500 MB database (free tier)
- **Tailscale**: 20 devices (free tier)
- **Playwright**: Free (open source)

### Scaling Assumptions

- 50 issues/month (realistic for mature codebase)
- 60% auto-resolved (30 issues)
- 40% require human intervention (20 issues)
- Average cost per fix: $2.50
- Total: **$75/month** (auto-fixes only)

### Return on Investment (ROI)

**Without Autonomous SWE:**
- Human developer: $60/hour
- 30 issues/month × 1 hour/issue = 30 hours
- Cost: **$1,800/month**

**With Autonomous SWE:**
- LLM costs: **$75/month**
- Human supervision: 5 hours/month × $60/hour = $300/month
- Total: **$375/month**

**Savings:** $1,425/month (79% reduction)

**Break-even:** Immediate (infrastructure already exists)

---

## Security & Governance

### Security Principles

1. **Least Privilege**: Each agent has minimal permissions
2. **Audit Trail**: All actions logged with correlation IDs
3. **Human Veto**: Humans can stop/override any agent
4. **Sandbox Isolation**: Code changes tested in isolated environment
5. **Credential Management**: No agents have access to production credentials
6. **Rate Limiting**: Prevent runaway LLM costs (max $50/day)

### Governance Model

**Decision Authority:**

| Decision Type | Authority | Approval Required |
|---------------|-----------|-------------------|
| Bug fix (P1/P2) | Autonomous | None |
| Bug fix (P3/P4) | Autonomous | Human review within 24h |
| Performance optimization | Autonomous (if <5% change) | Human approval |
| Feature addition | Creative Agent proposal | Human approval |
| Schema migration | Creative Agent proposal | Human approval |
| Security fix | Autonomous | Human notified immediately |
| Rollback | Autonomous | None (auto-trigger) |

**Escalation Path:**

```
1. Autonomous Agent attempts resolution
   ↓
2. If stuck after 30 minutes → escalate to human
   ↓
3. Human reviews diagnostic report
   ↓
4. Human provides guidance or takes over
```

### Audit & Compliance

**Audit Log Schema:**

```sql
CREATE TABLE swe_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(50),  -- investigation, development, deployment, rollback
    agent_name VARCHAR(100),
    action TEXT,
    target VARCHAR(200),  -- file, PR, issue
    correlation_id UUID,  -- Links related actions
    success BOOLEAN,
    error_message TEXT,
    metadata JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
```

**Retention:** 90 days (compliance with GDPR right to be forgotten)

---

## Success Metrics

### Key Performance Indicators (KPIs)

| Metric | Target | Measurement |
|--------|--------|-------------|
| **MTTR** (Mean Time To Resolution) | < 1 hour (P1/P2) | Time from detection to deployment |
| **Fix Success Rate** | > 90% | Fixes that don't get reverted |
| **Test Coverage** | > 85% | Lines covered by tests |
| **Zero Abandoned Issues** | 100% | All issues closed or explicitly deferred |
| **Cost per Fix** | < $3.00 | Average LLM cost per resolved issue |
| **Human Intervention Rate** | < 30% | Issues requiring human help |
| **Rollback Rate** | < 5% | Deployments that get rolled back |
| **Knowledge Base Hit Rate** | > 40% | Issues solved via cached solutions |

### Monitoring Dashboard

**Grafana Panels:**
1. Issues created vs resolved (daily)
2. Average resolution time (by severity)
3. LLM token usage (by agent)
4. Cost per fix (trend)
5. Test coverage delta (per PR)
6. Rollback frequency
7. Knowledge base usage

---

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| **Runaway LLM costs** | Medium | High | Rate limiting ($50/day), model tier enforcement |
| **Incorrect fixes** | Medium | High | Sandbox testing, CI validation, automatic rollback |
| **Knowledge base pollution** | Low | Medium | Human review of stored solutions, success_rate tracking |
| **Agent coordination failures** | Low | Medium | Timeout mechanisms, retry logic, human escalation |
| **Security vulnerabilities** | Low | Critical | Automated security scans (bandit, safety), human review |
| **Production outage** | Very Low | Critical | Post-deploy monitoring, 5-minute rollback window |

---

## Conclusion

The Autonomous SWE Team architecture leverages LinkedAi's existing A2A infrastructure, event-driven orchestration, and comprehensive testing framework to create a self-healing, self-improving development loop. By combining heuristic monitoring, LLM-powered diagnosis and development, and rigorous validation, the system can autonomously resolve 60-70% of issues while reducing human developer time by 79%.

The phased implementation plan (12 weeks) ensures careful validation at each stage, with full audit trails and human oversight. The token economy strategy keeps costs under $200/month while providing 24/7 coverage and sub-hour response times for critical issues.

**Next Steps:**
1. Get stakeholder approval on architecture
2. Allocate resources for Phase 1 implementation
3. Set up monitoring dashboard for tracking success metrics
4. Begin Phase 1: Core Infrastructure (Weeks 1-2)

---

## Appendices

### Appendix A: Adapter Configuration Reference

See `config/a2a_hub.yaml` for adapter definitions. New adapters to add:

```yaml
adapters:
  github:
    enabled: true
    type: "http"
    endpoint: "https://api.github.com"
    api_key: "${GITHUB_TOKEN:}"
    timeout: 30
    skills: [create_issue, create_pr, merge_pr, get_workflow_logs, search_code]

  monitor_agent:
    enabled: true
    type: "cli"
    binary: ".venv/bin/python3"
    args: ["src/swe_agents/monitor_agent.py", "--message", "{message}"]
    timeout: 60
    skills: [scan_logs, check_ci, detect_anomalies]

  triage_agent:
    enabled: true
    type: "cli"
    binary: ".venv/bin/python3"
    args: ["src/swe_agents/triage_agent.py", "--message", "{message}"]
    timeout: 120
    skills: [classify_issue, assign_severity, create_github_issue]

  investigation_orchestrator:
    enabled: true
    type: "cli"
    binary: ".venv/bin/python3"
    args: ["src/swe_agents/investigation_orchestrator.py", "--message", "{message}"]
    timeout: 600
    skills: [diagnose, root_cause_analysis, generate_report]

  dev_orchestrator:
    enabled: true
    type: "cli"
    binary: ".venv/bin/python3"
    args: ["src/swe_agents/dev_orchestrator.py", "--message", "{message}"]
    timeout: 1800
    skills: [implement_fix, write_tests, commit_changes]

  test_runner:
    enabled: true
    type: "cli"
    binary: ".venv/bin/python3"
    args: ["src/swe_agents/test_runner.py", "--message", "{message}"]
    timeout: 600
    skills: [run_unit_tests, run_integration_tests, check_coverage]

  deployment_agent:
    enabled: true
    type: "cli"
    binary: ".venv/bin/python3"
    args: ["src/swe_agents/deployment_agent.py", "--message", "{message}"]
    timeout: 900
    skills: [create_pr, monitor_ci, merge_pr, rollback]

  creative_agent:
    enabled: true
    type: "cli"
    binary: ".venv/bin/python3"
    args: ["src/swe_agents/creative_agent.py", "--message", "{message}"]
    timeout: 3600
    skills: [scan_codebase, draft_rfc, propose_optimization]
```

### Appendix B: Event Types

New event types to add to `src/a2a/events.py`:

```python
class EventType(str, enum.Enum):
    # ... existing events ...

    # New SWE events
    ERROR_DETECTED = "error_detected"
    ISSUE_CREATED = "issue_created"
    INVESTIGATION_COMPLETE = "investigation_complete"
    FIX_PROPOSED = "fix_proposed"
    FIX_IMPLEMENTED = "fix_implemented"
    TESTS_PASSED = "tests_passed"
    TESTS_FAILED = "tests_failed"
    DEPLOYED = "deployed"
    ROLLBACK_TRIGGERED = "rollback_triggered"
    RFC_PROPOSED = "rfc_proposed"
    RFC_APPROVED = "rfc_approved"
```

### Appendix C: GitHub Issue Templates

Add new template: `.github/ISSUE_TEMPLATE/auto_generated.md`

```yaml
name: Auto-Generated Issue
description: Automatically created by autonomous SWE system
title: "[AUTO] "
labels: ["auto-generated", "needs-triage"]
body:
  - type: markdown
    attributes:
      value: |
        This issue was automatically created by the autonomous SWE monitoring system.

  - type: textarea
    id: error
    attributes:
      label: Error Details
      description: Stack trace or error message
    validations:
      required: true

  - type: textarea
    id: context
    attributes:
      label: Context
      description: When and where the error occurred
    validations:
      required: true

  - type: textarea
    id: diagnostic
    attributes:
      label: Diagnostic Report
      description: Initial analysis from problem analyzer
    validations:
      required: false
```

### Appendix D: Database Migrations

**Migration:** `src/database/migrations/024_swe_knowledge_base.sql`

```sql
-- Create knowledge base table
CREATE TABLE swe_knowledge_base (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    issue_id VARCHAR(50) NOT NULL,
    problem_signature TEXT NOT NULL,
    error_category VARCHAR(50),
    root_cause TEXT,
    solution TEXT,
    files_modified JSONB,
    git_commit_sha VARCHAR(40),
    success_rate FLOAT DEFAULT 1.0,
    times_reused INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    last_used_at TIMESTAMP,
    embedding VECTOR(768),
    metadata JSONB
);

CREATE INDEX idx_knowledge_problem_sig ON swe_knowledge_base(problem_signature);
CREATE INDEX idx_knowledge_category ON swe_knowledge_base(error_category);
CREATE INDEX idx_knowledge_embedding ON swe_knowledge_base USING ivfflat (embedding vector_cosine_ops);

-- Create audit log table
CREATE TABLE swe_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(50),
    agent_name VARCHAR(100),
    action TEXT,
    target VARCHAR(200),
    correlation_id UUID,
    success BOOLEAN,
    error_message TEXT,
    metadata JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_audit_event_type ON swe_audit_log(event_type);
CREATE INDEX idx_audit_correlation ON swe_audit_log(correlation_id);
CREATE INDEX idx_audit_created_at ON swe_audit_log(created_at);

-- Add SWE metadata to pipeline_runs table
ALTER TABLE pipeline_runs
ADD COLUMN swe_enabled BOOLEAN DEFAULT FALSE,
ADD COLUMN swe_metadata JSONB;
```

---

**End of Document**

---

**Document Metadata:**
- **Version:** 1.0
- **Last Updated:** 2026-03-13
- **Authors:** Claude Sonnet 4.5 (AI Product Management)
- **Review Status:** Draft (awaiting stakeholder approval)
- **Related Issues:** GitHub Issue (to be created)
- **Related CRs:** CR-013, CR-015, CR-016, CR-019
