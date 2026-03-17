# SWE Squad — Autonomous Software Engineering Team

Self-healing, self-diagnosing development agents that monitor production systems, detect errors, investigate root causes, implement fixes, and learn from successes.

## Architecture

```
Every 30 min (cron):
│
├─ COLLECT — rsync logs from remote worker machines via SSH
├─ FETCH — pick up GitHub issues assigned to @Agent-Smith-AI
├─ MONITOR — scan all logs for ERROR, CRITICAL, Traceback patterns
├─ TRIAGE — route by severity and module, assign to specialists
├─ NOTIFY — Telegram alerts for HIGH/CRITICAL, GitHub issue creation
├─ DISTILL — check for cached deterministic fixes (zero LLM cost)
├─ INVESTIGATE — Claude Code CLI diagnosis (Sonnet or Opus)
├─ FIX — keep/discard loop with git branches, auto-revert on failure
├─ GATE — Ralph Wiggum stability gate (bugs before features)
├─ CREATIVE — weekly improvement proposals from ticket patterns
└─ DISPATCH — A2A events for cross-agent coordination
```

## Model Routing

| Scenario | Model | Why |
|----------|-------|-----|
| Routine HIGH bugs | **Sonnet** | Fast, cost-efficient |
| CRITICAL bugs | **Opus** | Orchestrates sub-agents |
| After 2 failed Sonnet attempts | **Opus** | Automatic escalation |
| Documentation, issue scanning | **Haiku** | Cheap, fast |
| Deterministic replay | **None** | Cached fix, zero LLM cost |

## Opus Orchestration

When Opus handles a CRITICAL bug, it delegates ALL work to sub-agents:

1. **Investigation** (Sonnet) — read code, find root cause
2. **Issue scan** (Haiku) — find related issues, link duplicates
3. **Planning** (Opus decides) — how many agents, what files
4. **Implementation** (Sonnet × N) — one agent per concern
5. **Verification** (Sonnet) — full test suite
6. **Documentation** (Haiku) — comment on GitHub issues

Opus keeps its context clean and never writes code itself.

## Ralph Wiggum Loop

The developer agent feeds failures forward into the next attempt:

```
Attempt 1 (Sonnet): try fix → tests fail → capture error
Attempt 2 (Sonnet): try fix WITH previous error context → tests fail → capture
Attempt 3 (Opus):   escalate, orchestrate sub-agents → tests pass → KEEP
```

If all 3 attempts fail → HITL escalation via Telegram.

## GitHub Integration

The squad comments on every issue it touches:
- **Pickup:** "SWE Squad picked up this issue"
- **Investigation:** Full diagnostic report
- **Fix success:** Branch name, files changed, test results
- **Fix failure:** Attempt count, errors, HITL escalation

## Components

| File | Purpose |
|------|---------|
| `src/swe_team/monitor_agent.py` | Log scanning, error detection, fingerprint dedup |
| `src/swe_team/triage_agent.py` | Severity routing, module specialist assignment |
| `src/swe_team/investigator.py` | Claude Code CLI diagnosis with model routing |
| `src/swe_team/developer.py` | Keep/discard fix loop with git branches |
| `src/swe_team/ralph_wiggum.py` | Stability gate — bugs before features |
| `src/swe_team/governance.py` | Deployment governor, complexity gate |
| `src/swe_team/creative_agent.py` | Proactive improvement proposals |
| `src/swe_team/distiller.py` | Trajectory distillation — cache successful fixes |
| `src/swe_team/notifier.py` | Telegram alerts and summaries |
| `src/swe_team/github_integration.py` | GitHub issue creation and commenting |
| `src/swe_team/remote_logs.py` | SSH log collection from remote workers |
| `src/swe_team/ticket_store.py` | JSON persistence with fingerprint dedup |
| `src/a2a/adapters/swe_team.py` | A2A protocol adapter |
| `scripts/ops/swe_team_runner.py` | Entry point — cron, daemon, bootstrap modes |
| `config/swe_team/programs/` | Markdown agent programs (investigate, fix, orchestrate) |

## Quick Start

```bash
# Install dependencies
pip install python-dotenv pyyaml httpx aiohttp

# Bootstrap (first run — acknowledge existing errors)
SWE_TEAM_ENABLED=true python scripts/ops/swe_team_runner.py --bootstrap -v

# Run a scan cycle
SWE_TEAM_ENABLED=true python scripts/ops/swe_team_runner.py -v

# Daemon mode (persistent loop)
SWE_TEAM_ENABLED=true python scripts/ops/swe_team_runner.py --daemon -v

# Daily summary
SWE_TEAM_ENABLED=true python scripts/ops/swe_team_runner.py --summary

# Run tests
python -m pytest tests/unit/test_swe_team.py -v
```

## Requirements

- Python 3.10+
- Claude Code CLI (`claude`)
- `gh` CLI (authenticated)
- SSH access to worker machines (for remote log collection)
- Telegram bot token (for notifications)

## License

Private — preparing for open source release.
