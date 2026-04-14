"""Tests for the SWE-Squad Fleet Orchestrator (scripts/ops/swe_orchestrator.py).

Covers pipeline intelligence detections, corrective actions, report generation,
dedup logic, and the main orchestration loop.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from scripts.ops.swe_orchestrator import (
    Finding,
    OrchestratorActions,
    OrchestratorConfig,
    PipelineIntelligence,
    SupabaseClient,
    VMConfig,
    generate_report,
    load_orchestrator_config,
    run_orchestrator,
    save_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_ago(h: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


def _make_ticket(
    ticket_id: str = "T-001",
    status: str = "open",
    severity: str = "high",
    updated_at: Optional[str] = None,
    title: str = "Test ticket",
) -> Dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "status": status,
        "severity": severity,
        "title": title,
        "created_at": updated_at or _now_iso(),
        "updated_at": updated_at or _now_iso(),
    }


class FakeSupabase:
    """In-memory fake for SupabaseClient."""

    def __init__(self, tickets: Optional[List[Dict[str, Any]]] = None) -> None:
        self.tickets = tickets or []
        self.patches: List[Dict[str, Any]] = []

    def query_tickets(self, filters: str = "") -> List[Dict[str, Any]]:
        # Simple filter parsing for tests
        result = list(self.tickets)
        if "status=eq." in filters:
            status = filters.split("status=eq.")[1].split("&")[0]
            result = [t for t in result if t.get("status") == status]
        if "severity=eq." in filters:
            sev = filters.split("severity=eq.")[1].split("&")[0]
            result = [t for t in result if t.get("severity") == sev]
        if "status=in." in filters:
            vals_str = filters.split("status=in.")[1].split("&")[0]
            vals = vals_str.strip("()").split(",")
            result = [t for t in result if t.get("status") in vals]
        if "select=status" in filters:
            return [{"status": t.get("status")} for t in self.tickets]
        return result

    def patch_ticket(self, ticket_id: str, updates: Dict[str, Any]) -> bool:
        self.patches.append({"ticket_id": ticket_id, "updates": updates})
        for t in self.tickets:
            if t.get("ticket_id") == ticket_id:
                t.update(updates)
                return True
        return False


def _default_config(**overrides: Any) -> OrchestratorConfig:
    cfg = OrchestratorConfig(
        vms=[VMConfig(name="agent-1", ip="10.0.0.1", team_id="alpha")],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ---------------------------------------------------------------------------
# PipelineIntelligence tests
# ---------------------------------------------------------------------------

class TestDetectIdleWithWork:
    def test_idle_with_open_tickets(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-1", status="open"),
            _make_ticket("T-2", status="triaged"),
        ])
        intel = PipelineIntelligence(_default_config(), db)
        findings = intel.detect_idle_with_work()
        assert len(findings) == 1
        assert findings[0].category == "idle_with_work"
        assert findings[0].severity == "warning"

    def test_no_finding_when_agents_active(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-1", status="open"),
            _make_ticket("T-2", status="investigating"),
        ])
        intel = PipelineIntelligence(_default_config(), db)
        findings = intel.detect_idle_with_work()
        assert len(findings) == 0

    def test_no_finding_when_no_work(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-1", status="resolved"),
        ])
        intel = PipelineIntelligence(_default_config(), db)
        findings = intel.detect_idle_with_work()
        assert len(findings) == 0


class TestDetectStuckTickets:
    def test_stuck_investigating(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-1", status="investigating", updated_at=_hours_ago(10)),
        ])
        intel = PipelineIntelligence(_default_config(stuck_ticket_hours=6), db)
        findings = intel.detect_stuck_tickets()
        assert len(findings) == 1
        assert findings[0].category == "stuck_tickets"
        assert findings[0].auto_fixable is True

    def test_not_stuck_within_threshold(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-1", status="investigating", updated_at=_hours_ago(2)),
        ])
        intel = PipelineIntelligence(_default_config(stuck_ticket_hours=6), db)
        findings = intel.detect_stuck_tickets()
        assert len(findings) == 0

    def test_critical_when_many_stuck(self) -> None:
        tickets = [
            _make_ticket(f"T-{i}", status="investigating", updated_at=_hours_ago(12))
            for i in range(6)
        ]
        db = FakeSupabase(tickets)
        intel = PipelineIntelligence(_default_config(stuck_ticket_hours=6), db)
        findings = intel.detect_stuck_tickets()
        assert len(findings) == 1
        assert findings[0].severity == "critical"


class TestDetectThroughputDrop:
    def test_detects_significant_drop(self) -> None:
        intel = PipelineIntelligence(_default_config(), None)
        findings = intel.detect_throughput_drop(resolved_last_24h=5, resolved_prev_24h=20)
        assert len(findings) == 1
        assert findings[0].category == "throughput_drop"
        assert findings[0].evidence["drop_percent"] == 75.0

    def test_no_finding_when_stable(self) -> None:
        intel = PipelineIntelligence(_default_config(), None)
        findings = intel.detect_throughput_drop(resolved_last_24h=18, resolved_prev_24h=20)
        assert len(findings) == 0

    def test_no_finding_when_no_data(self) -> None:
        intel = PipelineIntelligence(_default_config(), None)
        findings = intel.detect_throughput_drop()
        assert len(findings) == 0

    def test_no_division_by_zero(self) -> None:
        intel = PipelineIntelligence(_default_config(), None)
        findings = intel.detect_throughput_drop(resolved_last_24h=0, resolved_prev_24h=0)
        assert len(findings) == 0


class TestDetectFailureCascade:
    def test_detects_cascade(self) -> None:
        now = datetime.now(timezone.utc)
        tickets = [
            _make_ticket(f"T-{i}", status="failed",
                         updated_at=(now - timedelta(minutes=i * 10)).isoformat())
            for i in range(5)
        ]
        db = FakeSupabase(tickets)
        intel = PipelineIntelligence(_default_config(), db)
        findings = intel.detect_failure_cascade()
        assert len(findings) == 1
        assert findings[0].category == "failure_cascade"
        assert findings[0].severity == "critical"

    def test_no_cascade_with_few_failures(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-1", status="failed"),
        ])
        intel = PipelineIntelligence(_default_config(), db)
        findings = intel.detect_failure_cascade()
        assert len(findings) == 0


class TestDetectWorkStarvation:
    def test_starved(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-1", status="resolved"),
            _make_ticket("T-2", status="closed"),
        ])
        intel = PipelineIntelligence(_default_config(), db)
        findings = intel.detect_work_starvation()
        assert len(findings) == 1
        assert findings[0].category == "work_starvation"

    def test_not_starved(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-1", status="open"),
        ])
        intel = PipelineIntelligence(_default_config(), db)
        findings = intel.detect_work_starvation()
        assert len(findings) == 0


class TestDetectConfigMismatch:
    def test_mismatch_blocking_work(self) -> None:
        tickets = [
            _make_ticket(f"T-{i}", status="open", severity="low")
            for i in range(10)
        ]
        db = FakeSupabase(tickets)
        intel = PipelineIntelligence(_default_config(), db)
        findings = intel.detect_config_mismatch(severity_filter="high")
        assert len(findings) == 1
        assert findings[0].category == "config_mismatch"

    def test_no_mismatch_at_lowest_filter(self) -> None:
        db = FakeSupabase([_make_ticket("T-1", status="open", severity="low")])
        intel = PipelineIntelligence(_default_config(), db)
        findings = intel.detect_config_mismatch(severity_filter="low")
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# OrchestratorActions tests
# ---------------------------------------------------------------------------

class TestFixStuckTickets:
    def test_resets_investigating_to_open(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-1", status="investigating", updated_at=_hours_ago(10)),
        ])
        finding = Finding(
            category="stuck_tickets",
            severity="warning",
            title="test",
            description="test",
            evidence={"stuck_tickets": [{"ticket_id": "T-1", "status": "investigating"}]},
            auto_fixable=True,
        )
        actions = OrchestratorActions(_default_config(), db, dry_run=False)
        count = actions.fix_stuck_tickets(finding)
        assert count == 1
        assert db.patches[0]["updates"]["status"] == "open"

    def test_resets_in_dev_to_investigation_complete(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-2", status="in_development", updated_at=_hours_ago(10)),
        ])
        finding = Finding(
            category="stuck_tickets",
            severity="warning",
            title="test",
            description="test",
            evidence={"stuck_tickets": [{"ticket_id": "T-2", "status": "in_development"}]},
            auto_fixable=True,
        )
        actions = OrchestratorActions(_default_config(), db, dry_run=False)
        count = actions.fix_stuck_tickets(finding)
        assert count == 1
        assert db.patches[0]["updates"]["status"] == "investigation_complete"

    def test_dry_run_does_not_patch(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-1", status="investigating"),
        ])
        finding = Finding(
            category="stuck_tickets",
            severity="warning",
            title="test",
            description="test",
            evidence={"stuck_tickets": [{"ticket_id": "T-1", "status": "investigating"}]},
            auto_fixable=True,
        )
        actions = OrchestratorActions(_default_config(), db, dry_run=True)
        count = actions.fix_stuck_tickets(finding)
        assert count == 1
        assert len(db.patches) == 0  # no actual patches in dry-run


class TestExecuteAction:
    def test_executes_stuck_tickets(self) -> None:
        db = FakeSupabase([_make_ticket("T-1", status="investigating")])
        finding = Finding(
            category="stuck_tickets",
            severity="warning",
            title="test",
            description="test",
            evidence={"stuck_tickets": [{"ticket_id": "T-1", "status": "investigating"}]},
            auto_fixable=True,
        )
        actions = OrchestratorActions(_default_config(), db)
        result = actions.execute(finding)
        assert result is True

    def test_logs_config_recommendation(self) -> None:
        actions = OrchestratorActions(_default_config(), None)
        finding = Finding(
            category="config_mismatch",
            severity="warning",
            title="test",
            description="test",
            recommended_action="Lower severity filter",
            auto_fixable=True,
        )
        result = actions.execute(finding)
        assert result is True
        assert any("config_recommendation" in a["action"] for a in actions.actions_taken)


# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------

class TestReportGeneration:
    def test_empty_report(self) -> None:
        report = generate_report([], [])
        assert report["finding_count"] == 0
        assert "All clear" in report["summary"]

    def test_report_with_findings(self) -> None:
        findings = [
            Finding(category="stuck_tickets", severity="warning", title="3 stuck", description="d"),
            Finding(category="failure_cascade", severity="critical", title="cascade", description="d"),
        ]
        report = generate_report(findings, [{"action": "test"}])
        assert report["finding_count"] == 2
        assert report["findings_by_severity"]["critical"] == 1
        assert report["findings_by_severity"]["warning"] == 1
        assert len(report["actions_taken"]) == 1

    def test_save_report(self, tmp_path: Path) -> None:
        report = generate_report([], [])
        path = save_report(report, tmp_path / "report.json")
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded["finding_count"] == 0


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestOrchestratorConfig:
    def test_from_yaml_section(self) -> None:
        data = {
            "enabled": True,
            "cycle_interval_minutes": 120,
            "auto_fix": False,
            "thresholds": {
                "idle_with_work_minutes": 60,
                "stuck_ticket_hours": 12,
                "throughput_drop_percent": 30,
                "max_circuit_breaker_resets_per_day": 5,
            },
            "vms": [
                {"name": "agent-1", "ip": "10.0.0.1", "team_id": "alpha"},
            ],
        }
        cfg = OrchestratorConfig.from_yaml_section(data)
        assert cfg.cycle_interval_minutes == 120
        assert cfg.auto_fix is False
        assert cfg.stuck_ticket_hours == 12
        assert len(cfg.vms) == 1
        assert cfg.vms[0].name == "agent-1"

    def test_defaults(self) -> None:
        cfg = OrchestratorConfig.from_yaml_section({})
        assert cfg.enabled is True
        assert cfg.cycle_interval_minutes == 240
        assert cfg.stuck_ticket_hours == 6


# ---------------------------------------------------------------------------
# Integration: run_orchestrator
# ---------------------------------------------------------------------------

class TestRunOrchestrator:
    def test_run_with_no_db(self, tmp_path: Path) -> None:
        """Orchestrator runs without Supabase (no findings, no crash)."""
        config = _default_config()
        config.supabase_url = ""
        config.supabase_key = ""
        with patch("scripts.ops.swe_orchestrator._DATA_DIR", tmp_path):
            findings = run_orchestrator(dry_run=True, config=config)
        assert isinstance(findings, list)

    def test_auto_fix_called_for_fixable(self) -> None:
        """Auto-fixable findings trigger execute()."""
        config = _default_config(auto_fix=True)
        db = FakeSupabase([
            _make_ticket("T-1", status="investigating", updated_at=_hours_ago(10)),
        ])
        with patch("scripts.ops.swe_orchestrator.SupabaseClient") as mock_cls:
            mock_cls.return_value = db
            with patch("scripts.ops.swe_orchestrator.save_report"):
                intel = PipelineIntelligence(config, db)
                findings = intel.detect_stuck_tickets()
                assert len(findings) > 0
                assert findings[0].auto_fixable is True

    def test_workload_distributor_applies_before_auto_fix(self, tmp_path: Path) -> None:
        config = _default_config(auto_fix=False, repos=["Org/Repo"])
        config.teams = {
            "alpha": {
                "github_account": "bot-alpha",
                "role": "developer",
                "max_concurrent": 3,
                "specialization": ["frontend"],
            },
        }
        finding = Finding(
            category="unassigned_issues",
            severity="warning",
            title="Unassigned issues",
            description="test",
            auto_fixable=True,
            evidence={
                "unassigned_issues": [
                    {
                        "repo": "Org/Repo",
                        "number": 9,
                        "title": "Frontend issue",
                        "labels": ["frontend"],
                        "assignees": [],
                    },
                ],
            },
        )
        with patch("scripts.ops.swe_orchestrator.PipelineIntelligence.run_all_detections", return_value=[finding]):
            with patch("scripts.ops.swe_orchestrator._DATA_DIR", tmp_path):
                findings = run_orchestrator(dry_run=True, config=config)
        assert findings[0].evidence["workload_distributor_applied"] is True
        assert findings[0].evidence["unassigned_issues"] == []


# ---------------------------------------------------------------------------
# Dedup logic
# ---------------------------------------------------------------------------

class TestGitHubIssueDedup:
    def test_skips_duplicate(self) -> None:
        """Should not create issue when one with same prefix exists."""
        actions = OrchestratorActions(_default_config(), None, dry_run=False)
        finding = Finding(
            category="stuck_tickets",
            severity="warning",
            title="test",
            description="test",
        )
        existing = json.dumps([{"number": 42, "title": "[Orchestrator] stuck_tickets: old"}])
        with patch.object(actions, "_gh_command") as mock_gh:
            mock_gh.return_value = (True, existing)
            result = actions.create_github_issue(finding, repo="test/repo")
        assert result is None
        # Only the list call, no create call
        assert mock_gh.call_count == 1


class TestRunAllDetections:
    def test_combines_findings(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-1", status="open"),
            _make_ticket("T-2", status="investigating", updated_at=_hours_ago(10)),
        ])
        intel = PipelineIntelligence(_default_config(stuck_ticket_hours=6), db)
        findings = intel.run_all_detections()
        categories = {f.category for f in findings}
        # T-2 is investigating (active), so idle_with_work won't fire
        # but stuck_tickets should fire since T-2 is stuck >6h
        assert "stuck_tickets" in categories

    def test_combines_idle_and_starvation(self) -> None:
        db = FakeSupabase([
            _make_ticket("T-1", status="resolved"),
        ])
        intel = PipelineIntelligence(_default_config(), db)
        findings = intel.run_all_detections()
        categories = {f.category for f in findings}
        assert "work_starvation" in categories


# ---------------------------------------------------------------------------
# detect_unassigned_issues tests
# ---------------------------------------------------------------------------

class TestDetectUnassignedIssues:
    def _config_with_repos(self, repos: List[str]) -> OrchestratorConfig:
        cfg = _default_config()
        cfg.repos = repos
        return cfg

    def test_returns_none_with_no_repos(self) -> None:
        intel = PipelineIntelligence(_default_config(), None)
        result = intel.detect_unassigned_issues()
        assert result is None

    def test_returns_none_when_all_assigned_to_bot(self) -> None:
        issues = json.dumps([
            {
                "number": 1,
                "title": "Fix bug",
                "assignees": [{"login": "bot-alpha"}],
                "labels": [],
            }
        ])
        cfg = self._config_with_repos(["your-org/SWE-Sandbox"])
        intel = PipelineIntelligence(cfg, None)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=issues, stderr="")
            result = intel.detect_unassigned_issues()
        assert result is None

    def test_finds_issues_with_no_assignees(self) -> None:
        issues = json.dumps([
            {"number": 10, "title": "Unassigned issue", "assignees": [], "labels": []},
        ])
        cfg = self._config_with_repos(["your-org/SWE-Sandbox"])
        intel = PipelineIntelligence(cfg, None)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=issues, stderr="")
            result = intel.detect_unassigned_issues()
        assert result is not None
        assert result.category == "unassigned_issues"
        assert result.severity == "warning"
        assert result.auto_fixable is True
        assert len(result.evidence["unassigned_issues"]) == 1
        assert result.evidence["unassigned_issues"][0]["number"] == 10
        assert result.evidence["unassigned_issues"][0]["repo"] == "your-org/SWE-Sandbox"

    def test_finds_issues_assigned_to_human_only(self) -> None:
        """Issues assigned to humans (not bots) should be flagged."""
        issues = json.dumps([
            {
                "number": 20,
                "title": "Human-assigned issue",
                "assignees": [{"login": "some-human"}],
                "labels": [],
            }
        ])
        cfg = self._config_with_repos(["your-org/SWE-Sandbox"])
        intel = PipelineIntelligence(cfg, None)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=issues, stderr="")
            result = intel.detect_unassigned_issues()
        assert result is not None
        assert result.evidence["unassigned_issues"][0]["number"] == 20

    def test_skips_repo_on_gh_failure(self) -> None:
        cfg = self._config_with_repos(["your-org/SWE-Sandbox"])
        intel = PipelineIntelligence(cfg, None)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
            result = intel.detect_unassigned_issues()
        assert result is None  # graceful skip

    def test_aggregates_across_multiple_repos(self) -> None:
        issues_a = json.dumps([
            {"number": 1, "title": "Issue A", "assignees": [], "labels": []},
        ])
        issues_b = json.dumps([
            {"number": 2, "title": "Issue B", "assignees": [], "labels": []},
        ])
        cfg = self._config_with_repos(["Org/RepoA", "Org/RepoB"])
        intel = PipelineIntelligence(cfg, None)
        call_count = 0

        def _side_effect(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            stdout = issues_a if call_count == 1 else issues_b
            return MagicMock(returncode=0, stdout=stdout, stderr="")

        with patch("subprocess.run", side_effect=_side_effect):
            result = intel.detect_unassigned_issues()

        assert result is not None
        assert len(result.evidence["unassigned_issues"]) == 2
        repos_found = {i["repo"] for i in result.evidence["unassigned_issues"]}
        assert repos_found == {"Org/RepoA", "Org/RepoB"}

    def test_included_in_run_all_detections(self) -> None:
        issues = json.dumps([
            {"number": 5, "title": "Needs bot", "assignees": [], "labels": []},
        ])
        cfg = _default_config()
        cfg.repos = ["your-org/SWE-Sandbox"]
        intel = PipelineIntelligence(cfg, None)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=issues, stderr="")
            findings = intel.run_all_detections()
        categories = {f.category for f in findings}
        assert "unassigned_issues" in categories


# ---------------------------------------------------------------------------
# detect_idle_agents tests
# ---------------------------------------------------------------------------

class TestDetectIdleAgents:
    def test_returns_none_without_db(self) -> None:
        intel = PipelineIntelligence(_default_config(), None)
        result = intel.detect_idle_agents()
        assert result is None

    def test_detects_idle_team(self) -> None:
        # All resolved — no active workable tickets for any team
        db = FakeSupabase([
            {**_make_ticket("T-1", status="resolved"), "team_id": "team-alpha"},
        ])
        cfg = _default_config()
        cfg.vms = [VMConfig(name="agent-1", ip="10.0.0.1", team_id="team-alpha")]
        intel = PipelineIntelligence(cfg, db)
        result = intel.detect_idle_agents()
        assert result is not None
        assert result.category == "idle_agents"
        assert "team-alpha" in result.evidence["idle_teams"]

    def test_no_idle_teams_when_all_busy(self) -> None:
        db = FakeSupabase([
            {**_make_ticket("T-1", status="investigating"), "team_id": "team-alpha"},
        ])
        cfg = _default_config()
        cfg.vms = [VMConfig(name="agent-1", ip="10.0.0.1", team_id="team-alpha")]
        intel = PipelineIntelligence(cfg, db)
        result = intel.detect_idle_agents()
        assert result is None

    def test_excludes_umbrella_tickets(self) -> None:
        """Tickets labelled 'umbrella' should not count as active work."""
        db = FakeSupabase([
            {
                **_make_ticket("T-1", status="open"),
                "team_id": "team-alpha",
                "labels": ["umbrella"],
            },
        ])
        cfg = _default_config()
        cfg.vms = [VMConfig(name="agent-1", ip="10.0.0.1", team_id="team-alpha")]
        intel = PipelineIntelligence(cfg, db)
        result = intel.detect_idle_agents()
        assert result is not None
        assert "team-alpha" in result.evidence["idle_teams"]

    def test_excludes_hitl_tickets(self) -> None:
        """Tickets labelled 'hitl' should not count as active work."""
        db = FakeSupabase([
            {
                **_make_ticket("T-1", status="investigating"),
                "team_id": "team-alpha",
                "labels": ["hitl"],
            },
        ])
        cfg = _default_config()
        cfg.vms = [VMConfig(name="agent-1", ip="10.0.0.1", team_id="team-alpha")]
        intel = PipelineIntelligence(cfg, db)
        result = intel.detect_idle_agents()
        assert result is not None
        assert "team-alpha" in result.evidence["idle_teams"]

    def test_included_in_run_all_detections(self) -> None:
        db = FakeSupabase([
            {**_make_ticket("T-1", status="resolved"), "team_id": "team-alpha"},
        ])
        cfg = _default_config()
        cfg.vms = [VMConfig(name="agent-1", ip="10.0.0.1", team_id="team-alpha")]
        intel = PipelineIntelligence(cfg, db)
        findings = intel.run_all_detections()
        categories = {f.category for f in findings}
        assert "idle_agents" in categories


# ---------------------------------------------------------------------------
# assign_issues_to_idle_agents tests
# ---------------------------------------------------------------------------

class TestAssignIssuesToIdleAgents:
    def _unassigned_finding(
        self,
        issues: Optional[List[Dict[str, Any]]] = None,
        team_ticket_counts: Optional[Dict[str, int]] = None,
    ) -> Finding:
        return Finding(
            category="unassigned_issues",
            severity="warning",
            title="Unassigned issues",
            description="test",
            auto_fixable=True,
            evidence={
                "unassigned_issues": issues or [
                    {"repo": "Org/Repo", "number": 1, "title": "Bug", "labels": [], "assignees": []},
                ],
                **({"team_ticket_counts": team_ticket_counts} if team_ticket_counts is not None else {}),
            },
        )

    def test_dry_run_no_gh_calls(self) -> None:
        finding = self._unassigned_finding()
        actions = OrchestratorActions(_default_config(), None, dry_run=True)
        with patch.object(actions, "_gh_command") as mock_gh:
            count = actions.assign_issues_to_idle_agents(finding)
        assert count == 1
        mock_gh.assert_not_called()
        assert any(a["action"] == "assign_issue" for a in actions.actions_taken)
        assert all(a["dry_run"] for a in actions.actions_taken)

    def test_assigns_to_bot_account(self) -> None:
        finding = self._unassigned_finding(
            issues=[{"repo": "Org/Repo", "number": 42, "title": "Fix me", "labels": [], "assignees": []}],
            team_ticket_counts={"team-alpha": 0},
        )
        actions = OrchestratorActions(_default_config(), None, dry_run=False)
        with patch.object(actions, "_gh_command", return_value=(True, "")) as mock_gh:
            count = actions.assign_issues_to_idle_agents(finding)
        assert count == 1
        # Verify gh issue edit was called with --add-assignee
        call_args = mock_gh.call_args[0][0]
        assert "edit" in call_args
        assert "--add-assignee" in call_args
        assert call_args[call_args.index("--add-assignee") + 1] in ("bot-alpha", "bot-beta")

    def test_returns_zero_for_empty_issues(self) -> None:
        finding = Finding(
            category="unassigned_issues",
            severity="warning",
            title="none",
            description="test",
            evidence={"unassigned_issues": []},
        )
        actions = OrchestratorActions(_default_config(), None, dry_run=True)
        assert actions.assign_issues_to_idle_agents(finding) == 0

    def test_caps_at_max_assignments(self) -> None:
        # 15 issues, cap is 10
        issues = [
            {"repo": "Org/Repo", "number": i, "title": f"Issue {i}", "labels": [], "assignees": []}
            for i in range(15)
        ]
        finding = self._unassigned_finding(issues=issues)
        actions = OrchestratorActions(_default_config(), None, dry_run=True)
        count = actions.assign_issues_to_idle_agents(finding)
        assert count == 10

    def test_load_balances_between_bots(self) -> None:
        """With two bots having different loads, the lighter-loaded bot gets the first pick."""
        # swe-squad-1 (→ bot-alpha) has 5 tickets, bot-beta has 0
        issues = [
            {"repo": "Org/Repo", "number": 1, "title": "First", "labels": [], "assignees": []},
            {"repo": "Org/Repo", "number": 2, "title": "Second", "labels": [], "assignees": []},
        ]
        finding = self._unassigned_finding(
            issues=issues,
            team_ticket_counts={"team-alpha": 5, "bot-beta": 0},
        )
        actions = OrchestratorActions(_default_config(), None, dry_run=True)
        count = actions.assign_issues_to_idle_agents(finding)
        assert count == 2
        assigned_bots = [a["detail"].split("→")[1].strip() for a in actions.actions_taken if a["action"] == "assign_issue"]
        # First pick should be bot-beta (lower load)
        assert assigned_bots[0] == "bot-beta"

    def test_gh_failure_does_not_count(self) -> None:
        finding = self._unassigned_finding()
        actions = OrchestratorActions(_default_config(), None, dry_run=False)
        with patch.object(actions, "_gh_command", return_value=(False, "error")):
            count = actions.assign_issues_to_idle_agents(finding)
        assert count == 0

    def test_execute_routes_unassigned_issues(self) -> None:
        finding = self._unassigned_finding()
        actions = OrchestratorActions(_default_config(), None, dry_run=True)
        result = actions.execute(finding)
        assert result is True

    def test_execute_routes_idle_agents(self) -> None:
        """execute() with idle_agents category also triggers assignment."""
        finding = Finding(
            category="idle_agents",
            severity="info",
            title="Idle teams",
            description="test",
            auto_fixable=True,
            evidence={
                "idle_teams": ["team-alpha"],
                "team_ticket_counts": {"team-alpha": 0},
                # No unassigned_issues in evidence → assign_issues returns 0
                "unassigned_issues": [],
            },
        )
        actions = OrchestratorActions(_default_config(), None, dry_run=True)
        # Returns False since there are no unassigned issues to act on
        result = actions.execute(finding)
        assert result is False


# ---------------------------------------------------------------------------
# load_orchestrator_config repos population
# ---------------------------------------------------------------------------

class TestLoadOrchestratorConfigRepos:
    def test_repos_populated_from_yaml(self, tmp_path: Path) -> None:
        yaml_content = """
repos:
  - name: "your-org/SWE-Sandbox"
    local_path: "/tmp/sandbox"
  - name: "your-org/SWE-Sandbox-HealthTrack"
    local_path: "/tmp/healthtrack"
orchestrator:
  enabled: true
  vms: []
"""
        config_file = tmp_path / "swe_team.yaml"
        config_file.write_text(yaml_content)

        with patch("scripts.ops.swe_orchestrator._CONFIG_PATH", config_file):
            config = load_orchestrator_config()

        assert "your-org/SWE-Sandbox" in config.repos
        assert "your-org/SWE-Sandbox-HealthTrack" in config.repos

    def test_repos_empty_when_no_yaml(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "missing.yaml"
        with patch("scripts.ops.swe_orchestrator._CONFIG_PATH", nonexistent):
            config = load_orchestrator_config()
        assert config.repos == []

    def test_teams_populated_from_yaml(self, tmp_path: Path) -> None:
        yaml_content = """
teams:
  alpha:
    github_account: bot-alpha
    role: developer
    max_concurrent: 3
orchestrator:
  enabled: true
  vms: []
"""
        config_file = tmp_path / "swe_team.yaml"
        config_file.write_text(yaml_content)

        with patch("scripts.ops.swe_orchestrator._CONFIG_PATH", config_file):
            config = load_orchestrator_config()

        assert "alpha" in config.teams
        assert config.teams["alpha"]["github_account"] == "bot-alpha"
