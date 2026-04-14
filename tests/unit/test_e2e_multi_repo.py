"""End-to-end tests for multi-repo GitHub issue pipeline.

Tests cover:
  1. fetch_github_tickets iterates over multiple repos
  2. Only assigned issues are picked up
  3. Unassigned issues are skipped
  4. Deduplication prevents double-processing
  5. Cross-repo isolation (different repos get different fingerprints)
  6. Severity detection from labels and title
  7. Repo metadata is attached to tickets
  8. Scanner + assigned-fetch don't create duplicates
  9. Empty repos produce no tickets
  10. Fail-open on gh CLI errors
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional, Set
from unittest.mock import MagicMock, patch

import pytest

# We import the functions under test
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketSeverity


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_gh_issue(
    number: int,
    title: str,
    body: str = "",
    labels: Optional[List[str]] = None,
    assignees: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a mock GitHub issue JSON as returned by gh CLI."""
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": l} for l in (labels or [])],
        "assignees": [{"login": a} for a in (assignees or [])],
    }


class FakeStore:
    """Minimal store that tracks fingerprints."""

    def __init__(self, existing_fps: Optional[Set[str]] = None):
        self._fps: Set[str] = set(existing_fps or set())
        self._tickets: List[SWETicket] = []

    @property
    def known_fingerprints(self) -> Set[str]:
        return self._fps

    def add(self, ticket: SWETicket) -> None:
        fp = ticket.metadata.get("fingerprint", "")
        self._fps.add(fp)
        self._tickets.append(ticket)

    def list_all(self) -> List[SWETicket]:
        return self._tickets


# ── Mock subprocess runner ────────────────────────────────────────────────────

# Maps (repo, assignee) -> list of issues to return
_MOCK_ISSUES: Dict[str, List[Dict]] = {}


def _mock_subprocess_run(cmd, **kwargs):
    """Mock subprocess.run for gh issue list commands."""
    result = MagicMock()

    if cmd[0] == "gh" and cmd[1] == "issue" and cmd[2] == "list":
        # Parse --repo and --assignee from the command
        repo = ""
        assignee = ""
        for i, arg in enumerate(cmd):
            if arg == "--repo" and i + 1 < len(cmd):
                repo = cmd[i + 1]
            if arg == "--assignee" and i + 1 < len(cmd):
                assignee = cmd[i + 1]

        key = f"{repo}:{assignee}" if assignee else repo
        issues = _MOCK_ISSUES.get(key, [])
        result.returncode = 0
        result.stdout = json.dumps(issues)
        result.stderr = ""
    else:
        result.returncode = 1
        result.stdout = ""
        result.stderr = "unknown command"

    return result


# ── Test: Multi-Repo Assigned Issue Fetch ─────────────────────────────────────


class TestMultiRepoFetch:
    """Tests for fetch_github_tickets with multi-repo support."""

    @pytest.fixture(autouse=True)
    def setup_mock_issues(self):
        """Set up mock GitHub issue data for all 4 repos."""
        global _MOCK_ISSUES
        _MOCK_ISSUES = {
            # HealthTrack: 1 issue assigned to swe-squad-alpha
            "your-org/SWE-Sandbox-HealthTrack:swe-squad-alpha": [
                _make_gh_issue(1, "[CRITICAL] Patient data API returns 500", labels=["bug", "critical"]),
            ],
            # ShopStream: 1 issue assigned to swe-squad-alpha
            "your-org/SWE-Sandbox-ShopStream:swe-squad-alpha": [
                _make_gh_issue(1, "[HIGH] Cart total calculation wrong", labels=["bug"]),
            ],
            # GreenGrid: 0 assigned issues (4 exist but unassigned)
            "your-org/SWE-Sandbox-GreenGrid:swe-squad-alpha": [],
            # EduPath: 0 assigned issues
            "your-org/SWE-Sandbox-EduPath:swe-squad-alpha": [],
        }
        yield
        _MOCK_ISSUES = {}

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_fetches_from_all_repos(self, mock_run):
        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        repos = [
            "your-org/SWE-Sandbox-HealthTrack",
            "your-org/SWE-Sandbox-ShopStream",
            "your-org/SWE-Sandbox-GreenGrid",
            "your-org/SWE-Sandbox-EduPath",
        ]
        tickets = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)

        # Should get exactly 2 tickets (1 from HealthTrack + 1 from ShopStream)
        assert len(tickets) == 2

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_only_assigned_issues_picked_up(self, mock_run):
        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        repos = [
            "your-org/SWE-Sandbox-GreenGrid",
            "your-org/SWE-Sandbox-EduPath",
        ]
        tickets = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(tickets) == 0, "Unassigned repos should return 0 tickets"

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_repo_metadata_attached(self, mock_run):
        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        repos = ["your-org/SWE-Sandbox-HealthTrack"]
        tickets = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(tickets) == 1
        assert tickets[0].metadata["repo"] == "your-org/SWE-Sandbox-HealthTrack"

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_cross_repo_fingerprints_unique(self, mock_run):
        """Same issue number on different repos gets distinct fingerprints."""
        global _MOCK_ISSUES
        _MOCK_ISSUES["your-org/SWE-Sandbox-HealthTrack:swe-squad-alpha"] = [
            _make_gh_issue(1, "Bug A", labels=["bug"]),
        ]
        _MOCK_ISSUES["your-org/SWE-Sandbox-ShopStream:swe-squad-alpha"] = [
            _make_gh_issue(1, "Bug B", labels=["bug"]),  # Same issue #1, different repo
        ]

        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        repos = ["your-org/SWE-Sandbox-HealthTrack", "your-org/SWE-Sandbox-ShopStream"]
        tickets = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)

        assert len(tickets) == 2, "Same issue# on different repos should produce 2 distinct tickets"
        fps = {t.metadata["fingerprint"] for t in tickets}
        assert len(fps) == 2, "Fingerprints must be unique per repo"

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_dedup_skips_known_fingerprints(self, mock_run):
        from scripts.ops.swe_team_runner import fetch_github_tickets

        # Pre-seed store with HealthTrack issue #1
        store = FakeStore(existing_fps={"gh-issue-your-org/SWE-Sandbox-HealthTrack-1"})
        repos = ["your-org/SWE-Sandbox-HealthTrack"]
        tickets = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(tickets) == 0, "Already-known issue should be deduped"

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_second_fetch_dedupes(self, mock_run):
        """Running fetch twice should not produce duplicates."""
        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        repos = ["your-org/SWE-Sandbox-HealthTrack", "your-org/SWE-Sandbox-ShopStream"]

        first = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(first) == 2

        # Add first batch to store
        for t in first:
            store.add(t)

        second = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(second) == 0, "Second fetch should return 0 — all already known"

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_severity_detection_critical(self, mock_run):
        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        repos = ["your-org/SWE-Sandbox-HealthTrack"]
        tickets = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(tickets) == 1
        assert tickets[0].severity == TicketSeverity.CRITICAL

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_severity_detection_high_default(self, mock_run):
        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        repos = ["your-org/SWE-Sandbox-ShopStream"]
        tickets = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(tickets) == 1
        assert tickets[0].severity == TicketSeverity.HIGH

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_no_github_account_returns_empty(self, mock_run):
        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        tickets = fetch_github_tickets(store, github_account="", repos=["your-org/SWE-Sandbox"])
        assert tickets == []

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_no_repos_returns_empty(self, mock_run):
        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        tickets = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=[])
        assert tickets == []

    @patch("scripts.ops.swe_team_runner.subprocess.run")
    def test_gh_cli_error_returns_empty(self, mock_run):
        """gh CLI failure should return empty list, not crash."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="auth error")

        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        tickets = fetch_github_tickets(
            store, github_account="swe-squad-alpha",
            repos=["your-org/SWE-Sandbox-HealthTrack"],
        )
        assert tickets == []

    @patch("scripts.ops.swe_team_runner.subprocess.run")
    def test_gh_cli_timeout_returns_empty(self, mock_run):
        """subprocess timeout should be caught gracefully."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=15)

        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        tickets = fetch_github_tickets(
            store, github_account="swe-squad-alpha",
            repos=["your-org/SWE-Sandbox-HealthTrack"],
        )
        assert tickets == []


# ── Test: Severity Mapping ────────────────────────────────────────────────────


class TestSeverityMapping:
    """Test severity detection from labels and title patterns."""

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_critical_from_label(self, mock_run):
        global _MOCK_ISSUES
        _MOCK_ISSUES["test-repo:bot"] = [
            _make_gh_issue(10, "Something broke", labels=["critical"]),
        ]
        from scripts.ops.swe_team_runner import fetch_github_tickets

        tickets = fetch_github_tickets(FakeStore(), github_account="bot", repos=["test-repo"])
        assert tickets[0].severity == TicketSeverity.CRITICAL
        _MOCK_ISSUES.pop("test-repo:bot", None)

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_critical_from_p0_label(self, mock_run):
        global _MOCK_ISSUES
        _MOCK_ISSUES["test-repo:bot"] = [
            _make_gh_issue(11, "Outage", labels=["p0", "bug"]),
        ]
        from scripts.ops.swe_team_runner import fetch_github_tickets

        tickets = fetch_github_tickets(FakeStore(), github_account="bot", repos=["test-repo"])
        assert tickets[0].severity == TicketSeverity.CRITICAL
        _MOCK_ISSUES.pop("test-repo:bot", None)

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_high_from_label(self, mock_run):
        global _MOCK_ISSUES
        _MOCK_ISSUES["test-repo:bot"] = [
            _make_gh_issue(12, "Bad perf", labels=["high"]),
        ]
        from scripts.ops.swe_team_runner import fetch_github_tickets

        tickets = fetch_github_tickets(FakeStore(), github_account="bot", repos=["test-repo"])
        assert tickets[0].severity == TicketSeverity.HIGH
        _MOCK_ISSUES.pop("test-repo:bot", None)

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_low_from_label(self, mock_run):
        global _MOCK_ISSUES
        _MOCK_ISSUES["test-repo:bot"] = [
            _make_gh_issue(13, "Typo fix", labels=["low"]),
        ]
        from scripts.ops.swe_team_runner import fetch_github_tickets

        tickets = fetch_github_tickets(FakeStore(), github_account="bot", repos=["test-repo"])
        assert tickets[0].severity == TicketSeverity.LOW
        _MOCK_ISSUES.pop("test-repo:bot", None)

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_default_severity_is_high(self, mock_run):
        global _MOCK_ISSUES
        _MOCK_ISSUES["test-repo:bot"] = [
            _make_gh_issue(14, "Some issue", labels=["enhancement"]),
        ]
        from scripts.ops.swe_team_runner import fetch_github_tickets

        tickets = fetch_github_tickets(FakeStore(), github_account="bot", repos=["test-repo"])
        assert tickets[0].severity == TicketSeverity.HIGH
        _MOCK_ISSUES.pop("test-repo:bot", None)

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_module_from_label(self, mock_run):
        global _MOCK_ISSUES
        _MOCK_ISSUES["test-repo:bot"] = [
            _make_gh_issue(15, "Auth bug", labels=["bug", "module:auth"]),
        ]
        from scripts.ops.swe_team_runner import fetch_github_tickets

        tickets = fetch_github_tickets(FakeStore(), github_account="bot", repos=["test-repo"])
        assert tickets[0].source_module == "auth"
        _MOCK_ISSUES.pop("test-repo:bot", None)


# ── Test: Pipeline Integration ────────────────────────────────────────────────


class TestPipelineIntegration:
    """Tests simulating the full pipeline flow."""

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_full_4_repo_pipeline(self, mock_run):
        """Simulate real E2E: 4 repos, issues on all, assigned only on 2."""
        global _MOCK_ISSUES
        _MOCK_ISSUES = {
            "your-org/SWE-Sandbox-HealthTrack:swe-squad-alpha": [
                _make_gh_issue(1, "[CRITICAL] API 500", labels=["bug", "critical"]),
            ],
            "your-org/SWE-Sandbox-ShopStream:swe-squad-alpha": [
                _make_gh_issue(1, "[HIGH] Cart calc wrong", labels=["bug"]),
            ],
            "your-org/SWE-Sandbox-GreenGrid:swe-squad-alpha": [],
            "your-org/SWE-Sandbox-EduPath:swe-squad-alpha": [],
        }

        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        repos = [
            "your-org/SWE-Sandbox-HealthTrack",
            "your-org/SWE-Sandbox-ShopStream",
            "your-org/SWE-Sandbox-GreenGrid",
            "your-org/SWE-Sandbox-EduPath",
        ]

        tickets = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)

        # Verify: exactly 2 tickets
        assert len(tickets) == 2

        # Verify: correct repos
        repos_seen = {t.metadata["repo"] for t in tickets}
        assert repos_seen == {
            "your-org/SWE-Sandbox-HealthTrack",
            "your-org/SWE-Sandbox-ShopStream",
        }

        # Verify: no tickets from GreenGrid or EduPath
        for t in tickets:
            assert "GreenGrid" not in t.metadata["repo"]
            assert "EduPath" not in t.metadata["repo"]

        # Verify: severity correctly detected
        severity_map = {t.metadata["repo"]: t.severity for t in tickets}
        assert severity_map["your-org/SWE-Sandbox-HealthTrack"] == TicketSeverity.CRITICAL
        assert severity_map["your-org/SWE-Sandbox-ShopStream"] == TicketSeverity.HIGH

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_no_duplicates_across_pipeline_runs(self, mock_run):
        """Run pipeline 3 times — only first should produce tickets."""
        global _MOCK_ISSUES
        _MOCK_ISSUES = {
            "your-org/SWE-Sandbox-HealthTrack:swe-squad-alpha": [
                _make_gh_issue(1, "[CRITICAL] API 500", labels=["bug", "critical"]),
            ],
        }

        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        repos = ["your-org/SWE-Sandbox-HealthTrack"]

        # Run 1: should get 1 ticket
        run1 = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(run1) == 1
        for t in run1:
            store.add(t)

        # Run 2: dedup should prevent duplicates
        run2 = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(run2) == 0

        # Run 3: still no duplicates
        run3 = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(run3) == 0

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_mixed_repos_some_failing(self, mock_run):
        """One repo fails, others still work."""
        global _MOCK_ISSUES
        _MOCK_ISSUES = {
            "your-org/SWE-Sandbox-HealthTrack:swe-squad-alpha": [
                _make_gh_issue(1, "Bug", labels=["bug"]),
            ],
            # ShopStream not in mock → will get empty response (not error)
        }

        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        repos = ["your-org/SWE-Sandbox-HealthTrack", "your-org/SWE-Sandbox-ShopStream"]
        tickets = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(tickets) == 1  # HealthTrack works, ShopStream returns empty

    @patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=_mock_subprocess_run)
    def test_env_var_fallback_when_no_repos(self, mock_run):
        """When repos=None, falls back to SWE_GITHUB_REPO env var."""
        import os

        global _MOCK_ISSUES
        _MOCK_ISSUES = {
            "your-org/SWE-Sandbox:swe-squad-alpha": [
                _make_gh_issue(99, "Fallback issue", labels=["bug"]),
            ],
        }

        from scripts.ops.swe_team_runner import fetch_github_tickets

        store = FakeStore()
        with patch.dict(os.environ, {"SWE_GITHUB_REPO": "your-org/SWE-Sandbox"}):
            tickets = fetch_github_tickets(store, github_account="swe-squad-alpha")
        assert len(tickets) == 1
        assert tickets[0].metadata["repo"] == "your-org/SWE-Sandbox"


# ── Test: Scanner Multi-Repo ─────────────────────────────────────────────────


class TestScannerMultiRepo:
    """Tests for GitHubIssueScanner working across multiple repos."""

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_scanner_uses_repo_flag(self, mock_run):
        """Scanner should pass --repo to gh CLI."""
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")

        from src.swe_team.github_scanner import GitHubIssueScanner, GitHubScannerConfig

        config = GitHubScannerConfig(repo="your-org/SWE-Sandbox-HealthTrack")
        scanner = GitHubIssueScanner(config)
        scanner.scan()

        # Verify --repo was passed
        call_args = mock_run.call_args[0][0]
        assert "--repo" in call_args
        assert "your-org/SWE-Sandbox-HealthTrack" in call_args

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_scanner_picks_up_bug_label(self, mock_run):
        """Scanner should pick up issues with 'bug' label when assigned to bot."""
        issues = [
            _make_gh_issue(1, "A bug", labels=["bug"]),
            _make_gh_issue(2, "A question", labels=["question"]),
            _make_gh_issue(3, "Enhancement", labels=["enhancement"]),
        ]
        # Add createdAt and assignees — #1 is assigned to bot, others unassigned
        for i in issues:
            i["createdAt"] = "2026-03-27T00:00:00Z"
            i["assignees"] = []
        issues[0]["assignees"] = [{"login": "swe-squad-alpha"}]

        # First call: fetch open issues. Subsequent calls: merged PR check → no PRs
        _no_prs = MagicMock(returncode=0, stdout="[]", stderr="")
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(issues), stderr=""),
            _no_prs,  # PR check for issue #1 (assigned → eligible)
        ]

        from src.swe_team.github_scanner import GitHubIssueScanner, GitHubScannerConfig

        config = GitHubScannerConfig(
            repo="your-org/SWE-Sandbox-HealthTrack",
            github_account="swe-squad-alpha",
        )
        # Provide empty known sets to avoid disk-persisted dedup state
        scanner = GitHubIssueScanner(config, known_fingerprints=set(), known_issue_numbers=set())
        tickets = scanner.scan()

        # Should pick up #1 (bug + assigned to bot), skip #2 (question), skip #3 (no pickup label)
        assert len(tickets) == 1
        assert tickets[0].metadata["github_issue"] == 1

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_scanner_dedupes_with_known_issues(self, mock_run):
        """Scanner should skip issues already known."""
        issues = [
            {**_make_gh_issue(5, "Known bug", labels=["bug"]),
             "createdAt": "2026-03-27T00:00:00Z", "assignees": []},
        ]
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(issues), stderr=""
        )

        from src.swe_team.github_scanner import GitHubIssueScanner, GitHubScannerConfig

        config = GitHubScannerConfig(repo="your-org/SWE-Sandbox-HealthTrack")
        scanner = GitHubIssueScanner(config, known_issue_numbers={5})
        tickets = scanner.scan()
        assert len(tickets) == 0
