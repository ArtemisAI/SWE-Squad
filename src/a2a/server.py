"""
Lightweight A2A HTTP server for the SWE-Squad agent network.

Implements the Google A2A protocol over HTTP using only stdlib modules:
  - ``GET  /.well-known/agent-card.json`` -- agent card discovery
  - ``POST /a2a``                         -- JSON-RPC 2.0 endpoint (tasks/send, tasks/get, tasks/cancel)
  - ``GET  /v1/agents``                   -- list registered agents
  - ``POST /v1/events``                   -- receive events (log-only)
  - ``POST /v1/message:send``             -- handle incoming A2A messages
  - ``POST /v1/agents/<name>/message:send`` -- hub-style message routing
  - ``POST /v1/agents/register``          -- register an agent card
  - ``GET  /health``                      -- health check

Zero external dependencies -- uses ``http.server`` and ``json`` from the
standard library.

Usage::

    from src.a2a.server import A2AServer
    from src.a2a.adapters.swe_team import SWETeamAdapter

    server = A2AServer(adapter=adapter)
    server.start()   # runs in a background daemon thread
    ...
    server.stop()
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional

from src.a2a.adapters.base import AgentAdapter
from src.a2a.models import (
    AgentCard,
    DataPart,
    Message,
    Task,
    TaskState,
    TaskStatus,
)

logger = logging.getLogger(__name__)

# In-memory task store keyed by task ID
_tasks: Dict[str, Dict[str, Any]] = {}


def _agent_card_to_dict(card: AgentCard) -> Dict[str, Any]:
    """Serialize an AgentCard dataclass to a JSON-compatible dict."""
    return {
        "name": card.name,
        "description": card.description,
        "url": card.url,
        "version": card.version,
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "tags": s.tags,
            }
            for s in card.skills
        ],
        "provider": card.provider,
        "capabilities": {"streaming": False, "pushNotifications": False},
    }


def _task_to_dict(task: Task, task_id: str) -> Dict[str, Any]:
    """Serialize a Task dataclass to a JSON-compatible dict."""
    artifacts = []
    for artifact in task.artifacts:
        parts = []
        for part in artifact.parts:
            if isinstance(part, DataPart):
                parts.append({"kind": "data", "data": part.data})
            else:
                parts.append({"kind": "text", "text": str(part)})
        artifacts.append({"parts": parts})

    return {
        "id": task_id,
        "sessionId": task.session_id,
        "status": {
            "state": task.status.state.value,
            "message": task.status.message,
        },
        "artifacts": artifacts,
    }


def _make_jsonrpc_response(
    req_id: Any,
    result: Any,
) -> Dict[str, Any]:
    """Build a JSON-RPC 2.0 success response."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _make_jsonrpc_error(
    req_id: Any,
    code: int,
    message: str,
    data: Any = None,
) -> Dict[str, Any]:
    """Build a JSON-RPC 2.0 error response."""
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def _create_handler(adapter: Optional[AgentAdapter], a2a_server: "A2AServer") -> type:
    """Create a request handler class bound to the given adapter and server."""

    class A2ARequestHandler(BaseHTTPRequestHandler):
        """HTTP request handler for the A2A protocol."""

        # Suppress default logging to stderr
        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("A2A HTTP: %s", format % args)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/.well-known/agent-card.json":
                if adapter is None:
                    self._send_json({"error": "No adapter configured"}, status=503)
                    return
                card = adapter.agent_card()
                self._send_json(_agent_card_to_dict(card))
            elif self.path == "/v1/agents":
                self._send_json({"agents": a2a_server.registered_agents})
            elif self.path == "/health":
                self._send_json({"status": "ok"})
            else:
                self.send_error(404, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/a2a":
                self._handle_a2a_rpc()
            elif self.path == "/v1/events":
                self._handle_event()
            elif self.path == "/v1/message:send":
                self._handle_message_send()
            elif self.path == "/v1/agents/register":
                self._handle_agent_register()
            elif self.path.startswith("/v1/agents/") and self.path.endswith("/message:send"):
                self._handle_hub_message_send()
            else:
                self.send_error(404, "Not found")

        # ----------------------------------------------------------
        # Legacy JSON-RPC /a2a endpoint
        # ----------------------------------------------------------

        def _handle_a2a_rpc(self) -> None:
            """Handle the legacy /a2a JSON-RPC 2.0 endpoint."""
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length)

            try:
                request = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                resp = _make_jsonrpc_error(None, -32700, "Parse error")
                self._send_json(resp, status=400)
                return

            req_id = request.get("id")
            method = request.get("method", "")
            params = request.get("params", {})

            if method == "tasks/send":
                self._handle_tasks_send(req_id, params)
            elif method == "tasks/get":
                self._handle_tasks_get(req_id, params)
            elif method == "tasks/cancel":
                self._handle_tasks_cancel(req_id, params)
            else:
                resp = _make_jsonrpc_error(req_id, -32601, f"Method not found: {method}")
                self._send_json(resp)

        def _handle_tasks_send(self, req_id: Any, params: Dict[str, Any]) -> None:
            """Handle tasks/send -- create and execute a task."""
            task_id = str(uuid.uuid4())
            skill_id = params.get("skill_id", "")
            payload = params.get("payload", {})
            session_id = params.get("session_id")

            # Build a Message with the payload
            message = Message(parts=[DataPart(data={
                "action": skill_id,
                **payload,
            })])

            try:
                # Use synchronous handle_action if available (preferred for SWETeamAdapter)
                if adapter is not None and hasattr(adapter, "handle_action"):
                    result = adapter.handle_action(skill_id, payload)
                    task = Task(session_id=session_id)
                    task.status = TaskStatus(state=TaskState.COMPLETED)
                    from src.a2a.models import Artifact
                    task.artifacts.append(Artifact(parts=[DataPart(data={
                        "action": skill_id,
                        "result": result,
                    })]))
                elif adapter is not None:
                    # Fall back to async handle_message
                    import asyncio
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop and loop.is_running():
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            task = pool.submit(
                                asyncio.run,
                                adapter.handle_message(message, session_id=session_id),
                            ).result(timeout=60)
                    else:
                        task = asyncio.run(
                            adapter.handle_message(message, session_id=session_id)
                        )
                else:
                    raise RuntimeError("No adapter configured")

                task_dict = _task_to_dict(task, task_id)
                _tasks[task_id] = task_dict
                resp = _make_jsonrpc_response(req_id, task_dict)
                self._send_json(resp)

            except Exception as exc:
                logger.exception("tasks/send failed for skill=%s", skill_id)
                failed_task = Task(session_id=session_id)
                failed_task.status = TaskStatus(
                    state=TaskState.FAILED, message=str(exc)
                )
                task_dict = _task_to_dict(failed_task, task_id)
                _tasks[task_id] = task_dict
                resp = _make_jsonrpc_response(req_id, task_dict)
                self._send_json(resp)

        def _handle_tasks_get(self, req_id: Any, params: Dict[str, Any]) -> None:
            """Handle tasks/get -- retrieve a task by ID."""
            task_id = params.get("task_id", "")
            task_dict = _tasks.get(task_id)
            if task_dict is None:
                resp = _make_jsonrpc_error(req_id, -32602, f"Task not found: {task_id}")
                self._send_json(resp)
                return
            resp = _make_jsonrpc_response(req_id, task_dict)
            self._send_json(resp)

        def _handle_tasks_cancel(self, req_id: Any, params: Dict[str, Any]) -> None:
            """Handle tasks/cancel -- cancel a task by ID."""
            task_id = params.get("task_id", "")
            task_dict = _tasks.get(task_id)
            if task_dict is None:
                resp = _make_jsonrpc_error(req_id, -32602, f"Task not found: {task_id}")
                self._send_json(resp)
                return
            task_dict["status"] = {
                "state": TaskState.CANCELED.value,
                "message": "Canceled by client",
            }
            resp = _make_jsonrpc_response(req_id, task_dict)
            self._send_json(resp)

        # ----------------------------------------------------------
        # Hub-style endpoints
        # ----------------------------------------------------------

        def _handle_event(self) -> None:
            """Accept an event (log-only in standalone mode)."""
            body = self._read_json_body()
            if body is None:
                return
            a2a_server.event_log.append(body)
            logger.debug("Server received event: %s", body.get("event", "?"))
            self._send_json({"status": "accepted"})

        def _handle_message_send(self) -> None:
            """Handle an incoming A2A message on /v1/message:send."""
            if adapter is None:
                self._send_json({"error": "No adapter configured"}, status=503)
                return

            body = self._read_json_body()
            if body is None:
                return

            # Extract message from JSON-RPC envelope
            params = body.get("params", {})
            msg_data = params.get("message", {})
            parts = []
            for part in msg_data.get("parts", []):
                if part.get("kind") == "data":
                    parts.append(DataPart(data=part.get("data")))
                elif part.get("kind") == "text":
                    parts.append(DataPart(data={"text": part.get("text", "")}))
            message = Message(parts=parts)

            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result_task = pool.submit(asyncio.run, adapter.handle_message(message))
                    result_task = result_task.result(timeout=30)
            else:
                result_task = asyncio.run(adapter.handle_message(message))

            # Build JSON-RPC response
            response = {
                "jsonrpc": "2.0",
                "id": body.get("id", "1"),
                "result": {
                    "status": {
                        "state": result_task.status.state.value,
                        "message": result_task.status.message,
                    },
                    "artifacts": [
                        {"parts": [
                            {"kind": "data", "data": p.data}
                            for p in art.parts
                            if isinstance(p, DataPart)
                        ]}
                        for art in result_task.artifacts
                    ],
                },
            }
            self._send_json(response)

        def _handle_hub_message_send(self) -> None:
            """Handle hub-style /v1/agents/<name>/message:send routing."""
            # Extract agent name from path
            agent_name = self.path.split("/v1/agents/")[1].split("/message:send")[0]
            body = self._read_json_body()
            if body is None:
                return
            # Record the routed message
            a2a_server._received_messages.append({
                "agent": agent_name,
                "body": body,
            })

            # If adapter is available, actually process the message
            if adapter is not None:
                # Extract message from JSON-RPC envelope
                params = body.get("params", {})
                msg_data = params.get("message", {})
                parts = []
                for part in msg_data.get("parts", []):
                    if part.get("kind") == "data":
                        parts.append(DataPart(data=part.get("data")))
                    elif part.get("kind") == "text":
                        parts.append(DataPart(data={"text": part.get("text", "")}))
                message = Message(parts=parts)

                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        result_task = pool.submit(asyncio.run, adapter.handle_message(message))
                        result_task = result_task.result(timeout=30)
                else:
                    result_task = asyncio.run(adapter.handle_message(message))

                response = {
                    "jsonrpc": "2.0",
                    "id": body.get("id", "1"),
                    "result": {
                        "status": {
                            "state": result_task.status.state.value,
                            "message": result_task.status.message,
                        },
                        "artifacts": [
                            {"parts": [
                                {"kind": "data", "data": p.data}
                                for p in art.parts
                                if isinstance(p, DataPart)
                            ]}
                            for art in result_task.artifacts
                        ],
                    },
                }
                self._send_json(response)
            else:
                # Echo back a generic response
                response = {
                    "jsonrpc": "2.0",
                    "id": body.get("id", "1"),
                    "result": {
                        "status": {"state": "completed"},
                        "artifacts": [{"parts": [{"kind": "data", "data": {"routed": True}}]}],
                    },
                }
                self._send_json(response)

        def _handle_agent_register(self) -> None:
            """Handle /v1/agents/register -- register an agent card."""
            body = self._read_json_body()
            if body is None:
                return
            a2a_server.register_agent(body)
            self._send_json({"status": "registered"})

        # ----------------------------------------------------------
        # Helpers
        # ----------------------------------------------------------

        def _read_json_body(self) -> Optional[Dict[str, Any]]:
            """Read and parse the JSON request body."""
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                self._send_json({"error": "Empty request body"}, status=400)
                return None
            raw = self.rfile.read(content_length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_json({"error": f"Invalid JSON: {exc}"}, status=400)
                return None

        def _send_json(self, data: Any, status: int = 200) -> None:
            """Send a JSON response."""
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return A2ARequestHandler


class A2AServer:
    """Lightweight A2A HTTP server that routes requests to an AgentAdapter.

    Supports both the legacy JSON-RPC ``/a2a`` endpoint and the newer
    hub-style endpoints (``/v1/agents``, ``/v1/events``, ``/v1/message:send``).

    Parameters
    ----------
    adapter:
        The agent adapter that handles incoming tasks.  Can be ``None``
        for hub-only mode (agent list and event logging only).
    host:
        Bind address (default ``"0.0.0.0"``).
    port:
        Bind port (default ``18790``).

    Usage::

        server = A2AServer(adapter=my_adapter, port=18790)
        server.start()   # starts in a daemon thread
        # ... do other work ...
        server.stop()
    """

    def __init__(
        self,
        adapter: Optional[AgentAdapter] = None,
        host: str = "0.0.0.0",
        port: int = 18790,
    ) -> None:
        self._adapter = adapter
        self._host = host
        self._port = port
        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        # Hub-mode state
        self.registered_agents: List[Dict[str, Any]] = []
        self.event_log: List[Dict[str, Any]] = []
        self._received_messages: List[Dict[str, Any]] = []

    @property
    def adapter(self) -> Optional[AgentAdapter]:
        """Return the configured adapter, or None."""
        return self._adapter

    @property
    def port(self) -> int:
        """Return the port the server is bound to."""
        return self._port

    @property
    def host(self) -> str:
        """Return the host the server is bound to."""
        return self._host

    @property
    def server_address(self):
        """Return (host, port) tuple, compatible with HTTPServer API."""
        if self._httpd is not None:
            return self._httpd.server_address
        return (self._host, self._port)

    def register_agent(self, agent_card: Dict[str, Any]) -> None:
        """Register a local agent card for the ``/v1/agents`` endpoint."""
        name = agent_card.get("name", "")
        self.registered_agents = [
            a for a in self.registered_agents if a.get("name") != name
        ]
        self.registered_agents.append(agent_card)

    def start(self) -> None:
        """Start the A2A server in a background daemon thread."""
        handler_class = _create_handler(self._adapter, self)
        self._httpd = HTTPServer((self._host, self._port), handler_class)
        # Use the actual bound port (useful when port=0 for OS-assigned port)
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="a2a-server",
            daemon=True,
        )
        self._thread.start()
        logger.info("A2A server started on %s:%d", self._host, self._port)

    def start_background(self) -> None:
        """Start the server in a background daemon thread (alias for start)."""
        self.start()

    def stop(self) -> None:
        """Stop the A2A server."""
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        # Clear in-memory tasks
        _tasks.clear()
        logger.info("A2A server stopped")

    @property
    def is_running(self) -> bool:
        """Return True if the server thread is alive."""
        return self._thread is not None and self._thread.is_alive()
