You are investigating a production error.
You have read-only access. Do NOT modify any files.

## Error
{error_log}

## Module
{source_module}

## Instructions
1. Read the relevant source files in `src/{source_module}/`
2. Search the codebase for the error pattern using Grep
3. Check recent git history: `git log --oneline -10 -- src/{source_module}/`
4. Identify the root cause — what code path produces this error?
5. Propose a specific fix (exact file, exact line, exact change)
6. Assess blast radius — what else could break?

## Output (use this exact format)
- **Root cause:** (1-2 sentences explaining WHY this happens)
- **Affected files:** (exact paths)
- **Proposed fix:** (specific code change — show before/after)
- **Risk level:** LOW|MEDIUM|HIGH
- **Test command:** `.venv/bin/python3 -m pytest tests/unit/test_<module>.py -v`
- **Blast radius:** (what else could break if we change this)
