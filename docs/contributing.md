# Contributing to SWE-Squad

Thank you for your interest in contributing to SWE-Squad! This document provides guidelines and information for contributors.

## Getting started

1. **Fork** the repository
2. **Clone** your fork: `git clone https://github.com/YOUR_USERNAME/SWE-Squad.git`
3. **Install** dependencies: `pip install python-dotenv pyyaml pytest`
4. **Configure**: `cp .env.example .env` and fill in test credentials
5. **Test**: `python3 -m pytest tests/unit/ -q`

## Development workflow

1. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. Make your changes
3. Run the full test suite:
   ```bash
   python3 -m pytest tests/unit/ -v --tb=short
   ```
   All 827+ tests must pass before committing.
4. Commit with a descriptive message:
   ```
   type(scope): short summary
   ```
   Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
5. Push to your fork and open a Pull Request.

## Code style

- **Python 3.10+** with type hints on all function signatures.
- Use `dataclasses` for all data models — no Pydantic, no attrs.
- Keep dependencies minimal — prefer stdlib over external packages.
- No new runtime dependencies without discussion first.
- Follow existing import patterns: `from src.swe_team.*` and `from src.a2a.*`.

## What we are looking for

### High-priority contributions

- Bug fixes with test coverage
- Additional ticket store backends (Redis, SQLite)
- CI/CD pipeline integrations
- Notification channel plugins (Slack, Discord)
- Documentation improvements

### Good first issues

- Look for issues labeled [`good first issue`](https://github.com/ArtemisAI/SWE-Squad/labels/good%20first%20issue)
- Documentation typos and improvements
- Test coverage for edge cases

## Pull request guidelines

- **One concern per PR** — keep changes focused.
- **Include tests** for new functionality. Tests live in `tests/unit/` and must use only the standard library plus pytest (no external services required).
- **Update documentation** if behavior changes.
- **Keep it minimal** — the smallest change that solves the problem.
- **No new dependencies** unless absolutely necessary and discussed first.
- **PR description must include a test plan** — list the test command and expected output.

## Things not to do

- Do not hardcode paths — use `Path(__file__).resolve()` or environment variables.
- Do not commit `.env`, `*.key`, `*.pem`, or credential files.
- Do not call the `claude` CLI from library code (`embeddings.py`, `supabase_store.py`, etc.).
- Do not use `claude-haiku` via the BASE_LLM proxy — it is not available there.
- Do not break the test suite — run `make test` before committing.
- Do not push to `main` directly — always open a PR.

## Reporting issues

- Use the [GitHub issue tracker](https://github.com/ArtemisAI/SWE-Squad/issues).
- Include reproduction steps, expected vs actual behavior.
- Include relevant logs or error messages.
- Specify your Python version and OS.

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](https://github.com/ArtemisAI/SWE-Squad/blob/main/CODE_OF_CONDUCT.md). By participating, you agree to uphold this code.

## Questions?

Open a [Discussion](https://github.com/ArtemisAI/SWE-Squad/discussions) for questions, ideas, or general conversation about the project.
