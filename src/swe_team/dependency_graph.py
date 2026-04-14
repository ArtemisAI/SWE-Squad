"""Ticket dependency graph utilities."""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, Iterable, List, Set

from src.swe_team.models import SWETicket, TicketStatus

logger = logging.getLogger(__name__)

_RESOLVED_STATUSES = {TicketStatus.RESOLVED, TicketStatus.CLOSED}


class DependencyGraph:
    """Builds a ticket dependency graph from blocked_by/blocking metadata."""

    def __init__(self, all_tickets: Iterable[SWETicket]) -> None:
        self._tickets: List[SWETicket] = list(all_tickets)
        self._by_id: Dict[str, SWETicket] = {t.ticket_id: t for t in self._tickets}
        self._deps: Dict[str, Set[str]] = {}
        self._dependents: Dict[str, Set[str]] = defaultdict(set)
        self._build_edges()
        self._detect_cycles()

    def _build_edges(self) -> None:
        for ticket in self._tickets:
            deps: Set[str] = set()
            for dep in ticket.blocked_by + list(ticket.metadata.get("depends_on", [])):
                if not isinstance(dep, str):
                    continue
                normalized = dep.strip()
                if not normalized or normalized == ticket.ticket_id:
                    continue
                deps.add(normalized)
            self._deps[ticket.ticket_id] = deps
            for dep in deps:
                self._dependents[dep].add(ticket.ticket_id)

        # Add reverse links from explicit `blocking` relationships.
        for ticket in self._tickets:
            for blocked_id in ticket.blocking:
                if not isinstance(blocked_id, str):
                    continue
                blocked_id = blocked_id.strip()
                if not blocked_id or blocked_id == ticket.ticket_id:
                    continue
                self._deps.setdefault(blocked_id, set()).add(ticket.ticket_id)
                self._dependents[ticket.ticket_id].add(blocked_id)

    def _detect_cycles(self) -> None:
        visited: Set[str] = set()
        active: Set[str] = set()
        stack: List[str] = []

        def dfs(ticket_id: str) -> None:
            if ticket_id in active:
                try:
                    idx = stack.index(ticket_id)
                except ValueError:
                    logger.warning(
                        "Circular ticket dependency detected with inconsistent DFS stack: %s",
                        " -> ".join(stack + [ticket_id]),
                    )
                    return
                cycle = stack[idx:] + [ticket_id]
                logger.warning("Circular ticket dependency detected: %s", " -> ".join(cycle))
                return
            if ticket_id in visited:
                return

            visited.add(ticket_id)
            active.add(ticket_id)
            stack.append(ticket_id)
            for dep in self._deps.get(ticket_id, set()):
                if dep in self._by_id:
                    dfs(dep)
            stack.pop()
            active.remove(ticket_id)

        for ticket_id in self._by_id:
            dfs(ticket_id)

    def _has_resolved_status(self, ticket_id: str) -> bool:
        ticket = self._by_id.get(ticket_id)
        return bool(ticket and ticket.status in _RESOLVED_STATUSES)

    def unresolved_dependencies(self, ticket_id: str) -> List[str]:
        deps = self._deps.get(ticket_id, set())
        unresolved = [dep for dep in deps if not self._has_resolved_status(dep)]
        return sorted(unresolved)

    def is_blocked(self, ticket_id: str) -> bool:
        return bool(self.unresolved_dependencies(ticket_id))

    def get_ready_tickets(self, all_tickets: Iterable[SWETicket] | None = None) -> List[SWETicket]:
        candidates = list(all_tickets) if all_tickets is not None else list(self._tickets)
        order = {ticket.ticket_id: idx for idx, ticket in enumerate(candidates)}
        ready = [ticket for ticket in candidates if not self.is_blocked(ticket.ticket_id)]
        ready.sort(key=lambda ticket: order[ticket.ticket_id])
        return ready


def get_ready_tickets(all_tickets: Iterable[SWETicket]) -> List[SWETicket]:
    """Convenience wrapper returning ready tickets for the provided list."""
    return DependencyGraph(all_tickets).get_ready_tickets()
