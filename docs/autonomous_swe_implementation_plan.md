# Autonomous SWE Team Implementation Plan

**Project:** LinkedAi Autonomous Software Engineering Team
**Version:** 1.0
**Status:** Planning
**Start Date:** 2026-03-13
**Target Completion:** 2026-05-29 (12 weeks)

---

## Overview

This document provides the detailed implementation plan for building the Autonomous Software Engineering Team described in `docs/architecture/autonomous_swe_team_architecture.md`.

---

## Implementation Phases

### Phase 1: Core Infrastructure (Weeks 1-2)

**Duration:** 10 working days
**Goal:** Build foundational components for autonomous operation

#### Week 1: GitHub Integration & Monitoring

**Day 1-2: GitHub Adapter**
- [ ] Create `src/a2a/adapters/github_adapter.py`
- [ ] Implement GitHub API client (issues, PRs, workflows)
- [ ] Add skills: create_issue, create_pr, merge_pr, get_workflow_logs
- [ ] Add configuration to `config/a2a_hub.yaml`
- [ ] Write unit tests (mock GitHub API responses)
- [ ] Test with real GitHub API (using test repository)

**Day 3-4: Monitor Agent**
- [ ] Create `src/swe_agents/monitor_agent.py`
- [ ] Implement log scanning (error/warning detection)
- [ ] Add CI/CD pipeline monitoring (GitHub Actions API)
- [ ] Add metrics monitoring (from observability module)
- [ ] Implement alert triggering (via A2A Hub events)
- [ ] Write unit tests (mock log files, mock metrics)
- [ ] Integration test: Monitor → Trigger alert event

**Day 5: Knowledge Base Schema**
- [ ] Create database migration `src/database/migrations/024_swe_knowledge_base.sql`
- [ ] Add `swe_knowledge_base` table (with VECTOR support)
- [ ] Add `swe_audit_log` table
- [ ] Update `pipeline_runs` table with SWE metadata columns
- [ ] Create repository class `src/database/repositories/knowledge_base_repo.py`
- [ ] Write unit tests for repository CRUD operations

#### Week 2: Problem Analysis & Triage

**Day 6-7: Problem Analyzer**
- [ ] Create `src/swe_agents/problem_analyzer.py`
- [ ] Implement log parsing and stack trace analysis
- [ ] Add code search integration (grep, LSP)
- [ ] Add database query for context gathering
- [ ] Implement git history analysis (recent commits)
- [ ] Generate diagnostic report (structured JSON)
- [ ] Write unit tests (mock error scenarios)

**Day 8-9: Triage Agent**
- [ ] Create `src/swe_agents/triage_agent.py`
- [ ] Implement issue classification (LLM-based)
- [ ] Add severity assessment (CRITICAL/HIGH/MEDIUM/LOW)
- [ ] Add complexity estimation (XS/S/M/L/XL)
- [ ] Implement GitHub issue creation
- [ ] Add routing logic (assign to Investigation Team)
- [ ] Write unit tests (mock LLM responses, mock GitHub API)
- [ ] Integration test: Alert → Triage → GitHub issue created

**Day 10: Phase 1 Integration Test**
- [ ] End-to-end test: Error in logs → Monitor detects → Triage creates issue
- [ ] Verify knowledge base stores diagnostic data
- [ ] Check audit log entries
- [ ] Performance test (process 100 errors in <5 minutes)

---

### Phase 2: Development & Testing Pipeline (Weeks 3-4)

**Duration:** 10 working days
**Goal:** Enable autonomous code changes with validation

#### Week 3: Investigation Team

**Day 11-12: Investigation Orchestrator**
- [ ] Create `src/swe_agents/investigation_orchestrator.py`
- [ ] Implement subagent coordination (spawn multiple investigators)
- [ ] Add Database Investigator (SQL queries for state)
- [ ] Add Code Investigator (search codebase, trace execution)
- [ ] Add Browser Investigator (reproduce browser issues via Playwright)
- [ ] Implement report synthesis (combine findings from subagents)
- [ ] Write unit tests (mock subagent responses)

**Day 13-14: Knowledge Base Integration**
- [ ] Add semantic search (SBERT embeddings)
- [ ] Implement solution retrieval (by similarity)
- [ ] Add solution reuse logic (success_rate > 0.8)
- [ ] Update knowledge base after successful fix
- [ ] Write unit tests (mock embeddings, mock DB queries)

**Day 15: Investigation Team Integration Test**
- [ ] End-to-end test: Issue → Investigation → Diagnostic report
- [ ] Test knowledge base hit (reuse existing solution)
- [ ] Test knowledge base miss (new investigation required)

#### Week 4: Development Team

**Day 16-17: Dev Orchestrator**
- [ ] Create `src/swe_agents/dev_orchestrator.py`
- [ ] Implement subagent coordination (multiple devs working in parallel)
- [ ] Add Claude Code integration (via acpx CLI)
- [ ] Add Copilot integration (GitHub Copilot API)
- [ ] Implement file editing and git operations
- [ ] Write unit tests (mock acpx responses, mock git)

**Day 18-19: Test Runner**
- [ ] Create `src/swe_agents/test_runner.py`
- [ ] Implement pytest execution (unit + integration tests)
- [ ] Add coverage analysis (pytest-cov)
- [ ] Add linting validation (flake8, black)
- [ ] Add security scanning (bandit, safety)
- [ ] Implement test log parsing
- [ ] Write unit tests (mock pytest output)

**Day 20: Sandbox Environment**
- [ ] Create Docker sandbox configuration
- [ ] Implement sandbox creation/destruction
- [ ] Add environment isolation (no prod data/credentials)
- [ ] Write integration test: Deploy to sandbox → Run tests → Destroy

---

### Phase 3: Deployment & Rollback (Weeks 5-6)

**Duration:** 10 working days
**Goal:** Safe deployment with automatic rollback

#### Week 5: Deployment Agent

**Day 21-22: PR Creation & Merge**
- [ ] Create `src/swe_agents/deployment_agent.py`
- [ ] Implement PR creation (via GitHub API)
- [ ] Add PR description generation (summarize changes)
- [ ] Implement merge automation (after CI passes)
- [ ] Write unit tests (mock GitHub API)

**Day 23-24: CI/CD Monitoring**
- [ ] Add GitHub Actions workflow monitoring
- [ ] Implement CI failure detection
- [ ] Add workflow log retrieval and parsing
- [ ] Write unit tests (mock workflow API responses)

**Day 25: Phase 3 Integration Test Part 1**
- [ ] End-to-end test: Fix → PR creation → CI validation

#### Week 6: Rollback System

**Day 26-27: Rollback Automation**
- [ ] Implement automatic rollback on CI failure
- [ ] Add git revert logic
- [ ] Implement issue re-opening
- [ ] Add incident report generation
- [ ] Write unit tests (mock git operations)

**Day 28-29: Post-Deploy Monitoring**
- [ ] Add health check monitoring (5-minute window)
- [ ] Implement metric degradation detection
- [ ] Add automatic rollback trigger (error rate spike)
- [ ] Write unit tests (mock metrics)

**Day 30: Phase 3 Integration Test Part 2**
- [ ] End-to-end test: Deploy → CI fails → Rollback triggered
- [ ] End-to-end test: Deploy → Health degrades → Rollback triggered
- [ ] Verify issue re-opened with incident report

---

### Phase 4: Creative & Improvement Loop (Weeks 7-8)

**Duration:** 10 working days
**Goal:** Proactive optimization proposals

#### Week 7: Creative Agent

**Day 31-32: Weekly Scanner**
- [ ] Create `src/swe_agents/creative_agent.py`
- [ ] Implement codebase scanning (metrics, patterns)
- [ ] Add bottleneck detection (performance profiling)
- [ ] Add optimization opportunity identification
- [ ] Write unit tests (mock metrics, mock code analysis)

**Day 33-34: RFC Generation**
- [ ] Implement RFC template and generator
- [ ] Add problem statement synthesis
- [ ] Add proposal generation (with alternatives)
- [ ] Add risk assessment
- [ ] Write unit tests (mock LLM responses)

**Day 35: Phase 4 Integration Test Part 1**
- [ ] End-to-end test: Weekly scan → RFC generated

#### Week 8: Council & Approval

**Day 36-37: Council System**
- [ ] Implement multi-agent debate (3-5 agents)
- [ ] Add consensus mechanism
- [ ] Add refinement loop (iterate on proposal)
- [ ] Write unit tests (mock agent responses)

**Day 38-39: Human Approval Integration**
- [ ] Add Telegram notification for RFC
- [ ] Implement approval/rejection workflow
- [ ] Add GitHub issue creation for approved RFCs
- [ ] Write unit tests (mock Telegram API)

**Day 40: Phase 4 Integration Test Part 2**
- [ ] End-to-end test: RFC → Council → Human approval → Issue created

---

### Phase 5: Ralph Wiggum Loop & Governance (Week 9)

**Duration:** 5 working days
**Goal:** Enforce bug-first policy and governance

**Day 41: Priority Queue**
- [ ] Implement priority queue system (Redis or PostgreSQL)
- [ ] Add priority scoring (CRITICAL → P1, HIGH → P2, etc.)
- [ ] Add queue ordering logic
- [ ] Write unit tests (mock queue operations)

**Day 42: Bug-First Policy**
- [ ] Implement feature blocking when P1/P2 bugs exist
- [ ] Add policy enforcement checks
- [ ] Write unit tests (mock issue counts)

**Day 43: Human Veto Mechanism**
- [ ] Add human override capability (Telegram commands)
- [ ] Implement priority promotion (feature → P1)
- [ ] Add veto logging (audit trail)
- [ ] Write unit tests (mock Telegram commands)

**Day 44: Audit System**
- [ ] Implement comprehensive audit logging
- [ ] Add correlation ID tracking (trace full workflow)
- [ ] Add audit report generation
- [ ] Write unit tests (mock audit queries)

**Day 45: Phase 5 Integration Test**
- [ ] End-to-end test: Bug-first policy enforcement
- [ ] End-to-end test: Human veto mechanism
- [ ] Verify audit trail completeness

---

### Phase 6: Optimization & Monitoring (Weeks 10-12)

**Duration:** 15 working days
**Goal:** Cost optimization and comprehensive observability

#### Week 10: Cost Optimization

**Day 46-47: Token Usage Tracking**
- [ ] Add token usage tracking (per agent, per task)
- [ ] Implement cost calculation (by model)
- [ ] Add budget alerts ($50/day limit)
- [ ] Write unit tests (mock token counts)

**Day 48-49: Model Selection**
- [ ] Implement complexity-based model selection
- [ ] Add routing logic (simple → Haiku, complex → Opus)
- [ ] Write unit tests (mock complexity estimation)

**Day 50: Phase 6 Part 1 Integration Test**
- [ ] End-to-end test: Track tokens across full workflow
- [ ] Verify cost < $3.00 per fix (average)

#### Week 11: Metrics & Dashboard

**Day 51-52: Metrics Collection**
- [ ] Add MTTR tracking (Mean Time To Resolution)
- [ ] Add fix success rate tracking
- [ ] Add rollback rate tracking
- [ ] Add knowledge base hit rate tracking
- [ ] Write unit tests (mock metrics)

**Day 53-54: Grafana Dashboard**
- [ ] Create Grafana dashboard configuration
- [ ] Add panels for key metrics (MTTR, costs, success rate)
- [ ] Add alerting rules (MTTR > 2h, cost > $50/day)
- [ ] Test dashboard with sample data

**Day 55: Phase 6 Part 2 Integration Test**
- [ ] Verify all metrics collected correctly
- [ ] Test Grafana alerts

#### Week 12: Documentation & Final Testing

**Day 56-57: Runbooks**
- [ ] Write operational runbook (how to monitor, troubleshoot)
- [ ] Write agent configuration guide
- [ ] Write incident response guide
- [ ] Write FAQ document

**Day 58-59: Documentation**
- [ ] Update README with SWE team info
- [ ] Update architecture docs
- [ ] Create video tutorial (optional)

**Day 60: Final Integration Test**
- [ ] Full end-to-end test: Error → Resolution → Deployment → Monitoring
- [ ] Performance test: 50 issues in parallel
- [ ] Stress test: Rollback under failure
- [ ] Security audit: Verify no credentials leaked
- [ ] Human acceptance test

---

## Resource Requirements

### Human Resources

| Role | Time Commitment | Duration |
|------|----------------|----------|
| Senior Python Developer | 100% (full-time) | 12 weeks |
| DevOps Engineer | 25% (1 day/week) | 12 weeks |
| AI/ML Engineer | 25% (1 day/week) | 4 weeks (Phases 2-3) |
| QA Engineer | 25% (1 day/week) | 12 weeks |
| Product Manager | 10% (2 hours/week) | 12 weeks |

**Total Effort:** ~220 hours (1.1 FTE × 12 weeks)

### Infrastructure

- **Compute:**
  - GitHub Actions (2,000 minutes/month free tier)
  - Local Docker for sandbox (no cost)
- **Storage:**
  - Supabase PostgreSQL (500 MB free tier)
  - Vector extension (pgvector, free)
- **LLM APIs:**
  - Budget: $200/month for development/testing
- **Monitoring:**
  - Grafana Cloud (free tier, 10k metrics)

### Tools & Services

- **Required:**
  - GitHub API token (organization admin access)
  - Telegram bot token (for notifications)
  - Docker (for sandbox)
  - pytest, flake8, black (already installed)
- **Optional:**
  - GitHub Copilot API (for dev agent)
  - Sentry (for error tracking)

---

## Risk Management

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| **LLM API costs exceed budget** | Medium | High | Rate limiting, model tier enforcement, daily budget alerts |
| **Agents produce incorrect fixes** | Medium | High | Sandbox testing, CI validation, rollback automation |
| **Performance issues (slow resolution)** | Low | Medium | Parallel agent execution, caching, async operations |
| **GitHub API rate limiting** | Low | Medium | Token pooling, request caching, backoff strategy |
| **Security vulnerabilities introduced** | Low | Critical | Automated security scans, human review for P1/P2 |
| **Human oversight burden** | Medium | Medium | Reduce approval frequency, improve triage accuracy |

---

## Success Criteria (Phase-by-Phase)

### Phase 1 Success Criteria
- ✅ Monitor Agent detects errors from logs
- ✅ Triage Agent creates GitHub issues automatically
- ✅ Knowledge Base stores solutions
- ✅ All unit tests pass (>80% coverage)

### Phase 2 Success Criteria
- ✅ Investigation Team produces diagnostic reports
- ✅ Development Team generates code fixes
- ✅ Test Runner validates changes in sandbox
- ✅ Knowledge base semantic search works
- ✅ All unit + integration tests pass

### Phase 3 Success Criteria
- ✅ Deployment Agent creates PRs
- ✅ CI failures trigger rollback
- ✅ Health degradation triggers rollback
- ✅ Zero production outages during testing
- ✅ All tests pass

### Phase 4 Success Criteria
- ✅ Creative Agent runs weekly
- ✅ Generates ≥1 RFC per week
- ✅ Council system debates proposals
- ✅ Human can approve/reject via Telegram
- ✅ All tests pass

### Phase 5 Success Criteria
- ✅ Bug-first policy enforced
- ✅ Human can veto decisions
- ✅ Audit trail complete
- ✅ All tests pass

### Phase 6 Success Criteria
- ✅ Token costs < $200/month
- ✅ MTTR < 1 hour for HIGH severity
- ✅ Fix success rate > 90%
- ✅ Full documentation published
- ✅ Grafana dashboard operational
- ✅ All tests pass

---

## Deployment Strategy

### Staged Rollout

**Stage 1: Monitoring Only (Week 13)**
- Deploy Monitor Agent to production
- Triage Agent creates GitHub issues
- **NO** autonomous code changes
- Human reviews all issues manually
- Goal: Validate detection accuracy

**Stage 2: Investigation + Proposal (Week 14)**
- Deploy Investigation Team
- Diagnostic reports added to GitHub issues
- **NO** autonomous code changes
- Human implements fixes manually
- Goal: Validate diagnostic quality

**Stage 3: Sandbox Development (Week 15)**
- Deploy Development + Testing Teams
- Fixes tested in sandbox
- Results reported in GitHub comments
- Human reviews and merges manually
- Goal: Validate fix quality

**Stage 4: Autonomous Deployment (Week 16)**
- Deploy Deployment Team
- Full autonomous loop enabled
- Human supervision reduced to 30%
- Goal: Achieve target metrics

**Stage 5: Creative Loop (Week 17)**
- Deploy Creative Agent
- Weekly optimization proposals
- Goal: Proactive improvements

---

## Maintenance & Operations

### Daily Operations

**Automated:**
- Monitor Agent scans logs every 5 minutes
- Triage Agent processes alerts within 2 minutes
- Investigation Team starts within 5 minutes of assignment
- Development Team implements fixes within 30 minutes (P1/P2)
- Testing Team validates within 15 minutes
- Deployment Team merges within 10 minutes

**Human Oversight:**
- Review audit log (10 minutes/day)
- Approve/reject Creative Agent proposals (30 minutes/week)
- Escalation handling (as needed)

### Weekly Operations

**Automated:**
- Creative Agent weekly scan (Sunday 8 AM)
- Knowledge base cleanup (remove low-success solutions)
- Metrics report generation

**Human Oversight:**
- Review weekly metrics report (30 minutes)
- Adjust thresholds if needed (10 minutes)

### Monthly Operations

**Human Oversight:**
- Comprehensive audit (2 hours)
- Cost review and optimization (1 hour)
- Roadmap planning (2 hours)

---

## Next Steps

1. **Get Stakeholder Approval** (this week)
   - Review architecture document
   - Review implementation plan
   - Allocate resources (1 senior dev + 0.5 support)

2. **Setup Infrastructure** (Week 0)
   - Create GitHub API token
   - Setup Telegram bot
   - Configure Supabase database
   - Setup Grafana dashboard

3. **Begin Phase 1** (Week 1)
   - Kickoff meeting
   - Sprint planning
   - Start GitHub Adapter development

4. **Establish Rituals**
   - Daily standup (15 minutes)
   - Weekly demo (30 minutes)
   - Bi-weekly retrospective (1 hour)

---

## Appendix A: File Structure

```
src/swe_agents/
├── __init__.py
├── monitor_agent.py          # Phase 1
├── triage_agent.py            # Phase 1
├── problem_analyzer.py        # Phase 1
├── investigation_orchestrator.py  # Phase 2
├── investigators/
│   ├── __init__.py
│   ├── database_investigator.py
│   ├── code_investigator.py
│   ├── browser_investigator.py
│   └── external_investigator.py
├── dev_orchestrator.py        # Phase 2
├── test_runner.py             # Phase 2
├── deployment_agent.py        # Phase 3
├── creative_agent.py          # Phase 4
├── council.py                 # Phase 4
└── utils/
    ├── __init__.py
    ├── github_utils.py
    ├── llm_utils.py
    ├── git_utils.py
    └── metrics_utils.py

src/a2a/adapters/
├── github_adapter.py          # Phase 1
└── claude_code_adapter.py     # Phase 2 (acpx integration)

src/database/repositories/
├── knowledge_base_repo.py     # Phase 1
└── audit_log_repo.py          # Phase 5

src/database/migrations/
├── 024_swe_knowledge_base.sql # Phase 1
└── 025_swe_audit_enhancements.sql # Phase 5

config/
└── a2a_hub.yaml               # Updated throughout

tests/swe_agents/
├── unit/
│   ├── test_monitor_agent.py
│   ├── test_triage_agent.py
│   ├── test_problem_analyzer.py
│   ├── test_investigation_orchestrator.py
│   ├── test_dev_orchestrator.py
│   ├── test_test_runner.py
│   ├── test_deployment_agent.py
│   └── test_creative_agent.py
├── integration/
│   ├── test_end_to_end_workflow.py
│   ├── test_knowledge_base_integration.py
│   └── test_rollback_system.py
└── fixtures/
    ├── sample_logs.txt
    ├── sample_stack_traces.txt
    └── mock_github_responses.json

docs/
├── architecture/
│   └── autonomous_swe_team_architecture.md  # Created
└── operations/
    ├── swe_team_runbook.md     # Phase 6
    ├── swe_team_faq.md         # Phase 6
    └── incident_response.md    # Phase 6

.github/
├── ISSUE_TEMPLATE/
│   └── auto_generated.md      # Phase 1
└── workflows/
    └── swe_agent_ci.yml       # Phase 3 (CI for SWE agents themselves)
```

---

## Appendix B: Dependencies

**New Python packages to install:**

```
# requirements.txt additions
pygithub>=2.1.1          # GitHub API client
redis>=5.0.0             # Priority queue (optional)
sentence-transformers>=2.2.2  # SBERT embeddings
docker>=6.1.0            # Sandbox management
grafana-api>=1.0.3       # Grafana API (optional)
```

---

## Appendix C: Configuration Templates

### A2A Hub Configuration (`config/a2a_hub.yaml`)

```yaml
# Add to existing config

adapters:
  # ... existing adapters ...

  github:
    enabled: true
    type: "http"
    endpoint: "https://api.github.com"
    api_key: "${GITHUB_TOKEN:}"
    timeout: 30
    skills:
      - id: create_issue
        name: "Create GitHub Issue"
        description: "Create a new issue with labels and assignees"
        tags: [github, issue]

  monitor_agent:
    enabled: true
    type: "cli"
    binary: ".venv/bin/python3"
    args: ["src/swe_agents/monitor_agent.py", "--message", "{message}"]
    timeout: 60
    skills:
      - id: scan_logs
        name: "Scan Logs"
        description: "Scan logs for errors and warnings"
        tags: [monitoring, logs]

  # ... more agents ...

event_handlers:
  # ... existing handlers ...

  error_detected:
    command: [".venv/bin/python3", "src/swe_agents/triage_agent.py", "--payload", "{__json__}"]
    cooldown_seconds: 60
    timeout_seconds: 120
    enabled: true
```

---

**End of Implementation Plan**
