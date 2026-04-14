"""
Unit tests for src/swe_team/ralph_wiggum.py — RalphWiggumGate stability gate.
"""

from __future__ import annotations

import pytest

from src.swe_team.config import GovernanceConfig
from src.swe_team.events import SWEEvent, SWEEventType
from src.swe_team.models import (
    GovernanceVerdict,
    SWETicket,
    StabilityReport,
    TicketSeverity,
    TicketStatus,
)
from src.swe_team.ralph_wiggum import RalphWiggumGate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    *,
    enabled: bool = True,
    max_open_critical: int = 0,
    max_open_high: int = 3,
    require_ci_green: bool = True,
) -> GovernanceConfig:
    return GovernanceConfig(
        enabled=enabled,
        max_open_critical=max_open_critical,
        max_open_high=max_open_high,
        require_ci_green=require_ci_green,
    )


def _ticket(severity: TicketSeverity, status: TicketStatus = TicketStatus.OPEN) -> SWETicket:
    t = SWETicket(title="Test", description="desc", severity=severity)
    t.status = status
    return t


# ---------------------------------------------------------------------------
# Gate disabled
# ---------------------------------------------------------------------------

class TestGateDisabled:
    def test_disabled_gate_returns_pass(self):
        gate = RalphWiggumGate(_make_config(enabled=False))
        report = gate.evaluate(
            [_ticket(TicketSeverity.CRITICAL)],
            ci_green=False,
            failing_tests=100,
        )
        assert report.verdict == GovernanceVerdict.PASS
        assert "disabled" in report.details.lower()


# ---------------------------------------------------------------------------
# Empty / all-resolved ticket lists
# ---------------------------------------------------------------------------

class TestEmptyTickets:
    def test_empty_ticket_list_passes(self):
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([])
        assert report.verdict == GovernanceVerdict.PASS

    def test_all_resolved_tickets_pass(self):
        gate = RalphWiggumGate(_make_config())
        tickets = [
            _ticket(TicketSeverity.CRITICAL, TicketStatus.RESOLVED),
            _ticket(TicketSeverity.HIGH, TicketStatus.CLOSED),
        ]
        report = gate.evaluate(tickets)
        assert report.verdict == GovernanceVerdict.PASS

    def test_closed_tickets_not_counted(self):
        gate = RalphWiggumGate(_make_config(max_open_critical=0))
        tickets = [_ticket(TicketSeverity.CRITICAL, TicketStatus.CLOSED)]
        report = gate.evaluate(tickets)
        assert report.verdict == GovernanceVerdict.PASS
        assert report.open_critical == 0


# ---------------------------------------------------------------------------
# Critical ticket threshold
# ---------------------------------------------------------------------------

class TestCriticalThreshold:
    def test_one_critical_blocks_when_threshold_zero(self):
        gate = RalphWiggumGate(_make_config(max_open_critical=0))
        report = gate.evaluate([_ticket(TicketSeverity.CRITICAL)])
        assert report.verdict == GovernanceVerdict.BLOCK
        assert report.open_critical == 1

    def test_critical_within_threshold_passes(self):
        gate = RalphWiggumGate(_make_config(max_open_critical=2))
        tickets = [
            _ticket(TicketSeverity.CRITICAL),
            _ticket(TicketSeverity.CRITICAL),
        ]
        report = gate.evaluate(tickets)
        assert report.verdict == GovernanceVerdict.PASS

    def test_critical_exceeds_threshold_blocks(self):
        gate = RalphWiggumGate(_make_config(max_open_critical=1))
        tickets = [
            _ticket(TicketSeverity.CRITICAL),
            _ticket(TicketSeverity.CRITICAL),
        ]
        report = gate.evaluate(tickets)
        assert report.verdict == GovernanceVerdict.BLOCK

    def test_critical_in_progress_counts_as_open(self):
        """INVESTIGATING is still an open status."""
        gate = RalphWiggumGate(_make_config(max_open_critical=0))
        t = _ticket(TicketSeverity.CRITICAL, TicketStatus.INVESTIGATING)
        report = gate.evaluate([t])
        assert report.verdict == GovernanceVerdict.BLOCK
        assert report.open_critical == 1


# ---------------------------------------------------------------------------
# High ticket threshold
# ---------------------------------------------------------------------------

class TestHighThreshold:
    def test_high_within_threshold_passes(self):
        gate = RalphWiggumGate(_make_config(max_open_high=3))
        tickets = [_ticket(TicketSeverity.HIGH) for _ in range(3)]
        report = gate.evaluate(tickets)
        assert report.verdict == GovernanceVerdict.PASS
        assert report.open_high == 3

    def test_high_exceeds_threshold_blocks(self):
        gate = RalphWiggumGate(_make_config(max_open_high=3))
        tickets = [_ticket(TicketSeverity.HIGH) for _ in range(4)]
        report = gate.evaluate(tickets)
        assert report.verdict == GovernanceVerdict.BLOCK
        assert report.open_high == 4

    def test_medium_low_not_counted_in_high(self):
        gate = RalphWiggumGate(_make_config(max_open_high=1))
        tickets = [
            _ticket(TicketSeverity.MEDIUM),
            _ticket(TicketSeverity.LOW),
        ]
        report = gate.evaluate(tickets)
        assert report.verdict == GovernanceVerdict.PASS
        assert report.open_high == 0


# ---------------------------------------------------------------------------
# CI green requirement
# ---------------------------------------------------------------------------

class TestCIGreen:
    def test_ci_red_blocks_when_required(self):
        gate = RalphWiggumGate(_make_config(require_ci_green=True))
        report = gate.evaluate([], ci_green=False)
        assert report.verdict == GovernanceVerdict.BLOCK
        assert report.ci_status == "red"
        assert "CI" in report.details

    def test_ci_red_ignored_when_not_required(self):
        gate = RalphWiggumGate(_make_config(require_ci_green=False))
        report = gate.evaluate([], ci_green=False)
        assert report.verdict == GovernanceVerdict.PASS

    def test_ci_green_records_in_report(self):
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([], ci_green=True)
        assert report.ci_status == "green"


# ---------------------------------------------------------------------------
# Failing tests thresholds
# ---------------------------------------------------------------------------

class TestFailingTests:
    def test_high_fail_pct_blocks(self):
        """10%+ failing tests → BLOCK."""
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([], failing_tests=10, total_tests=100)
        assert report.verdict == GovernanceVerdict.BLOCK
        assert report.failing_tests == 10

    def test_moderate_fail_pct_warns(self):
        """5-9% failing tests → WARN (not BLOCK)."""
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([], failing_tests=5, total_tests=100)
        assert report.verdict == GovernanceVerdict.WARN

    def test_small_fail_pct_passes(self):
        """<5% isolated failures → PASS (just a log warning)."""
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([], failing_tests=1, total_tests=100)
        assert report.verdict == GovernanceVerdict.PASS

    def test_zero_failing_tests_passes(self):
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([], failing_tests=0, total_tests=500)
        assert report.verdict == GovernanceVerdict.PASS

    def test_failing_tests_no_total_blocks(self):
        """No total_tests provided → 100% failure rate → BLOCK."""
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([], failing_tests=1, total_tests=0)
        assert report.verdict == GovernanceVerdict.BLOCK


# ---------------------------------------------------------------------------
# Mixed scenarios
# ---------------------------------------------------------------------------

class TestMixedScenarios:
    def test_multiple_block_reasons_combined(self):
        gate = RalphWiggumGate(_make_config(max_open_critical=0, require_ci_green=True))
        tickets = [_ticket(TicketSeverity.CRITICAL)]
        report = gate.evaluate(tickets, ci_green=False)
        assert report.verdict == GovernanceVerdict.BLOCK
        # Both reasons should appear in details
        assert "critical" in report.details.lower()
        assert "CI" in report.details

    def test_pass_with_mix_of_resolved_and_open_low(self):
        gate = RalphWiggumGate(_make_config())
        tickets = [
            _ticket(TicketSeverity.LOW, TicketStatus.OPEN),
            _ticket(TicketSeverity.CRITICAL, TicketStatus.RESOLVED),
        ]
        report = gate.evaluate(tickets)
        assert report.verdict == GovernanceVerdict.PASS

    def test_deploying_status_counts_as_open(self):
        gate = RalphWiggumGate(_make_config(max_open_critical=0))
        t = _ticket(TicketSeverity.CRITICAL, TicketStatus.DEPLOYING)
        report = gate.evaluate([t])
        assert report.verdict == GovernanceVerdict.BLOCK


# ---------------------------------------------------------------------------
# StabilityReport structure
# ---------------------------------------------------------------------------

class TestStabilityReport:
    def test_report_fields_populated(self):
        gate = RalphWiggumGate(_make_config())
        tickets = [
            _ticket(TicketSeverity.CRITICAL),
            _ticket(TicketSeverity.HIGH),
            _ticket(TicketSeverity.HIGH),
        ]
        report = gate.evaluate(tickets, ci_green=True, failing_tests=0)
        assert report.open_critical == 1
        assert report.open_high == 2
        assert report.failing_tests == 0

    def test_pass_report_details_message(self):
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([])
        assert report.verdict == GovernanceVerdict.PASS
        assert "passed" in report.details.lower()


# ---------------------------------------------------------------------------
# build_event()
# ---------------------------------------------------------------------------

class TestBuildEvent:
    def test_build_event_returns_swe_event(self):
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([])
        event = gate.build_event(report)
        assert isinstance(event, SWEEvent)

    def test_build_event_type_is_stability_gate_result(self):
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([])
        event = gate.build_event(report)
        assert event.event == SWEEventType.STABILITY_GATE_RESULT

    def test_build_event_source_agent(self):
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([])
        event = gate.build_event(report)
        assert event.source_agent == "ralph_wiggum"

    def test_build_event_payload_contains_verdict(self):
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([])
        event = gate.build_event(report, ticket_id="abc123")
        assert event.ticket_id == "abc123"
        assert "verdict" in event.payload
        assert event.payload["verdict"] == GovernanceVerdict.PASS.value

    def test_build_event_block_verdict_in_payload(self):
        gate = RalphWiggumGate(_make_config(max_open_critical=0))
        tickets = [_ticket(TicketSeverity.CRITICAL)]
        report = gate.evaluate(tickets)
        event = gate.build_event(report)
        assert event.payload["verdict"] == GovernanceVerdict.BLOCK.value

    def test_build_event_has_event_id_and_timestamp(self):
        gate = RalphWiggumGate(_make_config())
        report = gate.evaluate([])
        event = gate.build_event(report)
        assert event.event_id
        assert event.timestamp
