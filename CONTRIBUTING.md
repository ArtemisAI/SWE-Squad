# Contributing to SWE-Squad

Thank you for your interest in SWE-Squad!

## Getting started

```bash
git clone https://github.com/ArtemisAI/SWE-Squad.git
cd SWE-Squad
pip install pytest pyyaml python-dotenv
python -m pytest tests/unit/ -v
```

## How to contribute

1. **Open an issue first** for non-trivial changes — let's align before you code.
2. **Fork** the repo and create a branch: `feat/short-description` or `fix/short-description`.
3. **Write tests** — all new code needs unit tests in `tests/unit/`.
4. **Run the test suite** before opening a PR: `python -m pytest tests/unit/ -q`
5. **Open a PR** against `main` with a description and test plan.

## Development workflow

1. Create a feature branch from `main`:
   ```bash
   git checkout -b feat/your-feature-name
   ```
2. Make your changes with tests.
3. Run the full test suite:
   ```bash
   python -m pytest tests/unit/ -v
   ```
4. Commit with a descriptive message: `type(scope): short summary`
5. Push to your fork and open a Pull Request against `main`.

## Code conventions

- **Dataclasses** for all data models (no Pydantic)
- **Type hints** on all function signatures
- **Stdlib + pyyaml + python-dotenv** only — no new runtime deps without discussion
- **No hardcoded paths or credentials** — use env vars

## What we're looking for

### High-priority contributions
- Bug fixes with test coverage
- Additional ticket store backends (Redis, SQLite)
- Notification channel plugins (Slack, Discord)
- Documentation improvements

### Good first issues
- Look for issues labeled [`good first issue`](https://github.com/ArtemisAI/SWE-Squad/labels/good%20first%20issue)
- Documentation typos and improvements
- Test coverage for edge cases

## Pull request guidelines

- **One concern per PR** — keep changes focused
- **Include tests** for new functionality
- **Update documentation** if behavior changes
- **No new dependencies** unless absolutely necessary and discussed first
- **No private/internal references** — no IPs, internal hostnames, or internal paths

## Reporting issues

- Use the [GitHub issue tracker](https://github.com/ArtemisAI/SWE-Squad/issues)
- Include reproduction steps, expected vs actual behavior
- Include relevant logs or error messages
- Specify your Python version and OS

## Security

If you find a security vulnerability, please **do not** open a public issue.
Email the maintainers directly or use GitHub's private vulnerability reporting.

## Code of conduct

Be respectful. We're building autonomous agents — let's keep the humans collaborative.

## Questions?

Open a [Discussion](https://github.com/ArtemisAI/SWE-Squad/discussions) for questions, ideas, or general conversation about the project.
