"""Knowledge graph persistence layer for SWE Squad.

Extends the Supabase ticket store with graph operations:
edges, PR nodes, code modules, and resolution clusters.
Uses the same stdlib urllib pattern — zero external deps.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.swe_team.models import (
    CodeModule,
    EdgeType,
    KnowledgeEdge,
    PRNode,
    ResolutionCluster,
)

logger = logging.getLogger(__name__)


class KnowledgeGraphStore:
    """Graph operations on top of the Supabase PostgREST API.

    Shares credentials with SupabaseTicketStore but operates on the
    knowledge_edges, pr_nodes, code_modules, and resolution_clusters tables.
    """

    def __init__(
        self,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
        team_id: str = "default",
    ) -> None:
        self._url = (supabase_url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self._key = supabase_key or os.environ.get(
            "SUPABASE_ANON_KEY",
            os.environ.get("SUPABASE_KEY", ""),
        )
        self._team_id = team_id
        self._rest = f"{self._url}/rest/v1"
        self._headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    # ------------------------------------------------------------------
    # HTTP helper (stdlib urllib — zero external deps)
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, str]] = None,
        body: Optional[Any] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Execute a request against the Supabase PostgREST API."""
        url = f"{self._rest}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, safe=".,()!")

        headers = dict(self._headers)
        if extra_headers:
            headers.update(extra_headers)

        data = json.dumps(body).encode() if body is not None else None

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode()
                if raw:
                    return json.loads(raw)
                return None
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode() if exc.fp else ""
            logger.error(
                "Supabase %s %s failed (%d): %s",
                method, path, exc.code, error_body[:500],
            )
            raise
        except urllib.error.URLError as exc:
            logger.error("Supabase connection error: %s", exc.reason)
            raise

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    def create_edge(self, edge: KnowledgeEdge) -> None:
        """Upsert a knowledge edge.

        Uses ``Prefer: resolution=merge-duplicates`` so that re-inserting
        an edge with the same (source_id, target_id, edge_type) updates
        the existing row rather than failing on a unique constraint.
        """
        row = edge.to_dict()
        row["team_id"] = self._team_id

        headers = {
            "Prefer": "resolution=merge-duplicates,return=representation",
        }
        try:
            self._request("POST", "/knowledge_edges", body=row, extra_headers=headers)
        except Exception:
            logger.warning(
                "Failed to upsert edge %s->%s (%s)",
                edge.source_id, edge.target_id, edge.edge_type.value,
                exc_info=True,
            )

    def get_edges(
        self,
        node_id: str,
        edge_type: Optional[EdgeType] = None,
        limit: int = 50,
    ) -> List[KnowledgeEdge]:
        """Get all edges for a node (both directions).

        Uses the ``get_node_edges`` RPC function which returns edges
        where the node appears as either source or target.
        """
        payload: Dict[str, Any] = {
            "p_node_id": node_id,
            "p_team": self._team_id,
            "p_limit": limit,
        }
        if edge_type is not None:
            payload["p_edge_type"] = edge_type.value

        try:
            rows = self._request("POST", "/rpc/get_node_edges", body=payload)
            return [KnowledgeEdge.from_dict(r) for r in (rows or [])]
        except Exception:
            logger.warning(
                "Failed to get edges for node %s", node_id, exc_info=True,
            )
            return []

    def count_edges(
        self,
        node_id: str,
        edge_type: Optional[EdgeType] = None,
    ) -> int:
        """Count edges for a node.

        Uses the ``count_edges`` RPC function.
        """
        payload: Dict[str, Any] = {
            "p_node_id": node_id,
            "p_team": self._team_id,
        }
        if edge_type is not None:
            payload["p_edge_type"] = edge_type.value

        try:
            result = self._request("POST", "/rpc/count_edges", body=payload)
            # RPC may return a scalar or a list with one row
            if isinstance(result, int):
                return result
            if isinstance(result, list) and result:
                val = result[0]
                if isinstance(val, dict):
                    return int(val.get("count", val.get("count_edges", 0)))
                return int(val)
            return 0
        except Exception:
            logger.warning(
                "Failed to count edges for node %s", node_id, exc_info=True,
            )
            return 0

    def create_edges_batch(self, edges: List[KnowledgeEdge]) -> int:
        """Upsert multiple edges in a single request. Returns count created.

        Deduplicates by (source_id, target_id, edge_type) within the batch
        to avoid Postgres "ON CONFLICT DO UPDATE cannot affect row a second
        time" errors when the same edge appears twice.
        """
        if not edges:
            return 0

        # Deduplicate: keep the highest-confidence edge per key
        seen: Dict[tuple, Dict[str, Any]] = {}
        for edge in edges:
            row = edge.to_dict()
            row["team_id"] = self._team_id
            key = (row["source_id"], row["target_id"], row["edge_type"])
            existing = seen.get(key)
            if existing is None or row.get("confidence", 0) > existing.get("confidence", 0):
                seen[key] = row

        rows = list(seen.values())

        headers = {
            "Prefer": "resolution=merge-duplicates,return=representation",
        }
        try:
            result = self._request(
                "POST", "/knowledge_edges", body=rows, extra_headers=headers,
            )
            return len(result) if isinstance(result, list) else 0
        except Exception:
            logger.warning(
                "Failed to batch-upsert %d edges", len(edges), exc_info=True,
            )
            return 0

    # ------------------------------------------------------------------
    # PR node operations
    # ------------------------------------------------------------------

    def upsert_pr_node(self, pr: PRNode) -> None:
        """Insert or update a PR node.

        Uses ``Prefer: resolution=merge-duplicates`` for upsert on ``pr_id``.
        """
        row = pr.to_dict()
        row["team_id"] = self._team_id

        headers = {
            "Prefer": "resolution=merge-duplicates,return=representation",
        }
        try:
            self._request("POST", "/pr_nodes", body=row, extra_headers=headers)
        except Exception:
            logger.warning(
                "Failed to upsert PR node %s", pr.pr_id, exc_info=True,
            )

    def get_pr_node(self, pr_id: str) -> Optional[PRNode]:
        """Get a PR node by ID."""
        params = {
            "pr_id": f"eq.{pr_id}",
            "team_id": f"eq.{self._team_id}",
        }
        try:
            rows = self._request("GET", "/pr_nodes", params=params)
            if rows:
                return self._row_to_pr_node(rows[0])
            return None
        except Exception:
            logger.warning(
                "Failed to get PR node %s", pr_id, exc_info=True,
            )
            return None

    def list_open_prs(self, repo: Optional[str] = None) -> List[PRNode]:
        """List all open PRs, optionally filtered by repo."""
        params: Dict[str, str] = {
            "team_id": f"eq.{self._team_id}",
            "status": "eq.open",
            "order": "created_at.desc",
        }
        if repo:
            params["repo"] = f"eq.{repo}"

        try:
            rows = self._request("GET", "/pr_nodes", params=params)
            return [self._row_to_pr_node(r) for r in (rows or [])]
        except Exception:
            logger.warning("Failed to list open PRs", exc_info=True)
            return []

    def find_conflicting_prs(self, pr: PRNode) -> List[PRNode]:
        """Find other open PRs that touch the same files as this PR.

        Fetches all open PRs for the same repo and checks for
        ``files_changed`` overlap in Python, since PostgREST cannot do
        JSONB array intersection natively.
        """
        if not pr.files_changed:
            return []

        open_prs = self.list_open_prs(repo=pr.repo)
        pr_files = set(pr.files_changed)
        conflicts: List[PRNode] = []

        for other in open_prs:
            if other.pr_id == pr.pr_id:
                continue
            if set(other.files_changed) & pr_files:
                conflicts.append(other)

        return conflicts

    # ------------------------------------------------------------------
    # Code module operations
    # ------------------------------------------------------------------

    def upsert_module(self, module: CodeModule) -> None:
        """Insert or update a code module."""
        row = module.to_dict()
        row["team_id"] = self._team_id

        headers = {
            "Prefer": "resolution=merge-duplicates,return=representation",
        }
        try:
            self._request("POST", "/code_modules", body=row, extra_headers=headers)
        except Exception:
            logger.warning(
                "Failed to upsert module %s", module.module_id, exc_info=True,
            )

    def get_module(self, module_id: str) -> Optional[CodeModule]:
        """Get a code module by ID."""
        params = {
            "module_id": f"eq.{module_id}",
            "team_id": f"eq.{self._team_id}",
        }
        try:
            rows = self._request("GET", "/code_modules", params=params)
            if rows:
                return CodeModule.from_dict(rows[0])
            return None
        except Exception:
            logger.warning(
                "Failed to get module %s", module_id, exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Resolution cluster operations
    # ------------------------------------------------------------------

    def get_cluster(self, cluster_id: str) -> Optional[ResolutionCluster]:
        """Get a resolution cluster by ID."""
        params = {
            "cluster_id": f"eq.{cluster_id}",
            "team_id": f"eq.{self._team_id}",
        }
        try:
            rows = self._request("GET", "/resolution_clusters", params=params)
            if rows:
                return self._row_to_cluster(rows[0])
            return None
        except Exception:
            logger.warning(
                "Failed to get cluster %s", cluster_id, exc_info=True,
            )
            return None

    def find_ticket_cluster(self, ticket_id: str) -> Optional[ResolutionCluster]:
        """Find the cluster containing a ticket.

        Uses the ``find_ticket_cluster`` RPC function which searches
        the ``ticket_ids`` JSONB array across all clusters.
        """
        payload = {
            "p_ticket_id": ticket_id,
            "p_team": self._team_id,
        }
        try:
            rows = self._request("POST", "/rpc/find_ticket_cluster", body=payload)
            if rows and isinstance(rows, list) and rows[0]:
                return self._row_to_cluster(rows[0])
            return None
        except Exception:
            logger.warning(
                "Failed to find cluster for ticket %s", ticket_id, exc_info=True,
            )
            return None

    def create_cluster(self, cluster: ResolutionCluster) -> None:
        """Insert or update a resolution cluster.

        Uses ``Prefer: resolution=merge-duplicates`` for upsert on
        ``cluster_id``.
        """
        row = cluster.to_dict()
        row["team_id"] = self._team_id

        headers = {
            "Prefer": "resolution=merge-duplicates,return=representation",
        }
        try:
            self._request(
                "POST", "/resolution_clusters", body=row, extra_headers=headers,
            )
        except Exception:
            logger.warning(
                "Failed to upsert cluster %s", cluster.cluster_id, exc_info=True,
            )

    def add_ticket_to_cluster(self, cluster_id: str, ticket_id: str) -> None:
        """Add a ticket to an existing cluster's ticket_ids array.

        Fetches the current cluster, appends the ticket ID if not already
        present, and PATCHes back the updated list.
        """
        cluster = self.get_cluster(cluster_id)
        if cluster is None:
            logger.warning(
                "Cannot add ticket %s to non-existent cluster %s",
                ticket_id, cluster_id,
            )
            return

        if ticket_id in cluster.ticket_ids:
            logger.debug(
                "Ticket %s already in cluster %s", ticket_id, cluster_id,
            )
            return

        updated_ids = cluster.ticket_ids + [ticket_id]
        params = {
            "cluster_id": f"eq.{cluster_id}",
            "team_id": f"eq.{self._team_id}",
        }
        body = {
            "ticket_ids": updated_ids,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._request("PATCH", "/resolution_clusters", params=params, body=body)
        except Exception:
            logger.warning(
                "Failed to add ticket %s to cluster %s",
                ticket_id, cluster_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_pr_node(row: Dict[str, Any]) -> PRNode:
        """Convert a Supabase row to a PRNode, handling JSONB fields."""
        for key in ("files_changed", "ticket_ids", "metadata"):
            val = row.get(key)
            if isinstance(val, str):
                try:
                    row[key] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
        return PRNode.from_dict(row)

    @staticmethod
    def _row_to_cluster(row: Dict[str, Any]) -> ResolutionCluster:
        """Convert a Supabase row to a ResolutionCluster, handling JSONB fields."""
        for key in ("ticket_ids", "metadata"):
            val = row.get(key)
            if isinstance(val, str):
                try:
                    row[key] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
        return ResolutionCluster.from_dict(row)
