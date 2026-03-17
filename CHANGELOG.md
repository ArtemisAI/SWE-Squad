# Changelog

All notable changes to SWE Squad will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-03-17

### Added
- Core agent loop: monitor, triage, investigate, develop, test
- Ralph Wiggum stability gate (bugs before features)
- Trajectory distillation for cached deterministic fixes
- Supabase ticket store with multi-team support and audit trail
- JSON ticket store as zero-dependency default
- A2A protocol adapter for inter-agent communication
- GitHub integration (issue creation, commenting, assignment)
- Telegram notifications (alerts, HITL escalation, daily summaries)
- Remote log collection via SSH/rsync
- Model routing: Haiku (cheap) → Sonnet (routine) → Opus (critical)
- Keep/discard fix loop with git branch isolation
- Deployment governor with complexity gates
- Creative agent for proactive improvement proposals
- Configurable via YAML and environment variables
- 132 unit tests
