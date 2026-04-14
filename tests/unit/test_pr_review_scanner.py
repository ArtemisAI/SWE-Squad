"""Unit tests for src.swe_team.pr_review_scanner."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.pr_review_scanner import (
    PRReviewResult,
    PRReviewScanner,
    PRReviewScannerConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(repo: str = "owner/repo", enabled: bool = True) -> PRReviewScannerConfig:
    return PRReviewScannerConfig(repo=repo, enabled=enabled, max_prs_per_scan=20)


def _make_scanner(repo: str = "owner/repo", enabled: bool = True) -> PRReviewScanner:
    return PRReviewScanner(_make_config(repo=repo, enabled=enabled))


def _proc(stdout: str = "[]", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = ""
    return m


def _make_pr(
    number: int = 1,
    title: str = "Fix: ticket abc123def456",
    branch: str = "swe-fix/ticket-abc123def456",
    review_decision: str = "CHANGES_REQUESTED",
    review_nodes: list | None = None,
) -> dict:
    if review_nodes is None:
        review_nodes = [
            {
                "author": {"login": "alice"},
                "state": "CHANGES_REQUESTED",
                "body": "Please add more tests.",
            }
        ]
    return {
        "number": number,
        "title": title,
        "headRefName": branch,
        "reviewDecision": review_decision,
        "reviews": {"nodes": review_nodes},
    }


# ---------------------------------------------------------------------------
# Branch → ticket ID mapping
# ---------------------------------------------------------------------------


class TestExtractTicketId:
    """Tests for the static branch-name parser."""

    def test_standard_branch(self):
        tid = PRReviewScanner.extract_ticket_id("swe-fix/ticket-abc123def456")
        assert tid == "abc123def456"

    def test_non_swe_branch_returns_none(self):
        assert PRReviewScanner.extract_ticket_id("main") is None
        assert PRReviewScanner.extract_ticket_id("feature/my-feature") is None
        assert PRReviewScanner.extract_ticket_id("dependabot/npm") is None

    def test_empty_string_returns_none(self):
        assert PRReviewScanner.extract_ticket_id("") is None

    def test_prefix_only_returns_none(self):
        # "swe-fix/ticket-" with nothing after is invalid
        assert PRReviewScanner.extract_ticket_id("swe-fix/ticket-") is None

    def test_short_hex_ticket_id(self):
        # Non-12-char IDs fall back to whole suffix
        tid = PRReviewScanner.extract_ticket_id("swe-fix/ticket-deadbeef")
        assert tid == "deadbeef"

    def test_ticket_id_with_retry_suffix(self):
        # Branch may have an extra counter: still parse first 12 hex chars
        tid = PRReviewScanner.extract_ticket_id("swe-fix/ticket-abc123def456-retry2")
        assert tid == "abc123def456"

    def test_custom_prefix(self):
        tid = PRReviewScanner.extract_ticket_id(
            "bot-fix/ticket-aabbccddeeff", prefix="bot-fix/ticket-"
        )
        assert tid == "aabbccddeeff"


# ---------------------------------------------------------------------------
# Review comment extraction
# ---------------------------------------------------------------------------


class TestExtractReviewComments:
    def test_single_reviewer(self):
        nodes = [
            {"author": {"login": "bob"}, "state": "CHANGES_REQUESTED", "body": "Needs refactor."}
        ]
        text = PRReviewScanner._extract_review_comments(nodes)
        assert "bob" in text
        assert "Needs refactor." in text
        assert "CHANGES_REQUESTED" in text

    def test_multiple_reviewers(self):
        nodes = [
            {"author": {"login": "alice"}, "state": "CHANGES_REQUESTED", "body": "Add tests."},
            {"author": {"login": "carol"}, "state": "CHANGES_REQUESTED", "body": "Fix the typo."},
        ]
        text = PRReviewScanner._extract_review_comments(nodes)
        assert "alice" in text
        assert "carol" in text
        assert "Add tests." in text
        assert "Fix the typo." in text

    def test_empty_body_nodes_are_skipped(self):
        nodes = [
            {"author": {"login": "dave"}, "state": "APPROVED", "body": ""},
            {"author": {"login": "eve"}, "state": "CHANGES_REQUESTED", "body": "Looks good overall."},
        ]
        text = PRReviewScanner._extract_review_comments(nodes)
        assert "dave" not in text
        assert "eve" in text

    def test_empty_list(self):
        assert PRReviewScanner._extract_review_comments([]) == ""

    def test_missing_author(self):
        nodes = [{"state": "CHANGES_REQUESTED", "body": "Something."}]
        text = PRReviewScanner._extract_review_comments(nodes)
        assert "Something." in text
        assert "reviewer" in text  # falls back to "reviewer"


# ---------------------------------------------------------------------------
# PRReviewScanner.scan() — subprocess mocked
# ---------------------------------------------------------------------------


class TestPRReviewScannerScan:
    def test_disabled_returns_empty(self):
        scanner = _make_scanner(enabled=False)
        with patch("subprocess.run") as mock_run:
            result = scanner.scan()
        assert result == []
        mock_run.assert_not_called()

    def test_no_repo_returns_empty(self):
        scanner = PRReviewScanner(PRReviewScannerConfig(repo="", enabled=True))
        with patch("subprocess.run") as mock_run:
            result = scanner.scan()
        assert result == []
        mock_run.assert_not_called()

    def test_gh_failure_returns_empty(self):
        scanner = _make_scanner()
        with patch("subprocess.run", return_value=_proc("", returncode=1)):
            result = scanner.scan()
        assert result == []

    def test_no_prs_returns_empty(self):
        scanner = _make_scanner()
        with patch("subprocess.run", return_value=_proc("[]")):
            result = scanner.scan()
        assert result == []

    def test_non_swe_branch_prs_excluded(self):
        prs = [
            {
                "number": 5,
                "title": "Dependabot bump",
                "headRefName": "dependabot/npm/lodash",
                "reviewDecision": "CHANGES_REQUESTED",
                "reviews": {"nodes": []},
            }
        ]
        scanner = _make_scanner()
        with patch("subprocess.run", return_value=_proc(json.dumps(prs))):
            result = scanner.scan()
        assert result == []

    def test_swe_branch_pr_is_included(self):
        pr = _make_pr()
        scanner = _make_scanner()
        with patch("subprocess.run", return_value=_proc(json.dumps([pr]))):
            results = scanner.scan()
        assert len(results) == 1
        r = results[0]
        assert r.ticket_id == "abc123def456"
        assert r.review_decision == "CHANGES_REQUESTED"
        assert "alice" in r.review_comments
        assert r.pr_number == 1

    def test_approved_pr_is_included_in_scan_but_not_scan_changes_requested(self):
        pr = _make_pr(review_decision="APPROVED", review_nodes=[])
        scanner = _make_scanner()
        with patch("subprocess.run", return_value=_proc(json.dumps([pr]))):
            all_results = scanner.scan()
            changes_results = scanner.scan_changes_requested()
        # scan() includes all SWE-branch PRs regardless of decision
        assert len(all_results) == 1
        # scan_changes_requested() filters to CHANGES_REQUESTED only
        assert changes_results == []

    def test_scan_changes_requested_filters_correctly(self):
        prs = [
            _make_pr(number=10, branch="swe-fix/ticket-aabbccddeeff", review_decision="CHANGES_REQUESTED"),
            _make_pr(number=11, branch="swe-fix/ticket-112233445566", review_decision="APPROVED"),
            _make_pr(number=12, branch="swe-fix/ticket-deadbeefcafe", review_decision=""),
        ]
        scanner = _make_scanner()
        with patch("subprocess.run", return_value=_proc(json.dumps(prs))):
            results = scanner.scan_changes_requested()
        assert len(results) == 1
        assert results[0].pr_number == 10
        assert results[0].ticket_id == "aabbccddeeff"

    def test_subprocess_exception_returns_empty(self):
        scanner = _make_scanner()
        with patch("subprocess.run", side_effect=OSError("gh not found")):
            result = scanner.scan()
        assert result == []

    def test_review_comments_concatenated(self):
        nodes = [
            {"author": {"login": "rev1"}, "state": "CHANGES_REQUESTED", "body": "Comment one."},
            {"author": {"login": "rev2"}, "state": "CHANGES_REQUESTED", "body": "Comment two."},
        ]
        pr = _make_pr(review_nodes=nodes)
        scanner = _make_scanner()
        with patch("subprocess.run", return_value=_proc(json.dumps([pr]))):
            results = scanner.scan()
        assert len(results) == 1
        assert "Comment one." in results[0].review_comments
        assert "Comment two." in results[0].review_comments

    def test_pr_with_no_review_nodes(self):
        pr = _make_pr(review_decision="CHANGES_REQUESTED", review_nodes=[])
        scanner = _make_scanner()
        with patch("subprocess.run", return_value=_proc(json.dumps([pr]))):
            results = scanner.scan()
        assert len(results) == 1
        assert results[0].review_comments == ""

    def test_repo_is_propagated_to_result(self):
        pr = _make_pr()
        scanner = _make_scanner(repo="myorg/myrepo")
        with patch("subprocess.run", return_value=_proc(json.dumps([pr]))):
            results = scanner.scan()
        assert results[0].repo == "myorg/myrepo"


# ---------------------------------------------------------------------------
# Rework transition logic (unit-level, no runner import)
# ---------------------------------------------------------------------------


class TestReworkTransitionLogic:
    """Verify the ticket state machine transitions used in the runner."""

    def _make_ticket(self, ticket_id: str = "abc123def456") -> SWETicket:
        t = SWETicket(
            title="Test ticket",
            description="desc",
            severity=TicketSeverity.HIGH,
        )
        object.__setattr__(t, "ticket_id", ticket_id)
        t.status = TicketStatus.IN_REVIEW
        t.investigation_report = "A" * 250  # satisfies resolution audit length
        t.metadata["attempts"] = [{"result": "success"}]
        return t

    def test_transition_in_review_to_rework_then_investigation_complete(self):
        ticket = self._make_ticket()
        ticket.transition(TicketStatus.REWORK_REQUESTED)
        assert ticket.status == TicketStatus.REWORK_REQUESTED
        ticket.transition(TicketStatus.INVESTIGATION_COMPLETE)
        assert ticket.status == TicketStatus.INVESTIGATION_COMPLETE

    def test_review_feedback_stored_in_metadata(self):
        ticket = self._make_ticket()
        feedback = "Please add type hints to all functions."
        ticket.metadata["review_feedback"] = feedback
        assert ticket.metadata["review_feedback"] == feedback

    def test_rework_pr_metadata_stored(self):
        ticket = self._make_ticket()
        ticket.metadata["rework_pr_number"] = 42
        ticket.metadata["rework_pr_branch"] = "swe-fix/ticket-abc123def456"
        assert ticket.metadata["rework_pr_number"] == 42
        assert ticket.metadata["rework_pr_branch"] == "swe-fix/ticket-abc123def456"

    def test_transition_from_wrong_status_still_works(self):
        """The transition() method does not enforce a strict state machine;
        any status → REWORK_REQUESTED is allowed at the model level."""
        ticket = self._make_ticket()
        ticket.status = TicketStatus.IN_DEVELOPMENT
        ticket.transition(TicketStatus.REWORK_REQUESTED)
        assert ticket.status == TicketStatus.REWORK_REQUESTED

    def test_rework_status_value(self):
        assert TicketStatus.REWORK_REQUESTED.value == "rework_requested"

    def test_rework_status_round_trips_through_from_dict(self):
        ticket = self._make_ticket()
        ticket.status = TicketStatus.REWORK_REQUESTED
        d = ticket.to_dict()
        assert d["status"] == "rework_requested"
        restored = SWETicket.from_dict(d)
        assert restored.status == TicketStatus.REWORK_REQUESTED


# ---------------------------------------------------------------------------
# Developer prompt injection (verifying review_feedback is picked up)
# ---------------------------------------------------------------------------


class TestDeveloperPromptInjection:
    """Smoke-test that the developer agent injects review_feedback into the prompt."""

    def test_review_feedback_injected_into_prompt(self):
        """DeveloperAgent._build_prompt must include review_feedback when present."""
        from src.swe_team.developer import DeveloperAgent

        agent = DeveloperAgent()
        ticket = SWETicket(
            title="Fix something",
            description="desc",
            severity=TicketSeverity.HIGH,
        )
        ticket.investigation_report = "Root cause: missing null check."
        ticket.metadata["review_feedback"] = "Add a unit test for the null-check path."

        # Patch _load_program to return a minimal template
        template = (
            "# Fix\n"
            "ticket_id={ticket_id}\n"
            "description={description}\n"
            "investigation_report={investigation_report}\n"
            "severity={severity}\n"
            "source_module={source_module}\n"
            "title={title}\n"
            "issue_type={issue_type}\n"
        )
        agent._program_cache = template

        prompt = agent._build_prompt(ticket)
        assert prompt is not None
        assert "Review Feedback (MUST ADDRESS)" in prompt
        assert "Add a unit test for the null-check path." in prompt

    def test_no_review_feedback_no_section(self):
        from src.swe_team.developer import DeveloperAgent

        agent = DeveloperAgent()
        ticket = SWETicket(
            title="Fix something",
            description="desc",
            severity=TicketSeverity.HIGH,
        )
        ticket.investigation_report = "Root cause: missing null check."

        template = (
            "# Fix\n"
            "ticket_id={ticket_id}\n"
            "description={description}\n"
            "investigation_report={investigation_report}\n"
            "severity={severity}\n"
            "source_module={source_module}\n"
            "title={title}\n"
            "issue_type={issue_type}\n"
        )
        agent._program_cache = template

        prompt = agent._build_prompt(ticket)
        assert prompt is not None
        assert "Review Feedback" not in prompt
