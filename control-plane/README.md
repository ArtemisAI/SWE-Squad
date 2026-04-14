# @swe-squad/control-plane

TypeScript control plane for SWE-Squad, built on [pi-mono](https://github.com/badlogic/pi-mono) (`@mariozechner/pi-coding-agent`).

Replaces the Python control plane (`scripts/ops/swe_team_runner.py`) with a provider-agnostic, extension-based orchestration layer.

## Quick Start

```bash
# From repo root
pnpm install

# Run tests
pnpm --filter @swe-squad/control-plane test

# Type check
pnpm --filter @swe-squad/control-plane typecheck
```

## Architecture

```
src/
  models/         Data types: SWETicket, enums, Zod schemas
  config/         YAML config loading with Zod validation
  safety/         5 safety gates: circuit breaker, throttle, stability, guardrails, governance
  providers/
    engine/       CodingEngine interface + Claude CLI + Pi SDK (Phase 3)
    supabase/     PostgREST client, ticket store, embeddings
    github/       gh CLI integration with circuit breaker
    notification/ Telegram Bot API with rate limiting
  agents/         Agent layer (Phase 3)
  extensions/     Pi extension layer (Phase 3)
  orchestration/  Cycle + daemon (Phase 4)
```

## Test Suite

1291 tests across 25 files:

| Category | Tests | Coverage |
|----------|-------|---------|
| Unit: models | 72 | Enums, schemas, ticket lifecycle, resolution audit |
| Unit: config | 46 | Schema defaults, YAML loading, env overrides |
| Unit: safety | 66 | Circuit breaker, throttle, Ralph Wiggum, governance |
| Unit: engine | 107 | Interface, registry, Claude CLI, Pi SDK (mocked subprocess) |
| Unit: providers | 104 | Supabase, GitHub, Telegram (mocked fetch/exec) |
| Unit: agents | 315 | Monitor, triage, investigator, developer, creative, distiller, fix-verifier |
| Unit: extensions | 75 | RBAC, tool guards |
| Unit: orchestration | 85 | Parallel executor, runner, logger, rate-limiter, status-writer |
| Integration: E2E | 90 | Full pipeline: config to status output |
| Integration: regression | 100 | Python/TypeScript behavioral parity |
| Integration: lifecycle | 56 | Agent lifecycle state transitions |
| Integration: validation | 83 | Dual-run validation (Phase 5) |

## Key Design Principles

1. **Zod schemas** for runtime validation, TypeScript inference for compile-time safety
2. **Native fetch()** (Node 20+) for all HTTP — no external HTTP libraries
3. **Provider-agnostic** CodingEngine interface — Claude, Pi, Gemini all pluggable
4. **ESM-only** with .js extensions in all imports
5. **Minimal dependencies** — yaml + zod runtime, tsx + vitest + typescript dev
6. **Regression-tested** — 100 parity tests verify TypeScript matches Python behavior exactly

## Deployment

### Install

```bash
# From repo root
pnpm install
pnpm --filter @swe-squad/control-plane build

# Verify
pnpm --filter @swe-squad/control-plane test
pnpm --filter @swe-squad/control-plane typecheck
```

### Production Cutover (Phase 6)

```bash
# 1. Stop Python daemon
sudo systemctl stop swe-team.service

# 2. Start TypeScript daemon
sudo systemctl start swe-manager.service

# 3. Verify health
curl -s http://localhost:18791/health | jq .
cat data/swe_team/status.json | jq .
```

### Rollback

```bash
# Revert to Python daemon
sudo systemctl stop swe-manager.service
sudo systemctl start swe-team.service
```

## Validation

### Dual-Run Mode (Phase 5)

Run both Python and TypeScript in parallel on the gamma VM, comparing outputs:

```bash
# Run validation test suite (no network required)
pnpm --filter @swe-squad/control-plane test -- tests/integration/validation.test.ts

# Run full regression suite
pnpm --filter @swe-squad/control-plane test -- tests/integration/regression.test.ts

# Run all tests
pnpm --filter @swe-squad/control-plane test
```

### Phase Cutover Verification

```bash
# Step 1: TypeScript dry-run alongside Python (gamma VM)
pnpm --filter @swe-squad/control-plane start -- --dry-run --once

# Step 2: Compare status.json output
diff <(python3 -c "import json; print(json.dumps(json.load(open('data/swe_team/status_py.json')), sort_keys=True, indent=2))") \
     <(cat data/swe_team/status.json | jq -S .)

# Step 3: Full cycle with TypeScript (gamma VM, no Python)
pnpm --filter @swe-squad/control-plane start -- --once

# Step 4: Production cutover (primary VM, after gamma passes)
sudo systemctl stop swe-team.service
sudo systemctl start swe-manager.service
```

## Phase 5-6 Checklist

- [x] Phase 1: Foundation (models, config, safety gates) -- 184 tests
- [x] Phase 2: Providers (engines, Supabase, GitHub, Telegram) -- 371 tests
- [x] Phase 3: Agents (8 agents, pi-sdk, extensions) -- 653 tests
- [x] Phase 4: Orchestration (cycle runner, daemon, CLI, utils) -- 1291 tests
- [ ] Phase 5: Dual-run validation on gamma VM
  - [x] Validation test suite (83 scenarios)
  - [ ] Gamma VM dry-run comparison
  - [ ] 48-hour dual-run soak test
  - [ ] Output diff analysis
- [ ] Phase 6: Production cutover
  - [ ] systemd unit file for swe-manager.service
  - [ ] Cron migration
  - [ ] CLAUDE.md architecture section update
  - [ ] Python daemon archived
  - [ ] 30-minute post-cutover monitoring
  - [ ] Rollback plan verified

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `SWE_TEAM_CONFIG` | Path to swe_team.yaml (default: config/swe_team.yaml) |
| `SWE_TEAM_ENABLED` | Kill switch (true/false) |
| `SWE_TEAM_ID` | Unique team identifier |
| `SWE_GITHUB_ACCOUNT` | Bot GitHub account |
| `SWE_MODEL_T1` | Override T1 model (opus) |
| `SWE_MODEL_T2` | Override T2 model (sonnet) |
| `SWE_MODEL_T3` | Override T3 model (haiku) |
| `SUPABASE_URL` | Supabase PostgREST endpoint |
| `SUPABASE_ANON_KEY` | Supabase API key |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |
| `BASE_LLM_API_URL` | Embedding/extraction proxy URL |
| `BASE_LLM_API_KEY` | Embedding/extraction API key |
