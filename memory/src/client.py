"""
Python client for the SWE-Squad Memory Service.

Wraps the claude-mem worker HTTP API with team_id scoping, authentication,
and SWE-Squad-specific conveniences. Drop-in usable by investigator.py,
developer.py, and other SWE agents.

Usage:
    from memory.src.client import MemoryClient

    client = MemoryClient(team_id="alpha")
    client.init_session(session_id="inv-123", project="SWE-Sandbox", agent_id="investigator")
    client.record_observation(session_id="inv-123", tool_name="Bash", ...)
    results = client.search("authentication bug", project="SWE-Sandbox")
    context = client.get_context(project="SWE-Sandbox")
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_DEFAULT_WORKER_HOST = os.environ.get("MEMORY_WORKER_HOST", "127.0.0.1")
_DEFAULT_WORKER_PORT = int(os.environ.get("MEMORY_WORKER_PORT", "37777"))
_DEFAULT_TEAM_ID = os.environ.get("SWE_TEAM_ID", "default")
_REQUEST_TIMEOUT = int(os.environ.get("MEMORY_REQUEST_TIMEOUT", "30"))


@dataclass
class MemoryObservation:
    """A single observation from the memory store."""
    id: int
    project: str
    type: Optional[str] = None
    title: Optional[str] = None
    narrative: Optional[str] = None
    facts: Optional[str] = None
    concepts: Optional[str] = None
    files_read: Optional[str] = None
    files_modified: Optional[str] = None
    created_at_epoch: int = 0
    platform_source: str = "claude"
    similarity: Optional[float] = None


@dataclass
class MemorySearchResult:
    """Search results with metadata."""
    observations: List[MemoryObservation] = field(default_factory=list)
    total: int = 0
    query: str = ""
    elapsed_ms: float = 0


class MemoryClient:
    """HTTP client for the SWE-Squad memory worker service.

    All requests are scoped by team_id. The worker service handles
    storage, search, and context injection.

    Parameters
    ----------
    team_id:
        Team identifier for multi-tenant scoping.
    host:
        Worker service hostname.
    port:
        Worker service port.
    api_key:
        Optional API key for authenticated access (required when
        worker is exposed to network).
    """

    def __init__(
        self,
        team_id: str = _DEFAULT_TEAM_ID,
        host: str = _DEFAULT_WORKER_HOST,
        port: int = _DEFAULT_WORKER_PORT,
        api_key: Optional[str] = None,
    ) -> None:
        self._team_id = team_id
        self._base_url = f"http://{host}:{port}"
        self._api_key = api_key or os.environ.get("MEMORY_API_KEY")
        self._session_cache: Dict[str, str] = {}  # content_session_id -> memory_session_id

    # -----------------------------------------------------------------------
    # Session lifecycle
    # -----------------------------------------------------------------------

    def init_session(
        self,
        session_id: str,
        project: str,
        prompt: str = "",
        agent_id: Optional[str] = None,
        platform_source: str = "swe-squad",
    ) -> Dict[str, Any]:
        """Initialize a memory session for an agent run.

        Maps to POST /api/sessions/init in the claude-mem worker.
        """
        body = {
            "contentSessionId": session_id,
            "project": project,
            "prompt": prompt,
            "platformSource": platform_source,
            "teamId": self._team_id,
        }
        if agent_id:
            body["agentId"] = agent_id

        result = self._post("/api/sessions/init", body)
        if result and "sessionDbId" in result:
            logger.info(
                "Memory session initialized: session=%s project=%s agent=%s",
                session_id, project, agent_id,
            )
        return result or {}

    def complete_session(self, session_id: str) -> None:
        """Mark a session as complete.

        Maps to POST /api/sessions/complete.
        """
        self._post_fire_and_forget("/api/sessions/complete", {
            "contentSessionId": session_id,
            "teamId": self._team_id,
        })

    # -----------------------------------------------------------------------
    # Observation recording
    # -----------------------------------------------------------------------

    def record_observation(
        self,
        session_id: str,
        tool_name: str,
        tool_input: Any = None,
        tool_response: str = "",
        cwd: str = "",
        project: Optional[str] = None,
    ) -> None:
        """Record a tool use observation (fire-and-forget).

        Maps to POST /api/sessions/observations.
        """
        # Truncate large tool responses
        max_len = 2000
        if len(tool_response) > max_len:
            tool_response = tool_response[:max_len] + "\n... [truncated]"

        self._post_fire_and_forget("/api/sessions/observations", {
            "contentSessionId": session_id,
            "tool_name": tool_name,
            "tool_input": tool_input or {},
            "tool_response": tool_response,
            "cwd": cwd,
            "platformSource": "swe-squad",
            "teamId": self._team_id,
        })

    def record_summary(
        self,
        session_id: str,
        summary: str,
    ) -> None:
        """Record a session summary.

        Maps to POST /api/sessions/summarize.
        """
        self._post_fire_and_forget("/api/sessions/summarize", {
            "contentSessionId": session_id,
            "last_assistant_message": summary,
            "teamId": self._team_id,
        })

    # -----------------------------------------------------------------------
    # Search & retrieval
    # -----------------------------------------------------------------------

    def search(
        self,
        query: str,
        project: Optional[str] = None,
        limit: int = 20,
        obs_type: Optional[str] = None,
    ) -> MemorySearchResult:
        """Search memory observations.

        Maps to GET /api/search.
        """
        params: Dict[str, str] = {
            "q": query,
            "limit": str(limit),
            "teamId": self._team_id,
        }
        if project:
            params["project"] = project
        if obs_type:
            params["type"] = obs_type

        start = time.monotonic()
        result = self._get("/api/search", params)
        elapsed = (time.monotonic() - start) * 1000

        if not result:
            return MemorySearchResult(query=query, elapsed_ms=elapsed)

        observations = []
        for item in result.get("observations", result.get("results", [])):
            observations.append(MemoryObservation(
                id=item.get("id", 0),
                project=item.get("project", ""),
                type=item.get("type"),
                title=item.get("title"),
                narrative=item.get("narrative"),
                facts=item.get("facts"),
                concepts=item.get("concepts"),
                files_read=item.get("files_read"),
                files_modified=item.get("files_modified"),
                created_at_epoch=item.get("created_at_epoch", 0),
                platform_source=item.get("platform_source", "unknown"),
                similarity=item.get("similarity"),
            ))

        return MemorySearchResult(
            observations=observations,
            total=len(observations),
            query=query,
            elapsed_ms=elapsed,
        )

    def get_context(
        self,
        project: str,
        platform_source: Optional[str] = None,
        full: bool = False,
    ) -> str:
        """Get formatted context for injection into agent prompts.

        Maps to GET /api/context/inject.
        Returns plain text ready for system prompt injection.
        """
        params: Dict[str, str] = {
            "project": project,
            "teamId": self._team_id,
        }
        if platform_source:
            params["platformSource"] = platform_source
        if full:
            params["full"] = "true"

        return self._get_text("/api/context/inject", params) or ""

    def get_semantic_context(
        self,
        query: str,
        project: str,
        limit: int = 10,
    ) -> str:
        """Get semantically relevant context for a query.

        Maps to POST /api/context/semantic.
        """
        result = self._post("/api/context/semantic", {
            "query": query,
            "project": project,
            "limit": limit,
            "teamId": self._team_id,
        })
        if result and "context" in result:
            return result["context"]
        return ""

    # -----------------------------------------------------------------------
    # Health & status
    # -----------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Check worker health status."""
        return self._get("/api/health") or {}

    def is_ready(self) -> bool:
        """Check if the worker is fully initialized."""
        try:
            result = self._get("/api/readiness")
            return bool(result and result.get("ready"))
        except Exception:
            return False

    # -----------------------------------------------------------------------
    # Convenience methods for SWE-Squad agents
    # -----------------------------------------------------------------------

    def get_investigation_context(
        self,
        ticket_title: str,
        project: str,
    ) -> str:
        """Get relevant memory context for an investigation.

        Combines semantic search with timeline context for maximum
        relevance to the investigator agent.
        """
        parts = []

        # Semantic search for similar past work
        results = self.search(ticket_title, project=project, limit=5)
        if results.observations:
            parts.append("## Relevant Past Observations")
            for obs in results.observations:
                parts.append(f"### {obs.title or 'Untitled'} ({obs.type or 'unknown'})")
                if obs.narrative:
                    parts.append(obs.narrative)
                if obs.facts:
                    parts.append(f"Facts: {obs.facts}")
                parts.append("")

        # Recent timeline context
        timeline = self.get_context(project)
        if timeline:
            parts.append("## Recent Project Timeline")
            parts.append(timeline)

        return "\n".join(parts) if parts else ""

    def record_investigation_result(
        self,
        session_id: str,
        ticket_id: str,
        report: str,
        project: str,
        cwd: str = "",
    ) -> None:
        """Record an investigation result as a rich observation."""
        self.record_observation(
            session_id=session_id,
            tool_name="investigation_complete",
            tool_input={"ticket_id": ticket_id, "project": project},
            tool_response=report[:2000],
            cwd=cwd,
            project=project,
        )

    # -----------------------------------------------------------------------
    # HTTP helpers (stdlib only — zero extra deps, matches SWE-Squad pattern)
    # -----------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _post(self, path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.warning("Memory POST %s returned %d", path, e.code)
            return None
        except Exception as e:
            logger.warning("Memory POST %s failed: %s", path, e)
            return None

    def _post_fire_and_forget(self, path: str, body: Dict[str, Any]) -> None:
        """Fire-and-forget POST — errors are logged, never raised."""
        try:
            self._post(path, body)
        except Exception as e:
            logger.debug("Memory fire-and-forget %s failed: %s", path, e)

    def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
        url = f"{self._base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.warning("Memory GET %s returned %d", path, e.code)
            return None
        except Exception as e:
            logger.warning("Memory GET %s failed: %s", path, e)
            return None

    def _get_text(self, path: str, params: Optional[Dict[str, str]] = None) -> Optional[str]:
        url = f"{self._base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            logger.warning("Memory GET text %s failed: %s", path, e)
            return None
