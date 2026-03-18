"""Graph-aware ticket priority scoring for SWE Squad.

Replaces the simple severity-only sort with a multi-factor score
that considers knowledge graph relationships: similar ticket count,
cluster size, age, failed attempts, and whether a PR already exists.

Falls back gracefully to severity-only scoring when the knowledge
graph store is unavailable or has no data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from src.swe_team.models import EdgeType, SWETicket, TicketSeverity

if TYPE_CHECKING:
    from src.swe_team.knowledge_store import KnowledgeGraphStore

logger = logging.getLogger(__name__)

# Severity base weights
SEVERITY_WEIGHT: Dict[TicketSeverity, float] = {
    TicketSeverity.CRITICAL: 10.0,
    TicketSeverity.HIGH: 5.0,
    TicketSeverity.MEDIUM: 2.0,
    TicketSeverity.LOW: 1.0,
}


def priority_score(
    ticket: SWETicket,
    graph_store: Optional[KnowledgeGraphStore] = None,
) -> float:
    """Compute a priority score for a ticket.

    Higher score = higher priority. Factors:
    - Base severity weight (CRITICAL=10, HIGH=5, MEDIUM=2, LOW=1)
    - Similar ticket count: more similar = higher impact fix (x1.3 per similar)
    - Cluster size: fix-once-resolve-many bonus (x1.5 per cluster member)
    - Age: older tickets get a gradual bonus (capped at 1 week)
    - Failed attempts: deprioritise repeat failures (div 1.4 per failure)
    - Open PR: if a PR already targets this ticket, deprioritise (x0.1)
    - Regression flag: regressions get a 2x boost

    Falls back to severity-only when graph_store is None or queries fail.
    """
    base = SEVERITY_WEIGHT.get(ticket.severity, 2.0)

    # Age factor (gradual increase, capped at 1 week = 168h)
    try:
        created = datetime.fromisoformat(ticket.created_at.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    except (ValueError, TypeError):
        age_hours = 0.0
    age_factor = 1.0 + min(age_hours, 168.0) / 48.0  # max ~4.5x

    # Failed attempts penalty
    attempts = ticket.metadata.get("attempts", [])
    fail_factor = 1.0 / (1.0 + len(attempts) * 0.4)

    # Regression boost
    is_regression = ticket.metadata.get("is_regression", False)
    regression_factor = 2.0 if is_regression else 1.0

    # Graph-aware factors (only if store available)
    similar_factor = 1.0
    cluster_factor = 1.0
    pr_factor = 1.0

    if graph_store is not None:
        try:
            # Count similar edges
            similar_count = graph_store.count_edges(
                ticket.ticket_id, edge_type=EdgeType.SIMILAR,
            )
            similar_factor = 1.0 + similar_count * 0.3
        except Exception:
            pass

        try:
            # Check for resolution cluster
            cluster = graph_store.find_ticket_cluster(ticket.ticket_id)
            if cluster:
                cluster_size = len(cluster.ticket_ids)
                cluster_factor = 1.0 + cluster_size * 0.5
        except Exception:
            pass

        try:
            # Check if PR already exists for this ticket
            edges = graph_store.get_edges(
                ticket.ticket_id, edge_type=EdgeType.RESOLVES,
            )
            if edges:
                pr_factor = 0.1  # PR exists -- deprioritise
        except Exception:
            pass

    score = (
        base
        * age_factor
        * fail_factor
        * regression_factor
        * similar_factor
        * cluster_factor
        * pr_factor
    )

    return round(score, 4)


def rank_tickets(
    tickets: List[SWETicket],
    graph_store: Optional[KnowledgeGraphStore] = None,
) -> List[SWETicket]:
    """Sort tickets by graph-aware priority score (highest first).

    This is a drop-in replacement for the severity-only sort used in
    triage_batch and backlog pickup.
    """
    scored = []
    for t in tickets:
        try:
            s = priority_score(t, graph_store)
        except Exception:
            # Fallback: severity only
            s = SEVERITY_WEIGHT.get(t.severity, 2.0)
        scored.append((s, t))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [t for _, t in scored]
