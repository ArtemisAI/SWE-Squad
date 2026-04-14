"""Unit tests for src.swe_team.github_scanner."""
from __future__ import annotations

import json
import subprocess
from typing import Set
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.github_scanner import GitHubIssueScanner, GitHubScannerConfig
from src.swe_team.models import TicketSeverity, TicketStatus, TicketType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_issue(
    number: int = 1,
    title: str = "Test issue",
    body: str = "Body text",
    labels=None,
    assignees=None,
) -> dict:
    label_list = [{"name": n} for n in (labels if labels is not None else ["swe-squad"])]
    # Default: assigned to your-bot-alpha (matches _scanner default github_account)
    assignee_list = [{"login": a} for a in (assignees if assignees is not None else ["your-bot-alpha"])]
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": label_list,
        "assignees": assignee_list,
        "createdAt": "2024-01-01T00:00:00Z",
    }


def _proc(stdout: str = "[]", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = ""
    return m


def _mock_gh_calls(issues_json: str = "[]", merged_prs_json: str = "[]", returncode: int = 0):
    """Mock subprocess.run to handle both issue list and merged PR checks.
    
    Returns a side_effect function that distinguishes between:
    - gh issue list (returns issues_json)
    - gh pr list --state merged (returns merged_prs_json, default empty)
    """
    def side_effect(cmd, *args, **kwargs):
        if "issue" in cmd and "list" in cmd:
            return _proc(issues_json, returncode=returncode)
        elif "pr" in cmd and "list" in cmd and "merged" in cmd:
            return _proc(merged_prs_json, returncode=returncode)
        return _proc("[]", returncode=returncode)
    return side_effect


def _scanner(
    repo: str = "owner/repo",
    pickup_labels=None,
    skip_labels=None,
    max_issues: int = 10,
    known=None,
    github_account: str = "your-bot-alpha",
) -> GitHubIssueScanner:
    cfg = GitHubScannerConfig(
        repo=repo,
        pickup_labels=pickup_labels if pickup_labels is not None else {"swe-squad"},
        skip_labels=skip_labels if skip_labels is not None else {"wontfix", "duplicate", "invalid", "needs-human-review"},
        max_issues_per_scan=max_issues,
        enabled=True,
        github_account=github_account,
    )
    # Convert integer issue numbers to fingerprint strings for the new API
    known_fps: Set[str] = set()
    known_nums: Set[int] = set()
    if known is not None:
        known_fps = {f"gh-issue-{n}" if isinstance(n, int) else n for n in known}
        known_nums = {int(n) for n in known if isinstance(n, int)}
    return GitHubIssueScanner(
        cfg,
        known_fingerprints=known_fps,
        known_issue_numbers=known_nums,
    )


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

class TestGitHubScannerConfig:
    def test_defaults(self):
        cfg = GitHubScannerConfig()
        assert cfg.repo == ""
        assert "swe-squad" in cfg.pickup_labels
        assert "automated" in cfg.pickup_labels
        # bug/critical/high/medium are assigned-only, NOT in pickup_labels
        assert "bug" not in cfg.pickup_labels
        assert "critical" not in cfg.pickup_labels
        assert "bug" in cfg.assigned_only_labels
        assert "critical" in cfg.assigned_only_labels
        assert "wontfix" in cfg.skip_labels
        assert cfg.max_issues_per_scan == 10
        assert cfg.enabled is True
        assert cfg.github_account == ""

    def test_custom_values(self):
        cfg = GitHubScannerConfig(repo="a/b", max_issues_per_scan=5, enabled=False)
        assert cfg.repo == "a/b"
        assert cfg.max_issues_per_scan == 5
        assert cfg.enabled is False


# ---------------------------------------------------------------------------
# scan() — disabled / unconfigured guards
# ---------------------------------------------------------------------------

class TestScanGuards:
    def test_disabled_returns_empty(self):
        cfg = GitHubScannerConfig(repo="owner/repo", enabled=False)
        scanner = GitHubIssueScanner(cfg)
        assert scanner.scan() == []

    def test_no_repo_returns_empty(self):
        cfg = GitHubScannerConfig(repo="", enabled=True)
        scanner = GitHubIssueScanner(cfg)
        assert scanner.scan() == []


# ---------------------------------------------------------------------------
# scan() — happy path
# ---------------------------------------------------------------------------

class TestScanHappyPath:
    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_returns_ticket_for_matching_issue(self, mock_run):
        issues = [_make_issue(number=42, title="Fix login", labels=["swe-squad"])]
        mock_run.side_effect = _mock_gh_calls(json.dumps(issues))
        tickets = _scanner().scan()
        assert len(tickets) == 1
        t = tickets[0]
        assert "[GH#42]" in t.title
        assert "Fix login" in t.title

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_ticket_status_and_type(self, mock_run):
        mock_run.side_effect = _mock_gh_calls(json.dumps([_make_issue(number=1)]))
        tickets = _scanner().scan()
        assert tickets[0].status == TicketStatus.OPEN
        # Default issue title/labels don't match BUG keywords — type is UNKNOWN
        assert tickets[0].ticket_type == TicketType.UNKNOWN

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_fingerprint_format(self, mock_run):
        mock_run.side_effect = _mock_gh_calls(json.dumps([_make_issue(number=7)]))
        tickets = _scanner(repo="org/proj").scan()
        assert tickets[0].metadata["fingerprint"] == "gh-issue-org/proj-7"

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_metadata_fields(self, mock_run):
        mock_run.side_effect = _mock_gh_calls(json.dumps([_make_issue(number=10)]))
        tickets = _scanner(repo="owner/repo").scan()
        meta = tickets[0].metadata
        assert meta["github_issue"] == 10
        assert meta["repo"] == "owner/repo"
        assert meta["source"] == "github_scanner"

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_depends_on_markers_parsed_to_metadata(self, mock_run):
        issue = _make_issue(
            number=13,
            body="Need this soon\nDepends-On: #11, #12\nextra text",
        )
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        tickets = _scanner(repo="owner/repo").scan()
        assert tickets[0].metadata["depends_on"] == [
            "gh-issue-owner/repo-11",
            "gh-issue-owner/repo-12",
        ]
        assert tickets[0].blocked_by == [
            "gh-issue-owner/repo-11",
            "gh-issue-owner/repo-12",
        ]

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_depends_on_parsing_handles_duplicates_and_multiple_markers(self, mock_run):
        issue = _make_issue(
            number=14,
            body="DEPENDS-ON: #11, #11\nnotes\ndepends-on: #12\n",
        )
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        tickets = _scanner(repo="owner/repo").scan()
        assert tickets[0].metadata["depends_on"] == [
            "gh-issue-owner/repo-11",
            "gh-issue-owner/repo-12",
        ]

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_depends_on_parsing_absent_marker_returns_empty(self, mock_run):
        issue = _make_issue(number=15, body="No dependency marker here")
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        tickets = _scanner(repo="owner/repo").scan()
        assert tickets[0].metadata["depends_on"] == []
        assert tickets[0].blocked_by == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_empty_issue_list_returns_empty(self, mock_run):
        mock_run.side_effect = _mock_gh_calls("[]")
        assert _scanner().scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_issue_with_merged_pr_skipped(self, mock_run):
        """Issues with merged PRs should be skipped (fix for #376)."""
        issue = _make_issue(number=68, title="Bug with merged PR")
        # PR #78 merged, references issue #68
        merged_pr = [{"number": 78}]
        mock_run.side_effect = _mock_gh_calls(
            json.dumps([issue]),
            merged_prs_json=json.dumps(merged_pr)
        )
        tickets = _scanner().scan()
        # Issue should be skipped because it has a merged PR
        assert len(tickets) == 0

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_issue_without_merged_pr_processed(self, mock_run):
        """Issues without merged PRs should be processed normally."""
        issue = _make_issue(number=69, title="Bug without merged PR")
        # No merged PRs
        mock_run.side_effect = _mock_gh_calls(
            json.dumps([issue]),
            merged_prs_json="[]"
        )
        tickets = _scanner().scan()
        # Issue should be processed because it has no merged PR
        assert len(tickets) == 1
        assert tickets[0].metadata["github_issue"] == 69


# ---------------------------------------------------------------------------
# Label filtering
# ---------------------------------------------------------------------------

class TestLabelFiltering:
    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_assigned_issue_picked_up_regardless_of_labels(self, mock_run):
        """Labels are metadata, not pickup triggers. Assigned = picked up."""
        issue = _make_issue(number=1, labels=["enhancement"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert len(_scanner().scan()) == 1

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_skip_label_blocks_pickup(self, mock_run):
        issue = _make_issue(number=1, labels=["swe-squad", "wontfix"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_needs_human_review_blocked(self, mock_run):
        issue = _make_issue(number=1, labels=["swe-squad", "needs-human-review"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_duplicate_label_blocked(self, mock_run):
        issue = _make_issue(number=1, labels=["swe-squad", "duplicate"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan() == []


# ---------------------------------------------------------------------------
# Assignee-gated pickup (bug/critical/high/medium without swe-squad label)
# ---------------------------------------------------------------------------

class TestAssigneeGatedPickup:
    """ALL issues must be assigned to the bot account to be picked up.
    Labels are informational metadata, never pickup triggers.
    This is the core isolation mechanism for multi-squad coexistence."""

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_unassigned_issue_skipped_even_with_swe_squad_label(self, mock_run):
        """swe-squad label does NOT bypass assignee check."""
        issue = _make_issue(number=1, labels=["swe-squad"], assignees=[])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_assigned_to_bot_picked_up(self, mock_run):
        issue = _make_issue(number=1, labels=["bug"], assignees=["your-bot-alpha"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        tickets = _scanner(github_account="your-bot-alpha").scan()
        assert len(tickets) == 1
        assert "[GH#1]" in tickets[0].title

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_assigned_to_other_user_skipped(self, mock_run):
        issue = _make_issue(number=1, labels=["bug"], assignees=["some-human"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner(github_account="your-bot-alpha").scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_assigned_to_other_squad_skipped(self, mock_run):
        """Alpha must not pick up issues assigned to beta."""
        issue = _make_issue(number=1, labels=["swe-squad"], assignees=["your-bot-beta"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner(github_account="your-bot-alpha").scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_no_github_account_configured_skips_all(self, mock_run):
        """If no bot account configured, nothing is picked up (fail-closed)."""
        issue = _make_issue(number=1, labels=["swe-squad"], assignees=["your-bot-alpha"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner(github_account="").scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_case_insensitive_assignee_match(self, mock_run):
        issue = _make_issue(number=7, labels=["high"], assignees=["Your-Bot-Alpha"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        tickets = _scanner(github_account="your-bot-alpha").scan()
        assert len(tickets) == 1

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_multiple_assignees_bot_included(self, mock_run):
        """Issue assigned to both human and bot — bot picks it up."""
        issue = _make_issue(number=2, labels=["enhancement"], assignees=["human-dev", "your-bot-alpha"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        tickets = _scanner(github_account="your-bot-alpha").scan()
        assert len(tickets) == 1

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_unassigned_automated_label_skipped(self, mock_run):
        """automated label does NOT bypass assignee check."""
        issue = _make_issue(number=3, labels=["automated"], assignees=[])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_ten_squads_isolation(self, mock_run):
        """Each squad only picks up its own assigned issues."""
        issues = [
            _make_issue(number=1, labels=["bug"], assignees=["your-bot-alpha"]),
            _make_issue(number=2, labels=["bug"], assignees=["your-bot-beta"]),
            _make_issue(number=3, labels=["bug"], assignees=["your-bot-gamma"]),
        ]
        mock_run.side_effect = _mock_gh_calls(json.dumps(issues))
        alpha_tickets = _scanner(github_account="your-bot-alpha").scan()
        assert len(alpha_tickets) == 1
        assert alpha_tickets[0].metadata["github_issue"] == 1


# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

class TestSeverityMapping:
    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_critical_label(self, mock_run):
        issue = _make_issue(labels=["swe-squad", "critical"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].severity == TicketSeverity.CRITICAL

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_high_label(self, mock_run):
        issue = _make_issue(labels=["swe-squad", "high"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].severity == TicketSeverity.HIGH

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_bug_label_maps_to_high(self, mock_run):
        issue = _make_issue(labels=["swe-squad", "bug"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].severity == TicketSeverity.HIGH

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_medium_label(self, mock_run):
        issue = _make_issue(labels=["swe-squad", "medium"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].severity == TicketSeverity.MEDIUM

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_low_label(self, mock_run):
        issue = _make_issue(labels=["swe-squad", "low"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].severity == TicketSeverity.LOW

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_no_severity_label_defaults_medium(self, mock_run):
        issue = _make_issue(labels=["swe-squad"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].severity == TicketSeverity.MEDIUM

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_critical_wins_over_high(self, mock_run):
        issue = _make_issue(labels=["swe-squad", "high", "critical"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].severity == TicketSeverity.CRITICAL

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_critical_wins_over_bug(self, mock_run):
        """bug maps to HIGH; critical must still win regardless of dict order."""
        issue = _make_issue(labels=["swe-squad", "bug", "critical"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].severity == TicketSeverity.CRITICAL

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_high_wins_over_medium_and_low(self, mock_run):
        issue = _make_issue(labels=["swe-squad", "low", "medium", "high"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].severity == TicketSeverity.HIGH

    def test_severity_priority_order_is_correct(self):
        """_SEVERITY_PRIORITY must be critical > high > medium > low/bug."""
        from src.swe_team.github_scanner import _SEVERITY_PRIORITY, _SEVERITY_MAP
        for label in _SEVERITY_PRIORITY:
            assert label in _SEVERITY_MAP, f"{label!r} in _SEVERITY_PRIORITY but missing from _SEVERITY_MAP"
        idx = {label: i for i, label in enumerate(_SEVERITY_PRIORITY)}
        assert idx["critical"] < idx["high"]
        assert idx["high"] < idx["medium"]
        assert idx["medium"] < idx["low"]


# ---------------------------------------------------------------------------
# Module detection
# ---------------------------------------------------------------------------

class TestModuleDetection:
    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_module_label_extracted(self, mock_run):
        issue = _make_issue(labels=["swe-squad", "module:auth"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].source_module == "auth"

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_no_module_label_defaults_github(self, mock_run):
        issue = _make_issue(labels=["swe-squad"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].source_module == "github"


# ---------------------------------------------------------------------------
# Title and body truncation
# ---------------------------------------------------------------------------

class TestTruncation:
    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_title_capped_at_120_chars(self, mock_run):
        long_title = "X" * 200
        issue = _make_issue(title=long_title)
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert len(_scanner().scan()[0].title) <= 120

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_body_capped_at_2000_chars(self, mock_run):
        issue = _make_issue(body="B" * 3000)
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert len(_scanner().scan()[0].description) <= 2000

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_empty_body_uses_title(self, mock_run):
        issue = _make_issue(title="My title", body="")
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].description == "My title"


# ---------------------------------------------------------------------------
# Assignee handling
# ---------------------------------------------------------------------------

class TestAssignees:
    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_first_assignee_used(self, mock_run):
        issue = _make_issue(assignees=["your-bot-alpha", "bob"])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan()[0].assigned_to == "your-bot-alpha"

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_no_assignee_skipped(self, mock_run):
        """Unassigned issues are never picked up (assignee-only gate)."""
        issue = _make_issue(assignees=[])
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan() == []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_known_issue_skipped(self, mock_run):
        issues = [_make_issue(number=1), _make_issue(number=2)]
        mock_run.side_effect = _mock_gh_calls(json.dumps(issues))
        tickets = _scanner(known={1}).scan()
        assert len(tickets) == 1
        assert tickets[0].metadata["github_issue"] == 2

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_issue_added_to_known_after_scan(self, mock_run):
        issues = [_make_issue(number=5)]
        mock_run.side_effect = _mock_gh_calls(json.dumps(issues))
        scanner = _scanner()
        scanner.scan()
        mock_run.side_effect = _mock_gh_calls(json.dumps(issues))
        assert scanner.scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_all_known_returns_empty(self, mock_run):
        issues = [_make_issue(number=3)]
        mock_run.side_effect = _mock_gh_calls(json.dumps(issues))
        assert _scanner(known={3}).scan() == []


# ---------------------------------------------------------------------------
# max_issues_per_scan cap
# ---------------------------------------------------------------------------

class TestMaxIssuesCap:
    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_cap_limits_returned_tickets(self, mock_run):
        issues = [_make_issue(number=i) for i in range(1, 11)]
        mock_run.side_effect = _mock_gh_calls(json.dumps(issues))
        assert len(_scanner(max_issues=3).scan()) == 3

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_cap_of_one(self, mock_run):
        issues = [_make_issue(number=i) for i in range(1, 6)]
        mock_run.side_effect = _mock_gh_calls(json.dumps(issues))
        assert len(_scanner(max_issues=1).scan()) == 1


# ---------------------------------------------------------------------------
# gh CLI failure handling
# ---------------------------------------------------------------------------

class TestGhCliFailures:
    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_non_zero_returncode_returns_empty(self, mock_run):
        mock_run.side_effect = _mock_gh_calls("", returncode=1)
        assert _scanner().scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_timeout_returns_empty(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        assert _scanner().scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_file_not_found_returns_empty(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        assert _scanner().scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_generic_exception_returns_empty(self, mock_run):
        mock_run.side_effect = OSError("network error")
        assert _scanner().scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_invalid_json_returns_empty(self, mock_run):
        mock_run.side_effect = _mock_gh_calls("not-json")
        assert _scanner().scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_empty_stdout_returns_empty(self, mock_run):
        mock_run.side_effect = _mock_gh_calls("")
        assert _scanner().scan() == []

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_issue_missing_number_skipped(self, mock_run):
        issue = {"title": "No number", "body": "", "labels": [{"name": "swe-squad"}], "assignees": [{"login": "your-bot-alpha"}]}
        mock_run.side_effect = _mock_gh_calls(json.dumps([issue]))
        assert _scanner().scan() == []


# ---------------------------------------------------------------------------
# gh CLI call verification
# ---------------------------------------------------------------------------

class TestGhCliCall:
    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_uses_correct_repo(self, mock_run):
        mock_run.side_effect = _mock_gh_calls("[]")
        _scanner(repo="myorg/myrepo").scan()
        cmd = mock_run.call_args[0][0]
        assert "--repo" in cmd
        idx = cmd.index("--repo")
        assert cmd[idx + 1] == "myorg/myrepo"

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_requests_open_issues(self, mock_run):
        mock_run.side_effect = _mock_gh_calls("[]")
        _scanner().scan()
        cmd = mock_run.call_args[0][0]
        assert "--state" in cmd
        idx = cmd.index("--state")
        assert cmd[idx + 1] == "open"

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_requests_json_output(self, mock_run):
        mock_run.side_effect = _mock_gh_calls("[]")
        _scanner().scan()
        cmd = mock_run.call_args[0][0]
        assert "--json" in cmd

    @patch("src.swe_team.github_scanner.subprocess.run")
    def test_timeout_applied(self, mock_run):
        mock_run.side_effect = _mock_gh_calls("[]")
        _scanner().scan()
        kwargs = mock_run.call_args[1]
        assert "timeout" in kwargs
        assert kwargs["timeout"] > 0
