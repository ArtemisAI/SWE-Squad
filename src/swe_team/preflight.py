"""
Pre-flight validation for the Autonomous SWE Team.

Validates execution context before any developer or investigator action
to prevent agents from running against the wrong repo, git identity,
GitHub account, or environment.  Returns a structured result so callers
can log, alert, and abort cleanly instead of silently proceeding in the
wrong context.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Environment variables that must be set for a valid SWE Team execution.
REQUIRED_ENV_VARS: List[str] = [
    "SWE_TEAM_ID",
    "SWE_GITHUB_REPO",
]


@dataclass
class PreflightResult:
    """Outcome of a pre-flight validation run."""

    passed: bool
    failures: List[str] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable one-liner for logs / alerts."""
        if self.passed:
            return "Preflight OK"
        return "Preflight FAILED: " + "; ".join(self.failures)


class PreflightCheck:
    """Validates execution context before agent work begins.

    Parameters
    ----------
    expected_git_name:
        Expected ``git config user.name`` value.  Skipped when *None*.
    expected_git_email:
        Expected ``git config user.email`` value.  Skipped when *None*.
    expected_github_account:
        Expected GitHub CLI authenticated account (``gh auth status``).
        Skipped when *None*.
    expected_repo_root:
        Expected repository root path.  Skipped when *None*.
    required_env_vars:
        List of environment variable names that must be set.
    """

    def __init__(
        self,
        *,
        expected_git_name: Optional[str] = None,
        expected_git_email: Optional[str] = None,
        expected_github_account: Optional[str] = None,
        expected_repo_root: Optional[Path] = None,
        required_env_vars: Optional[List[str]] = None,
    ) -> None:
        self._expected_git_name = expected_git_name
        self._expected_git_email = expected_git_email
        self._expected_github_account = expected_github_account
        self._expected_repo_root = expected_repo_root
        self._required_env_vars = (
            required_env_vars if required_env_vars is not None else REQUIRED_ENV_VARS
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> PreflightResult:
        """Execute all configured checks and return a consolidated result."""
        failures: List[str] = []

        failures.extend(self.check_git_identity())
        failures.extend(self.check_working_directory())
        failures.extend(self.check_github_auth())
        failures.extend(self.check_env_vars())

        passed = len(failures) == 0
        result = PreflightResult(passed=passed, failures=failures)

        if passed:
            logger.info("Preflight checks passed")
        else:
            logger.warning("Preflight checks FAILED: %s", result.summary())

        return result

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def check_git_identity(self) -> List[str]:
        """Verify git user.name and user.email match expected bot account."""
        failures: List[str] = []

        if self._expected_git_name is not None:
            actual = self._git_config("user.name")
            if actual is None:
                failures.append("git user.name is not configured")
            elif actual != self._expected_git_name:
                failures.append(
                    f"git user.name mismatch: expected '{self._expected_git_name}', "
                    f"got '{actual}'"
                )

        if self._expected_git_email is not None:
            actual = self._git_config("user.email")
            if actual is None:
                failures.append("git user.email is not configured")
            elif actual != self._expected_git_email:
                failures.append(
                    f"git user.email mismatch: expected '{self._expected_git_email}', "
                    f"got '{actual}'"
                )

        return failures

    def check_working_directory(self) -> List[str]:
        """Verify we are in the expected repository root."""
        if self._expected_repo_root is None:
            return []

        failures: List[str] = []
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                failures.append("Not inside a git repository")
            else:
                actual = Path(result.stdout.strip()).resolve()
                expected = self._expected_repo_root.resolve()
                if actual != expected:
                    failures.append(
                        f"Repo root mismatch: expected '{expected}', got '{actual}'"
                    )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            failures.append(f"git rev-parse failed: {exc}")

        return failures

    def check_github_auth(self) -> List[str]:
        """Verify ``gh auth status`` returns the expected account."""
        if self._expected_github_account is None:
            return []

        failures: List[str] = []
        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            combined = result.stdout + result.stderr
            if result.returncode != 0:
                failures.append(
                    f"gh auth status failed (rc={result.returncode}): "
                    f"{combined.strip()[:200]}"
                )
            elif self._expected_github_account not in combined:
                failures.append(
                    f"GitHub account mismatch: expected '{self._expected_github_account}' "
                    f"in gh auth output, but not found"
                )
        except FileNotFoundError:
            failures.append("gh CLI not found on PATH")
        except subprocess.TimeoutExpired:
            failures.append("gh auth status timed out")

        return failures

    def check_env_vars(self) -> List[str]:
        """Verify required environment variables are set."""
        failures: List[str] = []
        for var in self._required_env_vars:
            if not os.environ.get(var):
                failures.append(f"Required env var '{var}' is not set")
        return failures

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _git_config(key: str) -> Optional[str]:
        """Read a git config value, returning None on failure."""
        try:
            result = subprocess.run(
                ["git", "config", "--get", key],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
