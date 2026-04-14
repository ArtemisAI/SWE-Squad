"""QA Agent — runs build verification, visual tests, and regression checks.

Sits between CodeReviewer (APPROVE) and merge. Runs actual tests,
not just diff review. Provider-agnostic via CodingEngine protocol.

The agent runs subprocess commands directly for checks (typescript, build,
pytest, visual_tests, regression). CodingEngine is only used when the QA
agent needs an LLM to analyze failures.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.swe_team.models import SWETicket

logger = logging.getLogger("swe_team.qa_agent")

# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Outcome of a single QA check."""

    name: str
    passed: bool
    duration_s: float
    output: str


@dataclass
class QAResult:
    """Aggregate outcome of all QA checks for a ticket."""

    approved: bool
    checks: List[CheckResult] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Built-in check registry
# ---------------------------------------------------------------------------

# Each check is a (command_args, cwd_relative_to_repo) tuple.
# cwd=None means run from repo root.
_CHECK_REGISTRY: Dict[str, Dict[str, Any]] = {
    "typescript": {
        "cmd": ["npx", "tsc", "--noEmit"],
        "cwd_subdir": "ui",
        "description": "TypeScript type-check (npx tsc --noEmit)",
    },
    "build": {
        "cmd": ["npm", "run", "build"],
        "cwd_subdir": "ui",
        "description": "Production build (npm run build)",
    },
    "pytest": {
        "cmd": ["python3", "-m", "pytest", "tests/unit/", "-q", "--tb=short"],
        "cwd_subdir": None,
        "description": "Unit test suite (pytest)",
    },
    "visual_tests": {
        "cmd": ["python3", "scripts/ops/webui_visual_test.py"],
        "cwd_subdir": None,
        "description": "Visual regression tests",
    },
    "regression": {
        "cmd": ["python3", "-m", "pytest", "tests/unit/", "-q", "--tb=short"],
        "cwd_subdir": None,
        "description": "Full regression suite (compare pass count to baseline)",
    },
}


# ---------------------------------------------------------------------------
# QAAgent
# ---------------------------------------------------------------------------


class QAAgent:
    """Runs build verification, visual tests, and regression checks.

    Parameters
    ----------
    checks:
        List of check names to run (must be keys in _CHECK_REGISTRY).
    engine:
        Optional CodingEngine instance for LLM-based failure analysis.
    timeout:
        Per-check subprocess timeout in seconds.
    baseline_test_count:
        Minimum expected test-pass count for the ``regression`` check.
        If None, the regression check only verifies exit code.
    enabled:
        Kill switch. When False, ``run_qa`` returns approved=True immediately.
    """

    def __init__(
        self,
        checks: Optional[List[str]] = None,
        engine: Any = None,
        timeout: int = 300,
        baseline_test_count: Optional[int] = None,
        enabled: bool = True,
    ) -> None:
        self.checks = checks or ["pytest"]
        self.engine = engine
        self.timeout = timeout
        self.baseline_test_count = baseline_test_count
        self.enabled = enabled

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_qa(
        self,
        ticket: SWETicket,
        repo_path: str,
        checks: Optional[List[str]] = None,
    ) -> QAResult:
        """Run all configured QA checks against *repo_path*.

        Parameters
        ----------
        ticket:
            The ticket being verified (used for logging and metadata).
        repo_path:
            Absolute path to the repository root.
        checks:
            Override the instance-level check list for this run.

        Returns
        -------
        QAResult with approved=True only if every check passed.
        """
        if not self.enabled:
            logger.info("QA agent disabled — auto-approving ticket %s", ticket.ticket_id)
            return QAResult(approved=True, checks=[], summary="QA agent disabled")

        check_names = checks or self.checks
        results: List[CheckResult] = []

        for name in check_names:
            if name not in _CHECK_REGISTRY:
                logger.warning("QA: unknown check %r — skipping", name)
                results.append(CheckResult(
                    name=name,
                    passed=False,
                    duration_s=0.0,
                    output=f"Unknown check: {name}",
                ))
                continue

            result = self._run_check(name, repo_path)
            results.append(result)
            logger.info(
                "QA check %s: %s (%.1fs)",
                name,
                "PASS" if result.passed else "FAIL",
                result.duration_s,
            )

        all_passed = all(r.passed for r in results)

        # Build summary
        lines = []
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            lines.append(f"- [{status}] {r.name} ({r.duration_s:.1f}s)")
            if not r.passed:
                # Include truncated output for failures
                truncated = r.output[:2000] if len(r.output) > 2000 else r.output
                lines.append(f"  Output: {truncated}")

        summary = "\n".join(lines)

        logger.info(
            "QA result for ticket %s: %s (%d/%d checks passed)",
            ticket.ticket_id,
            "APPROVED" if all_passed else "REJECTED",
            sum(1 for r in results if r.passed),
            len(results),
        )

        return QAResult(approved=all_passed, checks=results, summary=summary)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_check(self, name: str, repo_path: str) -> CheckResult:
        """Execute a single check by name and return its result."""
        spec = _CHECK_REGISTRY[name]
        cmd = spec["cmd"]
        cwd_subdir = spec.get("cwd_subdir")

        cwd = repo_path
        if cwd_subdir:
            cwd = str(Path(repo_path) / cwd_subdir)

        # Verify the working directory exists
        if not Path(cwd).is_dir():
            return CheckResult(
                name=name,
                passed=False,
                duration_s=0.0,
                output=f"Working directory does not exist: {cwd}",
            )

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=cwd,
                env={**os.environ},
            )
            elapsed = time.monotonic() - start

            output = proc.stdout
            if proc.stderr:
                output += "\n--- stderr ---\n" + proc.stderr

            passed = proc.returncode == 0

            # Special handling for regression check: compare pass count
            if name == "regression" and passed and self.baseline_test_count is not None:
                passed = self._check_regression_count(proc.stdout)

            return CheckResult(
                name=name,
                passed=passed,
                duration_s=round(elapsed, 2),
                output=output,
            )

        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            return CheckResult(
                name=name,
                passed=False,
                duration_s=round(elapsed, 2),
                output=f"Check timed out after {self.timeout}s",
            )
        except FileNotFoundError as exc:
            elapsed = time.monotonic() - start
            return CheckResult(
                name=name,
                passed=False,
                duration_s=round(elapsed, 2),
                output=f"Command not found: {exc}",
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.exception("QA check %s raised unexpected error", name)
            return CheckResult(
                name=name,
                passed=False,
                duration_s=round(elapsed, 2),
                output=f"Unexpected error: {exc}",
            )

    def _check_regression_count(self, stdout: str) -> bool:
        """Parse pytest output for pass count and compare to baseline.

        Returns True if the number of passed tests >= baseline_test_count.
        """
        if self.baseline_test_count is None:
            return True

        # pytest -q outputs lines like "827 passed, 2 warnings in 12.34s"
        import re
        match = re.search(r"(\d+)\s+passed", stdout)
        if not match:
            logger.warning("QA regression: could not parse pass count from pytest output")
            return False

        actual = int(match.group(1))
        if actual < self.baseline_test_count:
            logger.warning(
                "QA regression: %d passed < baseline %d",
                actual,
                self.baseline_test_count,
            )
            return False

        logger.info(
            "QA regression: %d passed >= baseline %d — OK",
            actual,
            self.baseline_test_count,
        )
        return True
