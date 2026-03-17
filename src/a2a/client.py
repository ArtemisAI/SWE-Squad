"""
A2A client for discovering and communicating with other agents.

Uses only stdlib ``urllib`` — zero external dependencies.

Usage::

    from src.a2a.client import A2AClient

    client = A2AClient()

    # Discover an agent
    card = client.discover("http://100.96.188.64:18790")
    print(card["name"], card["skills"])

    # Send a task
    result = client.send_task(
        "http://100.96.188.64:18790",
        skill_id="investigate_ticket",
        payload={"ticket_id": "gh-17"},
    )

    # Check task status
    task = client.get_task("http://100.96.188.64:18790", task_id="uuid-here")
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
import uuid
from typing import Any, Dict, Optional

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

    Parameters
    ----------
    timeout:
        Default HTTP timeout in seconds for all requests.
    """

    def __init__(self, *, timeout: int = 30) -> None:
        self._timeout = timeout

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
