# Contributing to SWE Squad

Thank you for your interest in contributing to SWE Squad! This document provides guidelines and information for contributors.

## Getting Started

1. **Fork** the repository
2. **Clone** your fork: `git clone https://github.com/YOUR_USERNAME/SWE-Squad.git`
3. **Install** dependencies: `pip install python-dotenv pyyaml pytest`
4. **Configure**: `cp .env.example .env` and fill in test credentials
5. **Test**: `python -m pytest tests/unit/test_swe_team.py -v`

## Development Workflow

1. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. Make your changes
3. Run the test suite:
   ```bash
   python -m pytest tests/unit/test_swe_team.py -v
   ```
4. Commit with a clear message describing the change
5. Push to your fork and open a Pull Request

## Code Style

- **Python 3.10+** with type hints
- Use `dataclasses` for data models
- Keep dependencies minimal — prefer stdlib over external packages
- Follow existing patterns in the codebase

## What We're Looking For

### High-Priority Contributions
- Bug fixes with test coverage
- Additional ticket store backends (Redis, SQLite)
- CI/CD pipeline integrations
- Notification channel plugins (Slack, Discord)
- Documentation improvements

### Good First Issues
- Look for issues labeled [`good first issue`](https://github.com/ArtemisAI/SWE-Squad/labels/good%20first%20issue)
- Documentation typos and improvements
- Test coverage for edge cases

## Pull Request Guidelines

- **One concern per PR** — keep changes focused
- **Include tests** for new functionality
- **Update documentation** if behavior changes
- **Keep it minimal** — the smallest change that solves the problem
- **No new dependencies** unless absolutely necessary and discussed first

## Reporting Issues

- Use the [GitHub issue tracker](https://github.com/ArtemisAI/SWE-Squad/issues)
- Include reproduction steps, expected vs actual behavior
- Include relevant logs or error messages
- Specify your Python version and OS

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold this code.

## Questions?

Open a [Discussion](https://github.com/ArtemisAI/SWE-Squad/discussions) for questions, ideas, or general conversation about the project.
