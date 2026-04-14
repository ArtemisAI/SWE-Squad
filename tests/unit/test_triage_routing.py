"""Tests for external agent routing in TriageAgent.

Covers the _route_ticket() logic added for Issue #128:
  - Routing disabled by default (no behaviour change)
  - CRITICAL + large error_log routes to Gemini via AgentRegistry
  - Label-based routing for opencode/code-generation
  - Fallback to internal when external agent unavailable
  - Routing metadata recorded on ticket
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from src.swe_team.config import RoutingConfig, SWETeamConfig
from src.swe_team.models import (
    AgentRole,
    SWEAgentConfig,
    SWETicket,
    TicketSeverity,
    TicketStatus,
)
from src.swe_team.triage_agent import TriageAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    routing: Optional[RoutingConfig] = None,
) -> SWETeamConfig:
    """Return a minimal SWETeamConfig with one investigator and optional routing."""
    agents = [
        SWEAgentConfig(
            name="browser_investigator",
            role=AgentRole.INVESTIGATOR,
            description="test investigator",
            model="sonnet",
            enabled=True,
        ),
    ]
    cfg = SWETeamConfig(agents=agents)
    if routing is not None:
        cfg.routing = routing
    return cfg


def _make_ticket(
    severity: TicketSeverity = TicketSeverity.MEDIUM,
    error_log: Optional[str] = None,
    labels: Optional[List[str]] = None,
    title: str = "Test ticket",
    description: str = "A test ticket for routing",
) -> SWETicket:
    return SWETicket(
        title=title,
        description=description,
        severity=severity,
        error_log=error_log,
        labels=labels or [],
    )


def _make_registry_with_gemini() -> "FakeAgentRegistry":
    """Return a fake AgentRegistry containing a Gemini-CLI agent."""
    return FakeAgentRegistry(agents={
        "gemini-cli": {
            "name": "gemini-cli",
            "url": "local://gemini",
            "status": "online",
            "priority": 50,
            "skills": [
                {"id": "investigate", "tags": ["investigate", "diagnose"]},
                {"id": "review", "tags": ["review"]},
            ],
        },
    })


def _make_registry_with_opencode() -> "FakeAgentRegistry":
    """Return a fake AgentRegistry containing an OpenCode agent."""
    return FakeAgentRegistry(agents={
        "opencode": {
            "name": "opencode",
            "url": "local://opencode",
            "status": "online",
            "priority": 60,
            "skills": [
                {"id": "investigate", "tags": ["investigate"]},
                {"id": "fix", "tags": ["fix", "code_generation"]},
                {"id": "refactor", "tags": ["refactor"]},
            ],
        },
    })


def _make_registry_with_both() -> "FakeAgentRegistry":
    """Return a fake AgentRegistry with both Gemini and OpenCode."""
    gemini = _make_registry_with_gemini()
    opencode = _make_registry_with_opencode()
    agents = {**gemini._agents, **opencode._agents}
    return FakeAgentRegistry(agents=agents)


class FakeAgentRegistry:
    """Minimal stand-in for AgentRegistry that supports the methods used by triage."""

    def __init__(self, agents: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self._agents: Dict[str, Dict[str, Any]] = agents or {}
        self._registered_at: Dict[str, float] = {
            name: time.monotonic() for name in self._agents
        }

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return self._agents.get(name)

    def select_agent(
        self,
        task_type: str,
        severity: str = "medium",
        *,
        exclude: Optional[list] = None,
    ) -> Optional[Dict[str, Any]]:
        exclude_set = set(exclude or [])
        candidates = []
        for name, card in self._agents.items():
            if name in exclude_set:
                continue
            if card.get("status", "online") != "online":
                continue
            if self._agent_has_skill(card, task_type):
                candidates.append(card)
        if not candidates:
            return None
        candidates.sort(key=lambda c: c.get("priority", 100))
        return candidates[0]

    def list_agents(self, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
        agents = list(self._agents.values())
        if status:
            agents = [a for a in agents if a.get("status") == status]
        return agents

    @staticmethod
    def _agent_has_skill(card: Dict[str, Any], task_type: str) -> bool:
        for skill in card.get("skills", []):
            if skill.get("id") == task_type:
                return True
            if task_type in skill.get("tags", []):
                return True
        return False


# ===========================================================================
# Tests
# ===========================================================================


class TestRoutingDisabledByDefault:
    """When routing is disabled (the default), tickets always go to internal agents."""

    def test_default_config_routes_internally(self) -> None:
        config = _make_config()
        registry = _make_registry_with_gemini()
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL, error_log="x\n" * 100)
        result = agent.triage(ticket)
        assert result.assigned_to == "browser_investigator"
        assert "routing" not in result.metadata

    def test_explicit_false_routes_internally(self) -> None:
        routing = RoutingConfig(external_agents_enabled=False)
        config = _make_config(routing=routing)
        registry = _make_registry_with_gemini()
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL, error_log="x\n" * 100)
        result = agent.triage(ticket)
        assert result.assigned_to == "browser_investigator"

    def test_no_registry_routes_internally(self) -> None:
        routing = RoutingConfig(external_agents_enabled=True)
        config = _make_config(routing=routing)
        agent = TriageAgent(config, agent_registry=None)
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL, error_log="x\n" * 100)
        result = agent.triage(ticket)
        assert result.assigned_to == "browser_investigator"


class TestComplexityBasedRouting:
    """CRITICAL tickets with large error_logs route to Gemini when enabled."""

    def test_critical_large_log_routes_to_gemini(self) -> None:
        routing = RoutingConfig(
            external_agents_enabled=True,
            complexity_threshold=50,
        )
        config = _make_config(routing=routing)
        registry = _make_registry_with_gemini()
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.CRITICAL,
            error_log="ERROR: something bad\n" * 60,
        )
        result = agent.triage(ticket)
        assert result.assigned_to == "gemini-cli"
        assert result.metadata["routing"]["agent"] == "gemini-cli"
        assert result.metadata["routing"]["agent_type"] == "external"
        assert "complexity" in result.metadata["routing"]["reason"]

    def test_critical_small_log_routes_internally(self) -> None:
        routing = RoutingConfig(
            external_agents_enabled=True,
            complexity_threshold=50,
        )
        config = _make_config(routing=routing)
        registry = _make_registry_with_gemini()
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.CRITICAL,
            error_log="ERROR: something bad\n" * 10,
        )
        result = agent.triage(ticket)
        assert result.assigned_to == "browser_investigator"

    def test_high_severity_does_not_route_externally(self) -> None:
        routing = RoutingConfig(
            external_agents_enabled=True,
            complexity_threshold=50,
        )
        config = _make_config(routing=routing)
        registry = _make_registry_with_gemini()
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.HIGH,
            error_log="ERROR: something bad\n" * 100,
        )
        result = agent.triage(ticket)
        # HIGH severity does NOT trigger complexity routing
        assert result.assigned_to == "browser_investigator"

    def test_custom_threshold(self) -> None:
        routing = RoutingConfig(
            external_agents_enabled=True,
            complexity_threshold=10,
        )
        config = _make_config(routing=routing)
        registry = _make_registry_with_gemini()
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.CRITICAL,
            error_log="ERROR: line\n" * 15,
        )
        result = agent.triage(ticket)
        assert result.assigned_to == "gemini-cli"


class TestLabelBasedRouting:
    """Tickets with opencode/code-generation labels route to OpenCode."""

    def test_opencode_label_routes_to_opencode(self) -> None:
        routing = RoutingConfig(external_agents_enabled=True)
        config = _make_config(routing=routing)
        registry = _make_registry_with_both()
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.MEDIUM,
            labels=["opencode", "refactor"],
        )
        result = agent.triage(ticket)
        assert result.assigned_to == "opencode"
        assert "label-based" in result.metadata["routing"]["reason"]

    def test_code_generation_label(self) -> None:
        routing = RoutingConfig(external_agents_enabled=True)
        config = _make_config(routing=routing)
        registry = _make_registry_with_both()
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.LOW,
            labels=["code-generation"],
        )
        result = agent.triage(ticket)
        assert result.assigned_to == "opencode"

    def test_label_routing_takes_precedence_over_complexity(self) -> None:
        """When a ticket has both a matching label AND CRITICAL + large log,
        label-based routing wins (checked first)."""
        routing = RoutingConfig(
            external_agents_enabled=True,
            complexity_threshold=10,
        )
        config = _make_config(routing=routing)
        registry = _make_registry_with_both()
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.CRITICAL,
            error_log="ERROR\n" * 100,
            labels=["opencode"],
        )
        result = agent.triage(ticket)
        assert result.assigned_to == "opencode"
        assert "label-based" in result.metadata["routing"]["reason"]


class TestFallbackToInternal:
    """When external agents are unavailable, fall through to internal."""

    def test_empty_registry_falls_back(self) -> None:
        routing = RoutingConfig(external_agents_enabled=True)
        config = _make_config(routing=routing)
        registry = FakeAgentRegistry(agents={})
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.CRITICAL,
            error_log="ERROR\n" * 100,
        )
        result = agent.triage(ticket)
        assert result.assigned_to == "browser_investigator"
        assert "routing" not in result.metadata

    def test_offline_agent_falls_back(self) -> None:
        routing = RoutingConfig(external_agents_enabled=True)
        config = _make_config(routing=routing)
        registry = FakeAgentRegistry(agents={
            "gemini-cli": {
                "name": "gemini-cli",
                "status": "offline",
                "priority": 50,
                "skills": [{"id": "investigate", "tags": ["investigate"]}],
            },
        })
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.CRITICAL,
            error_log="ERROR\n" * 100,
        )
        result = agent.triage(ticket)
        assert result.assigned_to == "browser_investigator"

    def test_no_matching_capability_falls_back(self) -> None:
        routing = RoutingConfig(external_agents_enabled=True)
        config = _make_config(routing=routing)
        # Agent with only "review" skill, not "investigate"
        registry = FakeAgentRegistry(agents={
            "reviewer-bot": {
                "name": "reviewer-bot",
                "status": "online",
                "priority": 50,
                "skills": [{"id": "review", "tags": ["review"]}],
            },
        })
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.CRITICAL,
            error_log="ERROR\n" * 100,
        )
        result = agent.triage(ticket)
        assert result.assigned_to == "browser_investigator"


class TestRoutingMetadata:
    """Verify that routing metadata is correctly recorded on the ticket."""

    def test_metadata_fields(self) -> None:
        routing = RoutingConfig(external_agents_enabled=True, complexity_threshold=5)
        config = _make_config(routing=routing)
        registry = _make_registry_with_gemini()
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.CRITICAL,
            error_log="ERROR\n" * 10,
        )
        result = agent.triage(ticket)
        assert "routing" in result.metadata
        r = result.metadata["routing"]
        assert r["agent"] == "gemini-cli"
        assert r["agent_type"] == "external"
        assert isinstance(r["reason"], str) and len(r["reason"]) > 0

    def test_no_metadata_when_routed_internally(self) -> None:
        routing = RoutingConfig(external_agents_enabled=True, complexity_threshold=999)
        config = _make_config(routing=routing)
        registry = _make_registry_with_gemini()
        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(severity=TicketSeverity.MEDIUM)
        result = agent.triage(ticket)
        assert "routing" not in result.metadata


class TestRoutingConfig:
    """Unit tests for the RoutingConfig dataclass."""

    def test_defaults(self) -> None:
        rc = RoutingConfig()
        assert rc.external_agents_enabled is False
        assert rc.complexity_threshold == 50
        assert "investigation" in rc.capability_map
        assert "code_generation" in rc.capability_map

    def test_from_dict_empty(self) -> None:
        rc = RoutingConfig.from_dict({})
        assert rc.external_agents_enabled is False

    def test_from_dict_custom(self) -> None:
        rc = RoutingConfig.from_dict({
            "external_agents_enabled": True,
            "complexity_threshold": 100,
            "capability_map": {"investigation": "my-agent"},
        })
        assert rc.external_agents_enabled is True
        assert rc.complexity_threshold == 100
        assert rc.capability_map["investigation"] == "my-agent"

    def test_to_dict_roundtrip(self) -> None:
        rc = RoutingConfig(
            external_agents_enabled=True,
            complexity_threshold=25,
            capability_map={"test": "agent"},
        )
        d = rc.to_dict()
        rc2 = RoutingConfig.from_dict(d)
        assert rc2.external_agents_enabled == rc.external_agents_enabled
        assert rc2.complexity_threshold == rc.complexity_threshold
        assert rc2.capability_map == rc.capability_map
