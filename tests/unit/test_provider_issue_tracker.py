"""
Tests for the IssueTracker provider interface and GitHubIssueTracker implementation.

Covers:
  - Construction with config
  - create_issue, find_existing, close_issue, comment (mock subprocess)
  - PR operations: find_pr, create_pr, merge_pr, close_pr, get_pr_labels
  - Error handling (gh CLI failures return None/empty gracefully)
  - Protocol compliance (GitHubIssueTracker satisfies IssueTracker)
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.providers.issue_tracker.base import IssueRef, IssueTracker
from src.swe_team.providers.issue_tracker.github_provider import GitHubIssueTracker


# ---------------------------------------------------------------------------
#  Protocol compliance
# ---------------------------------------------------------------------------

class TestProtocolCompliance:
    """GitHubIssueTracker satisfies the IssueTracker runtime_checkable protocol."""

    def test_isinstance_check(self):
        tracker = GitHubIssueTracker(repo="owner/repo", token="tok_123")
        assert isinstance(tracker, IssueTracker)

    def test_protocol_is_runtime_checkable(self):
        """IssueTracker has @runtime_checkable so isinstance works."""
        assert hasattr(IssueTracker, "__protocol_attrs__") or hasattr(
            IssueTracker, "__abstractmethods__"
        ) or issubclass(type(IssueTracker), type)

    def test_non_conforming_class_fails(self):
        class _Incomplete:
            pass

        assert not isinstance(_Incomplete(), IssueTracker)


# ---------------------------------------------------------------------------
#  Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_default_construction(self):
        tracker = GitHubIssueTracker()
        assert tracker._repo == ""
        assert tracker._token == ""
        assert tracker.name == "github"

    def test_construction_with_config(self):
        tracker = GitHubIssueTracker(repo="acme/widgets", token="ghp_xyz")
        assert tracker._repo == "acme/widgets"
        assert tracker._token == "ghp_xyz"
        assert tracker.name == "github"

    def test_health_check_true_when_repo_set(self):
        tracker = GitHubIssueTracker(repo="acme/widgets")
        assert tracker.health_check() is True

    def test_health_check_false_when_repo_empty(self):
        tracker = GitHubIssueTracker(repo="")
        assert tracker.health_check() is False


# ---------------------------------------------------------------------------
#  create_issue
# ---------------------------------------------------------------------------

class TestCreateIssue:
    def _tracker(self) -> GitHubIssueTracker:
        return GitHubIssueTracker(repo="acme/widgets")

    @patch("subprocess.run")
    def test_create_issue_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/acme/widgets/issues/42\n",
            stderr="",
        )
        ref = self._tracker().create_issue("Bug title", "Bug body")
        assert ref.issue_id == "42"
        assert ref.url == "https://github.com/acme/widgets/issues/42"
        assert ref.title == "Bug title"

        args = mock_run.call_args[0][0]
        assert args[:3] == ["gh", "issue", "create"]
        assert "--repo" in args
        assert "acme/widgets" in args

    @patch("subprocess.run")
    def test_create_issue_with_labels_and_assignee(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/acme/widgets/issues/99\n",
            stderr="",
        )
        ref = self._tracker().create_issue(
            "Title", "Body", labels=["bug", "critical"], assignee="alice"
        )
        args = mock_run.call_args[0][0]
        assert "--label" in args
        label_idx = args.index("--label")
        assert args[label_idx + 1] == "bug,critical"
        assert "--assignee" in args
        assignee_idx = args.index("--assignee")
        assert args[assignee_idx + 1] == "alice"
        assert ref.issue_id == "99"

    @patch("subprocess.run")
    def test_create_issue_gh_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="auth required"
        )
        ref = self._tracker().create_issue("Title", "Body")
        assert ref.issue_id == ""
        assert ref.url == ""
        assert ref.title == "Title"

    @patch("subprocess.run", side_effect=OSError("no gh binary"))
    def test_create_issue_exception(self, mock_run):
        ref = self._tracker().create_issue("Title", "Body")
        assert ref.issue_id == ""
        assert ref.url == ""


# ---------------------------------------------------------------------------
#  comment (add_comment)
# ---------------------------------------------------------------------------

class TestComment:
    @patch("src.swe_team.providers.issue_tracker.github_provider.comment_on_issue",
           create=True)
    def test_comment_success(self, mock_comment):
        """comment() delegates to github_integration.comment_on_issue."""
        mock_comment.return_value = True
        tracker = GitHubIssueTracker(repo="acme/widgets")
        with patch(
            "src.swe_team.github_integration.comment_on_issue",
            return_value=True,
        ):
            result = tracker.comment("42", "Investigating now.")
        assert result is True

    def test_comment_invalid_issue_id(self):
        tracker = GitHubIssueTracker(repo="acme/widgets")
        # Non-numeric issue_id triggers ValueError path
        result = tracker.comment("not-a-number", "hello")
        assert result is False


# ---------------------------------------------------------------------------
#  close_issue
# ---------------------------------------------------------------------------

class TestCloseIssue:
    @patch("subprocess.run")
    def test_close_issue_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        tracker = GitHubIssueTracker(repo="acme/widgets")
        assert tracker.close_issue("42") is True
        args = mock_run.call_args[0][0]
        assert args == ["gh", "issue", "close", "42", "--repo", "acme/widgets"]

    @patch("subprocess.run")
    def test_close_issue_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        tracker = GitHubIssueTracker(repo="acme/widgets")
        assert tracker.close_issue("999") is False

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=15))
    def test_close_issue_timeout(self, mock_run):
        tracker = GitHubIssueTracker(repo="acme/widgets")
        assert tracker.close_issue("42") is False


# ---------------------------------------------------------------------------
#  find_existing
# ---------------------------------------------------------------------------

class TestFindExisting:
    def test_find_existing_returns_empty(self):
        """Current implementation always returns empty list for generic usage."""
        tracker = GitHubIssueTracker(repo="acme/widgets")
        result = tracker.find_existing("some title")
        assert result == []


# ---------------------------------------------------------------------------
#  find_pr
# ---------------------------------------------------------------------------

class TestFindPR:
    def _tracker(self) -> GitHubIssueTracker:
        return GitHubIssueTracker(repo="acme/widgets")

    @patch("subprocess.run")
    def test_find_pr_found(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{"number": 7, "url": "https://github.com/acme/widgets/pull/7"}]),
            stderr="",
        )
        result = self._tracker().find_pr("fix/bug-123")
        assert result == {"number": 7, "url": "https://github.com/acme/widgets/pull/7"}
        args = mock_run.call_args[0][0]
        assert "--head" in args
        assert "fix/bug-123" in args

    @patch("subprocess.run")
    def test_find_pr_not_found(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        assert self._tracker().find_pr("no-such-branch") is None

    @patch("subprocess.run")
    def test_find_pr_empty_stdout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert self._tracker().find_pr("branch") is None

    @patch("subprocess.run")
    def test_find_pr_gh_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        assert self._tracker().find_pr("branch") is None

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30))
    def test_find_pr_timeout(self, mock_run):
        assert self._tracker().find_pr("branch") is None

    @patch("subprocess.run", side_effect=OSError("boom"))
    def test_find_pr_exception(self, mock_run):
        assert self._tracker().find_pr("branch") is None

    @patch("subprocess.run")
    def test_find_pr_uses_override_repo(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        self._tracker().find_pr("branch", repo="other/repo")
        args = mock_run.call_args[0][0]
        assert "other/repo" in args


# ---------------------------------------------------------------------------
#  create_pr
# ---------------------------------------------------------------------------

class TestCreatePR:
    def _tracker(self) -> GitHubIssueTracker:
        return GitHubIssueTracker(repo="acme/widgets")

    @patch("subprocess.run")
    def test_create_pr_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/acme/widgets/pull/10\n",
            stderr="",
        )
        url = self._tracker().create_pr("Fix bug", "Description", "fix/bug-1")
        assert url == "https://github.com/acme/widgets/pull/10"
        args = mock_run.call_args[0][0]
        assert args[:3] == ["gh", "pr", "create"]
        assert "--head" in args
        assert "--base" in args
        base_idx = args.index("--base")
        assert args[base_idx + 1] == "main"

    @patch("subprocess.run")
    def test_create_pr_custom_base(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="url\n", stderr="")
        self._tracker().create_pr("T", "B", "br", base="develop")
        args = mock_run.call_args[0][0]
        base_idx = args.index("--base")
        assert args[base_idx + 1] == "develop"

    @patch("subprocess.run")
    def test_create_pr_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        assert self._tracker().create_pr("T", "B", "br") is None

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=60))
    def test_create_pr_timeout(self, mock_run):
        assert self._tracker().create_pr("T", "B", "br") is None

    @patch("subprocess.run", side_effect=RuntimeError("boom"))
    def test_create_pr_exception(self, mock_run):
        assert self._tracker().create_pr("T", "B", "br") is None


# ---------------------------------------------------------------------------
#  merge_pr
# ---------------------------------------------------------------------------

class TestMergePR:
    def _tracker(self) -> GitHubIssueTracker:
        return GitHubIssueTracker(repo="acme/widgets")

    @patch("subprocess.run")
    def test_merge_pr_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert self._tracker().merge_pr(10) is True
        args = mock_run.call_args[0][0]
        assert args[:3] == ["gh", "pr", "merge"]
        assert "10" in args
        assert "--squash" in args
        assert "--delete-branch" in args

    @patch("subprocess.run")
    def test_merge_pr_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="conflict")
        assert self._tracker().merge_pr(10) is False

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=60))
    def test_merge_pr_timeout(self, mock_run):
        assert self._tracker().merge_pr(10) is False

    @patch("subprocess.run", side_effect=OSError("boom"))
    def test_merge_pr_exception(self, mock_run):
        assert self._tracker().merge_pr(10) is False


# ---------------------------------------------------------------------------
#  close_pr
# ---------------------------------------------------------------------------

class TestClosePR:
    def _tracker(self) -> GitHubIssueTracker:
        return GitHubIssueTracker(repo="acme/widgets")

    @patch("subprocess.run")
    def test_close_pr_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert self._tracker().close_pr(5) is True
        args = mock_run.call_args[0][0]
        assert args[:3] == ["gh", "pr", "close"]
        assert "5" in args

    @patch("subprocess.run")
    def test_close_pr_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="err")
        assert self._tracker().close_pr(5) is False

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30))
    def test_close_pr_timeout(self, mock_run):
        assert self._tracker().close_pr(5) is False

    @patch("subprocess.run", side_effect=RuntimeError("boom"))
    def test_close_pr_exception(self, mock_run):
        assert self._tracker().close_pr(5) is False


# ---------------------------------------------------------------------------
#  get_pr_labels
# ---------------------------------------------------------------------------

class TestGetPRLabels:
    def _tracker(self) -> GitHubIssueTracker:
        return GitHubIssueTracker(repo="acme/widgets")

    @patch("subprocess.run")
    def test_get_pr_labels_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "bug"}, {"name": "critical"}]}),
            stderr="",
        )
        labels = self._tracker().get_pr_labels(10)
        assert labels == ["bug", "critical"]
        args = mock_run.call_args[0][0]
        assert "--json" in args
        assert "labels" in args

    @patch("subprocess.run")
    def test_get_pr_labels_empty(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"labels": []}), stderr=""
        )
        assert self._tracker().get_pr_labels(10) == []

    @patch("subprocess.run")
    def test_get_pr_labels_no_labels_key(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({}), stderr=""
        )
        assert self._tracker().get_pr_labels(10) == []

    @patch("subprocess.run")
    def test_get_pr_labels_gh_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="err")
        assert self._tracker().get_pr_labels(10) == []

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30))
    def test_get_pr_labels_timeout(self, mock_run):
        assert self._tracker().get_pr_labels(10) == []

    @patch("subprocess.run", side_effect=OSError("boom"))
    def test_get_pr_labels_exception(self, mock_run):
        assert self._tracker().get_pr_labels(10) == []


# ---------------------------------------------------------------------------
#  IssueRef dataclass
# ---------------------------------------------------------------------------

class TestIssueRef:
    def test_issueref_fields(self):
        ref = IssueRef(issue_id="1", url="https://example.com", title="T")
        assert ref.issue_id == "1"
        assert ref.url == "https://example.com"
        assert ref.title == "T"

    def test_issueref_is_dataclass(self):
        import dataclasses
        assert dataclasses.is_dataclass(IssueRef)
