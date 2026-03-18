"""
Knowledge graph for SWE Squad — auto-linked tickets, PRs, and modules.

Nodes represent tickets, pull requests, or code modules.
Edges represent relationships (similarity, references, fixes, etc.) and
are weighted by a cosine similarity score when embeddings are available,
or by explicit weight when created manually.

No external dependencies — pure stdlib + optional numpy for fast cosine.
"""

from __future__ import annotations

import enum
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class NodeType(enum.Enum):
    TICKET = "ticket"
    PR = "pr"
    MODULE = "module"


class EdgeType(enum.Enum):
    SIMILAR = "similar"        # semantic similarity via embeddings
    REFERENCES = "references"  # one item explicitly references another
    FIXES = "fixes"            # PR fixes a ticket
    AFFECTS = "affects"        # ticket/PR affects a module
    DEPENDS_ON = "depends_on"  # module depends on another module


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class KnowledgeNode:
    """A single vertex in the knowledge graph."""

    node_type: NodeType
    external_id: str          # ticket ID, PR number, module path, etc.
    title: str
    metadata: Dict[str, object] = field(default_factory=dict)
    embedding: Optional[List[float]] = field(default=None, repr=False)
    node_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class KnowledgeEdge:
    """A directed, weighted edge between two nodes."""

    source_id: str
    target_id: str
    edge_type: EdgeType
    weight: float = 1.0        # [0.0, 1.0]; higher = stronger relationship
    metadata: Dict[str, object] = field(default_factory=dict)
    edge_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Cosine similarity (pure Python, no numpy required)
# ---------------------------------------------------------------------------

def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Return cosine similarity ∈ [-1, 1] between two vectors."""
    if len(a) != len(b):
        raise ValueError(f"Vector length mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Knowledge graph
# ---------------------------------------------------------------------------

class KnowledgeGraph:
    """In-memory knowledge graph with embedding-based auto-linking.

    Usage::

        graph = KnowledgeGraph(similarity_threshold=0.75)
        ticket_node = KnowledgeNode(NodeType.TICKET, "abc123", "NullPointer in auth")
        pr_node = KnowledgeNode(NodeType.PR, "42", "Fix auth NPE")
        graph.add_node(ticket_node)
        graph.add_node(pr_node)
        graph.add_edge(pr_node.node_id, ticket_node.node_id, EdgeType.FIXES)
        # auto-link via embeddings:
        graph.auto_link_by_similarity()
    """

    def __init__(self, similarity_threshold: float = 0.75) -> None:
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be in [0, 1]")
        self._threshold = similarity_threshold
        self._nodes: Dict[str, KnowledgeNode] = {}
        self._edges: Dict[str, KnowledgeEdge] = {}

    # ------------------------------------------------------------------
    # Node management
    # ------------------------------------------------------------------

    def add_node(self, node: KnowledgeNode) -> KnowledgeNode:
        """Add or replace a node. Returns the node."""
        self._nodes[node.node_id] = node
        return node

    def get_node(self, node_id: str) -> Optional[KnowledgeNode]:
        return self._nodes.get(node_id)

    def remove_node(self, node_id: str) -> bool:
        """Remove a node and all edges that reference it. Returns True if found."""
        if node_id not in self._nodes:
            return False
        del self._nodes[node_id]
        stale = [
            eid for eid, e in self._edges.items()
            if e.source_id == node_id or e.target_id == node_id
        ]
        for eid in stale:
            del self._edges[eid]
        return True

    def nodes(self, node_type: Optional[NodeType] = None) -> Iterator[KnowledgeNode]:
        """Iterate over all nodes, optionally filtered by type."""
        for node in self._nodes.values():
            if node_type is None or node.node_type == node_type:
                yield node

    # ------------------------------------------------------------------
    # Edge management
    # ------------------------------------------------------------------

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: EdgeType,
        weight: float = 1.0,
        metadata: Optional[Dict[str, object]] = None,
    ) -> KnowledgeEdge:
        """Add a directed edge. Raises KeyError if either node is missing."""
        if source_id not in self._nodes:
            raise KeyError(f"Source node '{source_id}' not in graph")
        if target_id not in self._nodes:
            raise KeyError(f"Target node '{target_id}' not in graph")
        if not 0.0 <= weight <= 1.0:
            raise ValueError(f"Edge weight {weight!r} must be in [0, 1]")
        edge = KnowledgeEdge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            weight=weight,
            metadata=metadata or {},
        )
        self._edges[edge.edge_id] = edge
        return edge

    def remove_edge(self, edge_id: str) -> bool:
        if edge_id not in self._edges:
            return False
        del self._edges[edge_id]
        return True

    def edges(
        self,
        source_id: Optional[str] = None,
        edge_type: Optional[EdgeType] = None,
    ) -> Iterator[KnowledgeEdge]:
        """Iterate over edges, optionally filtered by source node or type."""
        for edge in self._edges.values():
            if source_id is not None and edge.source_id != source_id:
                continue
            if edge_type is not None and edge.edge_type != edge_type:
                continue
            yield edge

    def neighbors(
        self, node_id: str, edge_type: Optional[EdgeType] = None
    ) -> List[Tuple[KnowledgeNode, KnowledgeEdge]]:
        """Return (node, edge) pairs reachable from *node_id*."""
        result = []
        for edge in self.edges(source_id=node_id, edge_type=edge_type):
            target = self._nodes.get(edge.target_id)
            if target:
                result.append((target, edge))
        return result

    # ------------------------------------------------------------------
    # Embedding-based auto-linking
    # ------------------------------------------------------------------

    def auto_link_by_similarity(
        self,
        node_ids: Optional[Iterable[str]] = None,
        threshold: Optional[float] = None,
    ) -> List[KnowledgeEdge]:
        """Create SIMILAR edges for node pairs whose embeddings exceed threshold.

        Only nodes that carry embeddings are considered.  Already-existing
        SIMILAR edges between the same pair are skipped (idempotent).

        Args:
            node_ids: Optional subset of node IDs to consider. When omitted,
                      all nodes with embeddings are used.
            threshold: Override the graph-level similarity threshold for this
                       call.

        Returns:
            List of newly created edges.
        """
        cutoff = threshold if threshold is not None else self._threshold

        # Gather candidates
        candidate_ids = list(node_ids) if node_ids is not None else list(self._nodes.keys())
        candidates = [
            self._nodes[nid]
            for nid in candidate_ids
            if nid in self._nodes and self._nodes[nid].embedding is not None
        ]

        # Build a set of existing (source, target) SIMILAR pairs to skip duplicates
        existing_similar: set[Tuple[str, str]] = {
            (e.source_id, e.target_id)
            for e in self._edges.values()
            if e.edge_type == EdgeType.SIMILAR
        }

        new_edges: List[KnowledgeEdge] = []
        for i, a in enumerate(candidates):
            for b in candidates[i + 1:]:
                pair = (a.node_id, b.node_id)
                rev_pair = (b.node_id, a.node_id)
                if pair in existing_similar or rev_pair in existing_similar:
                    continue
                assert a.embedding is not None and b.embedding is not None  # narrowing
                score = _cosine_similarity(a.embedding, b.embedding)
                if score >= cutoff:
                    edge = self.add_edge(
                        a.node_id,
                        b.node_id,
                        EdgeType.SIMILAR,
                        weight=round(float(score), 6),
                        metadata={"auto_linked": True},
                    )
                    new_edges.append(edge)
        return new_edges

    # ------------------------------------------------------------------
    # Convenience / stats
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, int]:
        """Return basic graph statistics."""
        by_node_type: Dict[str, int] = {}
        for node in self._nodes.values():
            key = f"nodes_{node.node_type.value}"
            by_node_type[key] = by_node_type.get(key, 0) + 1

        by_edge_type: Dict[str, int] = {}
        for edge in self._edges.values():
            key = f"edges_{edge.edge_type.value}"
            by_edge_type[key] = by_edge_type.get(key, 0) + 1

        return {
            "nodes_total": len(self._nodes),
            "edges_total": len(self._edges),
            **by_node_type,
            **by_edge_type,
        }

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes
