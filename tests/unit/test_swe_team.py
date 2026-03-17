"""
Tests for the Autonomous SWE Team module.

Covers models, events, configuration, monitor agent, triage agent,
Ralph-Wiggum stability gate, deployment governance, and ticket store.
"""

from __future__ import annotations

import logging
logging.logAsyncioTasks = False

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.swe_team.models import (
    AgentRole,
    GovernanceVerdict,
    SWEAgentConfig,
    SWETicket,
    StabilityReport,
    TicketSeverity,
    TicketStatus,
)
from src.swe_team.events import SWEEvent, SWEEventType
from src.swe_team.config import (
    GovernanceConfig,
    MonitorConfig,
    SWETeamConfig,
    load_config,
)
from src.swe_team.monitor_agent import MonitorAgent, _fingerprint, _severity_from_pattern
from src.swe_team.triage_agent import TriageAgent
from src.swe_team.investigator import InvestigatorAgent, _parse_cost
from src.swe_team.ralph_wiggum import RalphWiggumGate
from src.swe_team.governance import DeploymentGovernor, DeploymentRecord, check_fix_complexity
from src.swe_team.developer import DeveloperAgent
from src.swe_team.ticket_store import TicketStore


# ======================================================================
# Models
# ======================================================================

class TestSWETicket:
    def test_create_default(self):
        t = SWETicket(title="Test bug", description="Something broke")
        assert t.title == "Test bug"
        assert t.severity == TicketSeverity.MEDIUM
        assert t.status == TicketStatus.OPEN
        assert len(t.ticket_id) == 12

    def test_roundtrip_dict(self):
        t = SWETicket(
            title="reCAPTCHA error",
            description="Scraper hit captcha",
            severity=TicketSeverity.HIGH,
            labels=["scraping", "captcha"],
            source_module="scraping",
        )
        d = t.to_dict()
        t2 = SWETicket.from_dict(d)
        assert t2.title == t.title
        assert t2.severity == t.severity
        assert t2.labels == t.labels
        assert t2.source_module == t.source_module

    def test_transition(self):
        t = SWETicket(title="x", description="y")
        assert t.status == TicketStatus.OPEN
        t.transition(TicketStatus.TRIAGED)
        assert t.status == TicketStatus.TRIAGED
        t.transition(TicketStatus.ACKNOWLEDGED)
        assert t.status == TicketStatus.ACKNOWLEDGED

    def test_from_dict_defaults(self):
        t = SWETicket.from_dict({"title": "a", "description": "b"})
        assert t.severity == TicketSeverity.MEDIUM
        assert t.status == TicketStatus.OPEN

    def test_status_roundtrip_acknowledged(self):
        t = SWETicket(title="x", description="y")
        t.transition(TicketStatus.ACKNOWLEDGED)
        t2 = SWETicket.from_dict(t.to_dict())
        assert t2.status == TicketStatus.ACKNOWLEDGED


class TestSWEAgentConfig:
    def test_roundtrip(self):
        cfg = SWEAgentConfig(
            name="tester", role=AgentRole.TESTER, tools=["pytest"], enabled=True
        )
        d = cfg.to_dict()
        cfg2 = SWEAgentConfig.from_dict(d)
        assert cfg2.name == "tester"
        assert cfg2.role == AgentRole.TESTER
        assert cfg2.tools == ["pytest"]


class TestStabilityReport:
    def test_roundtrip(self):
        r = StabilityReport(
            verdict=GovernanceVerdict.BLOCK,
            open_critical=1,
            failing_tests=3,
            details="1 critical bug",
        )
        d = r.to_dict()
        r2 = StabilityReport.from_dict(d)
        assert r2.verdict == GovernanceVerdict.BLOCK
        assert r2.open_critical == 1
        assert r2.failing_tests == 3


# ======================================================================
# Events
# ======================================================================

class TestSWEEvent:
    def test_issue_detected_factory(self):
        e = SWEEvent.issue_detected(
            ticket_id="abc123",
            source_agent="swe_monitor",
            error_summary="reCAPTCHA hit",
            module="scraping",
            severity="high",
        )
        assert e.event == SWEEventType.ISSUE_DETECTED
        assert e.ticket_id == "abc123"
        assert e.payload["error_summary"] == "reCAPTCHA hit"
        assert e.payload["module"] == "scraping"

    def test_roundtrip(self):
        e = SWEEvent.dev_complete(
            ticket_id="t1", source_agent="dev", branch="fix/captcha", files_changed=3
        )
        d = e.to_dict()
        e2 = SWEEvent.from_dict(d)
        assert e2.event == SWEEventType.DEV_COMPLETE
        assert e2.payload["branch"] == "fix/captcha"

    def test_triage_complete_factory(self):
        e = SWEEvent.triage_complete(
            ticket_id="t1", source_agent="triage", assigned_to="investigator_1"
        )
        assert e.event == SWEEventType.TRIAGE_COMPLETE
        assert e.payload["assigned_to"] == "investigator_1"

    def test_test_complete_factory(self):
        e = SWEEvent.test_complete(
            ticket_id="t1", source_agent="tester",
            passed=True, total=42, failures=0,
        )
        assert e.payload["passed"] is True
        assert e.payload["total"] == 42

    def test_deploy_complete_factory(self):
        e = SWEEvent.deploy_complete(
            ticket_id="t1", source_agent="deployer",
            deployment_id="dep1", success=True,
        )
        assert e.event == SWEEventType.DEPLOY_COMPLETE
        assert e.payload["deployment_id"] == "dep1"

    def test_rollback_triggered_factory(self):
        e = SWEEvent.rollback_triggered(
            ticket_id="t1", source_agent="deployer",
            reason="test regression", deployment_id="dep1",
        )
        assert e.event == SWEEventType.ROLLBACK_TRIGGERED

    def test_stability_gate_result_factory(self):
        e = SWEEvent.stability_gate_result(
            ticket_id="system", source_agent="ralph_wiggum",
            verdict="block", details="CI red",
        )
        assert e.event == SWEEventType.STABILITY_GATE_RESULT

    def test_all_event_types_covered(self):
        """Every SWEEventType must be a valid enum value."""
        for et in SWEEventType:
            assert isinstance(et.value, str)


# ======================================================================
# Configuration
# ======================================================================

class TestGovernanceConfig:
    def test_defaults(self):
        g = GovernanceConfig()
        assert g.max_open_critical == 0
        assert g.require_ci_green is True

    def test_from_dict(self):
        g = GovernanceConfig.from_dict({"max_open_high": 5, "enabled": False})
        assert g.max_open_high == 5
        assert g.enabled is False


class TestMonitorConfig:
    def test_defaults(self):
        m = MonitorConfig()
        assert "ERROR" in m.log_patterns
        assert m.scan_interval_minutes == 30

    def test_roundtrip(self):
        m = MonitorConfig(log_patterns=["WARN"], scan_interval_minutes=10)
        d = m.to_dict()
        m2 = MonitorConfig.from_dict(d)
        assert m2.log_patterns == ["WARN"]
        assert m2.scan_interval_minutes == 10


class TestSWETeamConfig:
    def test_empty_defaults(self):
        cfg = SWETeamConfig()
        assert cfg.agents == []
        assert cfg.governance.enabled is False
        assert cfg.enabled is False

    def test_from_dict_with_agents(self):
        data = {
            "agents": [
                {"name": "mon", "role": "monitor", "enabled": True},
                {"name": "tri", "role": "triage", "enabled": True},
            ],
            "governance": {"max_open_critical": 1},
        }
        cfg = SWETeamConfig.from_dict(data)
        assert len(cfg.agents) == 2
        assert cfg.governance.max_open_critical == 1

    def test_get_agents_by_role(self):
        cfg = SWETeamConfig.from_dict({
            "agents": [
                {"name": "a", "role": "monitor", "enabled": True},
                {"name": "b", "role": "triage", "enabled": True},
                {"name": "c", "role": "monitor", "enabled": False},
            ]
        })
        monitors = cfg.get_agents_by_role(AgentRole.MONITOR)
        assert len(monitors) == 1
        assert monitors[0].name == "a"


class TestLoadConfig:
    def test_load_missing_file(self):
        cfg = load_config("/nonexistent/path.yaml")
        assert isinstance(cfg, SWETeamConfig)
        assert cfg.enabled is False

    def test_load_from_yaml(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write("enabled: false\ngovernance:\n  max_open_high: 10\n")
            f.flush()
            cfg = load_config(f.name)
        os.unlink(f.name)
        assert cfg.enabled is False
        assert cfg.governance.max_open_high == 10

    def test_env_override(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write("enabled: true\n")
            f.flush()
            with patch.dict(os.environ, {"SWE_TEAM_CONFIG": f.name}):
                cfg = load_config()
        os.unlink(f.name)
        assert cfg.enabled is True


# ======================================================================
# Monitor Agent
# ======================================================================

class TestMonitorHelpers:
    def test_severity_mapping(self):
        assert _severity_from_pattern("CRITICAL") == TicketSeverity.CRITICAL
        assert _severity_from_pattern("ERROR") == TicketSeverity.HIGH
        assert _severity_from_pattern("Traceback") == TicketSeverity.HIGH
        assert _severity_from_pattern("FAILED") == TicketSeverity.MEDIUM
        assert _severity_from_pattern("unknown") == TicketSeverity.MEDIUM

    def test_fingerprint_stability(self):
        fp1 = _fingerprint("/logs/a.log", "2025-01-01 12:00:00 ERROR boom")
        fp2 = _fingerprint("/logs/a.log", "2026-03-13 08:00:00 ERROR boom")
        # Same error, different timestamp → same fingerprint
        assert fp1 == fp2

    def test_fingerprint_different_files(self):
        fp1 = _fingerprint("/logs/a.log", "ERROR boom")
        fp2 = _fingerprint("/logs/b.log", "ERROR boom")
        assert fp1 != fp2


class TestMonitorAgent:
    def test_scan_disabled(self):
        cfg = MonitorConfig(enabled=False)
        agent = MonitorAgent(cfg)
        tickets = agent.scan()
        assert tickets == []

    def test_scan_nonexistent_dir(self):
        cfg = MonitorConfig(enabled=True, log_directories=["/nonexistent/logs"])
        agent = MonitorAgent(cfg)
        tickets = agent.scan()
        assert tickets == []

    def test_scan_finds_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "test.log"
            log_file.write_text(
                "2025-01-01 10:00:00 INFO All good\n"
                "2025-01-01 10:01:00 ERROR Something broke\n"
                "2025-01-01 10:02:00 CRITICAL Fatal error\n"
                "2025-01-01 10:03:00 DEBUG Ignored\n"
            )
            cfg = MonitorConfig(enabled=True, log_directories=[tmpdir])
            agent = MonitorAgent(cfg)
            tickets = agent.scan()
            assert len(tickets) == 2
            severities = {t.severity for t in tickets}
            assert TicketSeverity.HIGH in severities
            assert TicketSeverity.CRITICAL in severities

    def test_dedup_across_scans(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "test.log"
            log_file.write_text("ERROR same error\n")
            cfg = MonitorConfig(enabled=True, log_directories=[tmpdir])
            agent = MonitorAgent(cfg)
            t1 = agent.scan()
            t2 = agent.scan()
            assert len(t1) == 1
            assert len(t2) == 0  # Already known

    def test_build_events(self):
        cfg = MonitorConfig(enabled=True)
        agent = MonitorAgent(cfg)
        ticket = SWETicket(
            title="ERROR: boom",
            description="test",
            severity=TicketSeverity.HIGH,
            source_module="scraping",
        )
        events = agent.build_events([ticket])
        assert len(events) == 1
        assert events[0].event == SWEEventType.ISSUE_DETECTED


# ======================================================================
# Triage Agent
# ======================================================================

class TestTriageAgent:
    def _make_config(self, agents=None):
        if agents is None:
            agents = [
                {"name": "browser_investigator", "role": "investigator", "enabled": True},
                {"name": "db_investigator", "role": "investigator", "enabled": True},
            ]
        return SWETeamConfig.from_dict({"agents": agents})

    def test_triage_assigns_module_specialist(self):
        cfg = self._make_config()
        triage = TriageAgent(cfg)
        ticket = SWETicket(
            title="Scraping error",
            description="x",
            severity=TicketSeverity.HIGH,
            source_module="scraping",
        )
        result = triage.triage(ticket)
        assert result.assigned_to == "browser_investigator"
        assert result.status == TicketStatus.TRIAGED

    def test_triage_db_module(self):
        cfg = self._make_config()
        triage = TriageAgent(cfg)
        ticket = SWETicket(
            title="DB timeout",
            description="x",
            source_module="database",
        )
        result = triage.triage(ticket)
        assert result.assigned_to == "db_investigator"

    def test_triage_critical_first_available(self):
        cfg = self._make_config()
        triage = TriageAgent(cfg)
        ticket = SWETicket(
            title="Critical failure",
            description="x",
            severity=TicketSeverity.CRITICAL,
            source_module="unknown_module",
        )
        result = triage.triage(ticket)
        assert result.assigned_to == "browser_investigator"  # First available

    def test_triage_fallback(self):
        cfg = self._make_config()
        triage = TriageAgent(cfg)
        ticket = SWETicket(
            title="Unknown error",
            description="x",
            source_module="unknown_module",
        )
        result = triage.triage(ticket)
        assert result.assigned_to == "browser_investigator"  # Fallback

    def test_triage_no_investigators(self):
        cfg = self._make_config(agents=[])
        triage = TriageAgent(cfg)
        ticket = SWETicket(title="x", description="y")
        result = triage.triage(ticket)
        assert result.assigned_to is None

    def test_triage_batch_sorts_by_severity(self):
        cfg = self._make_config()
        triage = TriageAgent(cfg)
        tickets = [
            SWETicket(title="low", description="x", severity=TicketSeverity.LOW),
            SWETicket(title="crit", description="x", severity=TicketSeverity.CRITICAL),
            SWETicket(title="high", description="x", severity=TicketSeverity.HIGH),
        ]
        results = triage.triage_batch(tickets)
        assert results[0].title == "crit"
        assert results[1].title == "high"
        assert results[2].title == "low"

    def test_build_events(self):
        cfg = self._make_config()
        triage = TriageAgent(cfg)
        ticket = SWETicket(
            title="x", description="y", severity=TicketSeverity.HIGH
        )
        triage.triage(ticket)
        events = triage.build_events([ticket])
        assert len(events) == 1
        assert events[0].event == SWEEventType.TRIAGE_COMPLETE


# ======================================================================
# Ralph-Wiggum Gate
# ======================================================================

class TestRalphWiggumGate:
    def _make_tickets(self, critical=0, high=0):
        tickets = []
        for _ in range(critical):
            tickets.append(SWETicket(
                title="crit", description="x",
                severity=TicketSeverity.CRITICAL, status=TicketStatus.OPEN,
            ))
        for _ in range(high):
            tickets.append(SWETicket(
                title="high", description="x",
                severity=TicketSeverity.HIGH, status=TicketStatus.OPEN,
            ))
        return tickets

    def test_pass_when_clean(self):
        gate = RalphWiggumGate(GovernanceConfig(enabled=True))
        report = gate.evaluate([], ci_green=True, failing_tests=0)
        assert report.verdict == GovernanceVerdict.PASS

    def test_block_on_critical(self):
        gate = RalphWiggumGate(GovernanceConfig(enabled=True, max_open_critical=0))
        tickets = self._make_tickets(critical=1)
        report = gate.evaluate(tickets, ci_green=True, failing_tests=0)
        assert report.verdict == GovernanceVerdict.BLOCK
        assert "critical" in report.details.lower()

    def test_block_on_too_many_high(self):
        gate = RalphWiggumGate(GovernanceConfig(enabled=True, max_open_high=2))
        tickets = self._make_tickets(high=3)
        report = gate.evaluate(tickets, ci_green=True, failing_tests=0)
        assert report.verdict == GovernanceVerdict.BLOCK

    def test_block_on_ci_red(self):
        gate = RalphWiggumGate(GovernanceConfig(enabled=True))
        report = gate.evaluate([], ci_green=False, failing_tests=0)
        assert report.verdict == GovernanceVerdict.BLOCK
        assert "CI" in report.details

    def test_block_on_failing_tests(self):
        gate = RalphWiggumGate(GovernanceConfig(enabled=True))
        report = gate.evaluate([], ci_green=True, failing_tests=5)
        assert report.verdict == GovernanceVerdict.BLOCK

    def test_pass_when_disabled(self):
        gate = RalphWiggumGate(GovernanceConfig(enabled=False))
        tickets = self._make_tickets(critical=10)
        report = gate.evaluate(tickets, ci_green=False, failing_tests=99)
        assert report.verdict == GovernanceVerdict.PASS

    def test_closed_tickets_ignored(self):
        gate = RalphWiggumGate(GovernanceConfig(enabled=True, max_open_critical=0))
        ticket = SWETicket(
            title="resolved", description="x",
            severity=TicketSeverity.CRITICAL,
        )
        ticket.transition(TicketStatus.RESOLVED)
        report = gate.evaluate([ticket], ci_green=True, failing_tests=0)
        assert report.verdict == GovernanceVerdict.PASS

    def test_build_event(self):
        gate = RalphWiggumGate(GovernanceConfig(enabled=True))
        report = StabilityReport(
            verdict=GovernanceVerdict.BLOCK, details="CI red"
        )
        event = gate.build_event(report)
        assert event.event == SWEEventType.STABILITY_GATE_RESULT
        assert event.payload["verdict"] == "block"


# ======================================================================
# Deployment Governance
# ======================================================================

class TestDeploymentGovernor:
    def test_can_deploy_pass(self):
        gov = DeploymentGovernor()
        report = StabilityReport(verdict=GovernanceVerdict.PASS)
        assert gov.can_deploy(report) is True

    @patch("src.swe_team.governance.logger")
    def test_can_deploy_blocked(self, mock_logger):
        gov = DeploymentGovernor()
        report = StabilityReport(verdict=GovernanceVerdict.BLOCK)
        assert gov.can_deploy(report) is False

    def test_full_lifecycle(self):
        gov = DeploymentGovernor()
        rec = gov.start_deployment("t1", branch="fix/captcha")
        assert rec.status == "deploying"
        assert rec.ticket_id == "t1"

        rec2 = gov.complete_deployment(
            rec.deployment_id, test_results={"passed": True}
        )
        assert rec2 is not None
        assert rec2.status == "deployed"
        assert rec2.test_results == {"passed": True}

    @patch("src.swe_team.governance.logger")
    def test_rollback(self, mock_logger):
        gov = DeploymentGovernor()
        rec = gov.start_deployment("t1")
        rolled = gov.rollback(rec.deployment_id, reason="test regression")
        assert rolled.status == "rolled_back"
        assert rolled.rollback_reason == "test regression"

    @patch("src.swe_team.governance.logger")
    def test_complete_nonexistent(self, mock_logger):
        gov = DeploymentGovernor()
        assert gov.complete_deployment("nope") is None

    @patch("src.swe_team.governance.logger")
    def test_rollback_nonexistent(self, mock_logger):
        gov = DeploymentGovernor()
        assert gov.rollback("nope") is None

    def test_build_deploy_event(self):
        gov = DeploymentGovernor()
        rec = gov.start_deployment("t1")
        gov.complete_deployment(rec.deployment_id)
        rec = gov._find(rec.deployment_id)
        event = gov.build_deploy_event(rec)
        assert event.event == SWEEventType.DEPLOY_COMPLETE
        assert event.payload["success"] is True

    def test_build_rollback_event(self):
        gov = DeploymentGovernor()
        rec = gov.start_deployment("t1")
        gov.rollback(rec.deployment_id, reason="bad")
        rec = gov._find(rec.deployment_id)
        event = gov.build_rollback_event(rec)
        assert event.event == SWEEventType.ROLLBACK_TRIGGERED

    def test_records_tracked(self):
        gov = DeploymentGovernor()
        gov.start_deployment("t1")
        gov.start_deployment("t2")
        assert len(gov.records) == 2


class TestDeploymentRecord:
    def test_roundtrip(self):
        rec = DeploymentRecord(
            ticket_id="t1", branch="main", status="deployed"
        )
        d = rec.to_dict()
        rec2 = DeploymentRecord.from_dict(d)
        assert rec2.ticket_id == "t1"
        assert rec2.status == "deployed"


# ======================================================================
# Phase C: Developer gate
# ======================================================================

class TestFixComplexityGate:
    def test_allows_single_module(self):
        ok, reason = check_fix_complexity(
            ["src/swe_team/runner.py", "tests/unit/test_swe_team.py"],
            120,
            allowed_modules={"swe_team"},
        )
        assert ok is True
        assert reason == "ok"

    def test_blocks_dependency_change(self):
        ok, reason = check_fix_complexity(
            ["requirements.txt"],
            10,
            allowed_modules={"swe_team"},
        )
        assert ok is False
        assert "Dependency" in reason

    def test_blocks_cross_module(self):
        ok, reason = check_fix_complexity(
            ["src/swe_team/runner.py", "src/scraping/job_scraper.py"],
            50,
            allowed_modules={"swe_team"},
        )
        assert ok is False
        assert "Cross-module" in reason

    def test_feature_detection(self):
        ticket = SWETicket(title="Add feature", description="x", labels=["feature"])
        assert DeveloperAgent._is_feature(ticket) is True


# ======================================================================
# Ticket Store
# ======================================================================

class TestTicketStore:
    def test_add_and_get(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "tickets.json")
            store = TicketStore(path)
            t = SWETicket(title="bug", description="x")
            store.add(t)
            assert store.get(t.ticket_id) is not None
            assert store.get(t.ticket_id).title == "bug"

    def test_list_all(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "tickets.json")
            store = TicketStore(path)
            store.add(SWETicket(title="a", description="x"))
            store.add(SWETicket(title="b", description="y"))
            assert len(store.list_all()) == 2

    def test_list_by_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "tickets.json")
            store = TicketStore(path)
            t1 = SWETicket(title="open", description="x")
            t2 = SWETicket(title="closed", description="y")
            t2.transition(TicketStatus.CLOSED)
            store.add(t1)
            store.add(t2)
            assert len(store.list_by_status(TicketStatus.OPEN)) == 1
            assert len(store.list_by_status(TicketStatus.CLOSED)) == 1

    def test_list_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "tickets.json")
            store = TicketStore(path)
            t1 = SWETicket(title="open", description="x")
            t2 = SWETicket(title="resolved", description="y")
            t2.transition(TicketStatus.RESOLVED)
            store.add(t1)
            store.add(t2)
            assert len(store.list_open()) == 1

    def test_list_open_excludes_acknowledged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "tickets.json")
            store = TicketStore(path)
            t1 = SWETicket(title="open", description="x")
            t2 = SWETicket(title="ack", description="y")
            t2.transition(TicketStatus.ACKNOWLEDGED)
            store.add(t1)
            store.add(t2)
            assert len(store.list_open()) == 1

    def test_persistence_across_instances(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "tickets.json")
            store1 = TicketStore(path)
            store1.add(SWETicket(title="persist", description="x"))
            store2 = TicketStore(path)
            assert len(store2.list_all()) == 1
            assert store2.list_all()[0].title == "persist"

    def test_fingerprint_tracking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "tickets.json")
            store = TicketStore(path)
            t = SWETicket(
                title="err", description="x",
                metadata={"fingerprint": "abc123"},
            )
            store.add(t)
            assert "abc123" in store.known_fingerprints

    def test_get_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "tickets.json")
            store = TicketStore(path)
            assert store.get("nonexistent") is None

    def test_load_corrupted_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "tickets.json")
            Path(path).write_text("not valid json")
            store = TicketStore(path)
            assert len(store.list_all()) == 0


# ======================================================================
# SWE_TEAM_ENABLED environment variable override
# ======================================================================

class TestSWETeamEnabledEnvOverride:
    """Verify that the SWE_TEAM_ENABLED env var overrides the YAML flag."""

    def test_env_enabled_true(self, tmp_path):
        """SWE_TEAM_ENABLED=true should override enabled: false in YAML."""
        cfg_file = tmp_path / "swe_team.yaml"
        cfg_file.write_text("enabled: false\n")
        with patch.dict(os.environ, {"SWE_TEAM_ENABLED": "true"}):
            config = load_config(str(cfg_file))
        assert config.enabled is True

    def test_env_enabled_false(self, tmp_path):
        """SWE_TEAM_ENABLED=false should override enabled: true in YAML."""
        cfg_file = tmp_path / "swe_team.yaml"
        cfg_file.write_text("enabled: true\n")
        with patch.dict(os.environ, {"SWE_TEAM_ENABLED": "false"}):
            config = load_config(str(cfg_file))
        assert config.enabled is False


# ======================================================================
# Phase B: Notifier
# ======================================================================

class TestNotifier:
    """Tests for src.swe_team.notifier — Telegram integration.

    Notifier functions are synchronous (async Telegram call is wrapped
    inside ``_send()``).  Tests mock ``_send`` to verify message formatting.
    """

    def test_notify_new_tickets_filters_low(self):
        """Only HIGH/CRITICAL tickets should be included."""
        from src.swe_team.notifier import notify_new_tickets

        low = SWETicket(title="minor", description="x", severity=TicketSeverity.LOW)
        med = SWETicket(title="medium", description="x", severity=TicketSeverity.MEDIUM)
        with patch("src.swe_team.notifier._send") as mock_send:
            notify_new_tickets([low, med])
            mock_send.assert_not_called()

    def test_notify_new_tickets_sends_for_high(self):
        """HIGH tickets should trigger a Telegram message."""
        from src.swe_team.notifier import notify_new_tickets

        high = SWETicket(
            title="Scraper crash", description="x",
            severity=TicketSeverity.HIGH, source_module="scraping",
            assigned_to="browser_investigator",
        )
        with patch("src.swe_team.notifier._send", return_value=True) as mock_send:
            notify_new_tickets([high])
            mock_send.assert_called_once()
            msg = mock_send.call_args[0][0]
            assert "HIGH" in msg
            assert "Scraper crash" in msg
            assert "scraping" in msg

    def test_notify_new_tickets_groups_multiple(self):
        """Multiple tickets should be grouped into a single message."""
        from src.swe_team.notifier import notify_new_tickets

        tickets = [
            SWETicket(title="Bug A", description="x", severity=TicketSeverity.CRITICAL),
            SWETicket(title="Bug B", description="x", severity=TicketSeverity.HIGH),
        ]
        with patch("src.swe_team.notifier._send", return_value=True) as mock_send:
            notify_new_tickets(tickets)
            mock_send.assert_called_once()
            msg = mock_send.call_args[0][0]
            assert "2 new ticket" in msg
            assert "Bug A" in msg
            assert "Bug B" in msg

    def test_notify_stability_gate_only_on_block(self):
        """Should not send if verdict is PASS."""
        from src.swe_team.notifier import notify_stability_gate

        report = StabilityReport(verdict=GovernanceVerdict.PASS, details="all good")
        with patch("src.swe_team.notifier._send") as mock_send:
            notify_stability_gate(report)
            mock_send.assert_not_called()

    def test_notify_stability_gate_sends_on_block(self):
        """Should send when verdict is BLOCK."""
        from src.swe_team.notifier import notify_stability_gate

        report = StabilityReport(
            verdict=GovernanceVerdict.BLOCK,
            open_critical=2, failing_tests=5,
            details="2 critical bugs open",
        )
        with patch("src.swe_team.notifier._send", return_value=True) as mock_send:
            notify_stability_gate(report)
            mock_send.assert_called_once()
            msg = mock_send.call_args[0][0]
            assert "BLOCKED" in msg
            assert "2" in msg

    def test_notify_daily_summary_empty(self):
        """Should send a 'no open tickets' message when store is empty."""
        from src.swe_team.notifier import notify_daily_summary

        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(os.path.join(tmpdir, "tickets.json"))
            with patch("src.swe_team.notifier._send", return_value=True) as mock_send:
                notify_daily_summary(store)
                mock_send.assert_called_once()
                msg = mock_send.call_args[0][0]
                assert "No open tickets" in msg

    def test_notify_daily_summary_with_tickets(self):
        """Should include severity counts in summary."""
        from src.swe_team.notifier import notify_daily_summary

        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(os.path.join(tmpdir, "tickets.json"))
            store.add(SWETicket(title="a", description="x", severity=TicketSeverity.CRITICAL))
            store.add(SWETicket(title="b", description="x", severity=TicketSeverity.HIGH))
            store.add(SWETicket(title="c", description="x", severity=TicketSeverity.HIGH))
            with patch("src.swe_team.notifier._send", return_value=True) as mock_send:
                notify_daily_summary(store)
                mock_send.assert_called_once()
                msg = mock_send.call_args[0][0]
                assert "3 open ticket" in msg
                assert "CRITICAL" in msg
                assert "HIGH" in msg

    def test_notify_investigation_summary_sends(self):
        """Should send an investigation summary when report exists."""
        from src.swe_team.notifier import notify_investigation_summary

        ticket = SWETicket(
            title="Scraper crash",
            description="x",
            severity=TicketSeverity.HIGH,
            source_module="scraping",
            investigation_report="Root cause: timeout\nDetails...",
        )
        with patch("src.swe_team.notifier._send", return_value=True) as mock_send:
            notify_investigation_summary(ticket)
            mock_send.assert_called_once()
            msg = mock_send.call_args[0][0]
            assert "Investigation complete" in msg
            assert "HIGH" in msg
            assert "Scraper crash" in msg
            assert "Root cause" in msg

    def test_notify_investigation_summary_noop_without_report(self):
        """Should no-op when no investigation report exists."""
        from src.swe_team.notifier import notify_investigation_summary

        ticket = SWETicket(
            title="Scraper crash",
            description="Scraper encountered timeout during job fetch",
        )
        with patch("src.swe_team.notifier._send") as mock_send:
            notify_investigation_summary(ticket)
            mock_send.assert_not_called()

    def test_notify_investigation_summary_send_failure(self):
        """Should attempt sending even if the send helper returns False."""
        from src.swe_team.notifier import notify_investigation_summary

        ticket = SWETicket(
            title="Scraper crash",
            description="Timeout during job fetch",
            severity=TicketSeverity.HIGH,
            source_module="scraping",
            investigation_report="Root cause: timeout",
        )
        with patch("src.swe_team.notifier._send", return_value=False) as mock_send:
            notify_investigation_summary(ticket)
            mock_send.assert_called_once()

    def test_esc_html(self):
        """HTML escaping should handle special characters."""
        from src.swe_team.notifier import _esc
        assert _esc("<b>test</b>") == "&lt;b&gt;test&lt;/b&gt;"
        assert _esc('a&b"c') == "a&amp;b&quot;c"


# ======================================================================
# Phase B: GitHub Integration
# ======================================================================

class TestGitHubIntegration:
    """Tests for src.swe_team.github_integration — gh CLI integration."""

    def test_create_issue_skips_low_severity(self):
        """Should return None for LOW/MEDIUM tickets."""
        from src.swe_team.github_integration import create_github_issue

        ticket = SWETicket(title="minor", description="x", severity=TicketSeverity.LOW)
        result = create_github_issue(ticket)
        assert result is None

    def test_create_issue_skips_medium_severity(self):
        """Should return None for MEDIUM tickets."""
        from src.swe_team.github_integration import create_github_issue

        ticket = SWETicket(title="medium", description="x", severity=TicketSeverity.MEDIUM)
        result = create_github_issue(ticket)
        assert result is None

    def test_create_issue_calls_gh(self):
        """Should call gh issue create with correct arguments."""
        from src.swe_team.github_integration import create_github_issue

        ticket = SWETicket(
            title="Scraper crash", description="Something broke",
            severity=TicketSeverity.CRITICAL, source_module="scraping",
            error_log="ERROR: boom", metadata={"fingerprint": "abc123"},
        )
        mock_result = type("R", (), {
            "returncode": 0,
            "stdout": "https://github.com/example-org/example-repo/issues/42\n",
            "stderr": "",
        })()
        with patch("src.swe_team.github_integration.subprocess.run", return_value=mock_result) as mock_run:
            issue_num = create_github_issue(ticket)
            assert issue_num == 42
            call_args = mock_run.call_args[0][0]
            assert "gh" in call_args
            assert "issue" in call_args
            assert "create" in call_args
            # Verify title has prefix
            title_idx = call_args.index("--title") + 1
            assert call_args[title_idx].startswith("[SWE-AUTO]")
            # Verify body contains fingerprint
            body_idx = call_args.index("--body") + 1
            assert "fingerprint:abc123" in call_args[body_idx]

    def test_create_issue_handles_failure(self):
        """Should return None when gh fails."""
        from src.swe_team.github_integration import create_github_issue

        ticket = SWETicket(title="crash", description="x", severity=TicketSeverity.HIGH)
        mock_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "auth error"})()
        with patch("src.swe_team.github_integration.subprocess.run", return_value=mock_result):
            assert create_github_issue(ticket) is None

    def test_comment_on_issue(self):
        """Should call gh issue comment."""
        from src.swe_team.github_integration import comment_on_issue

        mock_result = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("src.swe_team.github_integration.subprocess.run", return_value=mock_result) as mock_run:
            result = comment_on_issue(42, "Update: fixed")
            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "42" in call_args
            assert "Update: fixed" in call_args

    def test_comment_on_issue_failure(self):
        """Should return False when gh fails."""
        from src.swe_team.github_integration import comment_on_issue

        mock_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "not found"})()
        with patch("src.swe_team.github_integration.subprocess.run", return_value=mock_result):
            assert comment_on_issue(999, "test") is False

    def test_find_existing_issue_by_fingerprint(self):
        """Should find issue by fingerprint in body."""
        from src.swe_team.github_integration import find_existing_issue

        ticket = SWETicket(
            title="Scraper crash", description="x",
            severity=TicketSeverity.HIGH,
            metadata={"fingerprint": "abc123"},
        )
        issues_json = json.dumps([
            {"number": 10, "title": "[SWE-AUTO] Other issue", "body": "no match"},
            {"number": 15, "title": "[SWE-AUTO] Scraper crash", "body": "<!-- fingerprint:abc123 -->"},
        ])
        mock_result = type("R", (), {"returncode": 0, "stdout": issues_json, "stderr": ""})()
        with patch("src.swe_team.github_integration.subprocess.run", return_value=mock_result):
            result = find_existing_issue(ticket)
            assert result == 15

    def test_find_existing_issue_by_title(self):
        """Should fall back to title match when no fingerprint match."""
        from src.swe_team.github_integration import find_existing_issue

        ticket = SWETicket(
            title="Scraper crash in CDP", description="x",
            severity=TicketSeverity.HIGH, metadata={},
        )
        issues_json = json.dumps([
            {"number": 20, "title": "[SWE-AUTO] Scraper crash in CDP connection", "body": "details"},
        ])
        mock_result = type("R", (), {"returncode": 0, "stdout": issues_json, "stderr": ""})()
        with patch("src.swe_team.github_integration.subprocess.run", return_value=mock_result):
            result = find_existing_issue(ticket)
            assert result == 20

    def test_find_existing_issue_none(self):
        """Should return None when no matching issue found."""
        from src.swe_team.github_integration import find_existing_issue

        ticket = SWETicket(
            title="Brand new error", description="x",
            severity=TicketSeverity.HIGH, metadata={"fingerprint": "xyz"},
        )
        issues_json = json.dumps([])
        mock_result = type("R", (), {"returncode": 0, "stdout": issues_json, "stderr": ""})()
        with patch("src.swe_team.github_integration.subprocess.run", return_value=mock_result):
            assert find_existing_issue(ticket) is None


# ======================================================================
# Phase C: Investigation
# ======================================================================

class TestInvestigatorAgent:
    def test_investigator_updates_ticket(self, tmp_path):
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        ticket = SWETicket(
            title="Scraper crash",
            description="boom",
            severity=TicketSeverity.HIGH,
            source_module="scraping",
            error_log="Traceback: boom",
        )
        ticket.transition(TicketStatus.TRIAGED)
        ticket.metadata["github_issue"] = 42

        mock_result = type(
            "R",
            (),
            {"returncode": 0, "stdout": "Root cause: X\n", "stderr": "Cost: $0.04"},
        )()

        with (
            patch("src.swe_team.investigator.subprocess.run", return_value=mock_result) as mock_run,
            patch("src.swe_team.investigator.notify_investigation_summary") as mock_notify,
            patch("src.swe_team.investigator.comment_on_issue") as mock_comment,
        ):
            agent = InvestigatorAgent(program_path=program, claude_path="/usr/bin/claude")
            result = agent.investigate(ticket)

        assert result is True
        assert ticket.status == TicketStatus.INVESTIGATION_COMPLETE
        assert ticket.investigation_report == "Root cause: X"
        assert ticket.metadata["investigation"]["status"] == "complete"
        mock_notify.assert_called_once()
        mock_comment.assert_called_once()
        assert "--dangerously-skip-permissions" in mock_run.call_args[0][0]

    def test_investigator_skips_low_severity(self, tmp_path):
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        ticket = SWETicket(
            title="Minor issue",
            description="x",
            severity=TicketSeverity.LOW,
        )
        ticket.transition(TicketStatus.TRIAGED)

        with patch("src.swe_team.investigator.subprocess.run") as mock_run:
            agent = InvestigatorAgent(program_path=program)
            result = agent.investigate(ticket)

        assert result is False
        mock_run.assert_not_called()


class TestParseCost:
    def test_parse_cost_simple(self):
        assert _parse_cost("Cost: $0.04") == 0.04

    def test_parse_cost_with_commas(self):
        assert _parse_cost("Cost: $1,234.56") == 1234.56

    def test_parse_cost_missing(self):
        assert _parse_cost("no cost here") is None

    def test_parse_cost_empty(self):
        assert _parse_cost("") is None

    def test_parse_cost_invalid(self):
        assert _parse_cost("Cost: $abc") is None


# ======================================================================
# Phase C: A2A Adapter
# ======================================================================

class TestSWETeamAdapter:
    def test_agent_card_skills(self, tmp_path):
        from src.a2a.adapters.swe_team import SWETeamAdapter

        store = TicketStore(str(tmp_path / "tickets.json"))
        adapter = SWETeamAdapter(config=SWETeamConfig(), store=store)
        skill_ids = {skill.id for skill in adapter.agent_card().skills}
        assert {"monitor_scan", "triage_ticket", "investigate_ticket", "check_stability"} <= skill_ids

    def test_triage_ticket_action(self, tmp_path):
        from src.a2a.adapters.swe_team import SWETeamAdapter

        config = SWETeamConfig(
            agents=[SWEAgentConfig(name="inv", role=AgentRole.INVESTIGATOR, enabled=True)]
        )
        store = TicketStore(str(tmp_path / "tickets.json"))
        adapter = SWETeamAdapter(config=config, store=store)

        ticket_data = {
            "title": "recaptcha block",
            "description": "blocked by recaptcha",
            "severity": "high",
            "source_module": "scraping",
        }
        result = adapter.handle_action("triage_ticket", {"ticket": ticket_data})
        assert result["ticket"]["status"] == "triaged"

    def test_monitor_scan_action(self, tmp_path):
        from src.a2a.adapters.swe_team import SWETeamAdapter

        store = TicketStore(str(tmp_path / "tickets.json"))
        adapter = SWETeamAdapter(config=SWETeamConfig(), store=store)
        mock_ticket = SWETicket(title="error", description="x")

        with patch("src.a2a.adapters.swe_team.MonitorAgent") as mock_monitor:
            mock_monitor.return_value.scan.return_value = [mock_ticket]
            result = adapter.handle_action("monitor_scan", {})

        assert result["count"] == 1

    def test_swe_event_to_pipeline_event(self):
        from src.a2a.adapters.swe_team import swe_event_to_pipeline_event

        event = SWEEvent.issue_detected(
            ticket_id="t123",
            source_agent="swe_monitor",
            error_summary="boom",
            module="scraping",
            severity="high",
        )
        pipeline = swe_event_to_pipeline_event(event)
        assert pipeline.event.startswith("swe_team.")
        assert pipeline.payload["ticket_id"] == "t123"


# ======================================================================
# Phase C: Creative Agent
# ======================================================================

class TestCreativeAgent:
    def test_creative_agent_no_resolved(self, tmp_path):
        from src.swe_team.creative_agent import CreativeAgent

        store = TicketStore(str(tmp_path / "tickets.json"))
        agent = CreativeAgent()
        assert agent.propose(store) == []

    def test_creative_agent_proposes(self, tmp_path):
        from src.swe_team.creative_agent import CreativeAgent

        store = TicketStore(str(tmp_path / "tickets.json"))
        t1 = SWETicket(title="A", description="x", source_module="scraping")
        t1.transition(TicketStatus.RESOLVED)
        t2 = SWETicket(title="B", description="x", source_module="scraping")
        t2.transition(TicketStatus.RESOLVED)
        t3 = SWETicket(title="C", description="x", source_module="auth")
        t3.transition(TicketStatus.RESOLVED)
        store.add(t1)
        store.add(t2)
        store.add(t3)

        agent = CreativeAgent()
        proposals = agent.propose(store, limit=2)
        assert len(proposals) == 2
        assert all(p.severity == TicketSeverity.LOW for p in proposals)
        assert proposals[0].title.startswith("[SWE-CREATIVE]")

    def test_creative_issue_creation(self):
        from src.swe_team.creative_agent import CreativeAgent

        ticket = SWETicket(title="Proposal", description="x", severity=TicketSeverity.LOW)
        mock_result = type("R", (), {
            "returncode": 0,
            "stdout": "https://github.com/example-org/example-repo/issues/7",
            "stderr": "",
        })()
        with patch("src.swe_team.creative_agent.subprocess.run", return_value=mock_result):
            agent = CreativeAgent()
            assert agent._create_issue(ticket) == 7


# ======================================================================
# Phase C: Trajectory Distiller
# ======================================================================

class TestTrajectoryDistiller:
    def test_record_success_creates_file(self, tmp_path):
        from src.swe_team.distiller import TrajectoryDistiller

        distiller = TrajectoryDistiller(automations_dir=tmp_path)
        ticket = SWETicket(title="Fix", description="x", metadata={"fingerprint": "abc123"})
        record = distiller.record_success(ticket, steps=[["echo", "ok"]])

        assert record is not None
        assert distiller.automation_path("abc123").is_file()

    def test_run_automation_respects_threshold(self, tmp_path):
        from src.swe_team.distiller import TrajectoryDistiller

        distiller = TrajectoryDistiller(automations_dir=tmp_path, success_threshold=0.8)
        ticket = SWETicket(title="Fix", description="x", metadata={"fingerprint": "abc123"})
        distiller.record_success(ticket, steps=[["echo", "ok"]])

        path = distiller.automation_path("abc123")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["success_count"] = 1
        data["failure_count"] = 1
        data["success_rate"] = 0.5
        path.write_text(json.dumps(data), encoding="utf-8")

        with patch("src.swe_team.distiller.subprocess.run") as mock_run:
            assert distiller.run_automation(ticket) is False
            mock_run.assert_not_called()

    def test_run_automation_success_updates_rate(self, tmp_path):
        from src.swe_team.distiller import TrajectoryDistiller

        distiller = TrajectoryDistiller(automations_dir=tmp_path, success_threshold=0.8)
        ticket = SWETicket(title="Fix", description="x", metadata={"fingerprint": "abc123"})
        distiller.record_success(ticket, steps=[["echo", "ok"]])

        mock_proc = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("src.swe_team.distiller.subprocess.run", return_value=mock_proc):
            assert distiller.run_automation(ticket) is True

        record = distiller.get_automation("abc123")
        assert record is not None
        assert record.success_count == 2

# ======================================================================
# Phase B: Improved Module Detection
# ======================================================================

class TestGuessModuleContent:
    """Tests for content-based module detection in _guess_module."""

    def test_path_based_still_works(self):
        """Path-based detection should still take priority."""
        from src.swe_team.monitor_agent import _guess_module
        assert _guess_module("/home/agent/src/scraping/scraper.log") == "scraping"
        assert _guess_module("/home/agent/src/database/queries.log") == "database"

    def test_scraping_keywords(self):
        from src.swe_team.monitor_agent import _guess_module
        assert _guess_module("logs/main.log", "ERROR scraper failed to parse selector") == "scraping"
        assert _guess_module("logs/main.log", "Playwright CDP connection lost") == "scraping"
        assert _guess_module("logs/main.log", "job_scraper timeout on page load") == "scraping"

    def test_application_keywords(self):
        from src.swe_team.monitor_agent import _guess_module
        assert _guess_module("logs/main.log", "ERROR apply failed for job 123") == "application"
        assert _guess_module("logs/main.log", "Goose recipe execution error") == "application"
        assert _guess_module("logs/main.log", "submission blocked by CAPTCHA") == "application"

    def test_auth_keywords(self):
        from src.swe_team.monitor_agent import _guess_module
        assert _guess_module("logs/main.log", "ERROR session expired, cookie invalid") == "auth"
        assert _guess_module("logs/main.log", "li_at token refresh failed") == "auth"
        assert _guess_module("logs/main.log", "Chrome profile login required") == "auth"

    def test_evaluation_keywords(self):
        from src.swe_team.monitor_agent import _guess_module
        assert _guess_module("logs/main.log", "evaluation scoring failed") == "evaluation"
        assert _guess_module("logs/main.log", "SBERT embedding timeout") == "evaluation"
        assert _guess_module("logs/main.log", "ko_system filter error") == "evaluation"

    def test_database_keywords(self):
        from src.swe_team.monitor_agent import _guess_module
        assert _guess_module("logs/main.log", "asyncpg connection pool exhausted") == "database"
        assert _guess_module("logs/main.log", "supabase query timeout") == "database"
        assert _guess_module("logs/main.log", "PostgreSQL migration failed") == "database"

    def test_a2a_keywords(self):
        from src.swe_team.monitor_agent import _guess_module
        assert _guess_module("logs/main.log", "a2a dispatch failed") == "a2a"
        assert _guess_module("logs/main.log", "event_handler crashed") == "a2a"

    def test_notifications_keywords(self):
        from src.swe_team.monitor_agent import _guess_module
        assert _guess_module("logs/main.log", "telegram send failed") == "notifications"
        assert _guess_module("logs/main.log", "notification alert error") == "notifications"

    def test_infrastructure_keywords(self):
        from src.swe_team.monitor_agent import _guess_module
        assert _guess_module("logs/main.log", "health check failed on worker") == "infrastructure"
        assert _guess_module("logs/main.log", "daemon process crashed") == "infrastructure"

    def test_unknown_fallback(self):
        from src.swe_team.monitor_agent import _guess_module
        assert _guess_module("logs/main.log", "something unrelated happened") == "unknown"

    def test_path_takes_priority_over_content(self):
        """When path matches a module, content should be ignored."""
        from src.swe_team.monitor_agent import _guess_module
        # Path says scraping, content says database
        assert _guess_module("/src/scraping/main.log", "supabase query failed") == "scraping"

    def test_enrich_maps_to_scraping(self):
        from src.swe_team.monitor_agent import _guess_module
        assert _guess_module("logs/main.log", "company_research enrichment failed") == "scraping"
        assert _guess_module("logs/main.log", "google_jobs search error") == "scraping"


# ======================================================================
# Remote Log Collection
# ======================================================================

class TestRemoteLogs:
    TEST_NODES = [
        {"name": "worker-1", "ssh": "agent@10.0.0.1", "log_dir": "~/project/logs"},
        {"name": "worker-2", "ssh": "agent@10.0.0.2", "log_dir": "~/project/logs"},
    ]

    def test_collect_remote_logs_success(self, tmp_path):
        from src.swe_team.remote_logs import collect_remote_logs

        local_dir = str(tmp_path / "remote")
        mock_result = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("src.swe_team.remote_logs.REMOTE_NODES", self.TEST_NODES):
            with patch("src.swe_team.remote_logs.subprocess.run", return_value=mock_result):
                result = collect_remote_logs(local_dir=local_dir)

        assert len(result) == 2
        assert "worker-1" in result[0]
        assert "worker-2" in result[1]

    def test_collect_remote_logs_rsync_failure(self, tmp_path):
        from src.swe_team.remote_logs import collect_remote_logs

        local_dir = str(tmp_path / "remote")
        mock_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "connection refused"})()

        with patch("src.swe_team.remote_logs.REMOTE_NODES", self.TEST_NODES):
            with patch("src.swe_team.remote_logs.subprocess.run", return_value=mock_result):
                result = collect_remote_logs(local_dir=local_dir)

        assert result == []

    def test_collect_remote_logs_timeout(self, tmp_path):
        from src.swe_team.remote_logs import collect_remote_logs

        local_dir = str(tmp_path / "remote")

        with patch("src.swe_team.remote_logs.REMOTE_NODES", self.TEST_NODES):
            with patch("src.swe_team.remote_logs.subprocess.run", side_effect=subprocess.TimeoutExpired("rsync", 30)):
                result = collect_remote_logs(local_dir=local_dir)

        assert result == []

    def test_collect_remote_logs_rsync_not_found_fallback(self, tmp_path):
        from src.swe_team.remote_logs import collect_remote_logs

        local_dir = str(tmp_path / "remote")

        call_count = {"n": 0}
        def side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] % 2 == 1:
                raise FileNotFoundError("rsync not found")
            return type("R", (), {"returncode": 0, "stdout": "log content here", "stderr": ""})()

        with patch("src.swe_team.remote_logs.REMOTE_NODES", self.TEST_NODES):
            with patch("src.swe_team.remote_logs.subprocess.run", side_effect=side_effect):
                result = collect_remote_logs(local_dir=local_dir)

        assert len(result) == 2

    def test_collect_creates_directories(self, tmp_path):
        from src.swe_team.remote_logs import collect_remote_logs

        local_dir = str(tmp_path / "deep" / "nested" / "remote")
        mock_result = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("src.swe_team.remote_logs.REMOTE_NODES", self.TEST_NODES):
            with patch("src.swe_team.remote_logs.subprocess.run", return_value=mock_result):
                collect_remote_logs(local_dir=local_dir)

        assert Path(local_dir, "worker-1").is_dir()
        assert Path(local_dir, "worker-2").is_dir()


# ======================================================================
# GitHub Issue Pickup
# ======================================================================

class TestFetchGithubTickets:
    def test_fetch_github_tickets_new_issue(self, tmp_path):
        import importlib
        import scripts.ops.swe_team_runner as runner

        store = TicketStore(str(tmp_path / "tickets.json"))

        gh_output = json.dumps([
            {"number": 42, "title": "Fix scraper timeout", "body": "The scraper times out", "labels": []},
        ])
        mock_result = type("R", (), {"returncode": 0, "stdout": gh_output, "stderr": ""})()

        with patch("scripts.ops.swe_team_runner.subprocess.run", return_value=mock_result):
            tickets = runner.fetch_github_tickets(store, github_account="test-bot")

        assert len(tickets) == 1
        assert tickets[0].title == "[GH-42] Fix scraper timeout"
        assert tickets[0].severity == TicketSeverity.HIGH
        assert tickets[0].metadata["github_issue"] == 42
        assert tickets[0].metadata["fingerprint"] == "gh-issue-42"

    def test_fetch_github_tickets_dedup(self, tmp_path):
        import scripts.ops.swe_team_runner as runner

        store = TicketStore(str(tmp_path / "tickets.json"))
        # Pre-populate a ticket with the same fingerprint
        existing = SWETicket(
            title="[GH-42] existing",
            description="already tracked",
            metadata={"fingerprint": "gh-issue-42"},
        )
        store.add(existing)

        gh_output = json.dumps([
            {"number": 42, "title": "Fix scraper timeout", "body": "The scraper times out", "labels": []},
        ])
        mock_result = type("R", (), {"returncode": 0, "stdout": gh_output, "stderr": ""})()

        with patch("scripts.ops.swe_team_runner.subprocess.run", return_value=mock_result):
            tickets = runner.fetch_github_tickets(store, github_account="test-bot")

        assert len(tickets) == 0

    def test_fetch_github_tickets_gh_cli_failure(self, tmp_path):
        import scripts.ops.swe_team_runner as runner

        store = TicketStore(str(tmp_path / "tickets.json"))
        mock_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "auth required"})()

        with patch("scripts.ops.swe_team_runner.subprocess.run", return_value=mock_result):
            tickets = runner.fetch_github_tickets(store, github_account="test-bot")

        assert tickets == []

    def test_fetch_github_tickets_exception(self, tmp_path):
        import scripts.ops.swe_team_runner as runner

        store = TicketStore(str(tmp_path / "tickets.json"))

        with patch("scripts.ops.swe_team_runner.subprocess.run", side_effect=Exception("network")):
            tickets = runner.fetch_github_tickets(store, github_account="test-bot")

        assert tickets == []

    def test_fetch_github_tickets_empty_body(self, tmp_path):
        import scripts.ops.swe_team_runner as runner

        store = TicketStore(str(tmp_path / "tickets.json"))
        gh_output = json.dumps([
            {"number": 99, "title": "No body issue", "body": None, "labels": []},
        ])
        mock_result = type("R", (), {"returncode": 0, "stdout": gh_output, "stderr": ""})()

        with patch("scripts.ops.swe_team_runner.subprocess.run", return_value=mock_result):
            tickets = runner.fetch_github_tickets(store, github_account="test-bot")

        assert len(tickets) == 1
        assert tickets[0].description == ""
