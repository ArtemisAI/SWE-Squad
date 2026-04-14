# Contributing to SWE Squad

Thank you for your interest in contributing to SWE Squad! This guide will help you get started.

## Development Environment Setup

### Prerequisites

- Python 3.10+
- Node.js 18+ (for WebUI development)
- Git
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (for running agents)
- [GitHub CLI](https://cli.github.com/) (`gh`) (for issue management)

### 1. Fork and clone

```bash
git clone https://github.com/YOUR_USERNAME/SWE-Squad.git
cd SWE-Squad
```

### 2. Install Python dependencies

```bash
pip install python-dotenv pyyaml pytest
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your test credentials
```

### 4. Run the test suite

```bash
python3 -m pytest tests/unit/ -v
```

All tests must pass before submitting a PR. Tests use only the standard library plus pytest -- no external services required.

### 5. Set up the WebUI (optional)

```bash
cd ui
npm install
npm run dev
# Opens at http://localhost:5173, proxies API requests to :8888
```

To run with the backend:

```bash
# Terminal 1: start the dashboard server
python scripts/ops/dashboard_server.py --port 8888

# Terminal 2: start the React dev server
cd ui && npm run dev
```

## Running Tests

```bash
# Full test suite
python3 -m pytest tests/unit/ -v

# Specific test file
python3 -m pytest tests/unit/test_swe_team.py -v

# With short tracebacks
python3 -m pytest tests/unit/ -v --tb=short

# Via Makefile
make test
```

Tests live in `tests/unit/` and must not require network access, API keys, or running services.

## Code Style

### Python

- **Python 3.10+** with type hints on all function signatures
- **Dataclasses** for all data models -- no Pydantic, no attrs
- **Minimal dependencies** -- stdlib + pyyaml + python-dotenv. Optional extras only via install groups (e.g., `[embeddings]`, `[cli]`)
- **Imports** from `src.swe_team.*` and `src.a2a.*` use dotted paths rooted at the project directory
- Configuration loaded once via `load_config()` and threaded through as arguments
- No `os.environ` reads inside plugin/provider classes -- all config via constructor

### TypeScript (WebUI)

- React functional components with hooks
- TypeScript strict mode
- Tailwind CSS for styling

## Pull Request Guidelines

### Branch naming

Use descriptive branch names with a type prefix:

```
feat/add-slack-notification
fix/regression-detection-loop
refactor/ticket-store-interface
test/add-guardrails-coverage
docs/update-quickstart
```

### PR requirements

- **One concern per PR** -- keep changes focused
- **All tests must pass** -- run `python3 -m pytest tests/unit/ -v` before pushing
- **Include tests** for new functionality
- **Update documentation** if behavior changes
- **No new runtime dependencies** without prior discussion
- **No secrets in code** -- all credentials via `.env` or environment variables

### Commit messages

Use the conventional format:

```
type(scope): short summary

feat(providers): add Slack notification adapter
fix(investigator): handle empty error_log gracefully
test(guardrails): add circuit breaker edge case coverage
docs(readme): update Quick Start with Docker instructions
refactor(triage): extract severity classifier to separate module
chore(deps): bump pyyaml to 6.0.2
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`

### Plugin contributions

SWE Squad uses a provider-agnostic plugin architecture. To add a new provider:

1. Create a new file in `src/swe_team/providers/<domain>/` implementing the relevant Protocol from `base.py`
2. Add tests in `tests/unit/`
3. Register the provider name in `config/swe_team.yaml` under the appropriate `providers:` key
4. Update documentation if adding a new provider domain

See existing providers for reference patterns.

## What We're Looking For

### High-priority contributions

- New notification providers (Slack, Discord, email)
- New coding engine adapters (Cursor, Windsurf)
- Ticket store backends (Redis, SQLite)
- CI/CD pipeline integrations
- Test coverage improvements

### Good first issues

- Look for issues labeled [`good first issue`](../../labels/good%20first%20issue)
- Documentation improvements and typo fixes
- Test coverage for edge cases
- Adding type hints to untyped functions

## Reporting Issues

- Use the [GitHub issue tracker](../../issues)
- Include reproduction steps, expected vs actual behavior
- Include relevant logs or error messages
- Specify your Python version and OS

## Architecture Notes

Before making changes, familiarize yourself with the key design principles:

- **Provider-agnostic**: Every external service is behind a Protocol interface. Never import a concrete provider in core code.
- **Divide-and-conquer**: Large tasks should be broken into parallel sub-agents. Opus orchestrates; Sonnet/Haiku implement.
- **Stability gate**: The Ralph Wiggum gate enforces that bugs are fixed before features ship.
- **Idempotent operations**: GitHub comments, ticket updates, and state transitions must be idempotent.

See `CLAUDE.md` for the full architecture reference.

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold this code.

## Questions?

Open a [Discussion](../../discussions) for questions, ideas, or general conversation about the project.
