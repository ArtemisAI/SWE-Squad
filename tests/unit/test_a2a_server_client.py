"""
Tests for A2A server, client, agent registry wiring, and end-to-end integration.

Covers:
  - A2AServer: agent card serving, JSON-RPC tasks/send, tasks/get, tasks/cancel,
    error handling for unknown methods, parse errors, health endpoint
  - A2AClient: discover, send_task, get_task, cancel_task, health_check,
    connection errors, JSON-RPC errors
  - AgentRegistry: discovery via A2AClient, health checks, local adapter
    registration, get_local_adapter
  - Integration: client -> server -> adapter -> result round-trip
  - Agent card config file validation
  - A2A request CLI argument parsing
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ── Project bootstrap ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.a2a.server import A2AServer, _agent_card_to_dict, _task_to_dict, _tasks
from src.a2a.client import A2AClient, A2AClientError
from src.a2a.adapters.base import AgentAdapter
from src.a2a.adapters.swe_team import SWETeamAdapter
from src.a2a.models import (
    AgentCard,
    AgentSkill,
    Artifact,
    DataPart,
    Message,
    Task,
    TaskState,
    TaskStatus,
)
from src.swe_team.agent_registry import AgentRegistry
from src.swe_team.config import SWETeamConfig
from src.swe_team.ticket_store import TicketStore


# ======================================================================
# Fixtures
# ======================================================================

class StubAdapter(AgentAdapter):
    """Minimal adapter for testing the A2A server."""

    def __init__(self, name: str = "test-agent"):
        self._name = name
        self._handle_action_result: Dict[str, Any] = {"status": "ok"}
        self._handle_action_error: Optional[Exception] = None

    def agent_card(self) -> AgentCard:
        return AgentCard(
            name=self._name,
            description="Test agent",
            url="http://localhost:0",
            version="1.0.0",
            skills=[
                AgentSkill(id="test_skill", name="Test", description="A test skill", tags=["test"]),
                AgentSkill(id="echo", name="Echo", description="Echo back", tags=["echo"]),
            ],
        )

    async def handle_message(self, message: Message, session_id: Optional[str] = None) -> Task:
        task = Task(session_id=session_id)
        task.status = TaskStatus(state=TaskState.COMPLETED)
        task.artifacts.append(Artifact(parts=[DataPart(data={"echoed": True})]))
        return task

    def handle_action(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._handle_action_error:
            raise self._handle_action_error
        return {**self._handle_action_result, "action": action, "payload": payload}


@pytest.fixture
def stub_adapter():
    return StubAdapter()


@pytest.fixture
def a2a_server(stub_adapter):
    """Start an A2A server on a random port and tear it down after the test."""
    server = A2AServer(adapter=stub_adapter, host="127.0.0.1", port=0)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def base_url(a2a_server):
    return f"http://127.0.0.1:{a2a_server.port}"


@pytest.fixture
def client():
    return A2AClient(timeout=10)


# ======================================================================
# A2AServer — Agent Card
# ======================================================================

class TestA2AServerAgentCard:
    """Tests for the agent card endpoint."""

    def test_serves_agent_card(self, base_url):
        url = f"{base_url}/.well-known/agent-card.json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        assert data["name"] == "test-agent"
        assert data["version"] == "1.0.0"
        assert len(data["skills"]) == 2
        assert data["skills"][0]["id"] == "test_skill"

    def test_agent_card_has_capabilities(self, base_url):
        url = f"{base_url}/.well-known/agent-card.json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        assert "capabilities" in data
        assert data["capabilities"]["streaming"] is False

    def test_agent_card_content_type(self, base_url):
        url = f"{base_url}/.well-known/agent-card.json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            ct = resp.headers.get("Content-Type", "")
            assert "application/json" in ct

    def test_health_endpoint(self, base_url):
        url = f"{base_url}/health"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        assert data["status"] == "ok"

    def test_404_for_unknown_path(self, base_url):
        url = f"{base_url}/nonexistent"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(url, timeout=5)
        assert exc_info.value.code == 404


# ======================================================================
# A2AServer — JSON-RPC tasks/send
# ======================================================================

class TestA2AServerTasksSend:
    """Tests for the tasks/send JSON-RPC method."""

    def test_tasks_send_success(self, base_url):
        rpc = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tasks/send",
            "params": {
                "skill_id": "test_skill",
                "payload": {"key": "value"},
            },
        }
        data = json.dumps(rpc).encode()
        req = urllib.request.Request(
            f"{base_url}/a2a", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        assert result["jsonrpc"] == "2.0"
        assert result["id"] == "1"
        assert "result" in result
        assert result["result"]["status"]["state"] == "completed"

    def test_tasks_send_returns_task_id(self, base_url):
        rpc = {
            "jsonrpc": "2.0",
            "id": "2",
            "method": "tasks/send",
            "params": {"skill_id": "echo", "payload": {}},
        }
        data = json.dumps(rpc).encode()
        req = urllib.request.Request(
            f"{base_url}/a2a", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        task_id = result["result"]["id"]
        assert task_id  # non-empty UUID
        assert len(task_id) == 36  # UUID format

    def test_tasks_send_includes_artifacts(self, base_url):
        rpc = {
            "jsonrpc": "2.0",
            "id": "3",
            "method": "tasks/send",
            "params": {"skill_id": "test_skill", "payload": {"x": 1}},
        }
        data = json.dumps(rpc).encode()
        req = urllib.request.Request(
            f"{base_url}/a2a", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        artifacts = result["result"]["artifacts"]
        assert len(artifacts) >= 1
        assert artifacts[0]["parts"][0]["kind"] == "data"

    def test_tasks_send_with_session_id(self, base_url):
        rpc = {
            "jsonrpc": "2.0",
            "id": "4",
            "method": "tasks/send",
            "params": {
                "skill_id": "test_skill",
                "payload": {},
                "session_id": "session-abc",
            },
        }
        data = json.dumps(rpc).encode()
        req = urllib.request.Request(
            f"{base_url}/a2a", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        assert result["result"]["sessionId"] == "session-abc"

    def test_tasks_send_adapter_error_returns_failed_task(self, stub_adapter, base_url):
        stub_adapter._handle_action_error = ValueError("boom")
        rpc = {
            "jsonrpc": "2.0",
            "id": "5",
            "method": "tasks/send",
            "params": {"skill_id": "test_skill", "payload": {}},
        }
        data = json.dumps(rpc).encode()
        req = urllib.request.Request(
            f"{base_url}/a2a", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        assert result["result"]["status"]["state"] == "failed"
        assert "boom" in result["result"]["status"]["message"]
        # Reset for other tests
        stub_adapter._handle_action_error = None


# ======================================================================
# A2AServer — JSON-RPC tasks/get
# ======================================================================

class TestA2AServerTasksGet:
    """Tests for the tasks/get JSON-RPC method."""

    def test_tasks_get_after_send(self, base_url):
        # First send a task
        rpc_send = {
            "jsonrpc": "2.0", "id": "10",
            "method": "tasks/send",
            "params": {"skill_id": "test_skill", "payload": {}},
        }
        data = json.dumps(rpc_send).encode()
        req = urllib.request.Request(
            f"{base_url}/a2a", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            send_result = json.loads(resp.read())
        task_id = send_result["result"]["id"]

        # Now get it
        rpc_get = {
            "jsonrpc": "2.0", "id": "11",
            "method": "tasks/get",
            "params": {"task_id": task_id},
        }
        data = json.dumps(rpc_get).encode()
        req = urllib.request.Request(
            f"{base_url}/a2a", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            get_result = json.loads(resp.read())
        assert get_result["result"]["id"] == task_id
        assert get_result["result"]["status"]["state"] == "completed"

    def test_tasks_get_not_found(self, base_url):
        rpc = {
            "jsonrpc": "2.0", "id": "12",
            "method": "tasks/get",
            "params": {"task_id": "nonexistent-uuid"},
        }
        data = json.dumps(rpc).encode()
        req = urllib.request.Request(
            f"{base_url}/a2a", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        assert "error" in result
        assert result["error"]["code"] == -32602


# ======================================================================
# A2AServer — JSON-RPC tasks/cancel
# ======================================================================

class TestA2AServerTasksCancel:
    """Tests for the tasks/cancel JSON-RPC method."""

    def test_tasks_cancel(self, base_url):
        # Send a task first
        rpc_send = {
            "jsonrpc": "2.0", "id": "20",
            "method": "tasks/send",
            "params": {"skill_id": "test_skill", "payload": {}},
        }
        data = json.dumps(rpc_send).encode()
        req = urllib.request.Request(
            f"{base_url}/a2a", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            send_result = json.loads(resp.read())
        task_id = send_result["result"]["id"]

        # Cancel it
        rpc_cancel = {
            "jsonrpc": "2.0", "id": "21",
            "method": "tasks/cancel",
            "params": {"task_id": task_id},
        }
        data = json.dumps(rpc_cancel).encode()
        req = urllib.request.Request(
            f"{base_url}/a2a", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            cancel_result = json.loads(resp.read())
        assert cancel_result["result"]["status"]["state"] == "canceled"

    def test_tasks_cancel_not_found(self, base_url):
        rpc = {
            "jsonrpc": "2.0", "id": "22",
            "method": "tasks/cancel",
            "params": {"task_id": "no-such-task"},
        }
        data = json.dumps(rpc).encode()
        req = urllib.request.Request(
            f"{base_url}/a2a", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        assert "error" in result
        assert result["error"]["code"] == -32602


# ======================================================================
# A2AServer — Error handling
# ======================================================================

class TestA2AServerErrors:
    """Tests for server error handling."""

    def test_unknown_method(self, base_url):
        rpc = {
            "jsonrpc": "2.0", "id": "30",
            "method": "unknown/method",
            "params": {},
        }
        data = json.dumps(rpc).encode()
        req = urllib.request.Request(
            f"{base_url}/a2a", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        assert "error" in result
        assert result["error"]["code"] == -32601
        assert "unknown/method" in result["error"]["message"]

    def test_invalid_json(self, base_url):
        req = urllib.request.Request(
            f"{base_url}/a2a",
            data=b"not valid json{{{",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
            # Server should return 400 or a parse error in the response
            assert "error" in result
            assert result["error"]["code"] == -32700
        except urllib.error.HTTPError as exc:
            assert exc.code == 400

    def test_post_to_wrong_path(self, base_url):
        req = urllib.request.Request(
            f"{base_url}/wrong",
            data=b'{"jsonrpc":"2.0"}',
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 404


# ======================================================================
# A2AServer — Lifecycle
# ======================================================================

class TestA2AServerLifecycle:
    """Tests for server start/stop lifecycle."""

    def test_start_and_stop(self, stub_adapter):
        server = A2AServer(adapter=stub_adapter, host="127.0.0.1", port=0)
        server.start()
        assert server.is_running
        server.stop()
        assert not server.is_running

    def test_stop_idempotent(self, stub_adapter):
        server = A2AServer(adapter=stub_adapter, host="127.0.0.1", port=0)
        server.start()
        server.stop()
        server.stop()  # should not raise

    def test_port_assigned(self, stub_adapter):
        server = A2AServer(adapter=stub_adapter, host="127.0.0.1", port=0)
        server.start()
        assert server.port > 0
        server.stop()


# ======================================================================
# A2AClient — Discovery
# ======================================================================

class TestA2AClientDiscover:
    """Tests for A2AClient.discover."""

    def test_discover_success(self, client, base_url):
        card = client.discover(base_url)
        assert card["name"] == "test-agent"
        assert len(card["skills"]) == 2

    def test_discover_connection_error(self, client):
        with pytest.raises(A2AClientError, match="Connection failed"):
            client.discover("http://127.0.0.1:1", timeout=2)

    def test_discover_invalid_url(self, client):
        with pytest.raises(A2AClientError):
            client.discover("http://256.256.256.256:99999", timeout=2)


# ======================================================================
# A2AClient — send_task
# ======================================================================

class TestA2AClientSendTask:
    """Tests for A2AClient.send_task."""

    def test_send_task_success(self, client, base_url):
        result = client.send_task(base_url, "test_skill", {"key": "val"})
        assert result["status"]["state"] == "completed"
        assert "id" in result

    def test_send_task_with_session(self, client, base_url):
        result = client.send_task(
            base_url, "test_skill", {},
            session_id="my-session",
        )
        assert result["sessionId"] == "my-session"

    def test_send_task_empty_payload(self, client, base_url):
        result = client.send_task(base_url, "echo", {})
        assert result["status"]["state"] == "completed"

    def test_send_task_connection_error(self, client):
        with pytest.raises(A2AClientError, match="Connection failed"):
            client.send_task("http://127.0.0.1:1", "skill", {}, timeout=2)


# ======================================================================
# A2AClient — get_task
# ======================================================================

class TestA2AClientGetTask:
    """Tests for A2AClient.get_task."""

    def test_get_task_success(self, client, base_url):
        # First create a task
        send_result = client.send_task(base_url, "test_skill", {})
        task_id = send_result["id"]

        # Then retrieve it
        get_result = client.get_task(base_url, task_id)
        assert get_result["id"] == task_id
        assert get_result["status"]["state"] == "completed"

    def test_get_task_not_found(self, client, base_url):
        with pytest.raises(A2AClientError, match="Task not found"):
            client.get_task(base_url, "nonexistent")


# ======================================================================
# A2AClient — cancel_task
# ======================================================================

class TestA2AClientCancelTask:
    """Tests for A2AClient.cancel_task."""

    def test_cancel_task_success(self, client, base_url):
        send_result = client.send_task(base_url, "test_skill", {})
        task_id = send_result["id"]

        cancel_result = client.cancel_task(base_url, task_id)
        assert cancel_result["status"]["state"] == "canceled"

    def test_cancel_task_not_found(self, client, base_url):
        with pytest.raises(A2AClientError, match="Task not found"):
            client.cancel_task(base_url, "nonexistent")


# ======================================================================
# A2AClient — health_check
# ======================================================================

class TestA2AClientHealthCheck:
    """Tests for A2AClient.health_check."""

    def test_health_check_success(self, client, base_url):
        assert client.health_check(base_url) is True

    def test_health_check_failure(self, client):
        assert client.health_check("http://127.0.0.1:1") is False


# ======================================================================
# A2AClient — Error handling
# ======================================================================

class TestA2AClientErrors:
    """Tests for client error handling."""

    def test_error_has_code(self):
        err = A2AClientError("test", code=404)
        assert err.code == 404

    def test_error_has_data(self):
        err = A2AClientError("test", data={"detail": "x"})
        assert err.data == {"detail": "x"}

    def test_error_str(self):
        err = A2AClientError("something went wrong")
        assert str(err) == "something went wrong"


# ======================================================================
# AgentRegistry — A2AClient-based discovery
# ======================================================================

class TestRegistryA2ADiscovery:
    """Tests for registry discover() using A2AClient."""

    def test_discover_via_client(self, base_url):
        client = A2AClient(timeout=5)
        registry = AgentRegistry(
            discovery_urls=[base_url],
            a2a_client=client,
        )
        discovered = registry.discover()
        assert len(discovered) == 1
        assert discovered[0]["name"] == "test-agent"
        # Should be registered
        assert registry.get("test-agent") is not None

    def test_discover_failed_url_is_best_effort(self):
        client = A2AClient(timeout=2)
        registry = AgentRegistry(
            discovery_urls=["http://127.0.0.1:1"],
            a2a_client=client,
        )
        discovered = registry.discover()
        assert len(discovered) == 0

    def test_discover_without_client_falls_back(self, base_url):
        # Without a2a_client, uses internal _fetch_agent_card
        registry = AgentRegistry(discovery_urls=[base_url])
        discovered = registry.discover()
        assert len(discovered) == 1
        assert discovered[0]["name"] == "test-agent"

    def test_discover_multiple_urls(self, base_url):
        client = A2AClient(timeout=5)
        registry = AgentRegistry(
            discovery_urls=[base_url, "http://127.0.0.1:1"],  # second will fail
            a2a_client=client,
        )
        discovered = registry.discover()
        assert len(discovered) == 1  # only first succeeds


# ======================================================================
# AgentRegistry — Health checks
# ======================================================================

class TestRegistryHealthCheck:
    """Tests for registry check_health()."""

    def test_health_check_remote_agent(self, base_url):
        client = A2AClient(timeout=5)
        registry = AgentRegistry(a2a_client=client)
        registry.register({
            "name": "remote-agent",
            "url": base_url,
            "skills": [{"id": "test", "tags": ["test"]}],
            "status": "online",
        })
        assert registry.check_health("remote-agent") is True

    def test_health_check_unreachable_agent(self):
        client = A2AClient(timeout=2)
        registry = AgentRegistry(a2a_client=client)
        registry.register({
            "name": "dead-agent",
            "url": "http://127.0.0.1:1",
            "skills": [],
            "status": "online",
        })
        assert registry.check_health("dead-agent") is False
        assert registry.get("dead-agent")["status"] == "offline"

    def test_health_check_nonexistent_agent(self):
        registry = AgentRegistry()
        assert registry.check_health("no-such-agent") is False

    def test_health_check_local_adapter(self):
        registry = AgentRegistry()
        adapter = StubAdapter()
        # Stub is_available
        adapter.is_available = lambda: True
        registry.register_local(adapter)
        assert registry.check_health("test-agent") is True

    def test_health_check_local_adapter_unavailable(self):
        registry = AgentRegistry()
        adapter = StubAdapter()
        adapter.is_available = lambda: False
        registry.register_local(adapter)
        assert registry.check_health("test-agent") is False


# ======================================================================
# AgentRegistry — Local adapter registration
# ======================================================================

class TestRegistryLocalAdapter:
    """Tests for register_local() and get_local_adapter()."""

    def test_register_local(self):
        registry = AgentRegistry()
        adapter = StubAdapter(name="my-adapter")
        registry.register_local(adapter)
        card = registry.get("my-adapter")
        assert card is not None
        assert card["name"] == "my-adapter"
        assert card["status"] == "online"

    def test_get_local_adapter(self):
        registry = AgentRegistry()
        adapter = StubAdapter(name="my-adapter")
        registry.register_local(adapter)
        assert registry.get_local_adapter("my-adapter") is adapter

    def test_get_local_adapter_not_found(self):
        registry = AgentRegistry()
        assert registry.get_local_adapter("nope") is None

    def test_unregister_removes_local_adapter(self):
        registry = AgentRegistry()
        adapter = StubAdapter(name="my-adapter")
        registry.register_local(adapter)
        registry.unregister("my-adapter")
        assert registry.get_local_adapter("my-adapter") is None

    def test_register_local_with_priority(self):
        registry = AgentRegistry()
        adapter = StubAdapter(name="pri-agent")
        adapter._priority = 25
        registry.register_local(adapter)
        card = registry.get("pri-agent")
        assert card["priority"] == 25

    def test_register_local_checks_availability(self):
        registry = AgentRegistry()
        adapter = StubAdapter(name="avail-agent")
        adapter.is_available = lambda: False
        registry.register_local(adapter)
        card = registry.get("avail-agent")
        assert card["status"] == "offline"


# ======================================================================
# Integration: Client -> Server -> Adapter -> Result
# ======================================================================

class TestIntegrationRoundTrip:
    """End-to-end tests using client and server together."""

    def test_full_round_trip(self, client, base_url):
        # Discover
        card = client.discover(base_url)
        assert card["name"] == "test-agent"

        # Send task
        result = client.send_task(base_url, "test_skill", {"foo": "bar"})
        assert result["status"]["state"] == "completed"
        task_id = result["id"]

        # Get task
        got = client.get_task(base_url, task_id)
        assert got["id"] == task_id
        assert got["status"]["state"] == "completed"

    def test_send_and_cancel_round_trip(self, client, base_url):
        result = client.send_task(base_url, "echo", {})
        task_id = result["id"]

        canceled = client.cancel_task(base_url, task_id)
        assert canceled["status"]["state"] == "canceled"

        # Get should show canceled
        got = client.get_task(base_url, task_id)
        assert got["status"]["state"] == "canceled"

    def test_multiple_tasks(self, client, base_url):
        ids = []
        for i in range(5):
            result = client.send_task(base_url, "test_skill", {"i": i})
            ids.append(result["id"])
        # All should be retrievable
        for task_id in ids:
            got = client.get_task(base_url, task_id)
            assert got["id"] == task_id

    def test_registry_discover_then_send(self, client, base_url):
        """Registry discovers agent, then client sends a task to it."""
        registry = AgentRegistry(
            discovery_urls=[base_url],
            a2a_client=client,
        )
        discovered = registry.discover()
        assert len(discovered) == 1

        agent = registry.select_agent("test")
        assert agent is not None

        # The discovered agent's URL comes from the card (localhost:0),
        # but the actual server is at base_url. Use the discovery URL
        # when the card URL is not routable.
        agent_url = agent.get("url", "")
        if not agent_url or ":0" in agent_url:
            agent_url = base_url
        result = client.send_task(agent_url, "test_skill", {"via": "registry"})
        assert result["status"]["state"] == "completed"


# ======================================================================
# Agent Card config file
# ======================================================================

class TestAgentCardConfig:
    """Validate the agent-card.json config file."""

    def test_agent_card_file_exists(self):
        path = PROJECT_ROOT / "config" / "agent-card.json"
        assert path.is_file()

    def test_agent_card_file_valid_json(self):
        path = PROJECT_ROOT / "config" / "agent-card.json"
        with open(path) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_agent_card_file_has_required_fields(self):
        path = PROJECT_ROOT / "config" / "agent-card.json"
        with open(path) as f:
            data = json.load(f)
        assert data["name"] == "SWE-Squad"
        assert "description" in data
        assert "url" in data
        assert "version" in data
        assert "skills" in data
        assert len(data["skills"]) >= 4

    def test_agent_card_skills_have_ids(self):
        path = PROJECT_ROOT / "config" / "agent-card.json"
        with open(path) as f:
            data = json.load(f)
        skill_ids = {s["id"] for s in data["skills"]}
        assert "monitor_scan" in skill_ids
        assert "triage_ticket" in skill_ids
        assert "investigate_ticket" in skill_ids
        assert "check_stability" in skill_ids

    def test_agent_card_has_capabilities(self):
        path = PROJECT_ROOT / "config" / "agent-card.json"
        with open(path) as f:
            data = json.load(f)
        assert "capabilities" in data
        assert data["capabilities"]["streaming"] is False


# ======================================================================
# Helper functions
# ======================================================================

class TestHelperFunctions:
    """Tests for server helper functions."""

    def test_agent_card_to_dict(self):
        card = AgentCard(
            name="x",
            description="desc",
            url="http://x",
            version="1.0",
            skills=[AgentSkill(id="s1", name="S1", description="d1", tags=["t"])],
        )
        d = _agent_card_to_dict(card)
        assert d["name"] == "x"
        assert d["skills"][0]["id"] == "s1"
        assert "capabilities" in d

    def test_task_to_dict(self):
        task = Task(session_id="sess")
        task.status = TaskStatus(state=TaskState.COMPLETED, message="done")
        task.artifacts.append(Artifact(parts=[DataPart(data={"k": "v"})]))
        d = _task_to_dict(task, "task-123")
        assert d["id"] == "task-123"
        assert d["sessionId"] == "sess"
        assert d["status"]["state"] == "completed"
        assert d["status"]["message"] == "done"
        assert d["artifacts"][0]["parts"][0]["data"] == {"k": "v"}

    def test_task_to_dict_with_string_parts(self):
        task = Task()
        task.status = TaskStatus(state=TaskState.COMPLETED)
        task.artifacts.append(Artifact(parts=["plain text"]))
        d = _task_to_dict(task, "task-456")
        assert d["artifacts"][0]["parts"][0]["kind"] == "text"
        assert d["artifacts"][0]["parts"][0]["text"] == "plain text"


# ======================================================================
# A2A models — AgentCard.to_dict
# ======================================================================

class TestAgentCardModel:
    """Tests for the AgentCard dataclass."""

    def test_to_dict(self):
        card = AgentCard(
            name="test",
            description="desc",
            url="http://test",
            version="2.0",
            skills=[AgentSkill(id="s", name="S", description="d", tags=["t"])],
            provider={"org": "test"},
        )
        d = card.to_dict()
        assert d["name"] == "test"
        assert d["version"] == "2.0"
        assert len(d["skills"]) == 1
        assert d["provider"] == {"org": "test"}
        assert d["capabilities"]["streaming"] is False

    def test_default_capabilities(self):
        card = AgentCard()
        assert card.capabilities == {"streaming": False, "pushNotifications": False}


# ======================================================================
# A2A request CLI — argument parsing (no actual HTTP)
# ======================================================================

class TestA2ARequestCLI:
    """Tests for the a2a_request.py CLI argument parsing."""

    def test_import_module(self):
        """Verify the module can be imported."""
        import importlib
        mod = importlib.import_module("scripts.ops.a2a_request")
        assert hasattr(mod, "main")
        assert hasattr(mod, "cmd_discover")
        assert hasattr(mod, "cmd_send")
        assert hasattr(mod, "cmd_status")
        assert hasattr(mod, "cmd_cancel")
        assert hasattr(mod, "cmd_health")


# ======================================================================
# A2A hub script — importability
# ======================================================================

class TestA2AHubScript:
    """Tests for the a2a_hub.py script."""

    def test_import_module(self):
        import importlib
        mod = importlib.import_module("scripts.ops.a2a_hub")
        assert hasattr(mod, "main")
