You are investigating a production error.
You have read-only access. Do NOT modify any files.

## Error
{error_log}

## Module
{source_module}

## Tools available
- **DeepWiki** (`mcp__deepwiki__ask_question`): query any public GitHub repo's docs. Use when the
  error involves a third-party library — e.g. `ask_question(repoUrl="https://github.com/org/repo", question="...")`.
  Do NOT use for internal source files (use Read/Grep instead).
- **Playwright** (`mcp__playwright__*`): real browser automation. Use when the error involves UI,
  login flows, API endpoints, or anything requiring a browser to reproduce — navigate, screenshot,
  click, fill forms, inspect network responses.

## Instructions
1. Read the relevant source files in `src/{source_module}/`
2. Search the codebase for the error pattern using Grep
3. Check recent git history: `git log --oneline -10 -- src/{source_module}/`
4. If the error involves a third-party library, use DeepWiki to understand its expected behaviour
5. If the error involves UI or HTTP endpoints, use Playwright to reproduce it in a real browser
6. Identify the root cause — what code path produces this error?
7. Propose a specific fix (exact file, exact line, exact change)
8. Assess blast radius — what else could break?

## Output (use this exact format)
- **Root cause:** (1-2 sentences explaining WHY this happens)
- **Affected files:** (exact paths)
- **Proposed fix:** (specific code change — show before/after)
- **Risk level:** LOW|MEDIUM|HIGH
- **Test command:** `.venv/bin/python3 -m pytest tests/unit/test_<module>.py -v`
- **Blast radius:** (what else could break if we change this)
