"""
Tests for A2A hub integration (dispatch, client, server, registry hub mode).

Covers:
  - dispatch.py: configure(), dispatch_event() with hub and standalone
  - client.py: A2AClient hub and direct mode, discover, send, health
  - server.py: A2AServer standalone endpoints
  - agent_registry.py: hub mode registration, discovery, fallback
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.a2a.dispatch import (
    configure as dispatch_configure,
    dispatch_event,
    get_hub_url,
    _post_to_hub,
)
from src.a2a.events import PipelineEvent
from src.a2a.client import A2AClient
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

logging.logAsyncioTasks = False


# ======================================================================
# Helpers: Fake HTTP server for testing
# ======================================================================


class FakeHubHandler(BaseHTTPRequestHandler):
    """Handler for the fake A2A hub used in tests."""

    server: FakeHubServer  # type: ignore[assignment]

    def do_GET(self) -> None:
        if self.path == "/v1/agents":
            body = json.dumps({"agents": self.server.agents}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        elif self.path == "/.well-known/agent-card.json":
            card = {
                "name": "fake-hub",
                "url": f"http://localhost:{self.server.server_address[1]}",
                "skills": [],
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(card).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""
        parsed = json.loads(body.decode()) if body else {}

        if self.path == "/v1/events":
            self.server.received_events.append(parsed)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "accepted"}')
        elif self.path == "/v1/agents/register":
            self.server.registered_cards.append(parsed)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "registered"}')
        elif self.path.startswith("/v1/agents/") and self.path.endswith("/message:send"):
            # Extract agent name from path
            agent_name = self.path.split("/v1/agents/")[1].split("/message:send")[0]
            self.server.received_messages.append({
                "agent": agent_name,
                "body": parsed,
            })
            response = {
                "jsonrpc": "2.0",
                "id": parsed.get("id", "1"),
                "result": {
                    "status": {"state": "completed"},
                    "artifacts": [{"parts": [{"kind": "data", "data": {"echo": True}}]}],
                },
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress logging from test server."""
        pass


class FakeHubServer(HTTPServer):
    """Fake A2A hub server for testing."""

    def __init__(self, port: int = 0) -> None:
        self.agents: List[Dict[str, Any]] = []
        self.received_events: List[Dict[str, Any]] = []
        self.registered_cards: List[Dict[str, Any]] = []
        self.received_messages: List[Dict[str, Any]] = []
        super().__init__(("127.0.0.1", port), FakeHubHandler)
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        if self._thread:
            self._thread.join(timeout=5)


@pytest.fixture
def fake_hub():
    """Provide a running fake A2A hub server."""
    server = FakeHubServer()
    server.start()
    yield server
    server.stop()


# ======================================================================
# dispatch.py tests
# ======================================================================


class TestDispatchConfigure:
    """Tests for dispatch.configure() and get_hub_url()."""

    def teardown_method(self) -> None:
        dispatch_configure(None)  # Reset to standalone mode

    def test_configure_with_hub_url(self):
        dispatch_configure("http://localhost:18790")
        assert get_hub_url() == "http://localhost:18790"

    def test_configure_strips_trailing_slash(self):
        dispatch_configure("http://localhost:18790/")
        assert get_hub_url() == "http://localhost:18790"

    def test_configure_none_means_standalone(self):
        dispatch_configure("http://hub")
        dispatch_configure(None)
        assert get_hub_url() is None

    def test_configure_empty_string_means_standalone(self):
        dispatch_configure("")
        assert get_hub_url() is None


class TestDispatchEvent:
    """Tests for dispatch_event() in hub and standalone modes."""

    def teardown_method(self) -> None:
        dispatch_configure(None)

    def test_standalone_dispatch_returns_true(self):
        dispatch_configure(None)
        event = PipelineEvent(event="test_event", source_stage="test")
        result = asyncio.run(dispatch_event(event, agent="agent-a"))
        assert result is True

    def test_hub_dispatch_success(self, fake_hub: FakeHubServer):
        dispatch_configure(fake_hub.base_url)
        event = PipelineEvent(
            event="swe_team.issue_detected",
            source_stage="monitor",
            payload={"ticket_id": "T-001"},
        )
        result = asyncio.run(dispatch_event(event, agent="triage"))
        assert result is True
        assert len(fake_hub.received_events) == 1
        evt = fake_hub.received_events[0]
        assert evt["event"] == "swe_team.issue_detected"
        assert evt["target_agent"] == "triage"
        assert evt["payload"]["ticket_id"] == "T-001"

    def test_hub_dispatch_fallback_on_unreachable(self):
        dispatch_configure("http://127.0.0.1:1")  # Port 1 — unreachable
        event = PipelineEvent(event="fallback_test")
        result = asyncio.run(dispatch_event(event))
        # Falls back to standalone mode
        assert result is True

    def test_dispatch_without_agent(self, fake_hub: FakeHubServer):
        dispatch_configure(fake_hub.base_url)
        event = PipelineEvent(event="no_agent_event")
        result = asyncio.run(dispatch_event(event))
        assert result is True
        assert len(fake_hub.received_events) == 1
        assert "target_agent" not in fake_hub.received_events[0]


class TestPostToHub:
    """Tests for the internal _post_to_hub function."""

    def teardown_method(self) -> None:
        dispatch_configure(None)

    def test_post_to_hub_no_url(self):
        dispatch_configure(None)
        assert _post_to_hub({"event": "test"}) is False

    def test_post_to_hub_success(self, fake_hub: FakeHubServer):
        dispatch_configure(fake_hub.base_url)
        result = _post_to_hub({"event": "test_post", "payload": {}})
        assert result is True
        assert len(fake_hub.received_events) == 1

    def test_post_to_hub_unreachable(self):
        dispatch_configure("http://127.0.0.1:1")
        result = _post_to_hub({"event": "unreachable_test"})
        assert result is False


# ======================================================================
# client.py tests
# ======================================================================


class TestA2AClient:
    """Tests for A2AClient in hub and direct modes."""

    def test_init_hub_mode(self):
        client = A2AClient(hub_url="http://localhost:18790")
        assert client.is_hub_mode is True
        assert client.hub_url == "http://localhost:18790"

    def test_init_direct_mode(self):
        client = A2AClient()
        assert client.is_hub_mode is False
        assert client.hub_url is None

    def test_hub_url_strips_trailing_slash(self):
        client = A2AClient(hub_url="http://localhost:18790/")
        assert client.hub_url == "http://localhost:18790"

    def test_discover_agents_via_hub(self, fake_hub: FakeHubServer):
        fake_hub.agents = [
            {"name": "agent-a", "skills": [{"id": "fix"}]},
            {"name": "agent-b", "skills": [{"id": "investigate"}]},
        ]
        client = A2AClient(hub_url=fake_hub.base_url)
        agents = client.discover_agents()
        assert len(agents) == 2
        names = {a["name"] for a in agents}
        assert names == {"agent-a", "agent-b"}

    def test_discover_agents_no_hub(self):
        client = A2AClient()
        agents = client.discover_agents()
        assert agents == []

    def test_discover_agents_hub_unreachable(self):
        client = A2AClient(hub_url="http://127.0.0.1:1")
        agents = client.discover_agents()
        assert agents == []

    def test_send_message_via_hub(self, fake_hub: FakeHubServer):
        client = A2AClient(hub_url=fake_hub.base_url)
        result = client.send_message(
            agent_name="test-agent",
            action="investigate",
            payload={"ticket_id": "T-123"},
        )
        assert "error" not in result
        assert len(fake_hub.received_messages) == 1
        assert fake_hub.received_messages[0]["agent"] == "test-agent"

    def test_send_message_direct_url(self, fake_hub: FakeHubServer):
        # Use fake hub as a direct agent endpoint
        client = A2AClient()  # No hub
        result = client.send_message(
            agent_url=fake_hub.base_url,
            action="fix",
            payload={"ticket_id": "T-456"},
        )
        # Direct URL posts to /v1/message:send which is not a hub agent route
        # The fake hub should return 404 for /v1/message:send
        # This tests the fallback path
        assert isinstance(result, dict)

    def test_send_message_no_endpoint(self):
        client = A2AClient()
        result = client.send_message(agent_name="ghost-agent")
        assert "error" in result

    def test_send_message_hub_fallback_to_direct(self, fake_hub: FakeHubServer):
        # Hub is unreachable, but direct URL works
        client = A2AClient(hub_url="http://127.0.0.1:1")
        # Even the direct URL won't match the fake hub's routes perfectly,
        # but this tests the fallback logic
        result = client.send_message(
            agent_name="agent-x",
            agent_url=fake_hub.base_url,
            action="test",
        )
        # Should attempt hub first (fails), then direct
        assert isinstance(result, dict)

    def test_post_event_to_hub(self, fake_hub: FakeHubServer):
        client = A2AClient(hub_url=fake_hub.base_url)
        result = client.post_event({"event": "test", "payload": {"x": 1}})
        assert result is True
        assert len(fake_hub.received_events) == 1

    def test_post_event_no_hub(self):
        client = A2AClient()
        result = client.post_event({"event": "test"})
        assert result is False

    def test_hub_health_ok(self, fake_hub: FakeHubServer):
        client = A2AClient(hub_url=fake_hub.base_url)
        assert client.hub_health() is True

    def test_hub_health_no_hub(self):
        client = A2AClient()
        assert client.hub_health() is False

    def test_hub_health_unreachable(self):
        client = A2AClient(hub_url="http://127.0.0.1:1")
        assert client.hub_health() is False

    def test_get_agent_card(self, fake_hub: FakeHubServer):
        client = A2AClient()
        card = client.get_agent_card(fake_hub.base_url)
        assert card is not None
        assert card["name"] == "fake-hub"

    def test_get_agent_card_unreachable(self):
        client = A2AClient()
        card = client.get_agent_card("http://127.0.0.1:1")
        assert card is None

    def test_send_message_with_explicit_parts(self, fake_hub: FakeHubServer):
        client = A2AClient(hub_url=fake_hub.base_url)
        parts = [{"kind": "text", "text": "Hello world"}]
        result = client.send_message(
            agent_name="echo-agent",
            message_parts=parts,
        )
        assert len(fake_hub.received_messages) == 1


# ======================================================================
# server.py tests
# ======================================================================


class TestA2AServer:
    """Tests for the standalone A2A server."""

    def _make_adapter(self) -> MagicMock:
        """Create a mock AgentAdapter."""
        from src.a2a.adapters.base import AgentAdapter
        adapter = MagicMock(spec=AgentAdapter)
        adapter.agent_card.return_value = AgentCard(
            name="test-swe-squad",
            description="Test SWE Squad adapter",
            url="http://localhost:18791",
            version="0.2.0",
            skills=[
                AgentSkill(id="triage", name="Triage", description="Triage tickets"),
            ],
        )
        task = Task()
        task.status = TaskStatus(state=TaskState.COMPLETED, message="done")
        task.artifacts = [Artifact(parts=[DataPart(data={"result": "ok"})])]

        async def mock_handle_message(msg, session_id=None):
            return task
        adapter.handle_message = mock_handle_message
        return adapter

    def test_server_health(self):
        from src.a2a.server import A2AServer
        server = A2AServer(port=0)
        server.start_background()
        try:
            host, port = server.server_address
            url = f"http://{host}:{port}/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                assert data["status"] == "ok"
        finally:
            server.stop()

    def test_server_agent_card(self):
        from src.a2a.server import A2AServer
        adapter = self._make_adapter()
        server = A2AServer(adapter=adapter, port=0)
        server.start_background()
        try:
            host, port = server.server_address
            url = f"http://{host}:{port}/.well-known/agent-card.json"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                card = json.loads(resp.read())
                assert card["name"] == "test-swe-squad"
                assert card["version"] == "0.2.0"
                assert len(card["skills"]) == 1
        finally:
            server.stop()

    def test_server_agent_list(self):
        from src.a2a.server import A2AServer
        server = A2AServer(port=0)
        server.register_agent({"name": "agent-x", "status": "online"})
        server.start_background()
        try:
            host, port = server.server_address
            url = f"http://{host}:{port}/v1/agents"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                assert len(data["agents"]) == 1
                assert data["agents"][0]["name"] == "agent-x"
        finally:
            server.stop()

    def test_server_event_post(self):
        from src.a2a.server import A2AServer
        server = A2AServer(port=0)
        server.start_background()
        try:
            host, port = server.server_address
            url = f"http://{host}:{port}/v1/events"
            payload = json.dumps({"event": "test", "payload": {}}).encode()
            req = urllib.request.Request(
                url, data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                assert data["status"] == "accepted"
            assert len(server.event_log) == 1
        finally:
            server.stop()

    def test_server_message_send(self):
        from src.a2a.server import A2AServer
        adapter = self._make_adapter()
        server = A2AServer(adapter=adapter, port=0)
        server.start_background()
        try:
            host, port = server.server_address
            url = f"http://{host}:{port}/v1/message:send"
            body = {
                "jsonrpc": "2.0",
                "id": "42",
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [
                            {"kind": "data", "data": {"action": "triage_ticket", "ticket_id": "T-1"}}
                        ],
                    },
                },
            }
            payload = json.dumps(body).encode()
            req = urllib.request.Request(
                url, data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                assert data["result"]["status"]["state"] == "completed"
                assert data["id"] == "42"
        finally:
            server.stop()

    def test_server_404_on_unknown_get(self):
        from src.a2a.server import A2AServer
        server = A2AServer(port=0)
        server.start_background()
        try:
            host, port = server.server_address
            url = f"http://{host}:{port}/nonexistent"
            req = urllib.request.Request(url)
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req, timeout=5)
            assert exc_info.value.code == 404
        finally:
            server.stop()

    def test_server_no_adapter_returns_503(self):
        from src.a2a.server import A2AServer
        server = A2AServer(port=0)  # No adapter
        server.start_background()
        try:
            host, port = server.server_address
            url = f"http://{host}:{port}/.well-known/agent-card.json"
            req = urllib.request.Request(url)
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req, timeout=5)
            assert exc_info.value.code == 503
        finally:
            server.stop()

    def test_server_register_agent_replaces_existing(self):
        from src.a2a.server import A2AServer
        server = A2AServer(port=0)
        server.register_agent({"name": "x", "version": "1"})
        server.register_agent({"name": "x", "version": "2"})
        assert len(server.registered_agents) == 1
        assert server.registered_agents[0]["version"] == "2"


# ======================================================================
# AgentRegistry hub mode tests
# ======================================================================


class TestAgentRegistryHubMode:
    """Tests for AgentRegistry with hub_url (hub mode)."""

    def test_hub_mode_flag(self):
        registry = AgentRegistry(hub_url="http://localhost:18790")
        assert registry.is_hub_mode is True
        assert registry.hub_url == "http://localhost:18790"

    def test_standalone_mode_flag(self):
        registry = AgentRegistry()
        assert registry.is_hub_mode is False
        assert registry.hub_url is None

    def test_hub_url_strips_trailing_slash(self):
        registry = AgentRegistry(hub_url="http://localhost:18790/")
        assert registry.hub_url == "http://localhost:18790"

    def test_register_mirrors_to_hub(self, fake_hub: FakeHubServer):
        registry = AgentRegistry(hub_url=fake_hub.base_url)
        card = {
            "name": "test-agent",
            "url": "http://localhost:9000",
            "skills": [{"id": "fix", "tags": ["fix"]}],
            "status": "online",
        }
        registry.register(card)
        # Local registration works
        assert registry.get("test-agent") is not None
        # Hub received the registration
        assert len(fake_hub.registered_cards) == 1
        assert fake_hub.registered_cards[0]["name"] == "test-agent"

    def test_register_succeeds_if_hub_unreachable(self):
        registry = AgentRegistry(hub_url="http://127.0.0.1:1")
        card = {"name": "local-only", "status": "online"}
        # Should not raise even though hub is unreachable
        registry.register(card)
        assert registry.get("local-only") is not None

    def test_discover_from_hub(self, fake_hub: FakeHubServer):
        fake_hub.agents = [
            {"name": "hub-agent-1", "skills": [{"id": "investigate"}], "status": "online"},
            {"name": "hub-agent-2", "skills": [{"id": "fix"}], "status": "online"},
        ]
        registry = AgentRegistry(hub_url=fake_hub.base_url)
        discovered = registry.discover()
        assert len(discovered) == 2
        assert registry.get("hub-agent-1") is not None
        assert registry.get("hub-agent-2") is not None

    def test_discover_hub_unreachable_falls_back(self):
        registry = AgentRegistry(hub_url="http://127.0.0.1:1")
        # Should not raise, returns empty
        discovered = registry.discover()
        assert discovered == []

    def test_discover_combines_hub_and_standalone(self, fake_hub: FakeHubServer):
        fake_hub.agents = [
            {"name": "hub-agent", "skills": [{"id": "fix"}]},
        ]
        # Add a discovery URL that points to the fake hub's agent card endpoint
        registry = AgentRegistry(
            hub_url=fake_hub.base_url,
            discovery_urls=[fake_hub.base_url],
        )
        discovered = registry.discover()
        # Should discover both from hub agents list AND well-known endpoint
        names = {d["name"] for d in discovered}
        assert "hub-agent" in names
        assert "fake-hub" in names  # From well-known agent card

    def test_select_agent_with_hub_discovered_agents(self, fake_hub: FakeHubServer):
        fake_hub.agents = [
            {
                "name": "fast-fixer",
                "skills": [{"id": "fix", "tags": ["fix", "standard"]}],
                "status": "online",
                "priority": 50,
            },
        ]
        registry = AgentRegistry(hub_url=fake_hub.base_url)
        registry.discover()
        agent = registry.select_agent("fix")
        assert agent is not None
        assert agent["name"] == "fast-fixer"

    def test_hub_discover_does_not_re_register_with_hub(self, fake_hub: FakeHubServer):
        """Agents discovered from hub should not be re-posted to hub registration."""
        fake_hub.agents = [
            {"name": "hub-native", "skills": []},
        ]
        registry = AgentRegistry(hub_url=fake_hub.base_url)
        registry.discover()
        # The discover path does NOT call self.register() (which would post to hub).
        # It directly populates the local cache.
        assert len(fake_hub.registered_cards) == 0


class TestAgentRegistryStandalone:
    """Ensure existing standalone AgentRegistry tests still work."""

    def test_register_and_get(self):
        registry = AgentRegistry()
        card = {"name": "agent-a", "url": "http://localhost:9000"}
        registry.register(card)
        assert registry.get("agent-a") is not None

    def test_register_requires_name(self):
        registry = AgentRegistry()
        with pytest.raises(ValueError, match="name"):
            registry.register({"url": "http://localhost"})

    def test_discover_standalone_with_urls(self, fake_hub: FakeHubServer):
        registry = AgentRegistry(discovery_urls=[fake_hub.base_url])
        discovered = registry.discover()
        assert len(discovered) >= 1
        assert any(d["name"] == "fake-hub" for d in discovered)

    def test_is_hub_mode_false_by_default(self):
        registry = AgentRegistry()
        assert registry.is_hub_mode is False

    def test_hub_url_none_by_default(self):
        registry = AgentRegistry()
        assert registry.hub_url is None


# ======================================================================
# Integration: dispatch + hub together
# ======================================================================


class TestDispatchHubIntegration:
    """End-to-end: dispatch events to a fake hub and verify delivery."""

    def teardown_method(self) -> None:
        dispatch_configure(None)

    def test_full_event_flow(self, fake_hub: FakeHubServer):
        """Configure dispatch, send event, verify hub received it."""
        dispatch_configure(fake_hub.base_url)
        event = PipelineEvent(
            event="swe_team.triage_complete",
            source_stage="triage",
            payload={"ticket_id": "T-999", "severity": "critical"},
        )
        result = asyncio.run(dispatch_event(event, agent="investigator"))
        assert result is True
        assert len(fake_hub.received_events) == 1
        received = fake_hub.received_events[0]
        assert received["event"] == "swe_team.triage_complete"
        assert received["source_stage"] == "triage"
        assert received["target_agent"] == "investigator"
        assert received["payload"]["ticket_id"] == "T-999"

    def test_multiple_events_all_delivered(self, fake_hub: FakeHubServer):
        dispatch_configure(fake_hub.base_url)
        events = [
            PipelineEvent(event=f"event_{i}", payload={"i": i})
            for i in range(5)
        ]
        for event in events:
            asyncio.run(dispatch_event(event))
        assert len(fake_hub.received_events) == 5


# ======================================================================
# Integration: client + server together
# ======================================================================


class TestClientServerIntegration:
    """Test A2AClient talking to a running A2AServer."""

    def test_client_discovers_server_agents(self):
        from src.a2a.server import A2AServer
        server = A2AServer(port=0)
        server.register_agent({"name": "local-agent", "skills": [{"id": "fix"}]})
        server.start_background()
        try:
            host, port = server.server_address
            client = A2AClient(hub_url=f"http://{host}:{port}")
            agents = client.discover_agents()
            assert len(agents) == 1
            assert agents[0]["name"] == "local-agent"
        finally:
            server.stop()

    def test_client_checks_server_health(self):
        from src.a2a.server import A2AServer
        server = A2AServer(port=0)
        server.start_background()
        try:
            host, port = server.server_address
            client = A2AClient(hub_url=f"http://{host}:{port}")
            assert client.hub_health() is True
        finally:
            server.stop()

    def test_client_posts_event_to_server(self):
        from src.a2a.server import A2AServer
        server = A2AServer(port=0)
        server.start_background()
        try:
            host, port = server.server_address
            client = A2AClient(hub_url=f"http://{host}:{port}")
            result = client.post_event({"event": "test_event", "data": 42})
            assert result is True
            assert len(server.event_log) == 1
        finally:
            server.stop()
