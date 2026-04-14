#!/usr/bin/env python3
"""
Architecture lint: detect provider hardcoding violations.

Scans src/swe_team/ core modules for direct provider imports or
hardcoded subprocess calls to external tools (claude, gh, telegram, etc.)
that should go through the provider interface layer.

Usage:
    python3 scripts/ops/lint_providers.py
    make lint-providers

Exit code 0 = clean. Exit code 1 = violations found (creates GitHub issues if GH_TOKEN set).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "src" / "swe_team"

# Files that ARE the provider implementations — violations expected/allowed there
PROVIDER_PATHS = {
    "providers/",
    "telegram.py",
    "github_integration.py",
    "notifier.py",
    "gemini_cli_adapter.py",
    "rate_limiter.py",
}

# Patterns that indicate a provider is hardcoded in core code
VIOLATION_PATTERNS = [
    (r'subprocess\.run\(\s*\[.*?/usr/bin/claude', "Claude CLI hardcoded path — use CodingEngine interface"),
    (r'subprocess\.run\(\s*\[.*?\"claude\"', "Claude CLI hardcoded — use CodingEngine interface"),
    (r'subprocess\.run\(\s*\[.*?\"gh\s', "GitHub CLI hardcoded — use IssueTracker interface"),
    (r'import telegram', "Direct telegram import in core — use NotificationProvider interface"),
    (r'requests\.post.*api\.telegram', "Direct Telegram API call — use NotificationProvider interface"),
    (r'\"gemini\".*primary\|primary.*\"gemini\"', "Gemini hardcoded as primary — Claude Code is sole primary engine"),
    (r'_FALLBACK_MODEL\s*=.*["\'](?!.*env)', "Fallback model hardcoded — must read from env var"),
]

def is_provider_file(path: Path) -> bool:
    rel = str(path.relative_to(CORE_SRC))
    return any(rel.startswith(p) or rel == p for p in PROVIDER_PATHS)

def scan() -> list[dict]:
    violations = []
    for pyfile in sorted(CORE_SRC.rglob("*.py")):
        if is_provider_file(pyfile):
            continue
        content = pyfile.read_text(encoding="utf-8")
        for pattern, message in VIOLATION_PATTERNS:
            for m in re.finditer(pattern, content):
                line_no = content[:m.start()].count("\n") + 1
                violations.append({
                    "file": str(pyfile.relative_to(ROOT)),
                    "line": line_no,
                    "pattern": pattern,
                    "message": message,
                    "snippet": content.splitlines()[line_no - 1].strip(),
                })
    return violations

def report_issue(violation: dict) -> None:
    """Open a GitHub issue for the violation if GH_TOKEN is set."""
    token = os.environ.get("GH_TOKEN")
    if not token:
        return
    repo = os.environ.get("SWE_GITHUB_REPO", "owner/repo")
    title = f"architecture-violation: {violation['message']} in {violation['file']}:{violation['line']}"
    body = (
        f"## Architecture Violation\n\n"
        f"**Rule:** Provider-agnostic plugin architecture (see CLAUDE.md)\n\n"
        f"**File:** `{violation['file']}` line {violation['line']}\n\n"
        f"**Violation:** {violation['message']}\n\n"
        f"```python\n{violation['snippet']}\n```\n\n"
        f"**Fix:** Implement/use the appropriate provider interface in "
        f"`src/swe_team/providers/` and register in `swe_team.yaml`.\n"
    )
    subprocess.run(
        ["gh", "issue", "create", "--repo", repo,
         "--title", title, "--body", body, "--label", "architecture-violation"],
        capture_output=True, text=True
    )

def main() -> int:
    violations = scan()
    if not violations:
        print("✓ No provider hardcoding violations found.")
        return 0

    print(f"✗ {len(violations)} architecture violation(s) found:\n")
    for v in violations:
        print(f"  {v['file']}:{v['line']} — {v['message']}")
        print(f"    {v['snippet']}\n")

    # Open GitHub issues for each violation
    for v in violations:
        report_issue(v)

    print(f"\nSee CLAUDE.md § Provider-Agnostic Plugin Architecture for the fix pattern.")
    return 1

if __name__ == "__main__":
    sys.exit(main())
