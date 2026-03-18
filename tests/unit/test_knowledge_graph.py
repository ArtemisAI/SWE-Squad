"""Tests for the knowledge graph feature (issue #50).

Covers: models, knowledge store, graph scoring, batch resolution,
edge extraction, and PR sync utilities.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, PropertyMock

from src.swe_team.models import (
    CodeModule,
    EdgeType,
    KnowledgeEdge,
    PRNode,
    ResolutionCluster,
    SWETicket,
    TicketSeverity,
    TicketStatus,
)


# ──────────────────────────────────────────────────────────────────────────
# 1. Model tests
# ──────────────────────────────────────────────────────────────────────────

class TestEdgeType(unittest.TestCase):
    def test_all_values(self):
        assert EdgeType.SIMILAR.value == "similar"
        assert EdgeType.TOUCHES_MODULE.value == "touches_module"
        assert EdgeType.BLOCKS.value == "blocks"
        assert EdgeType.RESOLVES.value == "resolves"
        assert EdgeType.CONFLICTS_WITH.value == "conflicts_with"
        assert EdgeType.CAUSED_REGRESSION.value == "caused_regression"

    def test_from_string(self):
        assert EdgeType("similar") == EdgeType.SIMILAR
        assert EdgeType("resolves") == EdgeType.RESOLVES


class TestKnowledgeEdge(unittest.TestCase):
    def test_to_dict_from_dict_roundtrip(self):
        edge = KnowledgeEdge(
            source_id="ticket-abc",
            target_id="ticket-def",
            edge_type=EdgeType.SIMILAR,
            confidence=0.92,
            discovered_by="embedding",
            metadata={"note": "test"},
        )
        d = edge.to_dict()
        assert d["edge_type"] == "similar"
        assert d["confidence"] == 0.92

        restored = KnowledgeEdge.from_dict(d)
        assert restored.source_id == edge.source_id
        assert restored.edge_type == EdgeType.SIMILAR
        assert restored.confidence == 0.92
        assert restored.metadata == {"note": "test"}

    def test_defaults(self):
        edge = KnowledgeEdge(source_id="a", target_id="b", edge_type=EdgeType.BLOCKS)
        assert edge.confidence == 0.0
        assert edge.discovered_by == ""
        assert edge.metadata == {}

    def test_discovered_at_populated(self):
        edge = KnowledgeEdge(source_id="a", target_id="b", edge_type=EdgeType.SIMILAR)
        assert edge.discovered_at  # should be non-empty ISO string

    def test_from_dict_missing_optional_fields(self):
        d = {"source_id": "a", "target_id": "b"}
        edge = KnowledgeEdge.from_dict(d)
        assert edge.edge_type == EdgeType.SIMILAR  # default
        assert edge.confidence == 0.0
        assert edge.metadata == {}


class TestCodeModule(unittest.TestCase):
    def test_roundtrip(self):
        mod = CodeModule(module_id="security.py", repo="Org/Repo", file_path="src/security.py")
        d = mod.to_dict()
        restored = CodeModule.from_dict(d)
        assert restored.module_id == "security.py"
        assert restored.repo == "Org/Repo"

    def test_defaults(self):
        mod = CodeModule(module_id="test.py")
        assert mod.repo == ""
        assert mod.file_path == ""
        assert mod.metadata == {}

    def test_last_seen_populated(self):
        mod = CodeModule(module_id="test.py")
        assert mod.last_seen  # should be non-empty ISO string


class TestResolutionCluster(unittest.TestCase):
    def test_roundtrip(self):
        cluster = ResolutionCluster(
            cluster_id="clust-001",
            root_cause="Session token expired",
            primary_module="auth.py",
            ticket_ids=["t1", "t2", "t3"],
            status="investigating",
        )
        d = cluster.to_dict()
        assert d["ticket_ids"] == ["t1", "t2", "t3"]
        assert d["status"] == "investigating"

        restored = ResolutionCluster.from_dict(d)
        assert restored.cluster_id == "clust-001"
        assert len(restored.ticket_ids) == 3

    def test_defaults(self):
        cluster = ResolutionCluster(cluster_id="c1")
        assert cluster.status == "open"
        assert cluster.ticket_ids == []

    def test_timestamps_populated(self):
        cluster = ResolutionCluster(cluster_id="c1")
        assert cluster.created_at
        assert cluster.updated_at


class TestPRNode(unittest.TestCase):
    def test_roundtrip(self):
        pr = PRNode(
            pr_id="Org/Repo#42",
            repo="Org/Repo",
            number=42,
            branch="fix/auth-bug",
            title="Fix auth token expiry",
            status="open",
            author="dev1",
            files_changed=["src/auth.py", "src/session.py"],
            ticket_ids=["abc123"],
            review_status="pending",
        )
        d = pr.to_dict()
        assert d["number"] == 42
        assert len(d["files_changed"]) == 2

        restored = PRNode.from_dict(d)
        assert restored.pr_id == "Org/Repo#42"
        assert restored.files_changed == ["src/auth.py", "src/session.py"]

    def test_defaults(self):
        pr = PRNode(pr_id="Org/Repo#1")
        assert pr.status == "open"
        assert pr.review_status == "pending"
        assert pr.merged_at is None
        assert pr.files_changed == []

    def test_from_dict_number_coerced_to_int(self):
        d = {"pr_id": "R#1", "number": "42"}
        pr = PRNode.from_dict(d)
        assert pr.number == 42
        assert isinstance(pr.number, int)


# ──────────────────────────────────────────────────────────────────────────
# 2. Knowledge store tests (mock HTTP)
# ──────────────────────────────────────────────────────────────────────────

class TestKnowledgeGraphStore(unittest.TestCase):
    """Test KnowledgeGraphStore with mocked HTTP requests."""

    def setUp(self):
        from src.swe_team.knowledge_store import KnowledgeGraphStore
        self.store = KnowledgeGraphStore.__new__(KnowledgeGraphStore)
        self.store._url = "https://test.supabase.co"
        self.store._key = "test-key"
        self.store._team_id = "test-team"
        self.store._rest = "https://test.supabase.co/rest/v1"
        self.store._headers = {
            "apikey": "test-key",
            "Authorization": "Bearer test-key",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def test_create_edge(self):
        """Test edge creation calls _request with correct payload."""
        edge = KnowledgeEdge(
            source_id="t1", target_id="t2",
            edge_type=EdgeType.SIMILAR, confidence=0.85,
            discovered_by="embedding",
        )
        with patch.object(self.store, '_request', return_value=[edge.to_dict()]) as mock_req:
            self.store.create_edge(edge)
            mock_req.assert_called_once()
            call_args = mock_req.call_args
            assert call_args[0][0] == "POST"  # method
            assert "/knowledge_edges" in call_args[0][1]  # path

    def test_count_edges_rpc(self):
        """Test count_edges uses the RPC endpoint."""
        with patch.object(self.store, '_request', return_value=5) as mock_req:
            count = self.store.count_edges("ticket-abc")
            assert count == 5
            mock_req.assert_called_once()
            call_args = mock_req.call_args
            assert "/rpc/count_edges" in call_args[0][1]

    def test_count_edges_list_response(self):
        """Test count_edges handles list-of-dict response from RPC."""
        with patch.object(self.store, '_request', return_value=[{"count": 7}]):
            count = self.store.count_edges("ticket-abc")
            assert count == 7

    def test_count_edges_error_returns_zero(self):
        """Test count_edges returns 0 on failure."""
        with patch.object(self.store, '_request', side_effect=Exception("timeout")):
            count = self.store.count_edges("ticket-abc")
            assert count == 0

    def test_get_edges_rpc(self):
        """Test get_edges uses the get_node_edges RPC."""
        edge_data = [{
            "source_id": "t1", "target_id": "t2",
            "edge_type": "similar", "confidence": 0.9,
            "discovered_at": "2026-01-01T00:00:00+00:00",
            "discovered_by": "embedding",
        }]
        with patch.object(self.store, '_request', return_value=edge_data) as mock_req:
            edges = self.store.get_edges("t1")
            assert len(edges) == 1
            assert edges[0].edge_type == EdgeType.SIMILAR

    def test_get_edges_error_returns_empty(self):
        """Test get_edges returns empty list on failure."""
        with patch.object(self.store, '_request', side_effect=Exception("timeout")):
            edges = self.store.get_edges("t1")
            assert edges == []

    def test_upsert_pr_node(self):
        """Test PR node upsert."""
        pr = PRNode(pr_id="Org/Repo#1", repo="Org/Repo", number=1)
        with patch.object(self.store, '_request', return_value=[pr.to_dict()]) as mock_req:
            self.store.upsert_pr_node(pr)
            mock_req.assert_called_once()
            assert mock_req.call_args[0][0] == "POST"
            assert "/pr_nodes" in mock_req.call_args[0][1]

    def test_list_open_prs(self):
        """Test listing open PRs."""
        pr_data = [{
            "pr_id": "Org/Repo#1", "repo": "Org/Repo", "number": 1,
            "status": "open", "review_status": "pending",
            "files_changed": "[]", "ticket_ids": "[]",
            "metadata": "{}",
        }]
        with patch.object(self.store, '_request', return_value=pr_data):
            prs = self.store.list_open_prs()
            assert len(prs) == 1
            assert prs[0].status == "open"

    def test_list_open_prs_with_repo_filter(self):
        """Test listing open PRs filtered by repo."""
        with patch.object(self.store, '_request', return_value=[]) as mock_req:
            self.store.list_open_prs(repo="Org/Repo")
            params = mock_req.call_args[1].get("params") or mock_req.call_args[0][2] if len(mock_req.call_args[0]) > 2 else mock_req.call_args[1].get("params")
            # Just verify the call happened
            mock_req.assert_called_once()

    def test_find_conflicting_prs(self):
        """Test conflict detection between PRs."""
        pr_a = PRNode(pr_id="R#1", repo="Org/R", files_changed=["a.py", "b.py"], status="open")
        pr_b_data = {
            "pr_id": "R#2", "status": "open", "files_changed": json.dumps(["b.py", "c.py"]),
            "repo": "Org/R", "number": 2, "ticket_ids": "[]", "metadata": "{}",
            "review_status": "pending",
        }
        with patch.object(self.store, '_request', return_value=[pr_b_data]):
            conflicts = self.store.find_conflicting_prs(pr_a)
            assert len(conflicts) == 1
            assert "b.py" in conflicts[0].files_changed

    def test_find_conflicting_prs_no_files(self):
        """PR with no files_changed should return no conflicts."""
        pr = PRNode(pr_id="R#1", files_changed=[])
        conflicts = self.store.find_conflicting_prs(pr)
        assert conflicts == []

    def test_upsert_module(self):
        """Test code module upsert."""
        mod = CodeModule(module_id="auth.py", repo="Org/Repo")
        with patch.object(self.store, '_request', return_value=[mod.to_dict()]) as mock_req:
            self.store.upsert_module(mod)
            mock_req.assert_called_once()
            assert "/code_modules" in mock_req.call_args[0][1]

    def test_find_ticket_cluster_rpc(self):
        """Test cluster lookup via RPC."""
        cluster_data = [{
            "cluster_id": "c1", "root_cause": "bug",
            "primary_module": "auth", "ticket_ids": ["t1", "t2"],
            "status": "open",
        }]
        with patch.object(self.store, '_request', return_value=cluster_data):
            cluster = self.store.find_ticket_cluster("t1")
            assert cluster is not None
            assert cluster.cluster_id == "c1"
            assert "t1" in cluster.ticket_ids

    def test_find_ticket_cluster_empty(self):
        """Test cluster lookup returns None when not found."""
        with patch.object(self.store, '_request', return_value=[]):
            cluster = self.store.find_ticket_cluster("t-nonexistent")
            assert cluster is None

    def test_find_ticket_cluster_error(self):
        """Test cluster lookup returns None on error."""
        with patch.object(self.store, '_request', side_effect=Exception("err")):
            cluster = self.store.find_ticket_cluster("t1")
            assert cluster is None

    def test_create_edges_batch(self):
        """Test batch edge creation."""
        edges = [
            KnowledgeEdge(source_id="t1", target_id="t2", edge_type=EdgeType.SIMILAR),
            KnowledgeEdge(source_id="t1", target_id="t3", edge_type=EdgeType.SIMILAR),
        ]
        with patch.object(self.store, '_request', return_value=[{}, {}]) as mock_req:
            count = self.store.create_edges_batch(edges)
            assert count == 2

    def test_create_edges_batch_empty(self):
        """Test batch edge creation with empty list."""
        count = self.store.create_edges_batch([])
        assert count == 0

    def test_get_cluster(self):
        """Test get_cluster by ID."""
        cluster_data = [{
            "cluster_id": "c1", "root_cause": "bug",
            "ticket_ids": json.dumps(["t1"]),
            "metadata": "{}",
        }]
        with patch.object(self.store, '_request', return_value=cluster_data):
            cluster = self.store.get_cluster("c1")
            assert cluster is not None
            assert cluster.cluster_id == "c1"

    def test_create_cluster(self):
        """Test cluster creation."""
        cluster = ResolutionCluster(cluster_id="c1", ticket_ids=["t1", "t2"])
        with patch.object(self.store, '_request', return_value=None) as mock_req:
            self.store.create_cluster(cluster)
            mock_req.assert_called_once()
            assert "/resolution_clusters" in mock_req.call_args[0][1]

    def test_add_ticket_to_cluster(self):
        """Test adding a ticket to an existing cluster."""
        existing = ResolutionCluster(cluster_id="c1", ticket_ids=["t1"])
        with patch.object(self.store, 'get_cluster', return_value=existing):
            with patch.object(self.store, '_request', return_value=None) as mock_req:
                self.store.add_ticket_to_cluster("c1", "t2")
                mock_req.assert_called_once()
                assert mock_req.call_args[0][0] == "PATCH"

    def test_add_ticket_to_cluster_already_present(self):
        """Test adding a ticket that's already in the cluster is a no-op."""
        existing = ResolutionCluster(cluster_id="c1", ticket_ids=["t1"])
        with patch.object(self.store, 'get_cluster', return_value=existing):
            with patch.object(self.store, '_request') as mock_req:
                self.store.add_ticket_to_cluster("c1", "t1")
                mock_req.assert_not_called()

    def test_add_ticket_to_nonexistent_cluster(self):
        """Test adding a ticket to a cluster that doesn't exist is a no-op."""
        with patch.object(self.store, 'get_cluster', return_value=None):
            with patch.object(self.store, '_request') as mock_req:
                self.store.add_ticket_to_cluster("c-missing", "t1")
                mock_req.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────
# 3. Graph scoring tests
# ──────────────────────────────────────────────────────────────────────────

class TestGraphScoring(unittest.TestCase):

    def _make_ticket(self, severity="high", age_hours=0, attempts=0, is_regression=False):
        t = SWETicket(
            title="Test ticket",
            description="desc",
            severity=TicketSeverity(severity),
        )
        if age_hours > 0:
            created = datetime.now(timezone.utc) - timedelta(hours=age_hours)
            t.created_at = created.isoformat()
        if attempts > 0:
            t.metadata["attempts"] = [{"n": i} for i in range(attempts)]
        if is_regression:
            t.metadata["is_regression"] = True
        return t

    def test_severity_ordering_without_graph(self):
        from src.swe_team.graph_scoring import priority_score
        critical = self._make_ticket("critical")
        high = self._make_ticket("high")
        medium = self._make_ticket("medium")
        low = self._make_ticket("low")

        assert priority_score(critical) > priority_score(high)
        assert priority_score(high) > priority_score(medium)
        assert priority_score(medium) > priority_score(low)

    def test_age_increases_score(self):
        from src.swe_team.graph_scoring import priority_score
        fresh = self._make_ticket("high", age_hours=1)
        old = self._make_ticket("high", age_hours=100)
        assert priority_score(old) > priority_score(fresh)

    def test_failed_attempts_decrease_score(self):
        from src.swe_team.graph_scoring import priority_score
        no_fails = self._make_ticket("high")
        many_fails = self._make_ticket("high", attempts=5)
        assert priority_score(no_fails) > priority_score(many_fails)

    def test_regression_boost(self):
        from src.swe_team.graph_scoring import priority_score
        normal = self._make_ticket("high")
        regression = self._make_ticket("high", is_regression=True)
        assert priority_score(regression) > priority_score(normal)

    def test_rank_tickets_sorts_correctly(self):
        from src.swe_team.graph_scoring import rank_tickets
        tickets = [
            self._make_ticket("low"),
            self._make_ticket("critical"),
            self._make_ticket("medium"),
            self._make_ticket("high"),
        ]
        ranked = rank_tickets(tickets)
        severities = [t.severity.value for t in ranked]
        assert severities[0] == "critical"
        assert severities[-1] == "low"

    def test_rank_tickets_with_mock_graph(self):
        """Test that graph store enhances scoring."""
        from src.swe_team.graph_scoring import priority_score

        mock_store = MagicMock()
        mock_store.count_edges.return_value = 5  # Many similar tickets
        mock_store.find_ticket_cluster.return_value = ResolutionCluster(
            cluster_id="c1", ticket_ids=["t1", "t2", "t3", "t4"]
        )
        mock_store.get_edges.return_value = []  # No PR resolving this

        ticket = self._make_ticket("high")
        score_with_graph = priority_score(ticket, graph_store=mock_store)
        score_without = priority_score(ticket)

        assert score_with_graph > score_without

    def test_pr_exists_deprioritises(self):
        """When a PR already resolves this ticket, score drops."""
        from src.swe_team.graph_scoring import priority_score

        mock_store = MagicMock()
        mock_store.count_edges.return_value = 0
        mock_store.find_ticket_cluster.return_value = None
        mock_store.get_edges.return_value = [
            KnowledgeEdge(source_id="PR#1", target_id="t1", edge_type=EdgeType.RESOLVES)
        ]

        ticket = self._make_ticket("high")
        score_with_pr = priority_score(ticket, graph_store=mock_store)
        score_without_pr = priority_score(ticket)

        assert score_with_pr < score_without_pr

    def test_score_is_rounded(self):
        """priority_score should return a float rounded to 4 decimal places."""
        from src.swe_team.graph_scoring import priority_score
        ticket = self._make_ticket("high", age_hours=50)
        score = priority_score(ticket)
        # Check that rounding occurred (at most 4 decimal places)
        assert score == round(score, 4)

    def test_rank_tickets_empty_list(self):
        from src.swe_team.graph_scoring import rank_tickets
        assert rank_tickets([]) == []

    def test_similar_count_increases_score(self):
        """More similar tickets = higher impact, higher priority."""
        from src.swe_team.graph_scoring import priority_score

        mock_store_few = MagicMock()
        mock_store_few.count_edges.return_value = 1
        mock_store_few.find_ticket_cluster.return_value = None
        mock_store_few.get_edges.return_value = []

        mock_store_many = MagicMock()
        mock_store_many.count_edges.return_value = 10
        mock_store_many.find_ticket_cluster.return_value = None
        mock_store_many.get_edges.return_value = []

        ticket = self._make_ticket("high")
        score_few = priority_score(ticket, graph_store=mock_store_few)
        score_many = priority_score(ticket, graph_store=mock_store_many)
        assert score_many > score_few


# ──────────────────────────────────────────────────────────────────────────
# 4. Batch resolution tests
# ──────────────────────────────────────────────────────────────────────────

class TestCosineSimilarity(unittest.TestCase):
    def test_identical_vectors(self):
        from src.swe_team.batch_resolution import _cosine_similarity
        v = [1.0, 2.0, 3.0]
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        from src.swe_team.batch_resolution import _cosine_similarity
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(_cosine_similarity(a, b)) < 1e-6

    def test_opposite_vectors(self):
        from src.swe_team.batch_resolution import _cosine_similarity
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert abs(_cosine_similarity(a, b) - (-1.0)) < 1e-6

    def test_empty_vectors(self):
        from src.swe_team.batch_resolution import _cosine_similarity
        assert _cosine_similarity([], []) == 0.0

    def test_mismatched_length(self):
        from src.swe_team.batch_resolution import _cosine_similarity
        assert _cosine_similarity([1.0], [1.0, 2.0]) == 0.0

    def test_zero_vector(self):
        from src.swe_team.batch_resolution import _cosine_similarity
        assert _cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_scaled_vectors_same_direction(self):
        from src.swe_team.batch_resolution import _cosine_similarity
        a = [1.0, 2.0, 3.0]
        b = [2.0, 4.0, 6.0]
        assert abs(_cosine_similarity(a, b) - 1.0) < 1e-6


class TestPopulateRelatedTickets(unittest.TestCase):
    def test_populates_from_edges(self):
        from src.swe_team.batch_resolution import populate_related_tickets

        mock_store = MagicMock()
        mock_store.get_edges.return_value = [
            KnowledgeEdge(source_id="t1", target_id="t2", edge_type=EdgeType.SIMILAR),
            KnowledgeEdge(source_id="t3", target_id="t1", edge_type=EdgeType.SIMILAR),
        ]

        ticket = SWETicket(title="test", description="test", ticket_id="t1")
        assert ticket.related_tickets == []

        related = populate_related_tickets(ticket, mock_store)
        assert "t2" in related
        assert "t3" in related
        assert "t1" not in related  # shouldn't include self

    def test_handles_store_error(self):
        from src.swe_team.batch_resolution import populate_related_tickets

        mock_store = MagicMock()
        mock_store.get_edges.side_effect = Exception("connection error")

        ticket = SWETicket(title="test", description="test")
        result = populate_related_tickets(ticket, mock_store)
        assert result == []

    def test_updates_ticket_related_tickets_field(self):
        from src.swe_team.batch_resolution import populate_related_tickets

        mock_store = MagicMock()
        mock_store.get_edges.return_value = [
            KnowledgeEdge(source_id="t1", target_id="t2", edge_type=EdgeType.SIMILAR),
        ]

        ticket = SWETicket(title="test", description="test", ticket_id="t1")
        populate_related_tickets(ticket, mock_store)
        assert ticket.related_tickets == ["t2"]

    def test_deduplicates_related_tickets(self):
        from src.swe_team.batch_resolution import populate_related_tickets

        mock_store = MagicMock()
        # Two edges to the same target
        mock_store.get_edges.return_value = [
            KnowledgeEdge(source_id="t1", target_id="t2", edge_type=EdgeType.SIMILAR),
            KnowledgeEdge(source_id="t2", target_id="t1", edge_type=EdgeType.SIMILAR),
        ]

        ticket = SWETicket(title="test", description="test", ticket_id="t1")
        related = populate_related_tickets(ticket, mock_store)
        assert related.count("t2") == 1

    def test_caps_at_20(self):
        from src.swe_team.batch_resolution import populate_related_tickets

        mock_store = MagicMock()
        # Generate 25 unique edges
        mock_store.get_edges.return_value = [
            KnowledgeEdge(source_id="t1", target_id=f"t{i}", edge_type=EdgeType.SIMILAR)
            for i in range(2, 27)
        ]

        ticket = SWETicket(title="test", description="test", ticket_id="t1")
        related = populate_related_tickets(ticket, mock_store)
        assert len(related) <= 20


class TestDiscoverCluster(unittest.TestCase):
    def test_returns_existing_cluster(self):
        from src.swe_team.batch_resolution import discover_cluster

        mock_graph = MagicMock()
        mock_graph.find_ticket_cluster.return_value = ResolutionCluster(
            cluster_id="c-existing", ticket_ids=["t1"]
        )
        mock_ticket_store = MagicMock()

        ticket = SWETicket(title="test", description="test", ticket_id="t1")
        result = discover_cluster(ticket, [0.1, 0.2], mock_ticket_store, mock_graph)
        assert result == "c-existing"

    def test_adds_to_existing_cluster_of_similar(self):
        from src.swe_team.batch_resolution import discover_cluster

        mock_graph = MagicMock()
        # First call: ticket not in a cluster. Second call: similar ticket IS in a cluster.
        mock_graph.find_ticket_cluster.side_effect = [
            None,  # t1 not in any cluster
            ResolutionCluster(cluster_id="c-other", ticket_ids=["t2"]),  # t2 is
        ]
        mock_ticket_store = MagicMock()
        mock_ticket_store.find_similar.return_value = [
            {"ticket_id": "t2", "raw_similarity": 0.90},
        ]

        ticket = SWETicket(title="test", description="test", ticket_id="t1")
        result = discover_cluster(ticket, [0.1, 0.2], mock_ticket_store, mock_graph)
        assert result == "c-other"
        mock_graph.add_ticket_to_cluster.assert_called_once_with("c-other", "t1")

    def test_creates_new_cluster(self):
        from src.swe_team.batch_resolution import discover_cluster

        mock_graph = MagicMock()
        # No existing clusters for any ticket
        mock_graph.find_ticket_cluster.return_value = None
        mock_ticket_store = MagicMock()
        mock_ticket_store.find_similar.return_value = [
            {"ticket_id": "t2", "raw_similarity": 0.90},
        ]

        ticket = SWETicket(title="test", description="test", ticket_id="t1")
        result = discover_cluster(ticket, [0.1, 0.2], mock_ticket_store, mock_graph)
        assert result is not None
        mock_graph.create_cluster.assert_called_once()

    def test_no_cluster_when_no_similar(self):
        from src.swe_team.batch_resolution import discover_cluster

        mock_graph = MagicMock()
        mock_graph.find_ticket_cluster.return_value = None
        mock_ticket_store = MagicMock()
        mock_ticket_store.find_similar.return_value = []

        ticket = SWETicket(title="test", description="test", ticket_id="t1")
        result = discover_cluster(ticket, [0.1, 0.2], mock_ticket_store, mock_graph)
        assert result is None

    def test_error_returns_none(self):
        from src.swe_team.batch_resolution import discover_cluster

        mock_graph = MagicMock()
        mock_graph.find_ticket_cluster.side_effect = Exception("db error")
        mock_ticket_store = MagicMock()

        ticket = SWETicket(title="test", description="test", ticket_id="t1")
        result = discover_cluster(ticket, [0.1, 0.2], mock_ticket_store, mock_graph)
        assert result is None


# ──────────────────────────────────────────────────────────────────────────
# 5. Edge extraction tests
# ──────────────────────────────────────────────────────────────────────────

class TestExtractEdgesFromTicket(unittest.TestCase):
    def test_similar_edges_above_threshold(self):
        from src.swe_team.embeddings import extract_edges_from_ticket

        ticket = SWETicket(title="test", description="desc", ticket_id="t1")
        similar = [
            {"ticket_id": "t2", "raw_similarity": 0.85},
            {"ticket_id": "t3", "raw_similarity": 0.75},  # below default 0.80
        ]

        edges = extract_edges_from_ticket(ticket, similar)
        assert len(edges) == 1
        assert edges[0].target_id == "t2"
        assert edges[0].edge_type.value == "similar"

    def test_module_edge_from_source_module(self):
        from src.swe_team.embeddings import extract_edges_from_ticket

        ticket = SWETicket(
            title="test", description="desc", ticket_id="t1",
            source_module="security.py",
        )

        edges = extract_edges_from_ticket(ticket)
        module_edges = [e for e in edges if e.edge_type.value == "touches_module"]
        assert len(module_edges) == 1
        assert module_edges[0].target_id == "security.py"

    def test_module_edge_from_memory_facts(self):
        from src.swe_team.embeddings import extract_edges_from_ticket

        ticket = SWETicket(title="test", description="desc", ticket_id="t1")
        ticket.metadata["memory_facts"] = (
            "Root cause: Session expired\n"
            "Fix applied: Added refresh logic\n"
            "Affected module: auth_handler.py\n"
            "Tags: session, auth"
        )

        edges = extract_edges_from_ticket(ticket)
        module_edges = [e for e in edges if e.edge_type.value == "touches_module"]
        assert len(module_edges) == 1
        assert module_edges[0].target_id == "auth_handler.py"

    def test_no_edges_for_unknown_module(self):
        from src.swe_team.embeddings import extract_edges_from_ticket

        ticket = SWETicket(
            title="test", description="desc",
            source_module="unknown",
        )
        edges = extract_edges_from_ticket(ticket)
        assert len(edges) == 0

    def test_excludes_self_reference(self):
        from src.swe_team.embeddings import extract_edges_from_ticket

        ticket = SWETicket(title="test", description="desc", ticket_id="t1")
        similar = [{"ticket_id": "t1", "raw_similarity": 0.99}]  # self-reference

        edges = extract_edges_from_ticket(ticket, similar)
        similar_edges = [e for e in edges if e.edge_type.value == "similar"]
        assert len(similar_edges) == 0

    def test_custom_threshold(self):
        from src.swe_team.embeddings import extract_edges_from_ticket

        ticket = SWETicket(title="test", description="desc", ticket_id="t1")
        similar = [{"ticket_id": "t2", "raw_similarity": 0.82}]

        # Default threshold is 0.80 — should include
        edges = extract_edges_from_ticket(ticket, similar, similarity_edge_threshold=0.80)
        assert len(edges) == 1

        # Higher threshold — should exclude
        edges = extract_edges_from_ticket(ticket, similar, similarity_edge_threshold=0.90)
        assert len(edges) == 0

    def test_no_similar_no_module(self):
        from src.swe_team.embeddings import extract_edges_from_ticket

        ticket = SWETicket(title="test", description="desc", ticket_id="t1")
        edges = extract_edges_from_ticket(ticket)
        assert edges == []

    def test_confidence_matches_similarity(self):
        from src.swe_team.embeddings import extract_edges_from_ticket

        ticket = SWETicket(title="test", description="desc", ticket_id="t1")
        similar = [{"ticket_id": "t2", "raw_similarity": 0.88}]

        edges = extract_edges_from_ticket(ticket, similar)
        assert edges[0].confidence == 0.88

    def test_discovered_by_is_embedding(self):
        from src.swe_team.embeddings import extract_edges_from_ticket

        ticket = SWETicket(title="test", description="desc", ticket_id="t1")
        similar = [{"ticket_id": "t2", "raw_similarity": 0.85}]

        edges = extract_edges_from_ticket(ticket, similar)
        assert edges[0].discovered_by == "embedding"

    def test_module_edge_discovered_by_fact_extraction(self):
        from src.swe_team.embeddings import extract_edges_from_ticket

        ticket = SWETicket(
            title="test", description="desc", ticket_id="t1",
            source_module="auth.py",
        )
        edges = extract_edges_from_ticket(ticket)
        module_edges = [e for e in edges if e.edge_type == EdgeType.TOUCHES_MODULE]
        assert module_edges[0].discovered_by == "fact_extraction"


# ──────────────────────────────────────────────────────────────────────────
# 6. PR sync utility tests
# ──────────────────────────────────────────────────────────────────────────

class TestPRSyncUtilities(unittest.TestCase):
    def test_extract_ticket_refs_from_branch(self):
        from scripts.ops.pr_sync import extract_ticket_refs

        refs = extract_ticket_refs("swe-fix/ticket-abc123def0")
        assert "abc123def0" in refs

    def test_extract_ticket_refs_fix_branch(self):
        from scripts.ops.pr_sync import extract_ticket_refs

        refs = extract_ticket_refs("fix/deadbeef01")
        assert "deadbeef01" in refs

    def test_extract_ticket_refs_from_title(self):
        from scripts.ops.pr_sync import extract_ticket_refs

        refs = extract_ticket_refs("main", title="fixes ticket-abc123def0")
        assert "abc123def0" in refs

    def test_extract_ticket_refs_no_match(self):
        from scripts.ops.pr_sync import extract_ticket_refs

        refs = extract_ticket_refs("feature/new-ui")
        assert refs == []

    def test_extract_ticket_refs_from_body(self):
        from scripts.ops.pr_sync import extract_ticket_refs

        refs = extract_ticket_refs("main", body="closes ticket-aabbccddee")
        assert "aabbccddee" in refs

    def test_extract_ticket_refs_resolves_keyword(self):
        from scripts.ops.pr_sync import extract_ticket_refs

        refs = extract_ticket_refs("main", title="resolves ticket-1234abcd56")
        assert "1234abcd56" in refs

    def test_extract_ticket_refs_deduplicates(self):
        from scripts.ops.pr_sync import extract_ticket_refs

        # Same ID in both branch and title
        refs = extract_ticket_refs("fix/abc123def0", title="fixes ticket-abc123def0")
        assert refs.count("abc123def0") == 1

    def test_module_id_from_path(self):
        from scripts.ops.pr_sync import module_id_from_path

        assert module_id_from_path("src/application/security.py") == "security.py"
        assert module_id_from_path("auth.py") == "auth.py"
        assert module_id_from_path("deep/nested/module.py") == "module.py"

    def test_module_id_from_path_non_python(self):
        from scripts.ops.pr_sync import module_id_from_path

        assert module_id_from_path("config/settings.yaml") == "settings.yaml"
        assert module_id_from_path("Dockerfile") == "Dockerfile"


# ──────────────────────────────────────────────────────────────────────────
# 7. Self-heal PR health check tests
# ──────────────────────────────────────────────────────────────────────────

class TestSelfHealPRHealth(unittest.TestCase):
    """Test check_pr_health from self_heal.py.

    KnowledgeGraphStore is imported locally inside check_pr_health(),
    so we patch it at the source module level.
    """

    @patch.dict("os.environ", {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_ANON_KEY": "k",
        "SWE_TEAM_ID": "test",
    })
    def test_stale_pr_detected(self):
        """PRs open >48h without review should trigger alert."""
        old_pr = PRNode(
            pr_id="Org/R#1",
            title="Old PR",
            status="open",
            review_status="pending",
            created_at=(datetime.now(timezone.utc) - timedelta(hours=72)).isoformat(),
        )

        with patch("src.swe_team.knowledge_store.KnowledgeGraphStore") as MockStore:
            instance = MockStore.return_value
            instance.list_open_prs.return_value = [old_pr]
            instance.get_edges.return_value = []

            from scripts.ops.self_heal import check_pr_health
            alerts = check_pr_health()
            assert len(alerts) >= 1
            assert "Stale PR" in alerts[0]

    @patch.dict("os.environ", {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_ANON_KEY": "k",
        "SWE_TEAM_ID": "test",
    })
    def test_fresh_pr_no_alert(self):
        """Recent PRs should not trigger alerts."""
        fresh_pr = PRNode(
            pr_id="Org/R#2",
            title="Fresh PR",
            status="open",
            review_status="pending",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        with patch("src.swe_team.knowledge_store.KnowledgeGraphStore") as MockStore:
            instance = MockStore.return_value
            instance.list_open_prs.return_value = [fresh_pr]
            instance.get_edges.return_value = []

            from scripts.ops.self_heal import check_pr_health
            alerts = check_pr_health()
            assert len(alerts) == 0

    @patch.dict("os.environ", {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_ANON_KEY": "k",
        "SWE_TEAM_ID": "test",
    })
    def test_conflicting_prs_detected(self):
        """PRs with CONFLICTS_WITH edges should trigger alert."""
        pr = PRNode(
            pr_id="Org/R#3",
            title="Conflict PR",
            status="open",
            review_status="approved",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        conflict_edge = KnowledgeEdge(
            source_id="Org/R#3",
            target_id="Org/R#4",
            edge_type=EdgeType.CONFLICTS_WITH,
        )

        with patch("src.swe_team.knowledge_store.KnowledgeGraphStore") as MockStore:
            instance = MockStore.return_value
            instance.list_open_prs.return_value = [pr]
            instance.get_edges.return_value = [conflict_edge]

            from scripts.ops.self_heal import check_pr_health
            alerts = check_pr_health()
            conflict_alerts = [a for a in alerts if "conflict" in a.lower()]
            assert len(conflict_alerts) >= 1

    @patch.dict("os.environ", {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_ANON_KEY": "k",
        "SWE_TEAM_ID": "test",
    })
    def test_no_open_prs_no_alerts(self):
        """No open PRs should produce no alerts."""
        with patch("src.swe_team.knowledge_store.KnowledgeGraphStore") as MockStore:
            instance = MockStore.return_value
            instance.list_open_prs.return_value = []

            from scripts.ops.self_heal import check_pr_health
            alerts = check_pr_health()
            assert alerts == []
