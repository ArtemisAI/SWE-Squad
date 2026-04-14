"""Unit tests for src/swe_team/code_reviewer.py.

Covers:
- CodeReviewerAgent.review() — APPROVE and REQUEST_CHANGES paths
- Missing branch returns (False, "no branch recorded")
- _parse_response() — APPROVE, REQUEST_CHANGES, None, empty, unparseable
- _push_branch() — success, failure, timeout
- _find_existing_pr() — found, not found, timeout
- _create_pr() — success with URL parsing, failure
- _get_diff() — success, truncation, failure
- _handle_approve() — dry_run skips subprocess, ticket transitions to VERIFYING
- _handle_request_changes() — increments rejections, bounces to IN_DEVELOPMENT,
  escalates to HITL at max_rejections
- _store_save helper

Note: code_reviewer.py imports check_permission locally inside _merge_pr() via
`from src.swe_team.agent_rbac import check_permission`, so we patch at
src.swe_team.agent_rbac.check_permission.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.code_reviewer import CodeReviewerAgent, _store_save

# Patch target for check_permission (imported locally inside _merge_pr)
_RBAC_PATCH = "src.swe_team.agent_rbac.check_permission"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ticket(
    ticket_id: str = "t-cr-test",
    severity: TicketSeverity = TicketSeverity.MEDIUM,
    status: TicketStatus = TicketStatus.IN_REVIEW,
    branch: str = "fix/branch-abc",
    **kwargs,
) -> SWETicket:
    defaults = dict(
        ticket_id=ticket_id,
        title="Code review test",
        description="A bug that was fixed",
        severity=severity,
        status=status,
        investigation_report="Root cause: something. " * 12,
        metadata={
            "branch": branch,
            "repo": "your-org/SWE-Squad",
            "resolution_note": "fix_succeeded",
        },
    )
    defaults.update(kwargs)
    return SWETicket(**defaults)


def _reviewer(**kwargs) -> CodeReviewerAgent:
    defaults = dict(model="sonnet", diff_char_limit=6000, max_rejections=3)
    defaults.update(kwargs)
    return CodeReviewerAgent(**defaults)


def _store() -> MagicMock:
    store = MagicMock()
    store.save = MagicMock()
    return store


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# _parse_response (static method)
# ---------------------------------------------------------------------------

class TestParseResponse:
    def test_approve_returns_true(self):
        approved, reason = CodeReviewerAgent._parse_response("APPROVE\nLooks great.")
        assert approved is True
        assert "Looks great." in reason

    def test_approve_case_insensitive(self):
        approved, _ = CodeReviewerAgent._parse_response("approve\nFine.")
        assert approved is True

    def test_request_changes_returns_false(self):
        approved, reason = CodeReviewerAgent._parse_response(
            "REQUEST_CHANGES\nNeeds more tests."
        )
        assert approved is False
        assert "Needs more tests." in reason

    def test_none_response_defaults_to_reject(self):
        approved, reason = CodeReviewerAgent._parse_response(None)
        assert approved is False
        assert "REJECT" in reason

    def test_empty_string_defaults_to_reject(self):
        approved, reason = CodeReviewerAgent._parse_response("")
        assert approved is False
        assert "REJECT" in reason

    def test_unparseable_first_line_defaults_to_reject(self):
        approved, reason = CodeReviewerAgent._parse_response("MAYBE\nI don't know.")
        assert approved is False
        assert "REJECT" in reason

    def test_reasoning_captured(self):
        _, reason = CodeReviewerAgent._parse_response("APPROVE\nLine 2\nLine 3")
        assert "Line 2" in reason
        assert "Line 3" in reason


# ---------------------------------------------------------------------------
# _push_branch
# ---------------------------------------------------------------------------

class TestPushBranch:
    def test_success_returns_true(self):
        reviewer = _reviewer()
        with patch("subprocess.run", return_value=_proc(0)) as mock_run:
            result = reviewer._push_branch("fix/branch", "/repo")
        assert result is True
        mock_run.assert_called_once()

    def test_nonzero_returncode_returns_false(self):
        reviewer = _reviewer()
        with patch("subprocess.run", return_value=_proc(1, stderr="rejected")):
            result = reviewer._push_branch("fix/branch", "/repo")
        assert result is False

    def test_timeout_returns_false(self):
        reviewer = _reviewer()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 60)):
            result = reviewer._push_branch("fix/branch", "/repo")
        assert result is False

    def test_exception_returns_false(self):
        reviewer = _reviewer()
        with patch("subprocess.run", side_effect=OSError("no git")):
            result = reviewer._push_branch("fix/branch", "/repo")
        assert result is False


# ---------------------------------------------------------------------------
# _find_existing_pr
# ---------------------------------------------------------------------------

class TestFindExistingPr:
    def test_found_returns_pr_number(self):
        reviewer = _reviewer()
        with patch("subprocess.run",
                   return_value=_proc(0, stdout='[{"number": 42}]')):
            result = reviewer._find_existing_pr("fix/branch", "owner/repo")
        assert result == 42

    def test_empty_list_returns_none(self):
        reviewer = _reviewer()
        with patch("subprocess.run", return_value=_proc(0, stdout="[]")):
            result = reviewer._find_existing_pr("fix/branch", "owner/repo")
        assert result is None

    def test_nonzero_returncode_returns_none(self):
        reviewer = _reviewer()
        with patch("subprocess.run", return_value=_proc(1)):
            result = reviewer._find_existing_pr("fix/branch", "owner/repo")
        assert result is None

    def test_timeout_returns_none(self):
        reviewer = _reviewer()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 30)):
            result = reviewer._find_existing_pr("fix/branch", "owner/repo")
        assert result is None


# ---------------------------------------------------------------------------
# _create_pr
# ---------------------------------------------------------------------------

class TestCreatePr:
    def test_success_parses_pr_number(self):
        reviewer = _reviewer()
        ticket = _ticket()
        url = "https://github.com/your-org/SWE-Squad/pull/99"
        with patch("subprocess.run", return_value=_proc(0, stdout=url)):
            result = reviewer._create_pr("fix/branch", "owner/repo", ticket)
        assert result == 99

    def test_unparseable_url_returns_none(self):
        reviewer = _reviewer()
        ticket = _ticket()
        with patch("subprocess.run", return_value=_proc(0, stdout="not-a-url")):
            result = reviewer._create_pr("fix/branch", "owner/repo", ticket)
        assert result is None

    def test_nonzero_returncode_returns_none(self):
        reviewer = _reviewer()
        ticket = _ticket()
        with patch("subprocess.run", return_value=_proc(1, stderr="auth error")):
            result = reviewer._create_pr("fix/branch", "owner/repo", ticket)
        assert result is None

    def test_timeout_returns_none(self):
        reviewer = _reviewer()
        ticket = _ticket()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 60)):
            result = reviewer._create_pr("fix/branch", "owner/repo", ticket)
        assert result is None


# ---------------------------------------------------------------------------
# _get_diff
# ---------------------------------------------------------------------------

class TestGetDiff:
    def test_returns_diff_on_success(self):
        reviewer = _reviewer()
        with patch("subprocess.run", return_value=_proc(0, stdout="--- a\n+++ b\n")):
            diff = reviewer._get_diff("fix/branch", "/repo")
        assert "--- a" in diff

    def test_truncates_long_diff(self):
        reviewer = _reviewer(diff_char_limit=50)
        long_diff = "x" * 200
        with patch("subprocess.run", return_value=_proc(0, stdout=long_diff)):
            diff = reviewer._get_diff("fix/branch", "/repo")
        assert len(diff) < len(long_diff)
        assert "truncated" in diff

    def test_failure_returns_unavailable(self):
        reviewer = _reviewer()
        with patch("subprocess.run", return_value=_proc(1, stderr="error")):
            diff = reviewer._get_diff("fix/branch", "/repo")
        assert diff == "(diff unavailable)"

    def test_timeout_returns_unavailable(self):
        reviewer = _reviewer()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)):
            diff = reviewer._get_diff("fix/branch", "/repo")
        assert diff == "(diff unavailable)"


# ---------------------------------------------------------------------------
# review() — top-level integration of internal methods
# ---------------------------------------------------------------------------

class TestReview:
    def test_no_branch_returns_false(self):
        reviewer = _reviewer()
        ticket = _ticket(branch="")
        ticket.metadata.pop("branch", None)
        store = _store()

        approved, feedback = reviewer.review(ticket, store, repo_root="/repo")

        assert approved is False
        assert "no branch" in feedback

    def test_approved_path_returns_true(self):
        reviewer = _reviewer()
        ticket = _ticket()
        store = _store()

        with patch.object(reviewer, "_push_branch", return_value=True), \
             patch.object(reviewer, "_ensure_pr", return_value=10), \
             patch.object(reviewer, "_get_diff", return_value="diff content"), \
             patch.object(reviewer, "_call_claude",
                          return_value="APPROVE\nCode looks correct."), \
             patch.object(reviewer, "_merge_pr", return_value=True), \
             patch(_RBAC_PATCH, return_value=(True, "allowed")), \
             patch("subprocess.run", return_value=_proc(0)):
            approved, feedback = reviewer.review(ticket, store, repo_root="/repo")

        assert approved is True
        assert "approved" in feedback

    def test_rejected_path_returns_false(self):
        reviewer = _reviewer()
        ticket = _ticket()
        store = _store()

        with patch.object(reviewer, "_push_branch", return_value=True), \
             patch.object(reviewer, "_ensure_pr", return_value=5), \
             patch.object(reviewer, "_get_diff", return_value="diff content"), \
             patch.object(reviewer, "_call_claude",
                          return_value="REQUEST_CHANGES\nFix the bug first."), \
             patch("subprocess.run", return_value=_proc(0)):
            approved, feedback = reviewer.review(ticket, store, repo_root="/repo")

        assert approved is False
        assert "rejected" in feedback

    def test_dry_run_does_not_persist(self):
        reviewer = _reviewer()
        ticket = _ticket()
        store = _store()

        with patch.object(reviewer, "_push_branch", return_value=True), \
             patch.object(reviewer, "_ensure_pr", return_value=7), \
             patch.object(reviewer, "_get_diff", return_value="diff"), \
             patch.object(reviewer, "_call_claude",
                          return_value="APPROVE\nAll good."):
            approved, feedback = reviewer.review(
                ticket, store, repo_root="/repo", dry_run=True
            )

        assert approved is True
        store.save.assert_not_called()

    def test_claude_timeout_defaults_to_reject(self):
        reviewer = _reviewer()
        ticket = _ticket()
        store = _store()

        with patch.object(reviewer, "_push_branch", return_value=False), \
             patch.object(reviewer, "_get_diff", return_value="diff"), \
             patch.object(reviewer, "_call_claude", return_value=None), \
             patch("subprocess.run", return_value=_proc(0)):
            approved, feedback = reviewer.review(ticket, store, repo_root="/repo")

        assert approved is False

    def test_push_fails_blocks_resolution_via_pr_gate(self):
        """When push fails, _ensure_pr is skipped and the PR gate blocks resolution.

        Previously the reviewer would approve even without a PR; now the
        PR-verification gate (issue #367) catches this and returns the ticket
        to IN_DEVELOPMENT with needs_pr=True.
        """
        reviewer = _reviewer()
        ticket = _ticket()
        # No PR metadata — simulates a push-failed scenario
        ticket.metadata.pop("pr_url", None)
        ticket.metadata.pop("pr_number", None)
        store = _store()

        with patch.object(reviewer, "_push_branch", return_value=False), \
             patch.object(reviewer, "_get_diff", return_value="diff content"), \
             patch.object(reviewer, "_call_claude",
                          return_value="APPROVE\nLooks good."), \
             patch("subprocess.run", return_value=_proc(0)):
            approved, feedback = reviewer.review(ticket, store, repo_root="/repo")

        # push failed → no PR → gate blocks resolution
        assert approved is False
        assert "pr_gate" in feedback
        assert ticket.metadata.get("needs_pr") is True
        assert ticket.status == TicketStatus.IN_DEVELOPMENT


# ---------------------------------------------------------------------------
# _handle_request_changes — HITL escalation
# ---------------------------------------------------------------------------

class TestHandleRequestChanges:
    def test_increments_review_rejections(self):
        reviewer = _reviewer(max_rejections=3)
        ticket = _ticket()
        ticket.metadata["review_rejections"] = 1
        store = _store()

        reviewer._handle_request_changes(
            ticket, store, repo="", pr_number=None,
            reasoning="bad code", dry_run=False,
        )

        assert ticket.metadata["review_rejections"] == 2

    def test_bounces_to_in_development_below_max(self):
        reviewer = _reviewer(max_rejections=3)
        ticket = _ticket()
        ticket.metadata["review_rejections"] = 0
        store = _store()

        reviewer._handle_request_changes(
            ticket, store, repo="", pr_number=None,
            reasoning="needs work", dry_run=False,
        )

        assert ticket.status == TicketStatus.IN_DEVELOPMENT

    def test_sets_needs_hitl_at_max_rejections(self):
        reviewer = _reviewer(max_rejections=2)
        ticket = _ticket()
        ticket.metadata["review_rejections"] = 1  # will become 2 = max
        store = _store()

        approved, feedback = reviewer._handle_request_changes(
            ticket, store, repo="", pr_number=None,
            reasoning="still broken", dry_run=False,
        )

        assert approved is False
        assert ticket.metadata.get("needs_hitl") is True
        assert "hitl" in feedback

    def test_dry_run_does_not_mutate_ticket(self):
        reviewer = _reviewer(max_rejections=3)
        ticket = _ticket()
        original_status = ticket.status
        store = _store()

        reviewer._handle_request_changes(
            ticket, store, repo="", pr_number=None,
            reasoning="dry", dry_run=True,
        )

        assert ticket.status == original_status
        store.save.assert_not_called()

    def test_request_changes_does_not_auto_close_pr(self):
        reviewer = _reviewer(max_rejections=3)
        reviewer._close_pr = MagicMock()
        ticket = _ticket()
        store = _store()

        reviewer._handle_request_changes(
            ticket, store, repo="your-org/SWE-Squad", pr_number=565,
            reasoning="needs manual follow-up", dry_run=False,
        )

        reviewer._close_pr.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_approve
# ---------------------------------------------------------------------------

class TestHandleApprove:
    def test_transitions_ticket_to_verifying(self):
        """RBAC enforcement: _handle_approve no longer merges; it marks pr_approved
        and awaiting_merge, staying in IN_REVIEW until the SWE-Manager merges."""
        reviewer = _reviewer()
        ticket = _ticket()
        store = _store()

        approved, feedback = reviewer._handle_approve(
            ticket, store, repo="owner/repo", pr_number=10,
            reasoning="all good", dry_run=False,
        )

        assert approved is True
        assert ticket.status == TicketStatus.IN_REVIEW  # stays in review; merge is separate
        assert ticket.metadata.get("pr_approved") is True
        assert ticket.metadata.get("awaiting_merge") is True
        assert "approved" in feedback

    def test_dry_run_does_not_transition(self):
        reviewer = _reviewer()
        ticket = _ticket()
        store = _store()

        approved, _ = reviewer._handle_approve(
            ticket, store, repo="owner/repo", pr_number=10,
            reasoning="all good", dry_run=True,
        )

        assert approved is True
        assert ticket.status == TicketStatus.IN_REVIEW  # unchanged
        store.save.assert_not_called()

    def test_rbac_blocked_skips_merge(self):
        reviewer = _reviewer()
        ticket = _ticket()
        store = _store()

        merge_called = [False]

        def _fake_run(cmd, **kw):
            if isinstance(cmd, list) and "merge" in cmd:
                merge_called[0] = True
            return _proc(0)

        with patch(_RBAC_PATCH, return_value=(False, "not allowed")), \
             patch("subprocess.run", side_effect=_fake_run):
            reviewer._handle_approve(
                ticket, store, repo="owner/repo", pr_number=10,
                reasoning="ok", dry_run=False,
            )

        assert not merge_called[0], "gh pr merge should not be called when RBAC blocks"


# ---------------------------------------------------------------------------
# _store_save helper
# ---------------------------------------------------------------------------

class TestStoreSaveHelper:
    def test_uses_save_method(self):
        ticket = _ticket()
        store = MagicMock(spec=["save"])
        _store_save(store, ticket)
        store.save.assert_called_once_with(ticket)

    def test_falls_back_to_add(self):
        ticket = _ticket()
        store = MagicMock(spec=["add"])
        _store_save(store, ticket)
        store.add.assert_called_once_with(ticket)

    def test_no_save_no_add_does_not_raise(self):
        ticket = _ticket()
        store = MagicMock(spec=[])
        _store_save(store, ticket)  # must not raise


# ---------------------------------------------------------------------------
# CodingEngine injection
# ---------------------------------------------------------------------------

class TestCodingEngineInjection:
    def test_default_engine_construction(self):
        """When no engine is passed, a ClaudeCodeEngine is created."""
        with patch(
            "src.swe_team.code_reviewer.ClaudeCodeEngine",
            create=True,
        ):
            reviewer = _reviewer()
        assert reviewer._engine is not None

    def test_injected_engine_is_used(self):
        """Pass a mock engine; _call_claude() delegates to engine.run()."""
        from src.swe_team.providers.coding_engine.base import EngineResult

        mock_engine = MagicMock()
        mock_engine.run.return_value = EngineResult(
            stdout="APPROVE\nLooks good.", stderr="", returncode=0,
        )
        reviewer = _reviewer(engine=mock_engine)

        result = reviewer._call_claude("review this")

        mock_engine.run.assert_called_once()
        assert result == "APPROVE\nLooks good."

    def test_engine_failure_returns_none(self):
        """When engine.run() returns success=False, _call_claude() returns None."""
        from src.swe_team.providers.coding_engine.base import EngineResult

        mock_engine = MagicMock()
        mock_engine.run.return_value = EngineResult(
            stdout="", stderr="model error", returncode=1,
        )
        reviewer = _reviewer(engine=mock_engine)

        result = reviewer._call_claude("review this")

        assert result is None

    def test_engine_exception_handled(self):
        """When engine.run() raises, _call_claude() returns None (no crash)."""
        mock_engine = MagicMock()
        mock_engine.run.side_effect = Exception("connection refused")
        reviewer = _reviewer(engine=mock_engine)

        result = reviewer._call_claude("review this")

        assert result is None

    def test_engine_timeout_returns_none(self):
        """When engine.run() returns timeout error_type, _call_claude() returns None."""
        from src.swe_team.providers.coding_engine.base import EngineResult

        mock_engine = MagicMock()
        mock_engine.run.return_value = EngineResult(
            stdout="", stderr="timeout after 300s", returncode=124,
            metadata={"error_type": "timeout"},
        )
        reviewer = _reviewer(engine=mock_engine)

        result = reviewer._call_claude("review this")

        assert result is None
        # Verify the call included raise_on_timeout=False and timeout=300
        call_args = mock_engine.run.call_args
        assert call_args.kwargs.get("raise_on_timeout") is False
        assert call_args.kwargs.get("timeout") == 300


# ---------------------------------------------------------------------------
# IssueTracker injection
# ---------------------------------------------------------------------------

class TestIssueTrackerInjection:
    def test_fallback_to_subprocess_when_no_tracker(self):
        """When issue_tracker=None, _close_github_issue() falls back to subprocess."""
        reviewer = _reviewer(engine=MagicMock())
        assert reviewer._issue_tracker is None

        with patch("subprocess.run", return_value=_proc(0)) as mock_run:
            reviewer._close_github_issue(42, 10, "owner/repo")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "gh" in cmd
        assert "issue" in cmd
        assert "close" in cmd

    def test_issue_tracker_used_when_available(self):
        """Pass mock issue_tracker; _close_github_issue() calls tracker methods."""
        mock_tracker = MagicMock()
        reviewer = _reviewer(engine=MagicMock(), issue_tracker=mock_tracker)

        reviewer._close_github_issue(42, 10, "owner/repo")

        mock_tracker.comment.assert_called_once_with("42", "Fixed in PR #10")
        mock_tracker.close_issue.assert_called_once_with("42")

    def test_issue_tracker_exception_falls_back_to_subprocess(self):
        """When issue_tracker raises, _close_github_issue() falls back to subprocess."""
        mock_tracker = MagicMock()
        mock_tracker.comment.side_effect = Exception("API down")
        reviewer = _reviewer(engine=MagicMock(), issue_tracker=mock_tracker)

        with patch("subprocess.run", return_value=_proc(0)) as mock_run:
            reviewer._close_github_issue(42, 10, "owner/repo")

        # Tracker was attempted
        mock_tracker.comment.assert_called_once()
        # Fell back to subprocess
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# PR Verification Gate (issue #367)
# ---------------------------------------------------------------------------

class TestPRVerificationGate:
    """Ticket resolution is BLOCKED without a PR; resolves normally with one."""

    # -- _handle_approve blocks when no PR present ---------------------------

    def test_handle_approve_blocked_without_pr(self):
        """_handle_approve returns False and sets needs_pr when pr_number is None
        and ticket.metadata has no pr_url/pr_number."""
        reviewer = _reviewer()
        ticket = _ticket()
        # Remove any residual pr metadata
        ticket.metadata.pop("pr_url", None)
        ticket.metadata.pop("pr_number", None)
        store = _store()

        approved, feedback = reviewer._handle_approve(
            ticket, store, repo="owner/repo", pr_number=None,
            reasoning="looks good", dry_run=False,
        )

        assert approved is False
        assert "pr_gate" in feedback
        assert ticket.metadata.get("needs_pr") is True
        assert ticket.status == TicketStatus.IN_DEVELOPMENT

    def test_handle_approve_blocked_persists_ticket(self):
        """When blocked by PR gate, ticket is persisted via store."""
        reviewer = _reviewer()
        ticket = _ticket()
        ticket.metadata.pop("pr_url", None)
        ticket.metadata.pop("pr_number", None)
        store = _store()

        reviewer._handle_approve(
            ticket, store, repo="owner/repo", pr_number=None,
            reasoning="ok", dry_run=False,
        )

        store.save.assert_called_once_with(ticket)

    def test_handle_approve_verifies_with_pr_number(self):
        """When pr_number is provided, RBAC enforcement marks pr_approved/awaiting_merge
        without merging. Ticket stays in IN_REVIEW awaiting authorized merge."""
        reviewer = _reviewer()
        ticket = _ticket()
        store = _store()

        approved, feedback = reviewer._handle_approve(
            ticket, store, repo="owner/repo", pr_number=42,
            reasoning="all good", dry_run=False,
        )

        assert approved is True
        assert ticket.status == TicketStatus.IN_REVIEW
        assert ticket.metadata.get("pr_approved") is True
        assert ticket.metadata.get("awaiting_merge") is True

    def test_handle_approve_blocks_resolution_when_merge_fails(self):
        """RBAC enforcement: _handle_approve never calls _merge_pr.
        The ticket is approved (pr_approved=True) and stays in IN_REVIEW
        awaiting an authorized merge by SWE-Manager or human."""
        reviewer = _reviewer()
        ticket = _ticket()
        store = _store()

        approved, feedback = reviewer._handle_approve(
            ticket, store, repo="owner/repo", pr_number=42,
            reasoning="all good", dry_run=False,
        )

        assert approved is True  # RBAC: approval succeeds; merge is deferred
        assert "approved" in feedback
        assert ticket.status == TicketStatus.IN_REVIEW
        assert ticket.metadata.get("pr_approved") is True
        assert ticket.metadata.get("awaiting_merge") is True
        assert "merged_at" not in ticket.metadata.get("pr_lifecycle", {})

    def test_handle_approve_verifies_with_pr_url_in_metadata(self):
        """When pr_number=None but pr_url is in metadata, the PR gate passes
        (has_pr=True). RBAC enforcement returns approved=True with the ticket
        staying in IN_REVIEW awaiting an authorized merge."""
        reviewer = _reviewer()
        ticket = _ticket()
        ticket.metadata["pr_url"] = "https://github.com/owner/repo/pull/7"
        ticket.metadata.pop("pr_number", None)
        store = _store()

        approved, feedback = reviewer._handle_approve(
            ticket, store, repo="owner/repo", pr_number=None,
            reasoning="looks great", dry_run=False,
        )

        assert approved is True
        assert ticket.status == TicketStatus.IN_REVIEW
        assert "approved" in feedback

    def test_handle_approve_verifies_with_pr_number_in_metadata(self):
        """When pr_number arg is None but metadata has it, RBAC enforcement marks
        pr_approved/awaiting_merge. Ticket stays in IN_REVIEW (no auto-merge)."""
        reviewer = _reviewer()
        ticket = _ticket()
        ticket.metadata["pr_number"] = 55
        ticket.metadata.pop("pr_url", None)
        store = _store()

        approved, feedback = reviewer._handle_approve(
            ticket, store, repo="owner/repo", pr_number=None,
            reasoning="ship it", dry_run=False,
        )

        assert approved is True
        assert ticket.status == TicketStatus.IN_REVIEW
        assert ticket.metadata.get("pr_approved") is True
        assert ticket.metadata.get("awaiting_merge") is True

    # -- _create_pr stores metadata ------------------------------------------

    def test_create_pr_stores_pr_url_and_number(self):
        """Successful _create_pr() writes pr_url and pr_number to ticket.metadata."""
        reviewer = _reviewer()
        ticket = _ticket()
        url = "https://github.com/owner/repo/pull/123"
        with patch("subprocess.run", return_value=_proc(0, stdout=url)):
            pr_num = reviewer._create_pr("fix/branch", "owner/repo", ticket)

        assert pr_num == 123
        assert ticket.metadata["pr_url"] == url
        assert ticket.metadata["pr_number"] == 123
        assert datetime.fromisoformat(ticket.metadata["pr_lifecycle"]["pr_created_at"])

    def test_review_tracks_first_review_and_cycles(self):
        reviewer = _reviewer()
        ticket = _ticket()
        store = _store()

        with patch.object(reviewer, "_push_branch", return_value=True), \
             patch.object(reviewer, "_ensure_pr", return_value=5), \
             patch.object(reviewer, "_get_diff", return_value="diff content"), \
             patch.object(reviewer, "_call_claude",
                          return_value="REQUEST_CHANGES\nFix the bug first."), \
             patch("subprocess.run", return_value=_proc(0)):
            reviewer.review(ticket, store, repo_root="/repo")
            reviewer.review(ticket, store, repo_root="/repo")

        lifecycle = ticket.metadata["pr_lifecycle"]
        assert lifecycle["review_decision"] == "changes_requested"
        assert lifecycle["review_cycles"] == 2
        assert datetime.fromisoformat(lifecycle["first_review_at"])

    def test_create_pr_failure_does_not_store_metadata(self):
        """Failed PR creation must not store pr_url/pr_number."""
        reviewer = _reviewer()
        ticket = _ticket()
        # Ensure clean metadata
        ticket.metadata.pop("pr_url", None)
        ticket.metadata.pop("pr_number", None)

        with patch("subprocess.run", return_value=_proc(1, stderr="auth error")):
            pr_num = reviewer._create_pr("fix/branch", "owner/repo", ticket)

        assert pr_num is None
        assert "pr_url" not in ticket.metadata
        assert "pr_number" not in ticket.metadata

    def test_create_pr_timeout_does_not_store_metadata(self):
        """Timeout during PR creation must not leave stale pr metadata."""
        reviewer = _reviewer()
        ticket = _ticket()
        ticket.metadata.pop("pr_url", None)
        ticket.metadata.pop("pr_number", None)

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 60)):
            pr_num = reviewer._create_pr("fix/branch", "owner/repo", ticket)

        assert pr_num is None
        assert "pr_url" not in ticket.metadata
        assert "pr_number" not in ticket.metadata

    # -- _ensure_pr stores metadata on existing PR ---------------------------

    def test_ensure_pr_stores_metadata_for_existing_pr(self):
        """Reusing an existing PR populates pr_number (and pr_url) in metadata."""
        reviewer = _reviewer()
        ticket = _ticket()
        ticket.metadata.pop("pr_url", None)
        ticket.metadata.pop("pr_number", None)

        with patch.object(reviewer, "_find_existing_pr", return_value=77):
            pr_num = reviewer._ensure_pr("fix/branch", "owner/repo", ticket)

        assert pr_num == 77
        assert ticket.metadata["pr_number"] == 77
        assert "pr_url" in ticket.metadata

    # -- dry_run bypasses PR gate --------------------------------------------

    def test_handle_approve_dry_run_skips_pr_gate(self):
        """In dry_run mode the gate is not evaluated (no mutations happen at all)."""
        reviewer = _reviewer()
        ticket = _ticket()
        ticket.metadata.pop("pr_url", None)
        ticket.metadata.pop("pr_number", None)
        store = _store()

        approved, _ = reviewer._handle_approve(
            ticket, store, repo="owner/repo", pr_number=None,
            reasoning="dry", dry_run=True,
        )

        # dry_run returns True (no real gate evaluation)
        assert approved is True
        assert ticket.status == TicketStatus.IN_REVIEW  # not mutated
        store.save.assert_not_called()
