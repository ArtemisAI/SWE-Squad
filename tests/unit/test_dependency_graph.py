"""Unit tests for ticket dependency graph utilities."""
from __future__ import annotations

from src.swe_team.dependency_graph import DependencyGraph, get_ready_tickets
from src.swe_team.models import SWETicket, TicketStatus


def _ticket(
    ticket_id: str,
    *,
    status: TicketStatus = TicketStatus.OPEN,
    blocked_by: list[str] | None = None,
    blocking: list[str] | None = None,
) -> SWETicket:
    return SWETicket(
        ticket_id=ticket_id,
        title=f"Ticket {ticket_id}",
        description="test",
        status=status,
        blocked_by=blocked_by or [],
        blocking=blocking or [],
    )


def test_linear_chain_only_root_is_ready() -> None:
    a = _ticket("A")
    b = _ticket("B", blocked_by=["A"])
    c = _ticket("C", blocked_by=["B"])

    ready = get_ready_tickets([a, b, c])

    assert [ticket.ticket_id for ticket in ready] == ["A"]


def test_diamond_dependency_only_root_is_ready() -> None:
    a = _ticket("A", blocking=["B", "C"])
    b = _ticket("B", blocked_by=["A"], blocking=["D"])
    c = _ticket("C", blocked_by=["A"], blocking=["D"])
    d = _ticket("D", blocked_by=["B", "C"])

    graph = DependencyGraph([a, b, c, d])

    assert [ticket.ticket_id for ticket in graph.get_ready_tickets()] == ["A"]
    assert graph.is_blocked("D") is True


def test_cycle_detection_logs_warning(caplog) -> None:
    a = _ticket("A", blocked_by=["B"])
    b = _ticket("B", blocked_by=["A"])

    with caplog.at_level("WARNING"):
        graph = DependencyGraph([a, b])

    assert graph.get_ready_tickets() == []
    assert any("Circular ticket dependency detected" in record.message for record in caplog.records)


def test_no_dependencies_all_ready_in_input_priority_order() -> None:
    c = _ticket("C")
    a = _ticket("A")
    b = _ticket("B")

    graph = DependencyGraph([c, a, b])

    assert [ticket.ticket_id for ticket in graph.get_ready_tickets()] == ["C", "A", "B"]


def test_resolved_blockers_unblock_ticket() -> None:
    blocker = _ticket("A", status=TicketStatus.RESOLVED)
    blocked = _ticket("B", blocked_by=["A"])

    graph = DependencyGraph([blocked, blocker])

    assert graph.is_blocked("B") is False
    assert [ticket.ticket_id for ticket in graph.get_ready_tickets([blocked])] == ["B"]
