# Claude Code Skills Integration

This document maps the Claude Code skills in `.claude/skills/` to SWE-Squad agents,
identifies gaps in the current agent roster, and defines four new agents proposed for
implementation.

## What Are Claude Code Skills?

Skills are structured prompt programs (SKILL.md files) that extend Claude Code agents with
reusable, battle-tested workflows. When Claude Code runs from the repo root, it loads skills
from `.claude/skills/` automatically. Each skill is a self-contained instruction set — tools
declared, preamble logic, step-by-step instructions, and expected output format.

Skills in this repo are sourced from the [gstack](https://github.com/gstackio/gstack) toolkit.
Install gstack globally to enable preamble features (update checks, session tracking). Skills
work without gstack but with reduced scaffolding.

---

## Skill → Agent Mapping

### Existing Agents

| Skill | Agent | Notes |
|-------|-------|-------|
| `investigate` | `browser_investigator`, `db_investigator`, `infra_investigator` | Prompt pattern directly mirrors InvestigatorAgent logic — BUG/FEATURE/REGRESSION/DOCS workflows with DeepWiki + Playwright |
| `review` | `swe_reviewer` | Pre-landing PR review with SQL safety, LLM trust boundary checks, conditional side-effect analysis; aligns with `reviewer.py` |
| `codex` | `swe_reviewer` | Independent diff review via Codex CLI — useful as a second-opinion gate before merge |
| `ship` | `swe_deployer` | Full ship workflow: detect base branch, run tests, bump VERSION, update CHANGELOG, push, create PR — matches `developer.py` + `governance.py` |
| `land-and-deploy` | `swe_deployer` | Merge PR, wait for CI, verify production via canary — extends current deployer with structured post-merge checks |
| `setup-deploy` | `swe_deployer` | Deployment target configuration (Fly.io, Render, Vercel, Netlify, GitHub Actions) |
| `canary` | `swe_deployer`, `swe_monitor` | Post-deploy canary monitoring with console error detection, perf regression checks, page failure detection |
| `qa` | `swe_qa` | Full QA + bug fix cycle: iterative test → fix → commit loop |
| `qa-only` | `swe_qa` | Report-only QA: structured health score + screenshots; useful for pre-deploy snapshots |
| `benchmark` | `swe_tester` | Performance regression detection: baselines, Core Web Vitals, resource size tracking |
| `health` | `swe_monitor` | Code quality dashboard: type checker, linter, test runner, dead code detector, shell linter |
| `autoplan` | `orchestrator` (Opus) | Automated review pipeline running CEO, design, eng, and DX reviews sequentially |
| `plan-ceo-review` | `orchestrator` | CEO/founder-mode scope review: 10-star product thinking, scope expansion modes |
| `plan-eng-review` | `orchestrator` | Eng manager review: architecture lock-in, data flow, edge cases, test coverage, performance |
| `plan-design-review` | `swe_reviewer` | Designer's eye plan review: visual consistency, hierarchy, accessibility |
| `plan-devex-review` | `swe_creative` | Developer experience review: DX personas, competitor benchmarking, magical moments |
| `devex-review` | `swe_creative` | Live DX audit: navigates docs, tries getting-started flows, files actionable issues |
| `retro` | `swe_documenter` | Weekly engineering retrospective with commit history analysis, trend tracking |
| `document-release` | `swe_documenter` | Post-ship documentation update: cross-references diff against all project docs |
| `checkpoint` | `swe_developer` | Save/resume working state: captures git state, decisions, remaining work |
| `careful` | `swe_developer` | Safety guardrails: warns before rm -rf, DROP TABLE, force-push, reset --hard |
| `guard` | `swe_developer` | Full safety mode: careful + directory-scoped edit restrictions |

---

## Gap Analysis: Missing Agents

Cross-referencing the skill set against the current agent roster reveals four capability gaps:

### Gap 1: Security Investigator
The repo has `credential_scanner.py` and `guardrails.py` but no dedicated security-focused
investigator. Security issues currently fall through to generic investigators that lack
security-specific tooling (SAST patterns, secrets archaeology, supply chain checks).

### Gap 2: Performance Investigator
`benchmark` skill and `swe_tester` exist, but there is no agent that proactively hunts
performance regressions. The `swe_monitor` only scans logs for errors — not for latency
or resource size regressions across deploys.

### Gap 3: Plan Reviewer
`orchestrator` plans work (Opus-tier), but plans are not independently reviewed before
execution. The `autoplan` + `plan-eng-review` + `plan-ceo-review` skills enable a structured
multi-axis plan review that currently has no agent home.

### Gap 4: DevEx Agent
`devex-review` and `plan-devex-review` skills exist with no corresponding agent. Developer
experience degrades silently — no agent proactively audits docs, getting-started flows, or
CLI ergonomics.

---

## Proposed New Agents

### 1. `security_investigator`

```yaml
- name: security_investigator
  role: investigator
  description: >
    Security-focused investigator for credential leaks, vulnerable dependencies,
    supply chain risks, and CI/CD pipeline exposure. Runs on SECURITY-labelled
    tickets and on any ticket where credential_scanner raises an alert.
  model: sonnet
  tools:
    - code_search
    - credential_scan
    - acpx
  max_concurrent_tasks: 1
  enabled: true
  node: primary
  specialization: [security, credentials, dependencies, ci_cd]
  skill: .claude/skills/guard/SKILL.md
```

**Implementation:** `src/swe_team/security_investigator.py`
- Extends `InvestigatorAgent` base
- Calls `credential_scanner.py` as first pass on all files touched by the ticket
- Uses `guardrails.py` checks before proposing any fix
- Escalates to HITL immediately on confirmed credential exposure
- Prompt: new `config/swe_team/programs/investigate_security.md`

---

### 2. `perf_investigator`

```yaml
- name: perf_investigator
  role: investigator
  description: >
    Performance regression investigator. Establishes baselines on first run,
    then hunts regressions in page load times, Core Web Vitals, bundle size,
    and API latency. Triggered by PERF-labelled tickets or post-deploy canary
    alerts.
  model: sonnet
  tools:
    - code_search
    - browser_debug
    - acpx
  max_concurrent_tasks: 1
  enabled: true
  node: worker
  specialization: [performance, benchmarks, web_vitals, latency]
  skill: .claude/skills/benchmark/SKILL.md
```

**Implementation:** `src/swe_team/perf_investigator.py`
- Wraps the `benchmark` skill workflow (browse daemon, baseline storage, regression delta)
- Stores baselines in `ticket_store` / Supabase alongside ticket fingerprints
- Integrates with `fix_verifier.py` to re-run benchmarks post-fix
- Prompt: new `config/swe_team/programs/investigate_perf.md`

---

### 3. `plan_reviewer`

```yaml
- name: plan_reviewer
  role: reviewer
  description: >
    Multi-axis plan reviewer. Runs autoplan (CEO + eng + design + DX review)
    on any ticket tagged plan-review or when OrchestratorAgent produces a
    sub-task plan. Ensures plans are ambitious enough, architecturally sound,
    and developer-friendly before execution begins.
  model: opus
  tools:
    - code_search
    - acpx
  max_concurrent_tasks: 1
  enabled: true
  node: primary
  specialization: [planning, architecture, strategy]
  skill: .claude/skills/autoplan/SKILL.md
```

**Implementation:** `src/swe_team/plan_reviewer.py`
- Triggered after `orchestrator.py` produces a plan for CRITICAL/HIGH tickets
- Runs `plan-ceo-review` → `plan-eng-review` → `plan-devex-review` in sequence
- Produces a structured review with pass/fail axes and revision requests
- Plan only proceeds to `developer.py` after plan_reviewer signs off
- Prompt: `config/swe_team/programs/plan_review.md`

---

### 4. `devex_agent`

```yaml
- name: devex_agent
  role: creative
  description: >
    Developer experience auditor. Proactively navigates documentation, tests
    getting-started flows, audits CLI ergonomics, and files actionable issues
    when DX degrades. Runs weekly and after significant API or docs changes.
  model: sonnet
  tools:
    - browser_debug
    - code_search
    - github_issues
  max_concurrent_tasks: 1
  enabled: true
  node: primary
  specialization: [developer_experience, docs, onboarding, cli]
  skill: .claude/skills/devex-review/SKILL.md
```

**Implementation:** `src/swe_team/devex_agent.py`
- Extends `CreativeAgent` pattern (runs only when system is stable)
- Triggered weekly via cron or after PRs touching `docs/`, `README.md`, CLI files
- Uses Playwright to navigate real docs pages and test getting-started flows
- Files GitHub issues with DX severity scores
- Prompt: `config/swe_team/programs/devex_audit.md`

---

## Implementation Checklist

### Phase 1 — Skills Integration (this PR)
- [x] Add 22 development skills to `.claude/skills/`
- [x] Document skill → agent mapping
- [ ] Wire skill references into existing agent configs in `swe_team.yaml`

### Phase 2 — Agent Refinement (follow-up)
- [ ] Add `specialization` field to existing agent configs (see mapping table)
- [ ] Update `investigate.md` program to reference `investigate` skill patterns
- [ ] Update `fix.md` to incorporate `careful` / `guard` guardrail steps
- [ ] Add `document-release` trigger to `swe_documenter` post-merge hook
- [ ] Add `canary` post-deploy check to `swe_deployer` workflow

### Phase 3 — New Agents (follow-up)
- [ ] `security_investigator` — implement + test + add to `swe_team.yaml`
- [ ] `perf_investigator` — implement + test + add to `swe_team.yaml`
- [ ] `plan_reviewer` — implement + test + add to `swe_team.yaml`
- [ ] `devex_agent` — implement + test + add to `swe_team.yaml`

### Phase 4 — Prompt Programs (follow-up)
- [ ] `investigate_security.md`
- [ ] `investigate_perf.md`
- [ ] `plan_review.md`
- [ ] `devex_audit.md`
