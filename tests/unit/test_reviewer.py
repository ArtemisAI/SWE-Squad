"""Unit tests for src/swe_team/reviewer.py.

Covers:
- ReviewerAgent.review_batch() happy path (approved / rejected / hitl)
- Audit failures bounce ticket to IN_DEVELOPMENT
- dry_run mode — no mutations, all tickets treated as approved
- Empty list input
- HITL escalation via needs_hitl metadata
- _store_save helper (save vs add fallback)

Note: reviewer.py imports CodeReviewerAgent locally inside review_batch() via
`from src.swe_team.code_reviewer import CodeReviewerAgent`, so we patch at
src.swe_team.code_reviewer.CodeReviewerAgent.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.reviewer import ReviewerAgent, _store_save

# Patch target for CodeReviewerAgent (imported locally inside review_batch)
_CR_PATCH = "src.swe_team.code_reviewer.CodeReviewerAgent"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ticket(
    ticket_id: str = "t-test",
    severity: TicketSeverity = TicketSeverity.MEDIUM,
    status: TicketStatus = TicketStatus.IN_REVIEW,
    **kwargs,
) -> SWETicket:
    """Build a ticket that passes resolution_audit() by default."""
    defaults = dict(
        ticket_id=ticket_id,
        title="Test bug",
        description="Something broke",
        severity=severity,
        status=status,
        investigation_report="Root cause: xyz. Detailed analysis here. " * 10,
        metadata={"resolution_note": "fix_succeeded"},
    )
    defaults.update(kwargs)
    return SWETicket(**defaults)


def _store() -> MagicMock:
    store = MagicMock()
    store.save = MagicMock()
    return store


def _reviewer(**kwargs) -> ReviewerAgent:
    defaults = dict(model="haiku", timeout=5, repo_root="")
    defaults.update(kwargs)
    return ReviewerAgent(**defaults)


def _patch_code_reviewer(approved: bool, feedback: str = "looks good"):
    """Patch CodeReviewerAgent at its definition location."""
    mock_cr = MagicMock()
    mock_cr.review.return_value = (approved, feedback)
    return patch(_CR_PATCH, return_value=mock_cr)


# ---------------------------------------------------------------------------
# Tests: review_batch
# ---------------------------------------------------------------------------

class TestReviewBatch:
    def test_empty_list_returns_empty_tuples(self):
        reviewer = _reviewer()
        store = _store()

        resolved, rejected, hitl = reviewer.review_batch([], store)

        assert resolved == []
        assert rejected == []
        assert hitl == []

    def test_approved_ticket_goes_to_resolved_list(self):
        reviewer = _reviewer()
        store = _store()
        ticket = _ticket()

        with _patch_code_reviewer(approved=True, feedback="LGTM"):
            resolved, rejected, hitl = reviewer.review_batch([ticket], store)

        assert ticket in resolved
        assert rejected == []
        assert hitl == []

    def test_rejected_ticket_goes_to_rejected_list(self):
        reviewer = _reviewer()
        store = _store()
        ticket = _ticket()

        with _patch_code_reviewer(approved=False, feedback="Bad code"):
            resolved, rejected, hitl = reviewer.review_batch([ticket], store)

        assert ticket in rejected
        assert resolved == []
        assert hitl == []

    def test_hitl_escalated_ticket_goes_to_hitl_list(self):
        reviewer = _reviewer()
        store = _store()
        ticket = _ticket()
        ticket.metadata["needs_hitl"] = True

        with _patch_code_reviewer(approved=False, feedback="Too many rejections"):
            resolved, rejected, hitl = reviewer.review_batch([ticket], store)

        assert ticket in hitl
        assert resolved == []
        assert rejected == []

    def test_audit_failure_bounces_to_rejected_and_in_development(self):
        reviewer = _reviewer()
        store = _store()
        # Ticket with too-short investigation report and no bypass note → audit fails
        ticket = _ticket(
            investigation_report="short",
            metadata={},  # no resolution_note bypass
        )

        with _patch_code_reviewer(approved=True):
            resolved, rejected, hitl = reviewer.review_batch([ticket], store)

        assert ticket in rejected
        assert resolved == []
        assert ticket.status == TicketStatus.IN_DEVELOPMENT
        store.save.assert_called_once_with(ticket)

    def test_dry_run_skips_code_reviewer_and_marks_resolved(self):
        reviewer = _reviewer()
        store = _store()
        ticket = _ticket()

        # dry_run=True means CodeReviewerAgent.review() is never called
        with patch(_CR_PATCH) as MockCR:
            resolved, rejected, hitl = reviewer.review_batch(
                [ticket], store, dry_run=True
            )
            MockCR.return_value.review.assert_not_called()

        assert ticket in resolved
        assert rejected == []

    def test_dry_run_does_not_persist_rejected(self):
        reviewer = _reviewer()
        store = _store()
        # Audit will fail because investigation_report is too short
        ticket = _ticket(investigation_report="x", metadata={})

        with patch(_CR_PATCH):
            reviewer.review_batch([ticket], store, dry_run=True)

        # In dry_run mode, store.save must NOT be called even on audit failure
        store.save.assert_not_called()
        # Ticket status must NOT be mutated
        assert ticket.status == TicketStatus.IN_REVIEW

    def test_multiple_tickets_processed_individually(self):
        reviewer = _reviewer()
        store = _store()
        t1 = _ticket(ticket_id="t1")
        t2 = _ticket(ticket_id="t2")

        call_count = [0]

        def _review_side_effect(ticket, store, repo_root=""):
            call_count[0] += 1
            return (ticket.ticket_id == "t1", "feedback")

        mock_cr = MagicMock()
        mock_cr.review.side_effect = _review_side_effect

        with patch(_CR_PATCH, return_value=mock_cr):
            resolved, rejected, hitl = reviewer.review_batch([t1, t2], store)

        assert t1 in resolved
        assert t2 in rejected
        assert call_count[0] == 2

    def test_returns_correct_counts(self):
        reviewer = _reviewer()
        store = _store()
        tickets = [_ticket(ticket_id=f"t{i}") for i in range(4)]

        call_count = [0]

        def _alt_review(ticket, store, repo_root=""):
            result = call_count[0] % 2 == 0  # alternate approve/reject
            call_count[0] += 1
            return result, "feedback"

        mock_cr = MagicMock()
        mock_cr.review.side_effect = _alt_review

        with patch(_CR_PATCH, return_value=mock_cr):
            resolved, rejected, hitl = reviewer.review_batch(tickets, store)

        assert len(resolved) + len(rejected) + len(hitl) == 4

    def test_review_feedback_stored_in_metadata_on_audit_failure(self):
        reviewer = _reviewer()
        store = _store()
        ticket = _ticket(investigation_report="too short", metadata={})

        with _patch_code_reviewer(approved=True):
            reviewer.review_batch([ticket], store)

        assert "review_feedback" in ticket.metadata
        assert "audit failed" in ticket.metadata["review_feedback"].lower()


# ---------------------------------------------------------------------------
# Tests: _resolve_repo_root — per-ticket sandbox repo resolution
# ---------------------------------------------------------------------------

class TestResolveRepoRoot:
    def test_returns_repo_root_when_no_repos_map(self):
        reviewer = ReviewerAgent(repo_root="/fallback/root")
        ticket = _ticket(metadata={"resolution_note": "fix_succeeded", "repo": "sandbox-a"})
        assert reviewer._resolve_repo_root(ticket) == "/fallback/root"

    def test_resolves_from_repos_map_by_ticket_repo_name(self):
        reviewer = ReviewerAgent(
            repos_map={"sandbox-a": "/path/to/sandbox-a", "sandbox-b": "/path/to/sandbox-b"},
        )
        ticket = _ticket(metadata={"resolution_note": "fix_succeeded", "repo": "sandbox-a"})
        assert reviewer._resolve_repo_root(ticket) == "/path/to/sandbox-a"

    def test_falls_back_to_first_entry_when_repo_not_in_map(self):
        reviewer = ReviewerAgent(
            repos_map={"sandbox-a": "/path/to/sandbox-a"},
        )
        ticket = _ticket(metadata={"resolution_note": "fix_succeeded", "repo": "unknown-repo"})
        assert reviewer._resolve_repo_root(ticket) == "/path/to/sandbox-a"

    def test_falls_back_to_first_entry_when_no_repo_metadata(self):
        reviewer = ReviewerAgent(
            repos_map={"sandbox-a": "/path/to/sandbox-a"},
        )
        ticket = _ticket(metadata={"resolution_note": "fix_succeeded"})  # no "repo" key
        assert reviewer._resolve_repo_root(ticket) == "/path/to/sandbox-a"

    def test_per_ticket_repo_root_passed_to_code_reviewer(self):
        """review_batch must pass the per-ticket resolved path, not a single global one."""
        reviewer = ReviewerAgent(
            repos_map={
                "repo-x": "/sandbox/repo-x",
                "repo-y": "/sandbox/repo-y",
            },
            repo_root="/wrong/project/root",
        )
        store = _store()
        t1 = _ticket(ticket_id="t1", metadata={"resolution_note": "fix_succeeded", "repo": "repo-x"})
        t2 = _ticket(ticket_id="t2", metadata={"resolution_note": "fix_succeeded", "repo": "repo-y"})

        captured_roots: list = []

        def _capture_root(ticket, store, repo_root=""):
            captured_roots.append(repo_root)
            return True, "ok"

        mock_cr = MagicMock()
        mock_cr.review.side_effect = _capture_root

        with patch(_CR_PATCH, return_value=mock_cr):
            reviewer.review_batch([t1, t2], store)

        assert captured_roots == ["/sandbox/repo-x", "/sandbox/repo-y"], (
            f"Expected per-ticket sandbox paths, got: {captured_roots}"
        )


# ---------------------------------------------------------------------------
# Tests: PR Safety Net in review_batch (issue #367)
# ---------------------------------------------------------------------------

class TestReviewBatchPRSafetyNet:
    """Reviewer safety net: RESOLVED tickets without pr_url/pr_number are downgraded.

    This validates the second line of defence (issue #367).  The primary gate
    is in CodeReviewerAgent._handle_approve(); this safety net catches any code
    path that bypasses it.
    """

    def _make_code_reviewer_that_resolves(self):
        """Return a mock CodeReviewerAgent whose review() transitions ticket to RESOLVED."""
        def _review_side_effect(ticket, store, repo_root=""):
            # Simulate what _handle_approve does when it succeeds but no PR existed
            ticket.metadata["resolution_note"] = "fix_succeeded"
            ticket.transition(TicketStatus.RESOLVED)
            return True, "approved: LGTM"

        mock_cr = MagicMock()
        mock_cr.review.side_effect = _review_side_effect
        return mock_cr

    def test_resolved_without_pr_downgraded_to_in_development(self):
        """If CodeReviewerAgent resolves a ticket but no pr_url/pr_number exists,
        the safety net must downgrade it back to IN_DEVELOPMENT."""
        reviewer = _reviewer()
        store = _store()
        ticket = _ticket()
        ticket.metadata.pop("pr_url", None)
        ticket.metadata.pop("pr_number", None)

        mock_cr = self._make_code_reviewer_that_resolves()
        with patch(_CR_PATCH, return_value=mock_cr):
            resolved, rejected, hitl = reviewer.review_batch([ticket], store)

        assert ticket not in resolved
        assert ticket in rejected
        assert ticket.status == TicketStatus.IN_DEVELOPMENT
        assert ticket.metadata.get("needs_pr") is True

    def test_resolved_with_pr_url_passes_safety_net(self):
        """A ticket with pr_url in metadata passes the safety net and stays resolved."""
        reviewer = _reviewer()
        store = _store()
        ticket = _ticket()

        def _review_side_effect(ticket, store, repo_root=""):
            ticket.metadata["pr_url"] = "https://github.com/owner/repo/pull/9"
            ticket.metadata["resolution_note"] = "fix_succeeded"
            ticket.transition(TicketStatus.RESOLVED)
            return True, "approved"

        mock_cr = MagicMock()
        mock_cr.review.side_effect = _review_side_effect
        with patch(_CR_PATCH, return_value=mock_cr):
            resolved, rejected, hitl = reviewer.review_batch([ticket], store)

        assert ticket in resolved
        assert rejected == []

    def test_resolved_with_pr_number_passes_safety_net(self):
        """A ticket with pr_number in metadata passes the safety net."""
        reviewer = _reviewer()
        store = _store()
        ticket = _ticket()

        def _review_side_effect(ticket, store, repo_root=""):
            ticket.metadata["pr_number"] = 33
            ticket.metadata["resolution_note"] = "fix_succeeded"
            ticket.transition(TicketStatus.RESOLVED)
            return True, "approved"

        mock_cr = MagicMock()
        mock_cr.review.side_effect = _review_side_effect
        with patch(_CR_PATCH, return_value=mock_cr):
            resolved, rejected, hitl = reviewer.review_batch([ticket], store)

        assert ticket in resolved
        assert rejected == []

    def test_downgraded_ticket_persisted_to_store(self):
        """When safety net fires, the downgraded ticket is persisted."""
        reviewer = _reviewer()
        store = _store()
        ticket = _ticket()
        ticket.metadata.pop("pr_url", None)
        ticket.metadata.pop("pr_number", None)

        mock_cr = self._make_code_reviewer_that_resolves()
        with patch(_CR_PATCH, return_value=mock_cr):
            reviewer.review_batch([ticket], store)

        store.save.assert_called_with(ticket)

    def test_safety_net_sets_review_feedback_metadata(self):
        """The safety net must record why the ticket was downgraded."""
        reviewer = _reviewer()
        store = _store()
        ticket = _ticket()
        ticket.metadata.pop("pr_url", None)
        ticket.metadata.pop("pr_number", None)

        mock_cr = self._make_code_reviewer_that_resolves()
        with patch(_CR_PATCH, return_value=mock_cr):
            reviewer.review_batch([ticket], store)

        assert "review_feedback" in ticket.metadata
        assert "pr" in ticket.metadata["review_feedback"].lower()


# ---------------------------------------------------------------------------
# Tests: _store_save helper
# ---------------------------------------------------------------------------

class TestStoreSave:
    def test_uses_save_if_available(self):
        ticket = _ticket()
        store = MagicMock(spec=["save"])
        _store_save(store, ticket)
        store.save.assert_called_once_with(ticket)

    def test_falls_back_to_add(self):
        ticket = _ticket()
        store = MagicMock(spec=["add"])
        _store_save(store, ticket)
        store.add.assert_called_once_with(ticket)

    def test_logs_error_if_neither(self):
        ticket = _ticket()
        store = MagicMock(spec=[])  # no save, no add
        # Should not raise
        _store_save(store, ticket)
