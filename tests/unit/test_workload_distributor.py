from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.swe_team.config import TeamConfig
from src.swe_team.dependency_graph import DependencyGraph
from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.team_registry import TeamRegistry
from src.swe_team.workload_distributor import AssignmentDecision, WorkloadDistributor


class _FakeTicketStore:
    """Fake store that implements list_by_status for TeamRegistry.get_team_load()."""

    def __init__(self, investigating: int = 0, in_development: int = 0) -> None:
        self._investigating = investigating
        self._in_development = in_development

    def list_by_status(self, status: TicketStatus, limit: int = 500) -> list:
        if status == TicketStatus.INVESTIGATING:
            return [None] * self._investigating
        if status == TicketStatus.IN_DEVELOPMENT:
            return [None] * self._in_development
        return []


def _ticket(
    ticket_id: str,
    severity: TicketSeverity = TicketSeverity.MEDIUM,
    *,
    status: TicketStatus = TicketStatus.OPEN,
    labels: list[str] | None = None,
    blocked_by: list[str] | None = None,
    assigned_to: str | None = None,
    updated_hours_ago: int = 0,
) -> SWETicket:
    updated_at = (datetime.now(timezone.utc) - timedelta(hours=updated_hours_ago)).isoformat()
    return SWETicket(
        ticket_id=ticket_id,
        title=ticket_id,
        description=ticket_id,
        severity=severity,
        status=status,
        labels=labels or [],
        blocked_by=blocked_by or [],
        assigned_to=assigned_to,
        updated_at=updated_at,
    )


def _registry(alpha_load: int = 0) -> TeamRegistry:
    """Build a TeamRegistry with three teams.

    ``alpha_load`` controls how many in-flight tickets the alpha team appears
    to have (split evenly between INVESTIGATING and IN_DEVELOPMENT).
    """
    teams = {
        "alpha": TeamConfig(name="alpha", github_account="your-bot-alpha", role="developer", max_concurrent=3, specialization=["frontend", "astro"]),
        "beta": TeamConfig(name="beta", github_account="your-bot-beta", role="full", max_concurrent=3),
        "gamma": TeamConfig(name="gamma", github_account="your-bot-gamma", role="full", max_concurrent=3),
    }

    def _store_factory(team_id: str) -> _FakeTicketStore:
        if team_id == "alpha":
            return _FakeTicketStore(in_development=alpha_load)
        return _FakeTicketStore()

    return TeamRegistry(teams=teams, store_factory=_store_factory)


def test_capacity_based_routing_skips_full_team() -> None:
    dist = WorkloadDistributor(_registry(alpha_load=3), DependencyGraph([]))
    decisions = dist.distribute([_ticket("T1", labels=["frontend"])])
    assert decisions
    assert decisions[0].team_id != "alpha"


def test_role_filtering_skips_dev_only_for_investigation() -> None:
    dist = WorkloadDistributor(_registry(), DependencyGraph([]))
    decisions = dist.distribute([_ticket("T1", status=TicketStatus.INVESTIGATING)])
    assert decisions
    assert decisions[0].team_id in {"beta", "gamma"}


def test_priority_ordering_critical_before_low() -> None:
    dist = WorkloadDistributor(_registry(), DependencyGraph([]))
    low = _ticket("LOW", severity=TicketSeverity.LOW)
    critical = _ticket("CRIT", severity=TicketSeverity.CRITICAL)
    decisions = dist.distribute([low, critical])
    assert [d.ticket_id for d in decisions] == ["CRIT", "LOW"]


def test_dependency_respect_skips_blocked_tickets() -> None:
    blocked = _ticket("BLOCKED", blocked_by=["T-DEP"])
    free = _ticket("FREE")
    dist = WorkloadDistributor(_registry(), DependencyGraph([blocked, free]))
    decisions = dist.distribute([blocked, free])
    assert [d.ticket_id for d in decisions] == ["FREE"]


def test_specialization_matching_prefers_frontend_team() -> None:
    dist = WorkloadDistributor(_registry(), DependencyGraph([]))
    decisions = dist.distribute([_ticket("UI", labels=["frontend"])])
    assert decisions
    assert decisions[0].team_id == "alpha"


def test_rebalancing_stalled_ticket_reassigns_team() -> None:
    dist = WorkloadDistributor(_registry(), DependencyGraph([]))
    stalled = _ticket(
        "STALL",
        status=TicketStatus.IN_DEVELOPMENT,
        assigned_to="alpha",
        updated_hours_ago=3,
        labels=["frontend"],
    )
    decisions = dist.distribute([stalled])
    assert decisions
    assert decisions[0].team_id != "alpha"
    assert "reassigned stalled ticket from alpha" in decisions[0].reason


def test_apply_assignments_dry_run_counts_valid_targets() -> None:
    dist = WorkloadDistributor(_registry(), DependencyGraph([]))
    ticket = _ticket("T-1")
    ticket.metadata["repo"] = "Org/Repo"
    ticket.metadata["github_issue"] = 10
    dist.distribute([ticket])
    applied = dist._apply_assignments([AssignmentDecision("T-1", "alpha", "test")], dry_run=True)
    assert applied == 1


def test_apply_assignments_executes_gh_and_handles_failures() -> None:
    dist = WorkloadDistributor(_registry(), DependencyGraph([]))
    ticket = _ticket("T-1")
    ticket.metadata["repo"] = "Org/Repo"
    ticket.metadata["github_issue"] = 10
    dist.distribute([ticket])
    with patch("src.swe_team.workload_distributor.subprocess.run") as run_mock:
        run_mock.return_value.returncode = 1
        applied = dist._apply_assignments([AssignmentDecision("T-1", "alpha", "test")], dry_run=False)
    assert applied == 0
    assert run_mock.called
