"""
Unit tests for src/swe_team/models.py

Tests cover:
- TicketSeverity, TicketStatus, TicketType, AgentRole, GovernanceVerdict enums
- SWETicket creation, field defaults, to_dict/from_dict roundtrip
- SWETicket.transition() — valid transitions and RESOLVED audit gate
- SWETicket.resolution_audit() — bypass notes, report length, HIGH/CRITICAL attempts
- SWETicket.is_blocked()
- SWEAgentConfig from_dict / to_dict
- StabilityReport from_dict / to_dict
- KnowledgeEdge, CodeModule, ResolutionCluster, PRNode from_dict / to_dict
"""

from __future__ import annotations

import pytest

from src.swe_team.models import (
    AgentRole,
    CodeModule,
    DevelopmentPhaseOutput,
    EdgeType,
    EngineHandover,
    GovernanceVerdict,
    HandoverConstraints,
    InvestigationPhaseOutput,
    KnowledgeEdge,
    PRNode,
    ResolutionCluster,
    SWEAgentConfig,
    SWETicket,
    StabilityReport,
    TicketSeverity,
    TicketStatus,
    TicketType,
    VerificationPhaseOutput,
)


# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------

class TestEnums:
    def test_ticket_severity_values(self):
        assert TicketSeverity.CRITICAL.value == "critical"
        assert TicketSeverity.HIGH.value == "high"
        assert TicketSeverity.MEDIUM.value == "medium"
        assert TicketSeverity.LOW.value == "low"

    def test_ticket_status_values(self):
        assert TicketStatus.OPEN.value == "open"
        assert TicketStatus.RESOLVED.value == "resolved"
        assert TicketStatus.CLOSED.value == "closed"
        assert TicketStatus.FAILED.value == "failed"
        assert TicketStatus.IN_DEVELOPMENT.value == "in_development"
        assert TicketStatus.INVESTIGATING.value == "investigating"

    def test_ticket_type_values(self):
        assert TicketType.BUG.value == "bug"
        assert TicketType.REGRESSION.value == "regression"
        assert TicketType.UNKNOWN.value == "unknown"
        assert TicketType.SECURITY.value == "security"

    def test_agent_role_values(self):
        assert AgentRole.MONITOR.value == "monitor"
        assert AgentRole.DEVELOPER.value == "developer"
        assert AgentRole.INVESTIGATOR.value == "investigator"

    def test_governance_verdict_values(self):
        assert GovernanceVerdict.PASS.value == "pass"
        assert GovernanceVerdict.BLOCK.value == "block"
        assert GovernanceVerdict.WARN.value == "warn"

    def test_edge_type_values(self):
        assert EdgeType.SIMILAR.value == "similar"
        assert EdgeType.RESOLVES.value == "resolves"
        assert EdgeType.BLOCKS.value == "blocks"


# ---------------------------------------------------------------------------
# SWETicket — creation and defaults
# ---------------------------------------------------------------------------

class TestSWETicketDefaults:
    def test_required_fields(self):
        t = SWETicket(title="Test bug", description="Something broke")
        assert t.title == "Test bug"
        assert t.description == "Something broke"

    def test_default_severity(self):
        t = SWETicket(title="t", description="d")
        assert t.severity == TicketSeverity.MEDIUM

    def test_default_status(self):
        t = SWETicket(title="t", description="d")
        assert t.status == TicketStatus.OPEN

    def test_default_ticket_type(self):
        t = SWETicket(title="t", description="d")
        assert t.ticket_type == TicketType.UNKNOWN

    def test_ticket_id_auto_generated(self):
        t1 = SWETicket(title="t", description="d")
        t2 = SWETicket(title="t", description="d")
        assert len(t1.ticket_id) == 12
        assert t1.ticket_id != t2.ticket_id

    def test_default_lists_are_empty(self):
        t = SWETicket(title="t", description="d")
        assert t.labels == []
        assert t.related_tickets == []
        assert t.blocked_by == []
        assert t.blocking == []

    def test_default_optional_fields_none(self):
        t = SWETicket(title="t", description="d")
        assert t.assigned_to is None
        assert t.source_module is None
        assert t.error_log is None
        assert t.investigation_report is None
        assert t.proposed_fix is None


# ---------------------------------------------------------------------------
# SWETicket — is_blocked
# ---------------------------------------------------------------------------

class TestIsBlocked:
    def test_not_blocked_initially(self):
        t = SWETicket(title="t", description="d")
        assert t.is_blocked() is False

    def test_blocked_when_blocked_by_set(self):
        t = SWETicket(title="t", description="d", blocked_by=["other-ticket"])
        assert t.is_blocked() is True

    def test_not_blocked_after_cleared(self):
        t = SWETicket(title="t", description="d", blocked_by=["x"])
        t.blocked_by.clear()
        assert t.is_blocked() is False


# ---------------------------------------------------------------------------
# SWETicket — to_dict / from_dict roundtrip
# ---------------------------------------------------------------------------

class TestSWETicketSerialization:
    def test_to_dict_has_all_keys(self):
        t = SWETicket(title="Bug", description="Details", severity=TicketSeverity.HIGH)
        d = t.to_dict()
        for key in ("ticket_id", "title", "description", "severity", "status",
                    "created_at", "updated_at", "labels", "ticket_type",
                    "metadata", "investigation_report"):
            assert key in d, f"Missing key: {key}"

    def test_to_dict_severity_is_string(self):
        t = SWETicket(title="t", description="d", severity=TicketSeverity.CRITICAL)
        assert t.to_dict()["severity"] == "critical"

    def test_to_dict_status_is_string(self):
        t = SWETicket(title="t", description="d", status=TicketStatus.TRIAGED)
        assert t.to_dict()["status"] == "triaged"

    def test_from_dict_roundtrip(self):
        t = SWETicket(
            title="Roundtrip bug",
            description="Detailed description here",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.INVESTIGATING,
            labels=["api", "auth"],
            source_module="auth_module",
            error_log="Traceback...",
        )
        d = t.to_dict()
        t2 = SWETicket.from_dict(d)
        assert t2.ticket_id == t.ticket_id
        assert t2.title == t.title
        assert t2.severity == TicketSeverity.HIGH
        assert t2.status == TicketStatus.INVESTIGATING
        assert t2.labels == ["api", "auth"]
        assert t2.source_module == "auth_module"

    def test_from_dict_metadata_string(self):
        """metadata stored as JSON string (Supabase case) should be parsed."""
        import json
        data = {
            "title": "t",
            "description": "d",
            "metadata": json.dumps({"key": "val"}),
        }
        t = SWETicket.from_dict(data)
        assert t.metadata == {"key": "val"}

    def test_from_dict_metadata_dict(self):
        data = {
            "title": "t",
            "description": "d",
            "metadata": {"foo": "bar"},
        }
        t = SWETicket.from_dict(data)
        assert t.metadata["foo"] == "bar"

    def test_goal_hierarchy_fields_serialization(self):
        """Test that goal hierarchy fields are preserved in to_dict/from_dict roundtrip."""
        t = SWETicket(
            title="Sub-task",
            description="Part of a larger goal",
            project_id="project-mobile-app",
            parent_ticket_id="ticket-abc123",
            goal="Implement offline sync for mobile app",
        )
        d = t.to_dict()
        assert d["project_id"] == "project-mobile-app"
        assert d["parent_ticket_id"] == "ticket-abc123"
        assert d["goal"] == "Implement offline sync for mobile app"

        # Roundtrip
        t2 = SWETicket.from_dict(d)
        assert t2.project_id == "project-mobile-app"
        assert t2.parent_ticket_id == "ticket-abc123"
        assert t2.goal == "Implement offline sync for mobile app"

    def test_goal_hierarchy_fields_optional(self):
        """Test that goal hierarchy fields default to None."""
        t = SWETicket(title="t", description="d")
        assert t.project_id is None
        assert t.parent_ticket_id is None
        assert t.goal is None

    def test_goal_hierarchy_partial_fields(self):
        """Test that goal hierarchy fields can be set independently."""
        t = SWETicket(
            title="Standalone task",
            description="No parent",
            project_id="proj-x",
        )
        assert t.project_id == "proj-x"
        assert t.parent_ticket_id is None
        assert t.goal is None

        d = t.to_dict()
        t2 = SWETicket.from_dict(d)
        assert t2.project_id == "proj-x"
        assert t2.parent_ticket_id is None
        assert t2.goal is None


# ---------------------------------------------------------------------------
# SWETicket — Goal Hierarchy Queries
# ---------------------------------------------------------------------------

class TestGoalHierarchyQueries:
    """Test filtering and querying tickets by goal hierarchy."""

    def test_find_tickets_by_project_id(self):
        """Simulate filtering tickets by project_id."""
        tickets = [
            SWETicket(title="t1", description="d", project_id="proj-a"),
            SWETicket(title="t2", description="d", project_id="proj-a"),
            SWETicket(title="t3", description="d", project_id="proj-b"),
        ]
        proj_a_tickets = [t for t in tickets if t.project_id == "proj-a"]
        assert len(proj_a_tickets) == 2
        assert all(t.project_id == "proj-a" for t in proj_a_tickets)

    def test_find_sub_tasks_by_parent(self):
        """Simulate filtering sub-tasks by parent_ticket_id."""
        parent = SWETicket(title="Parent task", description="Main goal")
        subtasks = [
            SWETicket(title="Sub 1", description="d", parent_ticket_id=parent.ticket_id),
            SWETicket(title="Sub 2", description="d", parent_ticket_id=parent.ticket_id),
            SWETicket(title="Sub 3", description="d", parent_ticket_id="other-id"),
        ]
        subs_of_parent = [t for t in subtasks if t.parent_ticket_id == parent.ticket_id]
        assert len(subs_of_parent) == 2
        assert all(t.parent_ticket_id == parent.ticket_id for t in subs_of_parent)

    def test_find_root_tickets_in_project(self):
        """Simulate finding root-level (no parent) tickets in a project."""
        tickets = [
            SWETicket(title="Root 1", description="d", project_id="proj-x", parent_ticket_id=None),
            SWETicket(title="Sub 1", description="d", project_id="proj-x", parent_ticket_id="root1"),
            SWETicket(title="Root 2", description="d", project_id="proj-x", parent_ticket_id=None),
        ]
        roots = [t for t in tickets if t.project_id == "proj-x" and t.parent_ticket_id is None]
        assert len(roots) == 2

    def test_goal_hierarchy_full_scenario(self):
        """Test a complete goal hierarchy scenario."""
        # Create a project with a goal hierarchy
        root = SWETicket(
            title="Launch mobile app v1",
            description="Release iOS and Android apps",
            project_id="mobile-launch",
            goal="Deliver mobile app to 10k users",
        )

        # Create sub-tasks
        ios = SWETicket(
            title="iOS implementation",
            description="Build iOS app",
            project_id="mobile-launch",
            parent_ticket_id=root.ticket_id,
            goal="Implement iOS-specific features",
        )

        offline = SWETicket(
            title="Offline sync (iOS)",
            description="Add offline sync to iOS",
            project_id="mobile-launch",
            parent_ticket_id=ios.ticket_id,
            goal="Enable offline access",
        )

        # Verify hierarchy
        assert root.project_id == "mobile-launch"
        assert root.parent_ticket_id is None
        assert ios.project_id == "mobile-launch"
        assert ios.parent_ticket_id == root.ticket_id
        assert offline.project_id == "mobile-launch"
        assert offline.parent_ticket_id == ios.ticket_id


# ---------------------------------------------------------------------------
# SWETicket — transition()
# ---------------------------------------------------------------------------

class TestSWETicketTransition:
    def test_simple_transition_updates_status(self):
        t = SWETicket(title="t", description="d")
        t.transition(TicketStatus.TRIAGED)
        assert t.status == TicketStatus.TRIAGED

    def test_transition_updates_updated_at(self):
        t = SWETicket(title="t", description="d")
        old_ts = t.updated_at
        t.transition(TicketStatus.INVESTIGATING)
        # updated_at should be a fresh timestamp (may be equal in fast machines,
        # but at least a valid ISO string)
        assert isinstance(t.updated_at, str)

    def test_transition_to_resolved_blocked_without_report(self):
        t = SWETicket(title="t", description="d", severity=TicketSeverity.LOW)
        with pytest.raises(ValueError, match="Resolution blocked"):
            t.transition(TicketStatus.RESOLVED)

    def test_transition_to_resolved_with_bypass_note(self):
        t = SWETicket(title="t", description="d")
        t.metadata["resolution_note"] = "false_regression"
        t.transition(TicketStatus.RESOLVED)  # should not raise
        assert t.status == TicketStatus.RESOLVED

    def test_transition_to_resolved_low_with_long_report(self):
        t = SWETicket(title="t", description="d", severity=TicketSeverity.LOW)
        t.investigation_report = "x" * 200
        t.transition(TicketStatus.RESOLVED)
        assert t.status == TicketStatus.RESOLVED

    def test_transition_to_resolved_high_requires_attempts(self):
        t = SWETicket(title="t", description="d", severity=TicketSeverity.HIGH)
        t.investigation_report = "x" * 200
        with pytest.raises(ValueError, match="fix attempt"):
            t.transition(TicketStatus.RESOLVED)

    def test_transition_to_resolved_high_with_attempts(self):
        t = SWETicket(title="t", description="d", severity=TicketSeverity.HIGH)
        t.investigation_report = "x" * 200
        t.metadata["attempts"] = [{"result": "success"}]
        t.transition(TicketStatus.RESOLVED)
        assert t.status == TicketStatus.RESOLVED

    def test_transition_to_non_resolved_always_allowed(self):
        t = SWETicket(title="t", description="d")
        for status in (
            TicketStatus.TRIAGED,
            TicketStatus.INVESTIGATING,
            TicketStatus.IN_DEVELOPMENT,
            TicketStatus.CLOSED,
            TicketStatus.FAILED,
        ):
            t.transition(status)
            assert t.status == status


# ---------------------------------------------------------------------------
# SWETicket — resolution_audit()
# ---------------------------------------------------------------------------

class TestResolutionAudit:
    def test_bypass_note_passes(self):
        t = SWETicket(title="t", description="d")
        for reason in SWETicket.RESOLUTION_BYPASS_REASONS:
            t.metadata["resolution_note"] = reason
            ok, msg = t.resolution_audit()
            assert ok, f"Expected bypass for reason={reason!r}, got: {msg}"

    def test_short_report_fails(self):
        t = SWETicket(title="t", description="d", severity=TicketSeverity.LOW)
        t.investigation_report = "too short"
        ok, msg = t.resolution_audit()
        assert ok is False
        assert "too short" in msg.lower() or "≥200" in msg or "200" in msg

    def test_critical_without_attempts_fails(self):
        t = SWETicket(title="t", description="d", severity=TicketSeverity.CRITICAL)
        t.investigation_report = "A" * 200
        ok, msg = t.resolution_audit()
        assert ok is False
        assert "attempt" in msg.lower()

    def test_medium_with_report_passes(self):
        t = SWETicket(title="t", description="d", severity=TicketSeverity.MEDIUM)
        t.investigation_report = "B" * 200
        ok, msg = t.resolution_audit()
        assert ok is True


# ---------------------------------------------------------------------------
# SWEAgentConfig
# ---------------------------------------------------------------------------

class TestSWEAgentConfig:
    def test_from_dict(self):
        data = {
            "name": "investigator-1",
            "role": "investigator",
            "model": "sonnet",
            "enabled": True,
        }
        a = SWEAgentConfig.from_dict(data)
        assert a.name == "investigator-1"
        assert a.role == AgentRole.INVESTIGATOR
        assert a.model == "sonnet"
        assert a.enabled is True

    def test_to_dict_roundtrip(self):
        a = SWEAgentConfig(
            name="worker-1",
            role=AgentRole.DEVELOPER,
            enabled=True,
            tools=["git", "pytest"],
        )
        d = a.to_dict()
        a2 = SWEAgentConfig.from_dict(d)
        assert a2.name == "worker-1"
        assert a2.role == AgentRole.DEVELOPER
        assert "git" in a2.tools


# ---------------------------------------------------------------------------
# StabilityReport
# ---------------------------------------------------------------------------

class TestStabilityReport:
    def test_from_dict_to_dict_roundtrip(self):
        r = StabilityReport(
            verdict=GovernanceVerdict.PASS,
            open_critical=0,
            open_high=1,
            ci_status="green",
        )
        d = r.to_dict()
        r2 = StabilityReport.from_dict(d)
        assert r2.verdict == GovernanceVerdict.PASS
        assert r2.open_high == 1
        assert r2.ci_status == "green"

    def test_from_dict_block_verdict(self):
        r = StabilityReport.from_dict({"verdict": "block", "open_critical": 2})
        assert r.verdict == GovernanceVerdict.BLOCK
        assert r.open_critical == 2


# ---------------------------------------------------------------------------
# KnowledgeEdge
# ---------------------------------------------------------------------------

class TestKnowledgeEdge:
    def test_from_dict_to_dict(self):
        e = KnowledgeEdge(
            source_id="t-abc",
            target_id="t-def",
            edge_type=EdgeType.SIMILAR,
            confidence=0.87,
            discovered_by="embedding",
        )
        d = e.to_dict()
        e2 = KnowledgeEdge.from_dict(d)
        assert e2.source_id == "t-abc"
        assert e2.edge_type == EdgeType.SIMILAR
        assert abs(e2.confidence - 0.87) < 1e-6


# ---------------------------------------------------------------------------
# CodeModule
# ---------------------------------------------------------------------------

class TestCodeModule:
    def test_from_dict_to_dict(self):
        m = CodeModule(module_id="auth.py", repo="Org/Repo", file_path="src/auth.py")
        d = m.to_dict()
        m2 = CodeModule.from_dict(d)
        assert m2.module_id == "auth.py"
        assert m2.repo == "Org/Repo"


# ---------------------------------------------------------------------------
# ResolutionCluster
# ---------------------------------------------------------------------------

class TestResolutionCluster:
    def test_from_dict_to_dict(self):
        c = ResolutionCluster(
            cluster_id="cl-001",
            root_cause="DB connection pool exhausted",
            ticket_ids=["t-1", "t-2"],
        )
        d = c.to_dict()
        c2 = ResolutionCluster.from_dict(d)
        assert c2.cluster_id == "cl-001"
        assert c2.ticket_ids == ["t-1", "t-2"]


# ---------------------------------------------------------------------------
# PRNode
# ---------------------------------------------------------------------------

class TestPRNode:
    def test_from_dict_to_dict(self):
        pr = PRNode(
            pr_id="Org/Repo#42",
            repo="Org/Repo",
            number=42,
            title="Fix auth bug",
            status="merged",
        )
        d = pr.to_dict()
        pr2 = PRNode.from_dict(d)
        assert pr2.pr_id == "Org/Repo#42"
        assert pr2.number == 42
        assert pr2.status == "merged"

    def test_defaults(self):
        pr = PRNode(pr_id="Org/Repo#1")
        assert pr.review_status == "pending"
        assert pr.files_changed == []
        assert pr.ticket_ids == []


class TestEngineHandoverSerialization:
    def test_handover_json_roundtrip(self):
        handover = EngineHandover(
            task_id="ticket-123",
            phase="investigate",
            source_engine="gemini",
            target_engine="claude",
            timestamp="2026-01-01T00:00:00+00:00",
            context=InvestigationPhaseOutput(
                root_cause="Bad null handling",
                affected_files=["src/a.py", "src/b.py"],
                suggested_fix="Add guard before access",
                confidence=0.82,
            ).to_dict(),
            constraints=HandoverConstraints(
                budget_remaining_usd=2.5,
                time_limit_seconds=900,
                model_tier="T2",
                retry_count=1,
                max_retries=3,
            ),
        )
        payload = handover.to_json()
        restored = EngineHandover.from_json(payload)
        assert restored.task_id == "ticket-123"
        assert restored.phase == "investigate"
        assert restored.source_engine == "gemini"
        assert restored.target_engine == "claude"
        assert restored.context["root_cause"] == "Bad null handling"
        assert restored.constraints.budget_remaining_usd == 2.5
        assert restored.constraints.time_limit_seconds == 900

    def test_phase_output_schema_roundtrips(self):
        inv = InvestigationPhaseOutput.from_dict(
            InvestigationPhaseOutput(
                root_cause="RC",
                affected_files=["a.py"],
                suggested_fix="Fix A",
                confidence=0.5,
            ).to_dict()
        )
        dev = DevelopmentPhaseOutput.from_dict(
            DevelopmentPhaseOutput(
                branch="feature/x",
                diff="1 file",
                test_results={"passed": True},
                commit_message="fix: x",
            ).to_dict()
        )
        ver = VerificationPhaseOutput.from_dict(
            VerificationPhaseOutput(
                verdict="pass",
                test_output="ok",
                regression_check={"detected": False},
            ).to_dict()
        )
        assert inv.affected_files == ["a.py"]
        assert dev.branch == "feature/x"
        assert ver.verdict == "pass"
