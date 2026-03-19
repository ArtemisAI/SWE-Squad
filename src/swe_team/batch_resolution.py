"""Batch resolution — auto-close related tickets when one is fixed.

When a ticket is resolved:
1. Find its resolution cluster (if any)
2. For each sibling in the cluster, check embedding similarity
3. If cosine > 0.90, auto-resolve the sibling with a note
4. Also populate the (previously dead) related_tickets field on SWETicket

Additionally provides cluster discovery: when a new ticket is embedded,
check if it belongs to an existing cluster or if it should start one.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from src.swe_team.models import (
    EdgeType,
    KnowledgeEdge,
    ResolutionCluster,
    SWETicket,
    TicketSeverity,
    TicketStatus,
)

if TYPE_CHECKING:
    from src.swe_team.knowledge_store import KnowledgeGraphStore
    from src.swe_team.supabase_store import SupabaseTicketStore

logger = logging.getLogger(__name__)

# Minimum cosine similarity to auto-resolve a sibling
AUTO_RESOLVE_THRESHOLD = 0.90

# Minimum similarity to group tickets into a cluster
CLUSTER_THRESHOLD = 0.85

# Maximum siblings to auto-resolve in one pass (safety cap)
MAX_AUTO_RESOLVE = 10


def on_ticket_resolved(
    ticket: SWETicket,
    ticket_store: SupabaseTicketStore,
    graph_store: KnowledgeGraphStore,
    *,
    auto_resolve_threshold: float = AUTO_RESOLVE_THRESHOLD,
    max_auto_resolve: int = MAX_AUTO_RESOLVE,
) -> List[str]:
    """Called when a ticket transitions to RESOLVED.

    Checks for cluster siblings and auto-resolves those with
    cosine similarity above *auto_resolve_threshold* (default 0.90).

    Returns list of auto-resolved ticket IDs.
    """
    auto_resolved: List[str] = []

    try:
        # 1. Find cluster
        cluster = graph_store.find_ticket_cluster(ticket.ticket_id)
        if not cluster or len(cluster.ticket_ids) <= 1:
            return auto_resolved

        logger.info(
            "Ticket %s resolved — checking %d cluster siblings",
            ticket.ticket_id,
            len(cluster.ticket_ids) - 1,
        )

        # 2. Get the resolved ticket's embedding for comparison
        from src.swe_team.embeddings import embed_ticket
        resolved_embedding = embed_ticket(ticket)
        if not resolved_embedding:
            logger.info("No embedding for resolved ticket — skipping batch resolution")
            return auto_resolved

        # 3. Check each sibling
        resolved_count = 0
        for sibling_id in cluster.ticket_ids:
            if sibling_id == ticket.ticket_id:
                continue
            if resolved_count >= max_auto_resolve:
                logger.info("Reached auto-resolve cap (%d), stopping", max_auto_resolve)
                break

            try:
                sibling = ticket_store.get(sibling_id)
                if not sibling:
                    continue
                if sibling.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.ACKNOWLEDGED):
                    continue

                # Get sibling embedding via similarity search
                matches = ticket_store.find_similar(
                    resolved_embedding,
                    top_k=1,
                    similarity_floor=auto_resolve_threshold,
                )

                # Check if the sibling is among the high-similarity matches
                sibling_match = None
                for m in matches:
                    if str(m.get("ticket_id", "")) == sibling_id:
                        sibling_match = m
                        break

                if not sibling_match:
                    # Direct similarity check: embed sibling and compare
                    sibling_embedding = embed_ticket(sibling)
                    if sibling_embedding and resolved_embedding:
                        # Compute cosine similarity
                        similarity = _cosine_similarity(resolved_embedding, sibling_embedding)
                        if similarity < auto_resolve_threshold:
                            continue
                    else:
                        continue

                # Auto-resolve the sibling
                sibling.metadata["resolution_note"] = (
                    f"auto-resolved: likely fixed by {ticket.ticket_id} "
                    f"(batch_resolution, cluster={cluster.cluster_id})"
                )
                sibling.metadata["auto_resolved_by"] = ticket.ticket_id
                sibling.transition(TicketStatus.RESOLVED)
                ticket_store.add(sibling)
                auto_resolved.append(sibling_id)
                resolved_count += 1

                logger.info(
                    "Auto-resolved sibling %s (cluster %s)",
                    sibling_id, cluster.cluster_id,
                )
            except Exception:
                logger.warning(
                    "Failed to auto-resolve sibling %s", sibling_id, exc_info=True,
                )

        # 4. Update cluster status if all resolved
        if auto_resolved:
            try:
                remaining = [
                    tid for tid in cluster.ticket_ids
                    if tid not in auto_resolved and tid != ticket.ticket_id
                ]
                if not remaining:
                    cluster.status = "resolved"
                    graph_store.create_cluster(cluster)
            except Exception:
                pass

    except Exception:
        logger.warning(
            "Batch resolution failed for %s", ticket.ticket_id, exc_info=True,
        )

    return auto_resolved


def discover_cluster(
    ticket: SWETicket,
    embedding: List[float],
    ticket_store: SupabaseTicketStore,
    graph_store: KnowledgeGraphStore,
    *,
    cluster_threshold: float = CLUSTER_THRESHOLD,
) -> Optional[str]:
    """Check if a ticket belongs to an existing cluster, or create a new one.

    Called after embedding a ticket. If there are 2+ similar tickets
    (cosine > CLUSTER_THRESHOLD) that aren't in a cluster yet,
    creates a new cluster.

    Returns cluster_id if the ticket was added to or created a cluster.
    """
    try:
        # Check if already in a cluster
        existing = graph_store.find_ticket_cluster(ticket.ticket_id)
        if existing:
            return existing.cluster_id

        # Find similar resolved/open tickets
        matches = ticket_store.find_similar(
            embedding,
            top_k=10,
            similarity_floor=cluster_threshold,
        )

        # Filter out self
        similar_ids = [
            str(m.get("ticket_id", ""))
            for m in matches
            if str(m.get("ticket_id", "")) != ticket.ticket_id
            and float(m.get("raw_similarity", m.get("similarity", 0))) >= cluster_threshold
        ]

        if len(similar_ids) < 1:
            return None

        # Check if any of the similar tickets are already in a cluster
        for sid in similar_ids:
            existing_cluster = graph_store.find_ticket_cluster(sid)
            if existing_cluster:
                # Add this ticket to the existing cluster
                graph_store.add_ticket_to_cluster(existing_cluster.cluster_id, ticket.ticket_id)
                logger.info(
                    "Added ticket %s to existing cluster %s",
                    ticket.ticket_id, existing_cluster.cluster_id,
                )
                return existing_cluster.cluster_id

        # Create new cluster
        cluster_id = uuid.uuid4().hex[:12]
        all_ids = [ticket.ticket_id] + similar_ids

        cluster = ResolutionCluster(
            cluster_id=cluster_id,
            root_cause=f"Cluster of {len(all_ids)} similar tickets (auto-discovered)",
            primary_module=ticket.source_module or "",
            ticket_ids=all_ids,
        )
        graph_store.create_cluster(cluster)

        logger.info(
            "Created new cluster %s with %d tickets",
            cluster_id, len(all_ids),
        )
        return cluster_id

    except Exception:
        logger.warning(
            "Cluster discovery failed for %s", ticket.ticket_id, exc_info=True,
        )
        return None


def populate_related_tickets(
    ticket: SWETicket,
    graph_store: KnowledgeGraphStore,
) -> List[str]:
    """Populate the ticket's related_tickets field from knowledge edges.

    This activates the previously dead SWETicket.related_tickets field
    by querying the knowledge graph for 'similar' edges.

    Returns the list of related ticket IDs.
    """
    try:
        edges = graph_store.get_edges(ticket.ticket_id, edge_type=EdgeType.SIMILAR)
        related = []
        for edge in edges:
            other = edge.target_id if edge.source_id == ticket.ticket_id else edge.source_id
            if other and other != ticket.ticket_id:
                related.append(other)

        # Deduplicate and update
        ticket.related_tickets = list(dict.fromkeys(related))[:20]  # Cap at 20
        return ticket.related_tickets
    except Exception:
        logger.warning("Failed to populate related_tickets for %s", ticket.ticket_id, exc_info=True)
        return []


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
