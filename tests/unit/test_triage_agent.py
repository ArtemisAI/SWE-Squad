"""Unit tests for TriageAgent.

Covers: triage(), triage_batch(), _pick_assignee(), _detect_hitl(),
_classify_type(), _route_ticket(), _find_duplicate_in_progress(),
and ticket status transitions.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.config import RoutingConfig, SWETeamConfig
from src.swe_team.models import (
    AgentRole,
    SWEAgentConfig,
    SWETicket,
    TicketSeverity,
    TicketStatus,
    TicketType,
)
from src.swe_team.triage_agent import TriageAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    routing: Optional[RoutingConfig] = None,
    agents: Optional[List[SWEAgentConfig]] = None,
) -> SWETeamConfig:
    """Return a minimal SWETeamConfig with investigators."""
    if agents is None:
        agents = [
            SWEAgentConfig(
                name="browser_investigator",
                role=AgentRole.INVESTIGATOR,
                enabled=True,
            ),
            SWEAgentConfig(
                name="db_investigator",
                role=AgentRole.INVESTIGATOR,
                enabled=True,
            ),
        ]
    config = SWETeamConfig(agents=agents)
    if routing is not None:
        config.routing = routing
    return config


def _make_ticket(
    severity=TicketSeverity.HIGH,
    status=TicketStatus.OPEN,
    **kwargs,
):
    defaults = dict(
        ticket_id="T-TRI-TEST",
        title="Test triage ticket",
        description="Something needs fixing",
        severity=severity,
        status=status,
    )
    defaults.update(kwargs)
    return SWETicket(**defaults)


# ---------------------------------------------------------------------------
# 1. triage() with HIGH severity — assigned to correct agent
# ---------------------------------------------------------------------------

class TestTriageHighSeverity:
    def test_high_ticket_assigned_to_first_investigator(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(severity=TicketSeverity.HIGH)

        result = agent.triage(ticket)

        assert result.status == TicketStatus.TRIAGED
        assert result.assigned_to == "browser_investigator"

    def test_high_ticket_with_module_gets_specialist(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(
            severity=TicketSeverity.HIGH,
            source_module="database",
        )

        result = agent.triage(ticket)

        assert result.assigned_to == "db_investigator"


# ---------------------------------------------------------------------------
# 2. triage() with CRITICAL severity
# ---------------------------------------------------------------------------

class TestTriageCritical:
    def test_critical_ticket_assigned_to_first_investigator(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)

        result = agent.triage(ticket)

        assert result.status == TicketStatus.TRIAGED
        assert result.assigned_to == "browser_investigator"

    def test_critical_ticket_ignores_module_speciality(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(
            severity=TicketSeverity.CRITICAL,
            source_module="database",
        )

        result = agent.triage(ticket)

        # CRITICAL → first available, not specialist
        assert result.assigned_to == "browser_investigator"


# ---------------------------------------------------------------------------
# 3. Deduplication — same fingerprint blocks
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_duplicate_fingerprint_blocks_ticket(self):
        existing = _make_ticket(
            ticket_id="T-EXISTING",
            status=TicketStatus.IN_DEVELOPMENT,
        )
        existing.metadata["fingerprint"] = "abc12345xyz"

        store = MagicMock()
        store.list_by_status.return_value = [existing]

        config = _make_config()
        agent = TriageAgent(config, ticket_store=store)

        new_ticket = _make_ticket(ticket_id="T-NEW")
        new_ticket.metadata["fingerprint"] = "abc12345different"  # Same first 8 chars

        result = agent.triage(new_ticket)

        assert result.status == TicketStatus.BLOCKED
        assert "T-EXISTING" in result.blocked_by

    def test_no_duplicate_when_fingerprints_differ(self):
        existing = _make_ticket(
            ticket_id="T-EXISTING",
            status=TicketStatus.IN_DEVELOPMENT,
        )
        existing.metadata["fingerprint"] = "zzz99999xxx"

        store = MagicMock()
        store.list_by_status.return_value = [existing]

        config = _make_config()
        agent = TriageAgent(config, ticket_store=store)

        new_ticket = _make_ticket(ticket_id="T-NEW")
        new_ticket.metadata["fingerprint"] = "abc12345different"

        result = agent.triage(new_ticket)

        assert result.status == TicketStatus.TRIAGED

    def test_same_source_file_blocks(self):
        existing = _make_ticket(
            ticket_id="T-EXISTING",
            status=TicketStatus.IN_DEVELOPMENT,
        )
        existing.metadata["source_file"] = "src/auth/login.py"

        store = MagicMock()
        store.list_by_status.return_value = [existing]

        config = _make_config()
        agent = TriageAgent(config, ticket_store=store)

        new_ticket = _make_ticket(ticket_id="T-NEW")
        new_ticket.metadata["source_file"] = "src/auth/login.py"

        result = agent.triage(new_ticket)

        assert result.status == TicketStatus.BLOCKED


# ---------------------------------------------------------------------------
# 4. Severity classification via _classify_type
# ---------------------------------------------------------------------------

class TestClassifyType:
    def test_error_log_classifies_as_bug(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(error_log="Traceback:\n  RuntimeError: boom\nFailed at line 42")

        result = agent._classify_type(ticket)
        assert result == TicketType.BUG

    def test_regression_keyword_in_error_log(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(
            error_log="This regression broke the auth module. Used to work before deploy."
        )

        result = agent._classify_type(ticket)
        assert result == TicketType.REGRESSION

    def test_feature_keyword_in_title(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(title="feat: add dark mode support", error_log=None)

        result = agent._classify_type(ticket)
        assert result == TicketType.FEATURE

    def test_security_keyword(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(title="SEC P0 vulnerability in login", error_log=None)

        result = agent._classify_type(ticket)
        assert result == TicketType.SECURITY

    def test_no_signal_defaults_to_feature(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(
            title="Some random thing",
            description="No keywords here",
            error_log=None,
        )

        result = agent._classify_type(ticket)
        assert result == TicketType.FEATURE


# ---------------------------------------------------------------------------
# 5. Routing disabled — always internal
# ---------------------------------------------------------------------------

class TestRoutingDisabled:
    def test_default_routing_disabled(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)

        agent_id, agent_type, reason = agent._route_ticket(ticket)

        assert agent_id is None
        assert agent_type is None


# ---------------------------------------------------------------------------
# 6. Routing enabled with AgentRegistry
# ---------------------------------------------------------------------------

class TestRoutingEnabled:
    def test_critical_complex_routes_to_gemini(self):
        routing = RoutingConfig(
            external_agents_enabled=True,
            complexity_threshold=5,
            capability_map={"investigation": "gemini-cli"},
        )
        config = _make_config(routing=routing)

        registry = MagicMock()
        gemini_card = {"name": "gemini-cli", "status": "online"}
        registry.get.return_value = gemini_card
        registry._agent_has_skill.return_value = True

        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.CRITICAL,
            error_log="\n".join([f"Error line {i}" for i in range(100)]),
        )

        agent_id, agent_type, reason = agent._route_ticket(ticket)

        assert agent_id == "gemini-cli"
        assert "complexity" in reason

    def test_label_routing_to_opencode(self):
        routing = RoutingConfig(
            external_agents_enabled=True,
            capability_map={"code_generation": "opencode"},
        )
        config = _make_config(routing=routing)

        registry = MagicMock()
        opencode_card = {"name": "opencode", "status": "online"}
        registry.get.return_value = opencode_card
        registry._agent_has_skill.return_value = True

        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(labels=["opencode"])

        agent_id, agent_type, reason = agent._route_ticket(ticket)

        assert agent_id == "opencode"
        assert "label-based" in reason

    def test_no_external_agent_available_falls_through(self):
        routing = RoutingConfig(
            external_agents_enabled=True,
            complexity_threshold=5,
        )
        config = _make_config(routing=routing)

        registry = MagicMock()
        registry.get.return_value = None
        registry.select_agent.return_value = None

        agent = TriageAgent(config, agent_registry=registry)
        ticket = _make_ticket(
            severity=TicketSeverity.CRITICAL,
            error_log="\n".join(["Error"] * 100),
        )

        agent_id, _, _ = agent._route_ticket(ticket)
        assert agent_id is None


# ---------------------------------------------------------------------------
# 7. assign_ticket sets assigned_to
# ---------------------------------------------------------------------------

class TestAssignment:
    def test_triage_sets_assigned_to(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket()

        result = agent.triage(ticket)

        assert result.assigned_to is not None
        assert result.assigned_to != ""


# ---------------------------------------------------------------------------
# 8. Status transitions
# ---------------------------------------------------------------------------

class TestStatusTransitions:
    def test_open_to_triaged(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(status=TicketStatus.OPEN)

        result = agent.triage(ticket)
        assert result.status == TicketStatus.TRIAGED

    def test_blocked_ticket_stays_blocked(self):
        """When duplicate detected, ticket transitions to BLOCKED."""
        existing = _make_ticket(
            ticket_id="T-EXIST",
            status=TicketStatus.IN_DEVELOPMENT,
        )
        existing.metadata["fingerprint"] = "same1234rest"

        store = MagicMock()
        store.list_by_status.return_value = [existing]

        config = _make_config()
        agent = TriageAgent(config, ticket_store=store)

        ticket = _make_ticket(ticket_id="T-NEW")
        ticket.metadata["fingerprint"] = "same1234other"

        result = agent.triage(ticket)
        assert result.status == TicketStatus.BLOCKED


# ---------------------------------------------------------------------------
# 9. HITL detection
# ---------------------------------------------------------------------------

class TestHITLDetection:
    def test_captcha_triggers_hitl(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(
            title="Login page shows CAPTCHA",
            description="Cannot proceed past captcha",
        )

        result = agent.triage(ticket)

        assert result.metadata.get("needs_hitl") is True
        assert "CAPTCHA" in result.metadata.get("hitl_reason", "")
        assert result.assigned_to == agent.HUMAN_ASSIGNEE

    def test_chronic_failure_triggers_hitl(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket()
        ticket.metadata["attempts"] = [
            {"result": "fail"},
            {"result": "fail"},
            {"result": "fail"},
        ]

        result = agent.triage(ticket)

        assert result.metadata.get("needs_hitl") is True
        assert "Chronic failure" in result.metadata.get("hitl_reason", "")

    def test_already_flagged_preserves_hitl(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket()
        ticket.metadata["needs_hitl"] = True
        ticket.metadata["hitl_reason"] = "Previously escalated"

        result = agent.triage(ticket)

        assert result.metadata["needs_hitl"] is True
        assert result.assigned_to == agent.HUMAN_ASSIGNEE

    def test_normal_ticket_no_hitl(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(
            title="Simple bug in CSS",
            description="Button color is wrong",
        )

        result = agent.triage(ticket)

        assert result.metadata.get("needs_hitl") is not True

    def test_api_key_401_triggers_hitl(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(
            error_log="API key returned 401. All keys are returning 401 errors.",
        )

        result = agent.triage(ticket)

        assert result.metadata.get("needs_hitl") is True


# ---------------------------------------------------------------------------
# 10. Batch triage with priority ordering
# ---------------------------------------------------------------------------

class TestBatchTriage:
    def test_batch_sorts_critical_first(self):
        config = _make_config()
        agent = TriageAgent(config)

        tickets = [
            _make_ticket(ticket_id="T-LOW", severity=TicketSeverity.LOW),
            _make_ticket(ticket_id="T-CRIT", severity=TicketSeverity.CRITICAL),
            _make_ticket(ticket_id="T-HIGH", severity=TicketSeverity.HIGH),
            _make_ticket(ticket_id="T-MED", severity=TicketSeverity.MEDIUM),
        ]

        result = agent.triage_batch(tickets)

        # All should be triaged
        assert len(result) == 4
        # First triaged should be CRITICAL
        assert result[0].ticket_id == "T-CRIT"
        assert result[1].ticket_id == "T-HIGH"

    def test_batch_triages_all(self):
        config = _make_config()
        agent = TriageAgent(config)
        tickets = [
            _make_ticket(ticket_id=f"T-B-{i}")
            for i in range(5)
        ]

        result = agent.triage_batch(tickets)

        assert len(result) == 5
        for t in result:
            assert t.status == TicketStatus.TRIAGED


# ---------------------------------------------------------------------------
# 11. Event building
# ---------------------------------------------------------------------------

class TestBuildEvents:
    def test_build_events_creates_one_per_ticket(self):
        config = _make_config()
        agent = TriageAgent(config)

        tickets = [
            _make_ticket(ticket_id="T-E1", status=TicketStatus.TRIAGED),
            _make_ticket(ticket_id="T-E2", status=TicketStatus.TRIAGED),
        ]
        tickets[0].assigned_to = "browser_investigator"
        tickets[1].assigned_to = "db_investigator"

        events = agent.build_events(tickets)

        assert len(events) == 2


# ---------------------------------------------------------------------------
# 12. No investigators configured
# ---------------------------------------------------------------------------

class TestNoInvestigators:
    def test_no_investigators_assigns_none(self):
        config = _make_config(agents=[])
        agent = TriageAgent(config)
        ticket = _make_ticket()

        result = agent.triage(ticket)

        assert result.assigned_to is None
        assert result.status == TicketStatus.TRIAGED


# ---------------------------------------------------------------------------
# 13. Module speciality routing
# ---------------------------------------------------------------------------

class TestModuleSpeciality:
    def test_scraping_module_routes_to_browser(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(source_module="scraping")

        result = agent.triage(ticket)

        assert result.assigned_to == "browser_investigator"

    def test_unknown_module_falls_to_first(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(source_module="unknown_module")

        result = agent.triage(ticket)

        assert result.assigned_to == "browser_investigator"


# ---------------------------------------------------------------------------
# 14. Type classification sets ticket_type
# ---------------------------------------------------------------------------

class TestTypeClassificationInTriage:
    def test_triage_sets_ticket_type(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(title="fix: broken auth endpoint")

        agent.triage(ticket)

        assert ticket.ticket_type == TicketType.BUG

    def test_enhancement_keyword(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(
            title="Improve scraper performance",
            description="optimize the retry logic",
            error_log=None,
        )

        agent.triage(ticket)

        assert ticket.ticket_type == TicketType.ENHANCEMENT


# ---------------------------------------------------------------------------
# 15. Double regression detection
# ---------------------------------------------------------------------------

class TestDoubleRegression:
    def test_double_regression_triggers_hitl(self):
        config = _make_config()
        agent = TriageAgent(config)
        ticket = _make_ticket(
            title="[REGRESSION] [REGRESSION] Auth token expired again",
        )

        result = agent.triage(ticket)

        assert result.metadata.get("needs_hitl") is True
        assert "Double-regression" in result.metadata.get("hitl_reason", "")
