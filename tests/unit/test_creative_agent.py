"""Unit tests for src/swe_team/creative_agent.py."""

from __future__ import annotations

from subprocess import TimeoutExpired
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.creative_agent import CreativeAgent, _TITLE_PREFIX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolved_ticket(source_module="auth", title="Auth failure", **kwargs):
    return SWETicket(
        title=title,
        description="desc",
        severity=TicketSeverity.HIGH,
        status=TicketStatus.RESOLVED,
        source_module=source_module,
        **kwargs,
    )


def _open_ticket(source_module="auth", title="Auth issue open"):
    return SWETicket(
        title=title,
        description="desc",
        severity=TicketSeverity.HIGH,
        status=TicketStatus.OPEN,
        source_module=source_module,
    )


def _mock_store(all_tickets=None, open_tickets=None):
    store = MagicMock()
    store.list_all.return_value = all_tickets or []
    store.list_open.return_value = open_tickets or []
    return store


def _proc(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# CreativeAgent.propose
# ---------------------------------------------------------------------------

class TestPropose:
    def test_returns_empty_when_no_closed_tickets(self):
        agent = CreativeAgent()
        store = _mock_store(all_tickets=[_open_ticket()])
        result = agent.propose(store)
        assert result == []

    def test_returns_empty_when_store_empty(self):
        agent = CreativeAgent()
        store = _mock_store(all_tickets=[])
        result = agent.propose(store)
        assert result == []

    def test_proposes_for_resolved_module(self):
        agent = CreativeAgent()
        tickets = [
            _resolved_ticket("auth"),
            _resolved_ticket("auth"),
            _resolved_ticket("auth"),
        ]
        store = _mock_store(all_tickets=tickets)
        proposals = agent.propose(store)
        assert len(proposals) == 1
        assert "auth" in proposals[0].source_module

    def test_proposal_ticket_is_low_severity(self):
        agent = CreativeAgent()
        tickets = [_resolved_ticket("db")]
        store = _mock_store(all_tickets=tickets)
        proposals = agent.propose(store)
        assert proposals[0].severity == TicketSeverity.LOW

    def test_proposal_title_uses_prefix(self):
        agent = CreativeAgent()
        tickets = [_resolved_ticket("scraper")]
        store = _mock_store(all_tickets=tickets)
        proposals = agent.propose(store)
        assert proposals[0].title.startswith(_TITLE_PREFIX)

    def test_skips_duplicate_existing_proposals(self):
        agent = CreativeAgent()
        module = "billing"
        existing_title = f"{_TITLE_PREFIX} Prevent recurring {module} issues"
        closed = _resolved_ticket(module)
        existing = SWETicket(
            title=existing_title,
            description="already exists",
            severity=TicketSeverity.LOW,
            source_module=module,
        )
        store = _mock_store(all_tickets=[closed, existing])
        proposals = agent.propose(store)
        # Should not create a duplicate
        assert all(p.title != existing_title for p in proposals)

    def test_respects_limit(self):
        agent = CreativeAgent()
        modules = ["auth", "db", "scraper", "billing", "cache"]
        tickets = [_resolved_ticket(m) for m in modules]
        store = _mock_store(all_tickets=tickets)
        proposals = agent.propose(store, limit=2)
        assert len(proposals) <= 2

    def test_description_contains_module_count(self):
        agent = CreativeAgent()
        tickets = [
            _resolved_ticket("api"),
            _resolved_ticket("api"),
            _resolved_ticket("api"),
        ]
        store = _mock_store(all_tickets=tickets)
        proposals = agent.propose(store)
        assert "3" in proposals[0].description

    def test_includes_closed_status_tickets(self):
        agent = CreativeAgent()
        closed = SWETicket(
            title="Closed issue",
            description="desc",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.CLOSED,
            source_module="payments",
        )
        store = _mock_store(all_tickets=[closed])
        proposals = agent.propose(store)
        assert len(proposals) == 1

    def test_metadata_contains_creative_info(self):
        agent = CreativeAgent()
        tickets = [_resolved_ticket("api")]
        store = _mock_store(all_tickets=tickets)
        proposals = agent.propose(store)
        meta = proposals[0].metadata.get("creative", {})
        assert meta.get("module") == "api"
        assert meta.get("count") == 1


# ---------------------------------------------------------------------------
# CreativeAgent._create_issue
# ---------------------------------------------------------------------------

class TestCreateIssue:
    def test_returns_issue_number_on_success(self):
        agent = CreativeAgent()
        ticket = _resolved_ticket()
        with patch("src.swe_team.creative_agent.subprocess.run") as mock_run:
            mock_run.return_value = _proc(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/88\n",
            )
            result = agent._create_issue(ticket)
        assert result == 88

    def test_returns_none_on_subprocess_failure(self):
        agent = CreativeAgent()
        ticket = _resolved_ticket()
        with patch("src.swe_team.creative_agent.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=1, stderr="label not found")
            result = agent._create_issue(ticket)
        assert result is None

    def test_returns_none_when_output_unparseable(self):
        agent = CreativeAgent()
        ticket = _resolved_ticket()
        with patch("src.swe_team.creative_agent.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0, stdout="unexpected output")
            result = agent._create_issue(ticket)
        assert result is None

    def test_returns_none_on_timeout(self):
        agent = CreativeAgent()
        ticket = _resolved_ticket()
        with patch("src.swe_team.creative_agent.subprocess.run") as mock_run:
            mock_run.side_effect = TimeoutExpired(cmd="gh", timeout=30)
            result = agent._create_issue(ticket)
        assert result is None

    def test_returns_none_on_generic_exception(self):
        agent = CreativeAgent()
        ticket = _resolved_ticket()
        with patch("src.swe_team.creative_agent.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("gh not found")
            result = agent._create_issue(ticket)
        assert result is None


# ---------------------------------------------------------------------------
# CreativeAgent.publish_proposals
# ---------------------------------------------------------------------------

class TestPublishProposals:
    def test_returns_issue_numbers(self):
        agent = CreativeAgent()
        t1 = _resolved_ticket("auth")
        t2 = _resolved_ticket("db")
        with patch("src.swe_team.creative_agent.subprocess.run") as mock_run:
            call_count = [0]
            def side_effect(cmd, **kwargs):
                call_count[0] += 1
                return _proc(
                    returncode=0,
                    stdout=f"https://github.com/owner/repo/issues/{call_count[0] + 10}\n",
                )
            mock_run.side_effect = side_effect
            issues = agent.publish_proposals([t1, t2])
        assert len(issues) == 2

    def test_stores_issue_number_in_ticket_metadata(self):
        agent = CreativeAgent()
        ticket = _resolved_ticket("payments")
        with patch("src.swe_team.creative_agent.subprocess.run") as mock_run:
            mock_run.return_value = _proc(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/55\n",
            )
            agent.publish_proposals([ticket])
        assert ticket.metadata.get("github_issue") == 55

    def test_skips_failed_creation(self):
        agent = CreativeAgent()
        t1 = _resolved_ticket("good")
        t2 = _resolved_ticket("bad")
        call_count = [0]
        def side_effect(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _proc(returncode=0, stdout="https://github.com/x/y/issues/1\n")
            return _proc(returncode=1, stderr="failed")
        with patch("src.swe_team.creative_agent.subprocess.run", side_effect=side_effect):
            issues = agent.publish_proposals([t1, t2])
        assert issues == [1]

    def test_empty_proposals_returns_empty_list(self):
        agent = CreativeAgent()
        with patch("src.swe_team.creative_agent.subprocess.run") as mock_run:
            result = agent.publish_proposals([])
        mock_run.assert_not_called()
        assert result == []
