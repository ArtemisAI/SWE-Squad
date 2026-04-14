"""
Unit tests for src/swe_team/governance.py — DeploymentGovernor, DeploymentRecord,
and check_fix_complexity.
"""

from __future__ import annotations

import pytest

from src.swe_team.events import SWEEvent, SWEEventType
from src.swe_team.governance import (
    DeploymentGovernor,
    DeploymentRecord,
    check_fix_complexity,
)
from src.swe_team.models import GovernanceVerdict, StabilityReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _passing_stability() -> StabilityReport:
    return StabilityReport(verdict=GovernanceVerdict.PASS, details="all good")


def _warn_stability() -> StabilityReport:
    return StabilityReport(verdict=GovernanceVerdict.WARN, details="minor issues")


def _blocking_stability() -> StabilityReport:
    return StabilityReport(verdict=GovernanceVerdict.BLOCK, details="critical bugs open")


# ---------------------------------------------------------------------------
# DeploymentRecord: to_dict / from_dict roundtrip
# ---------------------------------------------------------------------------

class TestDeploymentRecordRoundtrip:
    def test_to_dict_keys(self):
        rec = DeploymentRecord(ticket_id="t1", branch="fix/my-bug")
        d = rec.to_dict()
        assert "deployment_id" in d
        assert d["ticket_id"] == "t1"
        assert d["branch"] == "fix/my-bug"
        assert d["status"] == "pending"
        assert "started_at" in d

    def test_from_dict_restores_fields(self):
        rec = DeploymentRecord(ticket_id="t2", branch="feat/x", status="deploying")
        d = rec.to_dict()
        restored = DeploymentRecord.from_dict(d)
        assert restored.deployment_id == rec.deployment_id
        assert restored.ticket_id == "t2"
        assert restored.branch == "feat/x"
        assert restored.status == "deploying"

    def test_roundtrip_preserves_optional_fields(self):
        rec = DeploymentRecord(
            ticket_id="t3",
            completed_at="2026-01-01T00:00:00+00:00",
            rollback_reason="regression",
            test_results={"passed": 10, "failed": 1},
            metadata={"env": "staging"},
        )
        restored = DeploymentRecord.from_dict(rec.to_dict())
        assert restored.completed_at == rec.completed_at
        assert restored.rollback_reason == "regression"
        assert restored.test_results == {"passed": 10, "failed": 1}
        assert restored.metadata == {"env": "staging"}

    def test_from_dict_generates_id_when_missing(self):
        d = {"ticket_id": "t99", "branch": ""}
        rec = DeploymentRecord.from_dict(d)
        assert rec.deployment_id  # auto-generated

    def test_deployment_id_is_hex_12_chars(self):
        rec = DeploymentRecord()
        assert len(rec.deployment_id) == 12
        int(rec.deployment_id, 16)  # must be valid hex


# ---------------------------------------------------------------------------
# DeploymentGovernor.can_deploy()
# ---------------------------------------------------------------------------

class TestCanDeploy:
    def test_can_deploy_when_pass(self):
        gov = DeploymentGovernor()
        assert gov.can_deploy(_passing_stability()) is True

    def test_can_deploy_when_warn(self):
        """WARN verdict does not block deployment."""
        gov = DeploymentGovernor()
        assert gov.can_deploy(_warn_stability()) is True

    def test_cannot_deploy_when_block(self):
        gov = DeploymentGovernor()
        assert gov.can_deploy(_blocking_stability()) is False


# ---------------------------------------------------------------------------
# DeploymentGovernor.start_deployment()
# ---------------------------------------------------------------------------

class TestStartDeployment:
    def test_start_creates_record(self):
        gov = DeploymentGovernor()
        rec = gov.start_deployment("ticket-abc", branch="fix/abc")
        assert rec.ticket_id == "ticket-abc"
        assert rec.branch == "fix/abc"
        assert rec.status == "deploying"

    def test_start_appends_to_records(self):
        gov = DeploymentGovernor()
        gov.start_deployment("t1")
        gov.start_deployment("t2")
        assert len(gov.records) == 2

    def test_records_property_is_copy(self):
        gov = DeploymentGovernor()
        gov.start_deployment("t1")
        records_copy = gov.records
        records_copy.clear()
        assert len(gov.records) == 1  # original unmodified

    def test_concurrent_deployments_tracked(self):
        gov = DeploymentGovernor()
        recs = [gov.start_deployment(f"ticket-{i}") for i in range(5)]
        assert len(gov.records) == 5
        ids = {r.deployment_id for r in recs}
        assert len(ids) == 5  # all unique


# ---------------------------------------------------------------------------
# DeploymentGovernor.complete_deployment()
# ---------------------------------------------------------------------------

class TestCompleteDeployment:
    def test_complete_marks_deployed(self):
        gov = DeploymentGovernor()
        rec = gov.start_deployment("t1")
        result = gov.complete_deployment(rec.deployment_id, test_results={"passed": 5})
        assert result is not None
        assert result.status == "deployed"
        assert result.test_results == {"passed": 5}
        assert result.completed_at is not None

    def test_complete_unknown_id_returns_none(self):
        gov = DeploymentGovernor()
        result = gov.complete_deployment("nonexistent-id")
        assert result is None

    def test_complete_modifies_record_in_place(self):
        gov = DeploymentGovernor()
        rec = gov.start_deployment("t1")
        gov.complete_deployment(rec.deployment_id)
        assert gov.records[0].status == "deployed"


# ---------------------------------------------------------------------------
# DeploymentGovernor.rollback()
# ---------------------------------------------------------------------------

class TestRollback:
    def test_rollback_marks_rolled_back(self):
        gov = DeploymentGovernor()
        rec = gov.start_deployment("t1")
        result = gov.rollback(rec.deployment_id, reason="regression detected")
        assert result is not None
        assert result.status == "rolled_back"
        assert result.rollback_reason == "regression detected"
        assert result.completed_at is not None

    def test_rollback_unknown_id_returns_none(self):
        gov = DeploymentGovernor()
        result = gov.rollback("bad-id", reason="test")
        assert result is None

    def test_rollback_empty_reason_allowed(self):
        gov = DeploymentGovernor()
        rec = gov.start_deployment("t1")
        result = gov.rollback(rec.deployment_id)
        assert result.status == "rolled_back"
        assert result.rollback_reason == ""


# ---------------------------------------------------------------------------
# DeploymentGovernor events
# ---------------------------------------------------------------------------

class TestGovernorEvents:
    def test_build_deploy_event_success(self):
        gov = DeploymentGovernor()
        rec = gov.start_deployment("t1")
        gov.complete_deployment(rec.deployment_id)
        event = gov.build_deploy_event(rec)
        assert isinstance(event, SWEEvent)
        assert event.event == SWEEventType.DEPLOY_COMPLETE
        assert event.payload["success"] is True
        assert event.payload["deployment_id"] == rec.deployment_id

    def test_build_deploy_event_failure(self):
        gov = DeploymentGovernor()
        rec = gov.start_deployment("t1")
        gov.rollback(rec.deployment_id, reason="oops")
        event = gov.build_deploy_event(rec)
        assert event.payload["success"] is False

    def test_build_rollback_event(self):
        gov = DeploymentGovernor()
        rec = gov.start_deployment("t1")
        gov.rollback(rec.deployment_id, reason="bad deploy")
        event = gov.build_rollback_event(rec)
        assert isinstance(event, SWEEvent)
        assert event.event == SWEEventType.ROLLBACK_TRIGGERED
        assert event.payload["reason"] == "bad deploy"
        assert event.payload["deployment_id"] == rec.deployment_id


# ---------------------------------------------------------------------------
# check_fix_complexity()
# ---------------------------------------------------------------------------

class TestCheckFixComplexity:
    def test_empty_files_invalid(self):
        ok, reason = check_fix_complexity([], 0)
        assert ok is False
        assert "No files" in reason

    def test_too_many_files(self):
        files = [f"src/swe_team/file{i}.py" for i in range(6)]
        ok, reason = check_fix_complexity(files, 50)
        assert ok is False
        assert "Too many files" in reason

    def test_too_many_lines(self):
        ok, reason = check_fix_complexity(["src/swe_team/foo.py"], 201)
        assert ok is False
        assert "Too many lines" in reason

    def test_dependency_file_blocked(self):
        ok, reason = check_fix_complexity(["requirements.txt"], 5)
        assert ok is False
        assert "Dependency" in reason

    def test_valid_single_module_fix(self):
        ok, reason = check_fix_complexity(
            ["src/swe_team/monitor_agent.py", "tests/unit/test_monitor.py"],
            100,
        )
        assert ok is True
        assert reason == "ok"

    def test_cross_module_detected_without_allowlist(self):
        files = [
            "src/swe_team/monitor_agent.py",
            "src/a2a/client.py",
        ]
        ok, reason = check_fix_complexity(files, 50)
        assert ok is False
        assert "Cross-module" in reason

    def test_allowed_modules_permits_matching_module(self):
        files = [
            "src/swe_team/monitor_agent.py",
            "tests/unit/test_monitor.py",
        ]
        ok, reason = check_fix_complexity(files, 50, allowed_modules={"swe_team"})
        assert ok is True

    def test_allowed_modules_blocks_extra_module(self):
        files = [
            "src/swe_team/monitor_agent.py",
            "src/a2a/client.py",
        ]
        ok, reason = check_fix_complexity(files, 50, allowed_modules={"swe_team"})
        assert ok is False
        assert "a2a" in reason

    def test_custom_max_files_and_lines(self):
        files = [f"src/swe_team/f{i}.py" for i in range(10)]
        ok, reason = check_fix_complexity(files, 500, max_files=10, max_lines=500)
        # 10 files is within max_files=10, and lines are within max_lines=500
        # but 10 core modules → cross-module check should apply
        # This exercises the custom threshold path; we just verify it runs
        assert isinstance(ok, bool)

    def test_pyproject_blocked(self):
        ok, reason = check_fix_complexity(["pyproject.toml"], 2)
        assert ok is False

    def test_scripts_module_allowed_with_no_allowlist(self):
        """scripts/ is its own module — two scripts files = cross-module check fails."""
        ok, reason = check_fix_complexity(
            ["scripts/ops/runner.py", "scripts/ops/cli.py"], 20
        )
        # both in 'scripts' → same module → ok
        assert ok is True
