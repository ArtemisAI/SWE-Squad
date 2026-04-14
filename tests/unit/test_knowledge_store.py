"""Unit tests for src/swe_team/knowledge_store.py — no real network calls."""
from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.knowledge_store import KnowledgeGraphStore
from src.swe_team.models import (
    CodeModule,
    EdgeType,
    KnowledgeEdge,
    PRNode,
    ResolutionCluster,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(url: str = "https://test.supabase.co") -> KnowledgeGraphStore:
    return KnowledgeGraphStore(
        supabase_url=url,
        supabase_key="test-key",
        team_id="test-team",
    )


def _mock_response(data: Any) -> MagicMock:
    raw = json.dumps(data).encode() if data is not None else b""
    mock_resp = MagicMock()
    mock_resp.read.return_value = raw
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_edge(
    source: str = "ticket-001",
    target: str = "ticket-002",
    edge_type: EdgeType = EdgeType.SIMILAR,
    confidence: float = 0.9,
) -> KnowledgeEdge:
    return KnowledgeEdge(
        source_id=source,
        target_id=target,
        edge_type=edge_type,
        confidence=confidence,
        discovered_by="test",
    )


def _make_pr(pr_id: str = "your-org/example-app#42") -> PRNode:
    return PRNode(
        pr_id=pr_id,
        repo="your-org/example-app",
        number=42,
        title="Fix something",
        files_changed=["src/foo.py", "src/bar.py"],
    )


def _make_cluster(cluster_id: str = "cluster-001") -> ResolutionCluster:
    return ResolutionCluster(
        cluster_id=cluster_id,
        root_cause="DB pool exhausted",
        ticket_ids=["t001", "t002"],
    )


# ---------------------------------------------------------------------------
# 1. Initialization
# ---------------------------------------------------------------------------

class TestInit:
    def test_url_and_key_set(self) -> None:
        store = _make_store()
        assert store._url == "https://test.supabase.co"
        assert store._key == "test-key"
        assert store._team_id == "test-team"

    def test_trailing_slash_stripped(self) -> None:
        store = KnowledgeGraphStore(
            supabase_url="https://test.supabase.co/",
            supabase_key="k",
        )
        assert not store._url.endswith("/")

    def test_rest_endpoint_built(self) -> None:
        store = _make_store()
        assert store._rest == "https://test.supabase.co/rest/v1"

    def test_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "env-key")
        store = KnowledgeGraphStore()
        assert store._url == "https://env.supabase.co"


# ---------------------------------------------------------------------------
# 2. create_edge / get_edges
# ---------------------------------------------------------------------------

class TestEdgeOperations:
    def test_create_edge_posts_to_knowledge_edges(self) -> None:
        store = _make_store()
        edge = _make_edge()
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req.full_url)
            return _mock_response([edge.to_dict()])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            store.create_edge(edge)
        assert any("/knowledge_edges" in u for u in captured)

    def test_create_edge_silently_handles_http_error(self) -> None:
        store = _make_store()
        edge = _make_edge()
        exc = urllib.error.HTTPError(
            url="http://x", code=500, msg="ISE",
            hdrs=None, fp=BytesIO(b"err"),
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            # Should not raise — create_edge swallows exceptions
            store.create_edge(edge)

    def test_get_edges_returns_list(self) -> None:
        store = _make_store()
        edge_row = _make_edge().to_dict()
        with patch("urllib.request.urlopen", return_value=_mock_response([edge_row])):
            edges = store.get_edges("ticket-001")
        assert len(edges) == 1
        assert edges[0].source_id == "ticket-001"

    def test_get_edges_returns_empty_on_error(self) -> None:
        store = _make_store()
        exc = urllib.error.URLError(reason="connection refused")
        with patch("urllib.request.urlopen", side_effect=exc):
            edges = store.get_edges("ticket-001")
        assert edges == []

    def test_get_edges_with_edge_type_filter(self) -> None:
        store = _make_store()
        edge_row = _make_edge(edge_type=EdgeType.TOUCHES_MODULE).to_dict()
        captured_body = []

        def fake_urlopen(req, timeout=None):
            if req.data:
                captured_body.append(json.loads(req.data.decode()))
            return _mock_response([edge_row])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            edges = store.get_edges("ticket-001", edge_type=EdgeType.TOUCHES_MODULE)
        assert any("p_edge_type" in b for b in captured_body)


# ---------------------------------------------------------------------------
# 3. create_edges_batch — deduplication
# ---------------------------------------------------------------------------

class TestCreateEdgesBatch:
    def test_batch_empty_returns_zero(self) -> None:
        store = _make_store()
        result = store.create_edges_batch([])
        assert result == 0

    def test_batch_deduplicates_by_key(self) -> None:
        store = _make_store()
        # Two edges with same (source, target, type) — only highest confidence kept
        edges = [
            _make_edge("a", "b", EdgeType.SIMILAR, confidence=0.8),
            _make_edge("a", "b", EdgeType.SIMILAR, confidence=0.95),
        ]
        captured_body = []

        def fake_urlopen(req, timeout=None):
            if req.data:
                captured_body.append(json.loads(req.data.decode()))
            return _mock_response([edges[1].to_dict()])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            count = store.create_edges_batch(edges)
        # Should only send 1 unique row
        if captured_body:
            sent = captured_body[0]
            assert isinstance(sent, list)
            assert len(sent) == 1
            assert sent[0]["confidence"] == 0.95

    def test_batch_returns_zero_on_error(self) -> None:
        store = _make_store()
        edges = [_make_edge()]
        exc = urllib.error.URLError(reason="timeout")
        with patch("urllib.request.urlopen", side_effect=exc):
            result = store.create_edges_batch(edges)
        assert result == 0


# ---------------------------------------------------------------------------
# 4. count_edges
# ---------------------------------------------------------------------------

class TestCountEdges:
    def test_count_edges_scalar_result(self) -> None:
        store = _make_store()
        with patch("urllib.request.urlopen", return_value=_mock_response(3)):
            count = store.count_edges("ticket-001")
        assert count == 3

    def test_count_edges_list_result(self) -> None:
        store = _make_store()
        with patch("urllib.request.urlopen", return_value=_mock_response([{"count": 7}])):
            count = store.count_edges("ticket-001")
        assert count == 7

    def test_count_edges_returns_zero_on_error(self) -> None:
        store = _make_store()
        exc = urllib.error.URLError(reason="timeout")
        with patch("urllib.request.urlopen", side_effect=exc):
            count = store.count_edges("ticket-001")
        assert count == 0


# ---------------------------------------------------------------------------
# 5. PR node operations
# ---------------------------------------------------------------------------

class TestPRNodeOperations:
    def test_upsert_pr_node_posts(self) -> None:
        store = _make_store()
        pr = _make_pr()
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req.full_url)
            return _mock_response([pr.to_dict()])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            store.upsert_pr_node(pr)
        assert any("/pr_nodes" in u for u in captured)

    def test_get_pr_node_returns_pr(self) -> None:
        store = _make_store()
        pr = _make_pr()
        with patch("urllib.request.urlopen", return_value=_mock_response([pr.to_dict()])):
            result = store.get_pr_node("your-org/example-app#42")
        assert result is not None
        assert result.pr_id == "your-org/example-app#42"

    def test_get_pr_node_returns_none_on_miss(self) -> None:
        store = _make_store()
        with patch("urllib.request.urlopen", return_value=_mock_response([])):
            result = store.get_pr_node("not-found")
        assert result is None

    def test_get_pr_node_returns_none_on_error(self) -> None:
        store = _make_store()
        exc = urllib.error.URLError(reason="timeout")
        with patch("urllib.request.urlopen", side_effect=exc):
            result = store.get_pr_node("any-id")
        assert result is None

    def test_find_conflicting_prs_empty_when_no_files(self) -> None:
        store = _make_store()
        pr = PRNode(pr_id="test#1", files_changed=[])
        result = store.find_conflicting_prs(pr)
        assert result == []

    def test_find_conflicting_prs_detects_overlap(self) -> None:
        store = _make_store()
        pr_a = _make_pr("repo#1")
        pr_a.files_changed = ["src/foo.py", "src/bar.py"]
        pr_b = _make_pr("repo#2")
        pr_b.files_changed = ["src/foo.py", "src/other.py"]

        with patch("urllib.request.urlopen", return_value=_mock_response([pr_b.to_dict()])):
            conflicts = store.find_conflicting_prs(pr_a)
        assert len(conflicts) == 1
        assert conflicts[0].pr_id == "repo#2"


# ---------------------------------------------------------------------------
# 6. Resolution cluster operations
# ---------------------------------------------------------------------------

class TestClusterOperations:
    def test_get_cluster_returns_cluster(self) -> None:
        store = _make_store()
        cluster = _make_cluster()
        with patch("urllib.request.urlopen", return_value=_mock_response([cluster.to_dict()])):
            result = store.get_cluster("cluster-001")
        assert result is not None
        assert result.cluster_id == "cluster-001"

    def test_get_cluster_returns_none_on_miss(self) -> None:
        store = _make_store()
        with patch("urllib.request.urlopen", return_value=_mock_response([])):
            result = store.get_cluster("nope")
        assert result is None

    def test_create_cluster_posts_to_endpoint(self) -> None:
        store = _make_store()
        cluster = _make_cluster()
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req.full_url)
            return _mock_response([cluster.to_dict()])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            store.create_cluster(cluster)
        assert any("/resolution_clusters" in u for u in captured)

    def test_add_ticket_to_cluster_appends(self) -> None:
        store = _make_store()
        cluster = _make_cluster()
        # First call: get_cluster, Second call: PATCH
        call_idx = [0]
        responses = [
            [cluster.to_dict()],  # GET cluster
            None,                  # PATCH
        ]

        def fake_urlopen(req, timeout=None):
            resp = responses[call_idx[0] % len(responses)]
            call_idx[0] += 1
            return _mock_response(resp)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            store.add_ticket_to_cluster("cluster-001", "t003")
        # 2 calls: GET + PATCH
        assert call_idx[0] == 2

    def test_add_ticket_to_cluster_skips_if_duplicate(self) -> None:
        store = _make_store()
        cluster = _make_cluster()
        # t001 already in cluster
        call_idx = [0]

        def fake_urlopen(req, timeout=None):
            call_idx[0] += 1
            return _mock_response([cluster.to_dict()])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            store.add_ticket_to_cluster("cluster-001", "t001")
        # Only 1 call: GET. No PATCH needed since t001 already present.
        assert call_idx[0] == 1

    def test_find_ticket_cluster_returns_cluster(self) -> None:
        store = _make_store()
        cluster = _make_cluster()
        with patch("urllib.request.urlopen", return_value=_mock_response([cluster.to_dict()])):
            result = store.find_ticket_cluster("t001")
        assert result is not None
        assert result.cluster_id == "cluster-001"

    def test_find_ticket_cluster_returns_none_on_miss(self) -> None:
        store = _make_store()
        with patch("urllib.request.urlopen", return_value=_mock_response([])):
            result = store.find_ticket_cluster("not-in-any-cluster")
        assert result is None


# ---------------------------------------------------------------------------
# 7. Code module operations
# ---------------------------------------------------------------------------

class TestCodeModuleOperations:
    def test_upsert_module_posts(self) -> None:
        store = _make_store()
        module = CodeModule(module_id="scraper.py", repo="your-org/example-app")
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req.full_url)
            return _mock_response([module.to_dict()])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            store.upsert_module(module)
        assert any("/code_modules" in u for u in captured)

    def test_get_module_returns_module(self) -> None:
        store = _make_store()
        module = CodeModule(module_id="scraper.py", repo="your-org/example-app")
        with patch("urllib.request.urlopen", return_value=_mock_response([module.to_dict()])):
            result = store.get_module("scraper.py")
        assert result is not None
        assert result.module_id == "scraper.py"

    def test_get_module_returns_none_on_miss(self) -> None:
        store = _make_store()
        with patch("urllib.request.urlopen", return_value=_mock_response([])):
            result = store.get_module("nonexistent.py")
        assert result is None
