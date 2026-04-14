"""Unit tests for the developer failure feedback loop (re-investigation).

Covers:
- _build_failure_context() helper formatting
- _try_reinvestigation() logic: triggers when eligible, skips when exhausted
- Ticket state transitions through the re-investigation flow
- InvestigatorAgent.investigate() with prompt_override parameter
- CycleConfig.max_reinvestigations field
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(
    severity=TicketSeverity.HIGH,
    status=TicketStatus.INVESTIGATION_COMPLETE,
    investigation_report="Root cause: unclosed sessions. Fix: add close() call. " * 10,
    metadata=None,
    **kwargs,
):
    defaults = dict(
        ticket_id="T-FEEDBACK-001",
        title="Memory leak in scraper",
        description="Scraper leaks memory over time",
        severity=severity,
        status=status,
        investigation_report=investigation_report,
        metadata=metadata or {},
    )
    defaults.update(kwargs)
    return SWETicket(**defaults)


# ---------------------------------------------------------------------------
# _build_failure_context tests
# ---------------------------------------------------------------------------

class TestBuildFailureContext:
    """Test the _build_failure_context helper function."""

    def test_formats_multiple_attempts(self):
        """Multiple attempt records are formatted correctly."""
        # Import at test time to avoid module-level side effects
        import sys
        import os
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from scripts.ops.swe_team_runner import _build_failure_context

        ticket = _make_ticket(metadata={
            "attempts": [
                {"error": "Test failed: assert 1 == 2", "result": "fail", "model": "sonnet"},
                {"error": "Syntax error on line 42", "result": "fail", "model": "sonnet"},
            ]
        })
        result = _build_failure_context(ticket)
        assert "Attempt 1" in result
        assert "Attempt 2" in result
        assert "sonnet" in result
        assert "assert 1 == 2" in result
        assert "Syntax error" in result

    def test_single_attempt(self):
        """Single attempt is formatted correctly."""
        from scripts.ops.swe_team_runner import _build_failure_context

        ticket = _make_ticket(metadata={
            "attempts": [
                {"error": "ImportError: no module named foo", "result": "fail", "model": "haiku"},
            ]
        })
        result = _build_failure_context(ticket)
        assert "Attempt 1" in result
        assert "haiku" in result
        assert "ImportError" in result
        assert "Attempt 2" not in result

    def test_no_attempts(self):
        """Returns fallback message when no attempts recorded."""
        from scripts.ops.swe_team_runner import _build_failure_context

        ticket = _make_ticket(metadata={})
        result = _build_failure_context(ticket)
        assert "No attempt details available" in result

    def test_truncates_long_errors(self):
        """Error messages longer than 300 chars are truncated."""
        from scripts.ops.swe_team_runner import _build_failure_context

        long_error = "X" * 500
        ticket = _make_ticket(metadata={
            "attempts": [
                {"error": long_error, "result": "fail", "model": "sonnet"},
            ]
        })
        result = _build_failure_context(ticket)
        # The error should be truncated to 300 chars
        assert len(long_error) > 300
        assert "X" * 300 in result
        assert "X" * 301 not in result

    def test_missing_fields_in_attempt(self):
        """Attempts with missing fields use default values."""
        from scripts.ops.swe_team_runner import _build_failure_context

        ticket = _make_ticket(metadata={
            "attempts": [{}]  # No error, result, or model
        })
        result = _build_failure_context(ticket)
        assert "unknown" in result
        assert "model=?" in result


# ---------------------------------------------------------------------------
# _try_reinvestigation tests
# ---------------------------------------------------------------------------

class TestTryReinvestigation:
    """Test the _try_reinvestigation function."""

    def test_triggers_reinvestigation_on_first_failure(self):
        """Re-investigation is triggered when reinvestigation_count < max."""
        from scripts.ops.swe_team_runner import _try_reinvestigation

        ticket = _make_ticket(
            status=TicketStatus.IN_DEVELOPMENT,
            metadata={
                "attempts": [
                    {"error": "Test failed", "result": "fail", "model": "sonnet"},
                ],
            },
        )

        investigator = MagicMock()
        investigator.investigate.return_value = True

        dev = MagicMock()
        dev.attempt_fix.return_value = True

        store = MagicMock()

        result = _try_reinvestigation(ticket, investigator, dev, store, max_reinvestigations=1)

        assert result is True
        assert ticket.metadata["reinvestigation_count"] == 1
        investigator.investigate.assert_called_once()
        # Check that prompt_override was passed
        _, kwargs = investigator.investigate.call_args
        assert "prompt_override" in kwargs
        assert "Re-investigation Required" in kwargs["prompt_override"]
        assert "Developer Failure Context" in kwargs["prompt_override"]
        dev.attempt_fix.assert_called_once()

    def test_skips_when_max_reached(self):
        """Re-investigation is NOT triggered when reinvestigation_count >= max."""
        from scripts.ops.swe_team_runner import _try_reinvestigation

        ticket = _make_ticket(
            status=TicketStatus.IN_DEVELOPMENT,
            metadata={
                "reinvestigation_count": 1,
                "attempts": [
                    {"error": "Test failed", "result": "fail", "model": "sonnet"},
                ],
            },
        )

        investigator = MagicMock()
        dev = MagicMock()
        store = MagicMock()

        result = _try_reinvestigation(ticket, investigator, dev, store, max_reinvestigations=1)

        assert result is False
        investigator.investigate.assert_not_called()
        dev.attempt_fix.assert_not_called()

    def test_skips_when_no_attempts(self):
        """Re-investigation is NOT triggered when there are no attempt records."""
        from scripts.ops.swe_team_runner import _try_reinvestigation

        ticket = _make_ticket(
            status=TicketStatus.IN_DEVELOPMENT,
            metadata={},
        )

        investigator = MagicMock()
        dev = MagicMock()
        store = MagicMock()

        result = _try_reinvestigation(ticket, investigator, dev, store, max_reinvestigations=1)

        assert result is False
        investigator.investigate.assert_not_called()

    def test_returns_false_when_fix_fails_again(self):
        """Returns False when the re-attempt also fails."""
        from scripts.ops.swe_team_runner import _try_reinvestigation

        ticket = _make_ticket(
            status=TicketStatus.IN_DEVELOPMENT,
            metadata={
                "attempts": [
                    {"error": "Test failed", "result": "fail", "model": "sonnet"},
                ],
            },
        )

        investigator = MagicMock()
        investigator.investigate.return_value = True

        dev = MagicMock()
        dev.attempt_fix.return_value = False

        store = MagicMock()

        result = _try_reinvestigation(ticket, investigator, dev, store, max_reinvestigations=1)

        assert result is False
        assert ticket.metadata["reinvestigation_count"] == 1

    def test_transitions_to_investigating_then_back(self):
        """Ticket transitions: IN_DEVELOPMENT -> INVESTIGATING during re-investigation."""
        from scripts.ops.swe_team_runner import _try_reinvestigation

        ticket = _make_ticket(
            status=TicketStatus.IN_DEVELOPMENT,
            metadata={
                "attempts": [
                    {"error": "Test failed", "result": "fail", "model": "sonnet"},
                ],
            },
        )

        statuses_seen = []

        def track_investigate(t, *, prompt_override=None):
            statuses_seen.append(t.status)
            t.investigation_report = "Updated: fix the other thing"
            t.transition(TicketStatus.INVESTIGATION_COMPLETE)
            return True

        investigator = MagicMock()
        investigator.investigate.side_effect = track_investigate

        dev = MagicMock()
        dev.attempt_fix.return_value = True

        store = MagicMock()

        result = _try_reinvestigation(ticket, investigator, dev, store, max_reinvestigations=1)

        assert result is True
        assert TicketStatus.INVESTIGATING in statuses_seen

    def test_handles_investigation_exception(self):
        """Re-investigation exception is caught and returns False."""
        from scripts.ops.swe_team_runner import _try_reinvestigation

        ticket = _make_ticket(
            status=TicketStatus.IN_DEVELOPMENT,
            metadata={
                "attempts": [
                    {"error": "Test failed", "result": "fail", "model": "sonnet"},
                ],
            },
        )

        investigator = MagicMock()
        investigator.investigate.side_effect = RuntimeError("CLI crash")

        dev = MagicMock()
        store = MagicMock()

        result = _try_reinvestigation(ticket, investigator, dev, store, max_reinvestigations=1)

        assert result is False

    def test_enriched_prompt_contains_original_report(self):
        """The enriched prompt includes the original investigation report."""
        from scripts.ops.swe_team_runner import _try_reinvestigation

        original_report = "Root cause: memory leak in connection pool"
        ticket = _make_ticket(
            status=TicketStatus.IN_DEVELOPMENT,
            investigation_report=original_report,
            metadata={
                "attempts": [
                    {"error": "patch did not apply", "result": "fail", "model": "sonnet"},
                ],
            },
        )

        investigator = MagicMock()
        investigator.investigate.return_value = True
        dev = MagicMock()
        dev.attempt_fix.return_value = True
        store = MagicMock()

        _try_reinvestigation(ticket, investigator, dev, store, max_reinvestigations=1)

        _, kwargs = investigator.investigate.call_args
        prompt = kwargs["prompt_override"]
        assert "memory leak in connection pool" in prompt
        assert "patch did not apply" in prompt

    def test_multiple_reinvestigations_allowed(self):
        """When max_reinvestigations=2, two re-investigation attempts are possible."""
        from scripts.ops.swe_team_runner import _try_reinvestigation

        ticket = _make_ticket(
            status=TicketStatus.IN_DEVELOPMENT,
            metadata={
                "reinvestigation_count": 0,
                "attempts": [
                    {"error": "fail 1", "result": "fail", "model": "sonnet"},
                ],
            },
        )

        investigator = MagicMock()
        investigator.investigate.return_value = True
        dev = MagicMock()
        dev.attempt_fix.return_value = False
        store = MagicMock()

        # First attempt
        result = _try_reinvestigation(ticket, investigator, dev, store, max_reinvestigations=2)
        assert result is False
        assert ticket.metadata["reinvestigation_count"] == 1

        # Second attempt — still under max
        result = _try_reinvestigation(ticket, investigator, dev, store, max_reinvestigations=2)
        assert result is False
        assert ticket.metadata["reinvestigation_count"] == 2

        # Third attempt — now at max, should skip
        result = _try_reinvestigation(ticket, investigator, dev, store, max_reinvestigations=2)
        assert result is False
        # Count should not increment
        assert ticket.metadata["reinvestigation_count"] == 2


# ---------------------------------------------------------------------------
# InvestigatorAgent._eligible with reinvestigation flag
# ---------------------------------------------------------------------------

class TestInvestigatorEligibleReinvestigation:
    """Test that _eligible correctly handles the reinvestigation flag."""

    def _make_investigator(self):
        """Create an InvestigatorAgent with minimal mocked dependencies."""
        from src.swe_team.investigator import InvestigatorAgent
        return InvestigatorAgent(
            program_path="/tmp/fake.md",
            claude_path="/usr/bin/fake-claude",
        )

    def test_normal_eligible_rejects_ticket_with_report(self):
        """Normal eligibility check rejects tickets that have a report."""
        inv = self._make_investigator()
        ticket = _make_ticket(
            status=TicketStatus.INVESTIGATING,
            investigation_report="Some report here",
        )
        assert inv._eligible(ticket) is False

    def test_reinvestigation_allows_ticket_with_report(self):
        """Reinvestigation flag allows tickets that already have a report."""
        inv = self._make_investigator()
        ticket = _make_ticket(
            status=TicketStatus.INVESTIGATING,
            investigation_report="Some report here",
        )
        assert inv._eligible(ticket, reinvestigation=True) is True

    def test_reinvestigation_rejects_wrong_status(self):
        """Reinvestigation flag still rejects tickets not in INVESTIGATING."""
        inv = self._make_investigator()
        ticket = _make_ticket(
            status=TicketStatus.IN_DEVELOPMENT,
            investigation_report="Some report",
        )
        assert inv._eligible(ticket, reinvestigation=True) is False

    def test_reinvestigation_rejects_low_severity(self):
        """Reinvestigation flag still rejects LOW severity tickets."""
        inv = self._make_investigator()
        ticket = _make_ticket(
            severity=TicketSeverity.LOW,
            status=TicketStatus.INVESTIGATING,
        )
        assert inv._eligible(ticket, reinvestigation=True) is False


# ---------------------------------------------------------------------------
# CycleConfig.max_reinvestigations
# ---------------------------------------------------------------------------

class TestCycleConfigMaxReinvestigations:
    """Test that CycleConfig includes the max_reinvestigations field."""

    def test_default_value(self):
        from src.swe_team.config import CycleConfig
        cc = CycleConfig()
        assert cc.max_reinvestigations == 1

    def test_from_dict(self):
        from src.swe_team.config import CycleConfig
        cc = CycleConfig.from_dict({"max_reinvestigations": 3})
        assert cc.max_reinvestigations == 3

    def test_from_dict_default(self):
        from src.swe_team.config import CycleConfig
        cc = CycleConfig.from_dict({})
        assert cc.max_reinvestigations == 1

    def test_to_dict(self):
        from src.swe_team.config import CycleConfig
        cc = CycleConfig(max_reinvestigations=2)
        d = cc.to_dict()
        assert d["max_reinvestigations"] == 2


# ---------------------------------------------------------------------------
# InvestigatorAgent.investigate with prompt_override
# ---------------------------------------------------------------------------

class TestInvestigatePromptOverride:
    """Test that investigate() correctly uses prompt_override."""

    def test_prompt_override_bypasses_template(self):
        """When prompt_override is provided, the template is not used."""
        from src.swe_team.investigator import InvestigatorAgent

        inv = InvestigatorAgent(
            program_path="/tmp/nonexistent.md",  # Would fail if used
            claude_path="/usr/bin/fake-claude",
        )

        ticket = _make_ticket(
            status=TicketStatus.INVESTIGATING,
            investigation_report="old report",
        )

        custom_prompt = "Re-investigate with this context: failure details here"

        _valid_report = (
            "Root Cause: A retry loop silently swallowed a timeout from the upstream proxy, "
            "so the worker emitted incomplete diagnostics and the orchestrator persisted an invalid response.\n\n"
            "Affected Files: src/swe_team/investigator.py, src/swe_team/developer.py, tests/unit/test_investigator.py\n\n"
            "Fix Plan: Validate report structure before persistence, reject known Claude error envelopes, "
            "require minimum report length, and keep sectioned investigation output for downstream automation."
        )

        # Mock _run_claude to return a report
        with patch.object(inv, "_run_claude", return_value=(_valid_report, "")) as mock_run:
            with patch.object(inv, "_backoff") as mock_backoff:
                mock_backoff.execute.return_value = (_valid_report, "")
                result = inv.investigate(ticket, prompt_override=custom_prompt)

        assert result is True
        assert ticket.investigation_report == _valid_report
        assert ticket.status == TicketStatus.INVESTIGATION_COMPLETE
