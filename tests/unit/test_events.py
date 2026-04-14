"""
Unit tests for src/swe_team/events.py

Tests cover:
- SWEEventType enum values (all 16 members)
- SWEEvent dataclass creation with required and optional fields
- SWEEvent.to_dict() / from_dict() roundtrip
- Factory methods: issue_detected, triage_complete, investigation_complete,
  dev_complete, test_complete, deploy_complete, rollback_triggered,
  stability_gate_result
- event_id is auto-generated and unique
- timestamp is auto-generated
"""

from __future__ import annotations

import pytest

from src.swe_team.events import SWEEvent, SWEEventType


# ---------------------------------------------------------------------------
# SWEEventType enum
# ---------------------------------------------------------------------------

class TestSWEEventType:
    def test_all_event_type_values(self):
        expected = {
            "issue_detected",
            "triage_complete",
            "investigation_started",
            "investigation_complete",
            "dev_started",
            "dev_complete",
            "review_requested",
            "review_complete",
            "test_started",
            "test_complete",
            "deploy_started",
            "deploy_complete",
            "rollback_triggered",
            "stability_check",
            "stability_gate_result",
        }
        actual = {e.value for e in SWEEventType}
        assert expected.issubset(actual), f"Missing: {expected - actual}"

    def test_enum_members_accessible_by_name(self):
        assert SWEEventType.ISSUE_DETECTED.value == "issue_detected"
        assert SWEEventType.ROLLBACK_TRIGGERED.value == "rollback_triggered"
        assert SWEEventType.STABILITY_GATE_RESULT.value == "stability_gate_result"


# ---------------------------------------------------------------------------
# SWEEvent creation
# ---------------------------------------------------------------------------

class TestSWEEventCreation:
    def test_minimal_creation(self):
        e = SWEEvent(
            event=SWEEventType.ISSUE_DETECTED,
            ticket_id="t-abc123",
            source_agent="monitor",
        )
        assert e.event == SWEEventType.ISSUE_DETECTED
        assert e.ticket_id == "t-abc123"
        assert e.source_agent == "monitor"

    def test_event_id_auto_generated(self):
        e1 = SWEEvent(
            event=SWEEventType.TRIAGE_COMPLETE,
            ticket_id="t-1",
            source_agent="triage",
        )
        e2 = SWEEvent(
            event=SWEEventType.TRIAGE_COMPLETE,
            ticket_id="t-2",
            source_agent="triage",
        )
        assert len(e1.event_id) == 16
        assert e1.event_id != e2.event_id

    def test_timestamp_auto_generated(self):
        e = SWEEvent(
            event=SWEEventType.DEV_COMPLETE,
            ticket_id="t-1",
            source_agent="developer",
        )
        assert isinstance(e.timestamp, str)
        assert "T" in e.timestamp  # ISO format

    def test_default_empty_payload(self):
        e = SWEEvent(
            event=SWEEventType.TEST_COMPLETE,
            ticket_id="t-1",
            source_agent="tester",
        )
        assert e.payload == {}

    def test_default_empty_target_agents(self):
        e = SWEEvent(
            event=SWEEventType.DEPLOY_COMPLETE,
            ticket_id="t-1",
            source_agent="deployer",
        )
        assert e.target_agents == []

    def test_explicit_payload_and_targets(self):
        e = SWEEvent(
            event=SWEEventType.STABILITY_CHECK,
            ticket_id="t-1",
            source_agent="ralph",
            payload={"verdict": "pass"},
            target_agents=["developer", "deployer"],
        )
        assert e.payload["verdict"] == "pass"
        assert "developer" in e.target_agents


# ---------------------------------------------------------------------------
# SWEEvent serialisation
# ---------------------------------------------------------------------------

class TestSWEEventSerialization:
    def test_to_dict_has_required_keys(self):
        e = SWEEvent(
            event=SWEEventType.INVESTIGATION_COMPLETE,
            ticket_id="t-xyz",
            source_agent="investigator",
        )
        d = e.to_dict()
        for key in ("event", "ticket_id", "source_agent", "payload",
                    "event_id", "timestamp", "target_agents"):
            assert key in d, f"Missing key: {key}"

    def test_to_dict_event_is_string(self):
        e = SWEEvent(
            event=SWEEventType.ROLLBACK_TRIGGERED,
            ticket_id="t-1",
            source_agent="deployer",
        )
        d = e.to_dict()
        assert d["event"] == "rollback_triggered"

    def test_from_dict_roundtrip(self):
        e = SWEEvent(
            event=SWEEventType.DEV_COMPLETE,
            ticket_id="t-dev",
            source_agent="developer",
            payload={"branch": "fix/bug-123", "files_changed": 3},
            target_agents=["reviewer"],
        )
        d = e.to_dict()
        e2 = SWEEvent.from_dict(d)
        assert e2.event == SWEEventType.DEV_COMPLETE
        assert e2.ticket_id == "t-dev"
        assert e2.source_agent == "developer"
        assert e2.payload["branch"] == "fix/bug-123"
        assert e2.event_id == e.event_id
        assert "reviewer" in e2.target_agents

    def test_from_dict_missing_optional_fields_get_defaults(self):
        data = {
            "event": "test_complete",
            "ticket_id": "t-1",
            "source_agent": "tester",
        }
        e = SWEEvent.from_dict(data)
        assert e.payload == {}
        assert e.target_agents == []
        assert len(e.event_id) == 16


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

class TestSWEEventFactories:
    def test_issue_detected_factory(self):
        e = SWEEvent.issue_detected(
            "t-001",
            "monitor",
            error_summary="NullPointerException in auth",
            module="auth",
            severity="critical",
        )
        assert e.event == SWEEventType.ISSUE_DETECTED
        assert e.ticket_id == "t-001"
        assert e.payload["error_summary"] == "NullPointerException in auth"
        assert e.payload["module"] == "auth"
        assert e.payload["severity"] == "critical"

    def test_triage_complete_factory(self):
        e = SWEEvent.triage_complete(
            "t-002",
            "triage",
            assigned_to="investigator-1",
            severity="high",
        )
        assert e.event == SWEEventType.TRIAGE_COMPLETE
        assert e.payload["assigned_to"] == "investigator-1"
        assert e.payload["severity"] == "high"

    def test_investigation_complete_factory(self):
        e = SWEEvent.investigation_complete(
            "t-003",
            "investigator",
            report="Full report here",
            root_cause="DB pool",
        )
        assert e.event == SWEEventType.INVESTIGATION_COMPLETE
        assert e.payload["report"] == "Full report here"
        assert e.payload["root_cause"] == "DB pool"

    def test_dev_complete_factory(self):
        e = SWEEvent.dev_complete(
            "t-004",
            "developer",
            branch="fix/issue-42",
            files_changed=5,
        )
        assert e.event == SWEEventType.DEV_COMPLETE
        assert e.payload["branch"] == "fix/issue-42"
        assert e.payload["files_changed"] == 5

    def test_test_complete_factory(self):
        e = SWEEvent.test_complete(
            "t-005",
            "tester",
            passed=True,
            total=100,
            failures=0,
        )
        assert e.event == SWEEventType.TEST_COMPLETE
        assert e.payload["passed"] is True
        assert e.payload["total"] == 100
        assert e.payload["failures"] == 0

    def test_deploy_complete_factory(self):
        e = SWEEvent.deploy_complete(
            "t-006",
            "deployer",
            deployment_id="deploy-xyz",
            success=True,
        )
        assert e.event == SWEEventType.DEPLOY_COMPLETE
        assert e.payload["deployment_id"] == "deploy-xyz"
        assert e.payload["success"] is True

    def test_rollback_triggered_factory(self):
        e = SWEEvent.rollback_triggered(
            "t-007",
            "deployer",
            reason="test failure",
            deployment_id="deploy-xyz",
        )
        assert e.event == SWEEventType.ROLLBACK_TRIGGERED
        assert e.payload["reason"] == "test failure"
        assert e.payload["deployment_id"] == "deploy-xyz"

    def test_stability_gate_result_factory(self):
        e = SWEEvent.stability_gate_result(
            "t-008",
            "ralph",
            verdict="block",
            details="2 critical bugs open",
        )
        assert e.event == SWEEventType.STABILITY_GATE_RESULT
        assert e.payload["verdict"] == "block"
        assert e.payload["details"] == "2 critical bugs open"

    def test_factory_default_payload_values(self):
        """Factories with no keyword args should still produce valid events."""
        e = SWEEvent.issue_detected("t-001", "monitor")
        assert e.payload["error_summary"] == ""
        assert e.payload["severity"] == "medium"

        e2 = SWEEvent.dev_complete("t-002", "developer")
        assert e2.payload["branch"] == ""
        assert e2.payload["files_changed"] == 0
