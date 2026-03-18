"""
A2A client for SWE-Squad.

Provides a unified interface for communicating with:
  - **Individual agents** via their direct A2A endpoints (discover, send_task, etc.)
  - **The centralized A2A hub** (``/v1/agents/*``, ``/v1/message:send``, ``/v1/events``)

The client auto-detects whether to route through the hub or directly to an
agent based on configuration.  When a hub URL is configured, hub-mode methods
(``discover_agents``, ``send_message``, ``post_event``, ``hub_health``) become
available alongside the existing direct-mode methods.

Uses only stdlib (``urllib``) to avoid external dependencies.

Usage::

    from src.a2a.client import A2AClient

    # Direct mode (original API)
    client = A2AClient()
    card = client.discover("http://your-hub-host:18790")
    result = client.send_task("http://your-hub-host:18790",
                              skill_id="investigate_ticket",
                              payload={"ticket_id": "gh-17"})

    # Hub mode (new API)
    client = A2AClient(hub_url="http://localhost:18790")
    agents = client.discover_agents()
    result = client.send_message(agent_name="triage", action="triage_ticket",
                                 payload={"ticket_id": "T-1"})
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Well-known discovery path per the A2A spec
AGENT_CARD_PATH = "/.well-known/agent-card.json"
A2A_ENDPOINT = "/a2a"


class A2AClientError(Exception):
    """Raised when an A2A client operation fails."""

    def __init__(self, message: str, code: Optional[int] = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class A2AClient:
    """Client for the A2A agent-to-agent protocol.

    Supports two routing modes:
      - **Direct mode** (default): sends requests directly to agent endpoints
        using ``discover()``, ``send_task()``, ``get_task()``, etc.
      - **Hub mode**: when *hub_url* is provided, enables hub-aware methods
        like ``discover_agents()``, ``send_message()``, ``post_event()``,
        and ``hub_health()``.

    Parameters
    ----------
    hub_url:
        Base URL of the centralized A2A hub (e.g. ``http://localhost:18790``).
        When ``None``, only direct-mode methods are available.
    timeout:
        Default HTTP timeout in seconds for all requests.
    """

    def __init__(self, *, hub_url: Optional[str] = None, timeout: int = 30) -> None:
        self._hub_url = hub_url.rstrip("/") if hub_url else None
        self._timeout = timeout

    @property
    def hub_url(self) -> Optional[str]:
        """Return the configured hub URL, or None if direct-only mode."""
        return self._hub_url

    @property
    def is_hub_mode(self) -> bool:
        """True if the client is configured to use a hub."""
        return self._hub_url is not None

    # ------------------------------------------------------------------
    # Hub-mode: agent discovery
    # ------------------------------------------------------------------

    def discover_agents(self) -> List[Dict[str, Any]]:
        """Discover agents through the hub.

        Queries the hub's ``/v1/agents`` endpoint for all registered agents.
        Returns an empty list if the hub is unreachable or not configured.

        Returns
        -------
        list
            List of agent card dicts.
        """
        if not self._hub_url:
            return []
        url = self._hub_url + "/v1/agents"
        data = self._get_json(url)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("agents", [])
        return []

    def get_agent_card(self, agent_url: str) -> Optional[Dict[str, Any]]:
        """Fetch an agent card from a direct agent URL.

        Queries ``<agent_url>/.well-known/agent-card.json``.

        Parameters
        ----------
        agent_url:
            Base URL of the agent.

        Returns
        -------
        dict or None
            The agent card, or None if unreachable.
        """
        url = agent_url.rstrip("/") + "/.well-known/agent-card.json"
        return self._get_json(url)

    # ------------------------------------------------------------------
    # Hub-mode: message sending
    # ------------------------------------------------------------------

    def send_message(
        self,
        *,
        agent_name: Optional[str] = None,
        agent_url: Optional[str] = None,
        message_parts: Optional[List[Dict[str, Any]]] = None,
        action: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send a message to an agent.

        Routing priority:
          1. If hub is configured and *agent_name* is provided, route
             through the hub (``/v1/agents/<name>/message:send``).
          2. If *agent_url* is provided, send directly to the agent.
          3. If hub routing fails, fall back to direct URL if available.

        Parameters
        ----------
        agent_name:
            Name of the target agent (for hub routing).
        agent_url:
            Direct URL of the target agent (fallback or direct mode).
        message_parts:
            A2A message parts.  If not provided, one is built from
            *action* and *payload*.
        action:
            Action name (convenience -- wraps into a ``DataPart``).
        payload:
            Action payload (convenience -- wraps into a ``DataPart``).

        Returns
        -------
        dict
            The response from the agent (A2A Task or error dict).
        """
        if message_parts is None:
            data_part: Dict[str, Any] = {}
            if action:
                data_part["action"] = action
            if payload:
                data_part.update(payload)
            message_parts = [{"kind": "data", "data": data_part}]

        body = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": message_parts,
                },
            },
        }

        # Try hub routing first
        if self._hub_url and agent_name:
            hub_endpoint = (
                self._hub_url + f"/v1/agents/{agent_name}/message:send"
            )
            result = self._post_json(hub_endpoint, body)
            if result is not None:
                return result
            logger.debug(
                "Hub routing failed for %s; trying direct URL", agent_name
            )

        # Direct agent URL
        if agent_url:
            direct_endpoint = agent_url.rstrip("/") + "/v1/message:send"
            result = self._post_json(direct_endpoint, body)
            if result is not None:
                return result

        return {"error": "No reachable endpoint for agent", "agent_name": agent_name}

    # ------------------------------------------------------------------
    # Hub-mode: event posting
    # ------------------------------------------------------------------

    def post_event(self, event: Dict[str, Any]) -> bool:
        """Post an event to the hub.

        This is a convenience wrapper around the hub's ``/v1/events``
        endpoint.  Returns False if the hub is not configured or unreachable.

        Parameters
        ----------
        event:
            Event dict to post.

        Returns
        -------
        bool
            True if the event was accepted by the hub.
        """
        if not self._hub_url:
            return False
        url = self._hub_url + "/v1/events"
        result = self._post_json(url, event)
        return result is not None

    # ------------------------------------------------------------------
    # Hub-mode: health check
    # ------------------------------------------------------------------

    def hub_health(self) -> bool:
        """Check if the hub is reachable.

        Returns True if the hub responds to a health check, False otherwise.
        """
        if not self._hub_url:
            return False
        url = self._hub_url + "/health"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status < 300
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Direct-mode: agent discovery and task management
    # ------------------------------------------------------------------

    def discover(self, base_url: str, *, timeout: Optional[int] = None) -> Dict[str, Any]:
        """Fetch the agent card from a remote A2A endpoint.

        Parameters
        ----------
        base_url:
            The base URL of the remote agent (e.g. ``"http://host:18790"``).
        timeout:
            Override the default timeout for this request.

        Returns
        -------
        dict
            The agent card as a JSON-compatible dict.

        Raises
        ------
        A2AClientError
            If the request fails or returns invalid data.
        """
        url = base_url.rstrip("/") + AGENT_CARD_PATH
        effective_timeout = timeout or self._timeout
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if not isinstance(data, dict):
                    raise A2AClientError(f"Invalid agent card from {url}: expected dict")
                return data
        except urllib.error.HTTPError as exc:
            raise A2AClientError(
                f"HTTP {exc.code} fetching agent card from {url}",
                code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise A2AClientError(
                f"Connection failed to {url}: {exc.reason}",
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise A2AClientError(f"Invalid JSON from {url}: {exc}") from exc

    def send_task(
        self,
        base_url: str,
        skill_id: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Send a task to a remote A2A agent.

        Parameters
        ----------
        base_url:
            The base URL of the remote agent.
        skill_id:
            The skill to invoke (e.g. ``"investigate_ticket"``).
        payload:
            Additional parameters for the skill.
        session_id:
            Optional session ID for task continuity.
        timeout:
            Override the default timeout.

        Returns
        -------
        dict
            The JSON-RPC result (task dict).

        Raises
        ------
        A2AClientError
            If the request fails or the server returns an error.
        """
        rpc_request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tasks/send",
            "params": {
                "skill_id": skill_id,
                "payload": payload or {},
                "session_id": session_id,
            },
        }
        return self._post_rpc(base_url, rpc_request, timeout=timeout)

    def get_task(
        self,
        base_url: str,
        task_id: str,
        *,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Get the status of a task from a remote A2A agent.

        Parameters
        ----------
        base_url:
            The base URL of the remote agent.
        task_id:
            The task ID to query.
        timeout:
            Override the default timeout.

        Returns
        -------
        dict
            The JSON-RPC result (task dict).

        Raises
        ------
        A2AClientError
            If the request fails or the server returns an error.
        """
        rpc_request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tasks/get",
            "params": {"task_id": task_id},
        }
        return self._post_rpc(base_url, rpc_request, timeout=timeout)

    def cancel_task(
        self,
        base_url: str,
        task_id: str,
        *,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Cancel a task on a remote A2A agent.

        Parameters
        ----------
        base_url:
            The base URL of the remote agent.
        task_id:
            The task ID to cancel.
        timeout:
            Override the default timeout.

        Returns
        -------
        dict
            The JSON-RPC result (task dict).

        Raises
        ------
        A2AClientError
            If the request fails or the server returns an error.
        """
        rpc_request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tasks/cancel",
            "params": {"task_id": task_id},
        }
        return self._post_rpc(base_url, rpc_request, timeout=timeout)

    def health_check(self, base_url: str, *, timeout: Optional[int] = None) -> bool:
        """Check if a remote A2A agent is reachable.

        Returns True if the agent card endpoint returns a valid response.
        """
        try:
            card = self.discover(base_url, timeout=timeout or 5)
            return isinstance(card, dict) and bool(card.get("name"))
        except A2AClientError:
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _post_rpc(
        self,
        base_url: str,
        rpc_request: Dict[str, Any],
        *,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Post a JSON-RPC 2.0 request and return the result."""
        url = base_url.rstrip("/") + A2A_ENDPOINT
        effective_timeout = timeout or self._timeout
        data = json.dumps(rpc_request).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise A2AClientError(
                f"HTTP {exc.code} from {url}",
                code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise A2AClientError(
                f"Connection failed to {url}: {exc.reason}",
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise A2AClientError(f"Invalid JSON from {url}: {exc}") from exc

        # Check for JSON-RPC error
        if "error" in body:
            err = body["error"]
            raise A2AClientError(
                err.get("message", "Unknown JSON-RPC error"),
                code=err.get("code"),
                data=err.get("data"),
            )

        return body.get("result", {})

    # ------------------------------------------------------------------
    # Hub-mode internals
    # ------------------------------------------------------------------

    def _get_json(self, url: str) -> Optional[Any]:
        """GET a URL and parse the JSON response.  Returns None on failure."""
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            logger.debug("GET %s failed: %s", url, exc)
            return None
        except Exception:
            logger.debug("GET %s failed unexpectedly", url, exc_info=True)
            return None

    def _post_json(
        self, url: str, body: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """POST JSON to a URL and parse the response.  Returns None on failure."""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                if raw:
                    return json.loads(raw)
                return {}
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            logger.debug("POST %s failed: %s", url, exc)
            return None
        except Exception:
            logger.debug("POST %s failed unexpectedly", url, exc_info=True)
            return None
