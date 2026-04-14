"""Unit tests for InvestigatorAgent.

Covers: investigate(), investigate_batch(), _build_prompt(), _select_model(),
_eligible(), _record_failure(), _record_timeout(), semantic memory injection,
notification, rate limiting, and fallback agents.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus, TicketType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_investigator(**kwargs):
    """Create an InvestigatorAgent with safe defaults for unit testing.

    Always injects a mock notifier to avoid lazy imports to telegram/notifier.
    """
    with patch("src.swe_team.investigator.CtagsRepoMapProvider"):
        with patch("src.swe_team.investigator.DotenvEnvProvider") as mock_env:
            import os
            mock_env.return_value.build_env.return_value = os.environ.copy()
            from src.swe_team.investigator import InvestigatorAgent
            ep = MagicMock()
            ep.build_env.return_value = os.environ.copy()
            defaults = dict(
                program_path=Path("/tmp/fake_investigate.md"),
                claude_path="/usr/bin/fake-claude",
                timeout_seconds=60,
                env_provider=ep,
                notifier=kwargs.pop("notifier", MagicMock()),
            )
            defaults.update(kwargs)
            return InvestigatorAgent(**defaults)


def _make_ticket(
    severity=TicketSeverity.HIGH,
    status=TicketStatus.TRIAGED,
    **kwargs,
):
    defaults = dict(
        ticket_id="T-INV-TEST",
        title="Test investigation ticket",
        description="Something broke",
        severity=severity,
        status=status,
        error_log="Traceback:\n  File foo.py\nRuntimeError: boom",
    )
    defaults.update(kwargs)
    return SWETicket(**defaults)


# Convenience decorator stack used by most investigate() tests
_PATCH_WORKER = patch("src.swe_team.investigator.fetch_worker_logs", return_value=None)
_PATCH_EMBED = patch("src.swe_team.investigator.embed_ticket", return_value=None)
_VALID_REPORT = (
    "Root Cause: A retry loop silently swallowed a timeout from the upstream proxy, "
    "so the worker emitted incomplete diagnostics and the orchestrator persisted an invalid response.\n\n"
    "Affected Files: src/swe_team/investigator.py, src/swe_team/developer.py, tests/unit/test_investigator.py\n\n"
    "Fix Plan: Validate report structure before persistence, reject known Claude error envelopes, "
    "require minimum report length, and keep sectioned investigation output for downstream automation."
)


# ---------------------------------------------------------------------------
# 1. investigate() with subprocess success
# ---------------------------------------------------------------------------

class TestInvestigateSuccess:
    """investigate() with mocked subprocess returning success."""

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_sets_investigation_complete(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_claude", return_value=(_VALID_REPORT, "")):
            result = agent.investigate(ticket)

        assert result is True
        assert ticket.status == TicketStatus.INVESTIGATION_COMPLETE
        assert ticket.investigation_report == _VALID_REPORT

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_records_metadata(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_claude", return_value=(_VALID_REPORT, "")):
            agent.investigate(ticket)

        inv = ticket.metadata["investigation"]
        assert inv["status"] == "complete"
        assert "started_at" in inv
        assert "completed_at" in inv
        assert "duration_s" in inv


# ---------------------------------------------------------------------------
# 2. investigate() with subprocess failure
# ---------------------------------------------------------------------------

class TestInvestigateFailure:
    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_runtime_error_records_failure(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_claude", side_effect=RuntimeError("CLI crashed")):
            result = agent.investigate(ticket)

        assert result is False
        assert ticket.status == TicketStatus.TRIAGED
        assert ticket.metadata["investigation"]["status"] == "failed"
        assert "CLI crashed" in ticket.metadata["investigation"]["error"]

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_empty_report_treated_as_failure(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_claude", return_value=("", "")):
            result = agent.investigate(ticket)

        assert result is False
        assert ticket.metadata["investigation"]["status"] == "failed"
        assert "Empty" in ticket.metadata["investigation"]["error"]


# ---------------------------------------------------------------------------
# 3. investigate() with no prompt template
# ---------------------------------------------------------------------------

class TestInvestigateNoTemplate:
    def test_missing_template_returns_false(self):
        agent = _make_investigator()
        ticket = _make_ticket()

        result = agent.investigate(ticket)

        assert result is False
        assert ticket.metadata["investigation"]["status"] == "failed"
        assert "Prompt template missing" in ticket.metadata["investigation"]["error"]


# ---------------------------------------------------------------------------
# 4. investigate_batch() with multiple tickets
# ---------------------------------------------------------------------------

class TestInvestigateBatch:
    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_batch_investigates_all_eligible(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        store = MagicMock()
        agent._store = store

        tickets = [
            _make_ticket(ticket_id=f"T-BATCH-{i}", status=TicketStatus.TRIAGED)
            for i in range(3)
        ]

        with patch.object(agent, "_run_claude", return_value=(_VALID_REPORT, "")):
            updated = agent.investigate_batch(tickets)

        assert len(updated) == 3
        for t in updated:
            assert t.status == TicketStatus.INVESTIGATION_COMPLETE

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_batch_persists_mid_batch(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        store = MagicMock()
        agent._store = store

        tickets = [_make_ticket(ticket_id="T-MID-1", status=TicketStatus.TRIAGED)]

        with patch.object(agent, "_run_claude", return_value=(_VALID_REPORT, "")):
            agent.investigate_batch(tickets)

        store.add.assert_called()

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_batch_calls_on_complete(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        callback = MagicMock()

        tickets = [_make_ticket(ticket_id="T-CB-1", status=TicketStatus.TRIAGED)]

        with patch.object(agent, "_run_claude", return_value=(_VALID_REPORT, "")):
            agent.investigate_batch(tickets, on_complete=callback)

        callback.assert_called_once()

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_batch_respects_limit(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"

        tickets = [
            _make_ticket(ticket_id=f"T-LIM-{i}", status=TicketStatus.TRIAGED)
            for i in range(10)
        ]

        with patch.object(agent, "_run_claude", return_value=(_VALID_REPORT, "")):
            updated = agent.investigate_batch(tickets, limit=2)

        assert len(updated) == 2


# ---------------------------------------------------------------------------
# 5. _build_prompt() includes ticket fields
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_prompt_includes_error_log_and_module(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Error: {error_log}\nModule: {source_module}"

        ticket = _make_ticket(
            error_log="NullPointerException at line 42",
            source_module="scraping",
        )

        prompt = agent._build_prompt(ticket)
        assert "NullPointerException at line 42" in prompt
        assert "scraping" in prompt

    def test_prompt_handles_no_error_log(self):
        agent = _make_investigator()
        agent._program_cache = "Error: {error_log}\nModule: {source_module}"
        ticket = _make_ticket(error_log=None, source_module=None)

        with patch("src.swe_team.investigator.fetch_worker_logs", return_value=None):
            with patch("src.swe_team.investigator.embed_ticket", return_value=None):
                prompt = agent._build_prompt(ticket)

        assert "No error log provided" in prompt
        assert "unknown" in prompt

    def test_regression_context_appended(self):
        agent = _make_investigator()
        agent._program_cache = "Error: {error_log}\nModule: {source_module}"
        ticket = _make_ticket()
        ticket.metadata["is_regression"] = True
        ticket.metadata["regression_of"] = "T-PARENT"

        with patch("src.swe_team.investigator.fetch_worker_logs", return_value=None):
            with patch("src.swe_team.investigator.embed_ticket", return_value=None):
                prompt = agent._build_prompt(ticket)

        assert "REGRESSION ALERT" in prompt
        assert "T-PARENT" in prompt


# ---------------------------------------------------------------------------
# 6. _parse_cost helper
# ---------------------------------------------------------------------------

class TestParseCost:
    def test_extracts_dollar_amount(self):
        from src.swe_team.investigator import _parse_cost
        assert _parse_cost("Total cost: $1.23") == 1.23

    def test_returns_none_for_no_cost(self):
        from src.swe_team.investigator import _parse_cost
        assert _parse_cost("No cost info here") is None

    def test_handles_commas(self):
        from src.swe_team.investigator import _parse_cost
        assert _parse_cost("Session cost: $1,234.56") == 1234.56


# ---------------------------------------------------------------------------
# 7. Semantic memory injection
# ---------------------------------------------------------------------------

class TestSemanticMemory:
    def test_memory_context_in_prompt_via_mock(self):
        agent = _make_investigator()
        agent._program_cache = "Error: {error_log}\nModule: {source_module}"

        with patch.object(agent, "_semantic_memory_context", return_value="## Semantic Memory\nSimilar ticket: T-OLD-1"):
            with patch("src.swe_team.investigator.fetch_worker_logs", return_value=None):
                prompt = agent._build_prompt(
                    _make_ticket(error_log="some error", source_module="auth")
                )

        assert "Semantic Memory" in prompt

    def test_no_memory_when_store_is_none(self):
        agent = _make_investigator(store=None)
        result = agent._semantic_memory_context(_make_ticket())
        assert result == ""


# ---------------------------------------------------------------------------
# 8. Model tier selection
# ---------------------------------------------------------------------------

class TestModelSelection:
    def _agent(self):
        mc = MagicMock()
        mc.t1_heavy = "opus"
        mc.t2_standard = "sonnet"
        return _make_investigator(model_config=mc)

    def test_critical_first_attempt_uses_standard(self):
        agent = self._agent()
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        assert agent._select_model(ticket) == "sonnet"

    def test_critical_after_failure_uses_heavy(self):
        agent = self._agent()
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        ticket.metadata["investigation"] = {"status": "failed"}
        assert agent._select_model(ticket) == "opus"

    def test_high_uses_standard(self):
        agent = self._agent()
        ticket = _make_ticket(severity=TicketSeverity.HIGH)
        assert agent._select_model(ticket) == "sonnet"

    def test_high_after_failure_escalates(self):
        agent = self._agent()
        ticket = _make_ticket(severity=TicketSeverity.HIGH)
        ticket.metadata["investigation"] = {"status": "failed"}
        assert agent._select_model(ticket) == "opus"

    def test_regression_always_uses_heavy(self):
        agent = self._agent()
        ticket = _make_ticket(severity=TicketSeverity.HIGH)
        ticket.metadata["is_regression"] = True
        assert agent._select_model(ticket) == "opus"

    def test_medium_uses_standard(self):
        agent = self._agent()
        ticket = _make_ticket(severity=TicketSeverity.MEDIUM)
        assert agent._select_model(ticket) == "sonnet"

    def test_timeout_fallback_overrides_escalation(self):
        agent = self._agent()
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)
        ticket.metadata["investigation"] = {"status": "failed"}
        ticket.metadata["investigation_timeout_count"] = 2
        assert agent._select_model(ticket) == "sonnet"


# ---------------------------------------------------------------------------
# 9. on_complete callback
# ---------------------------------------------------------------------------

class TestOnCompleteCallback:
    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_callback_receives_ticket(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        callback = MagicMock()
        ticket = _make_ticket(ticket_id="T-CALLBACK")

        with patch.object(agent, "_run_claude", return_value=(_VALID_REPORT, "")):
            agent.investigate_batch([ticket], on_complete=callback)

        callback.assert_called_once()
        assert callback.call_args[0][0].ticket_id == "T-CALLBACK"


# ---------------------------------------------------------------------------
# 10. Timeout handling
# ---------------------------------------------------------------------------

class TestTimeoutHandling:
    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_timeout_records_count(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_claude", side_effect=subprocess.TimeoutExpired("claude", 60)):
            result = agent.investigate(ticket)

        assert result is False
        assert ticket.status == TicketStatus.TRIAGED
        assert ticket.metadata["investigation_timeout_count"] == 1
        assert ticket.metadata["investigation"]["status"] == "timeout"

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_max_timeouts_writes_stub_report(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket()
        ticket.metadata["investigation_timeout_count"] = 2

        with patch.object(agent, "_run_claude", side_effect=subprocess.TimeoutExpired("claude", 60)):
            agent.investigate(ticket)

        assert ticket.investigation_report is not None
        assert "timed out" in ticket.investigation_report

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_single_timeout_leaves_report_empty(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket()

        with patch.object(agent, "_run_claude", side_effect=subprocess.TimeoutExpired("claude", 60)):
            agent.investigate(ticket)

        assert ticket.investigation_report is None  # Not yet at max


# ---------------------------------------------------------------------------
# 11. NotificationProvider mock
# ---------------------------------------------------------------------------

class TestNotification:
    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_critical_ticket_sends_notification(self, _e, _f):
        notifier = MagicMock()
        agent = _make_investigator(notifier=notifier)
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket(severity=TicketSeverity.CRITICAL)

        with patch.object(agent, "_run_claude", return_value=(_VALID_REPORT, "")):
            agent.investigate(ticket)

        notifier.send_alert.assert_called_once()
        assert "Investigation complete" in notifier.send_alert.call_args[0][0]

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_high_ticket_no_alert(self, _e, _f):
        notifier = MagicMock()
        agent = _make_investigator(notifier=notifier)
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket(severity=TicketSeverity.HIGH)

        with patch.object(agent, "_run_claude", return_value=(_VALID_REPORT, "")):
            agent.investigate(ticket)

        notifier.send_alert.assert_not_called()


# ---------------------------------------------------------------------------
# 12. Rate limit handling
# ---------------------------------------------------------------------------

class TestRateLimitHandling:
    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_rate_limit_marks_ticket(self, _e, _f):
        from src.swe_team.rate_limiter import RateLimitCooldown, RateLimitExhausted
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket()

        with patch.object(agent._backoff, "execute", side_effect=RateLimitExhausted("sonnet", 3)):
            with pytest.raises(RateLimitCooldown) as exc_info:
                agent.investigate(ticket)

        assert ticket.metadata["rate_limited"] is True
        assert "rate_limited_at" in ticket.metadata
        assert exc_info.value.global_pause is False

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_rate_limit_sends_alert(self, _e, _f):
        from src.swe_team.rate_limiter import RateLimitCooldown, RateLimitExhausted
        notifier = MagicMock()
        agent = _make_investigator(notifier=notifier)
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket()

        with patch.object(agent._backoff, "execute", side_effect=RateLimitExhausted("sonnet", 3)):
            with pytest.raises(RateLimitCooldown):
                agent.investigate(ticket)

        notifier.send_alert.assert_called()
        assert "Rate Limit" in notifier.send_alert.call_args[0][0]


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

class TestEligibility:
    def test_low_severity_not_eligible(self):
        agent = _make_investigator()
        assert agent._eligible(_make_ticket(severity=TicketSeverity.LOW)) is False

    def test_already_investigated_not_eligible(self):
        agent = _make_investigator()
        t = _make_ticket()
        t.investigation_report = "Already done"
        assert agent._eligible(t) is False

    def test_wrong_status_not_eligible(self):
        agent = _make_investigator()
        assert agent._eligible(_make_ticket(status=TicketStatus.IN_DEVELOPMENT)) is False

    def test_umbrella_not_eligible(self):
        agent = _make_investigator()
        assert agent._eligible(_make_ticket(title="[UMBRELLA] Tracking all auth bugs")) is False

    def test_triaged_high_eligible(self):
        agent = _make_investigator()
        assert agent._eligible(_make_ticket(severity=TicketSeverity.HIGH, status=TicketStatus.TRIAGED)) is True

    def test_open_critical_eligible(self):
        agent = _make_investigator()
        assert agent._eligible(_make_ticket(severity=TicketSeverity.CRITICAL, status=TicketStatus.OPEN)) is True


# ---------------------------------------------------------------------------
# Fallback introduction detection
# ---------------------------------------------------------------------------

class TestFallbackIntroduction:
    def test_introduction_detected(self):
        from src.swe_team.investigator import _is_fallback_introduction
        assert _is_fallback_introduction("I just came online and have no context yet") is True

    def test_real_report_not_detected(self):
        from src.swe_team.investigator import _is_fallback_introduction
        assert _is_fallback_introduction("Root cause: the auth token expired") is False

    def test_blank_slate_detected(self):
        from src.swe_team.investigator import _is_fallback_introduction
        assert _is_fallback_introduction("I'm a blank slate with no memory bank") is True


# ---------------------------------------------------------------------------
# Feature/enhancement prompt routing
# ---------------------------------------------------------------------------

class TestFeaturePromptRouting:
    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_feature_ticket_uses_feature_prompt(self, _e, _f):
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket(status=TicketStatus.TRIAGED)
        ticket.ticket_type = TicketType.FEATURE

        with patch.object(agent, "_build_feature_prompt", return_value="Feature prompt") as mock_fp:
            with patch.object(agent, "_run_claude", return_value=(_VALID_REPORT, "")):
                agent.investigate(ticket)
            mock_fp.assert_called_once()


# ---------------------------------------------------------------------------
# LogQueryProvider integration
# ---------------------------------------------------------------------------

class TestLogQueryProviderIntegration:
    """Tests for optional LogQueryProvider in _fetch_worker_logs."""

    def test_no_provider_uses_ssh_only(self):
        """When no log_query_provider is set, behaviour is unchanged."""
        agent = _make_investigator(worker_module_map={"browser": ["worker-browser-1"]})
        assert agent._log_query_provider is None
        ticket = _make_ticket(source_module="browser")
        with patch("src.swe_team.investigator.fetch_worker_logs", return_value="ssh log line"):
            result = agent._fetch_worker_logs(ticket)
        assert result is not None
        assert "ssh log line" in result
        assert "Log Query Provider" not in result

    def test_provider_returns_entries(self):
        """When provider is configured and returns entries, they appear in output."""
        from src.swe_team.providers.log_query.base import LogEntry

        mock_provider = MagicMock()
        mock_provider.query_logs.return_value = [
            LogEntry(
                timestamp="2026-03-22T10:00:00Z",
                level="ERROR",
                message="connection refused",
                source="browser-service",
            ),
        ]
        agent = _make_investigator(log_query_provider=mock_provider)
        ticket = _make_ticket(source_module="browser")
        with patch("src.swe_team.investigator.fetch_worker_logs", return_value=None):
            result = agent._fetch_worker_logs(ticket)
        assert result is not None
        assert "LogQueryProvider" in result
        assert "connection refused" in result
        mock_provider.query_logs.assert_called_once_with(
            service="browser", level="ERROR", since_minutes=60,
        )

    def test_provider_merges_with_ssh(self):
        """When both provider and SSH return data, both appear."""
        from src.swe_team.providers.log_query.base import LogEntry

        mock_provider = MagicMock()
        mock_provider.query_logs.return_value = [
            LogEntry(
                timestamp="2026-03-22T10:00:00Z",
                level="ERROR",
                message="provider log",
                source="svc",
            ),
        ]
        agent = _make_investigator(
            log_query_provider=mock_provider,
            worker_module_map={"browser": ["worker-browser-1"]},
        )
        ticket = _make_ticket(source_module="browser")
        with patch("src.swe_team.investigator.fetch_worker_logs", return_value="ssh logs here"):
            result = agent._fetch_worker_logs(ticket)
        assert "LogQueryProvider" in result
        assert "provider log" in result
        assert "ssh logs here" in result

    def test_provider_error_falls_back_to_ssh(self):
        """When provider raises, SSH fetch still works."""
        mock_provider = MagicMock()
        mock_provider.query_logs.side_effect = RuntimeError("backend down")
        agent = _make_investigator(
            log_query_provider=mock_provider,
            worker_module_map={"browser": ["worker-browser-1"]},
        )
        ticket = _make_ticket(source_module="browser")
        with patch("src.swe_team.investigator.fetch_worker_logs", return_value="ssh fallback"):
            result = agent._fetch_worker_logs(ticket)
        assert result is not None
        assert "ssh fallback" in result
        assert "Log Query Provider" not in result

    def test_provider_empty_results(self):
        """When provider returns empty list, falls back to SSH."""
        mock_provider = MagicMock()
        mock_provider.query_logs.return_value = []
        agent = _make_investigator(
            log_query_provider=mock_provider,
            worker_module_map={"browser": ["worker-browser-1"]},
        )
        ticket = _make_ticket(source_module="browser")
        with patch("src.swe_team.investigator.fetch_worker_logs", return_value="ssh data"):
            result = agent._fetch_worker_logs(ticket)
        assert "ssh data" in result
        assert "Log Query Provider" not in result


# ---------------------------------------------------------------------------
# Fix #356: investigation_complete status transition tests
# ---------------------------------------------------------------------------

class TestInvestigationStatusTransition:
    """Tests for the investigation → investigation_complete status transition.

    Covers the bug described in GitHub issue #356 where tickets with a
    populated investigation_report were left in open/triaged status instead
    of being transitioned to INVESTIGATION_COMPLETE.
    """

    # -- _eligible() repair logic --

    def test_eligible_repairs_stale_open_status_when_report_exists(self):
        """_eligible() must auto-transition a ticket that has a report but
        is still OPEN, and then return False (no re-investigation needed)."""
        agent = _make_investigator()
        ticket = _make_ticket(
            status=TicketStatus.OPEN,
            investigation_report="Previous report",
        )
        result = agent._eligible(ticket)
        assert result is False
        assert ticket.status == TicketStatus.INVESTIGATION_COMPLETE

    def test_eligible_repairs_stale_triaged_status_when_report_exists(self):
        """Same repair for TRIAGED status."""
        agent = _make_investigator()
        ticket = _make_ticket(
            status=TicketStatus.TRIAGED,
            investigation_report="Some report content",
        )
        result = agent._eligible(ticket)
        assert result is False
        assert ticket.status == TicketStatus.INVESTIGATION_COMPLETE

    def test_eligible_repairs_stale_investigating_status_when_report_exists(self):
        """INVESTIGATING with existing report → repair to INVESTIGATION_COMPLETE."""
        agent = _make_investigator()
        ticket = _make_ticket(
            status=TicketStatus.INVESTIGATING,
            investigation_report="Report from crashed session",
        )
        result = agent._eligible(ticket)
        assert result is False
        assert ticket.status == TicketStatus.INVESTIGATION_COMPLETE

    def test_eligible_does_not_touch_already_complete_ticket(self):
        """Tickets already in INVESTIGATION_COMPLETE are left unchanged."""
        agent = _make_investigator()
        ticket = _make_ticket(
            status=TicketStatus.INVESTIGATION_COMPLETE,
            investigation_report="Report",
        )
        result = agent._eligible(ticket)
        assert result is False
        assert ticket.status == TicketStatus.INVESTIGATION_COMPLETE

    def test_eligible_does_not_touch_resolved_ticket(self):
        """RESOLVED tickets with report are not altered."""
        agent = _make_investigator()
        ticket = _make_ticket(
            status=TicketStatus.RESOLVED,
            investigation_report="Report",
        )
        result = agent._eligible(ticket)
        assert result is False
        assert ticket.status == TicketStatus.RESOLVED

    def test_eligible_does_not_touch_failed_ticket(self):
        """FAILED tickets with report are not altered."""
        agent = _make_investigator()
        ticket = _make_ticket(
            status=TicketStatus.FAILED,
            investigation_report="Report",
        )
        result = agent._eligible(ticket)
        assert result is False
        assert ticket.status == TicketStatus.FAILED

    # -- investigate() always transitions on success --

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_investigate_transitions_on_success(self, _e, _f):
        """After investigate() returns True, status must be INVESTIGATION_COMPLETE."""
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket(status=TicketStatus.TRIAGED)

        with patch.object(agent, "_run_claude", return_value=(_VALID_REPORT, "")):
            ok = agent.investigate(ticket)

        assert ok is True
        assert ticket.status == TicketStatus.INVESTIGATION_COMPLETE
        assert ticket.investigation_report == _VALID_REPORT

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_investigate_does_not_transition_on_failure(self, _e, _f):
        """When investigate() returns False (empty report), status must NOT be
        INVESTIGATION_COMPLETE."""
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket(status=TicketStatus.TRIAGED)

        with patch.object(agent, "_run_claude", return_value=("", "")):
            ok = agent.investigate(ticket)

        assert ok is False
        assert ticket.status != TicketStatus.INVESTIGATION_COMPLETE

    # -- Full investigation → investigation_complete → development flow --

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_full_flow_report_then_eligible_for_dev(self, _e, _f):
        """After a successful investigate(), the ticket must have:
        1. investigation_report set
        2. status == INVESTIGATION_COMPLETE
        3. _eligible() returns False (no re-investigation)

        This models the handoff to the developer agent.
        """
        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket(status=TicketStatus.TRIAGED)

        with patch.object(agent, "_run_claude", return_value=(_VALID_REPORT, "")):
            ok = agent.investigate(ticket)

        assert ok is True
        assert ticket.investigation_report == _VALID_REPORT
        assert ticket.status == TicketStatus.INVESTIGATION_COMPLETE
        # Ticket is now ineligible for re-investigation — it should go to dev
        assert agent._eligible(ticket) is False

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_stale_ticket_repaired_and_ineligible_for_reinvestigation(self, _e, _f):
        """Simulate a crash-and-recover scenario:
        A ticket has a report but was left in OPEN status.
        _eligible() must repair it and return False so it goes to the dev path.
        """
        agent = _make_investigator()
        ticket = _make_ticket(
            status=TicketStatus.OPEN,
            investigation_report="Crash happened mid-session; partial report",
        )

        # _eligible should repair and return False
        eligible = agent._eligible(ticket)
        assert eligible is False
        assert ticket.status == TicketStatus.INVESTIGATION_COMPLETE

        # After repair, a second call to _eligible should also return False
        # (already in INVESTIGATION_COMPLETE, normal path)
        eligible2 = agent._eligible(ticket)
        assert eligible2 is False
        assert ticket.status == TicketStatus.INVESTIGATION_COMPLETE


# ---------------------------------------------------------------------------
# _sanitize_report — strip Claude CLI tool-call artifacts (issue #375)
# ---------------------------------------------------------------------------

class TestSanitizeReport:
    """_sanitize_report must remove tool-call JSON/XML that leaks into reports."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from src.swe_team.investigator import _is_valid_report, _sanitize_report
        self.is_valid = _is_valid_report
        self.sanitize = _sanitize_report

    def test_clean_text_unchanged(self):
        """Plain investigation text must pass through without modification."""
        text = "Root cause: missing null check in auth module.\n\nFix: add guard."
        assert self.sanitize(text) == text

    def test_strips_tool_bash_input_block(self):
        """[Tool: Bash] + Input: JSON line must be removed."""
        raw = (
            "[Tool: Bash]\n"
            'Input: {"command": "git ls-remote --heads origin"}\n'
            "\n"
            "The root cause is a missing dependency."
        )
        result = self.sanitize(raw)
        assert "[Tool:" not in result
        assert "Input:" not in result
        assert "root cause" in result

    def test_strips_tool_read_block(self):
        """[Tool: Read] artifact is also removed."""
        raw = (
            "Before the bug:\n"
            "\n"
            "[Tool: Read]\n"
            'Input: {"file_path": "/src/foo.py"}\n'
            "\n"
            "After analysis: the issue is clear."
        )
        result = self.sanitize(raw)
        assert "[Tool:" not in result
        assert "Before the bug" in result
        assert "After analysis" in result

    def test_strips_multiple_tool_blocks(self):
        """Multiple consecutive tool-call blocks are all removed."""
        raw = (
            "[Tool: Bash]\n"
            'Input: {"command": "ls -la"}\n'
            "\n"
            "[Tool: Grep]\n"
            'Input: {"pattern": "import os"}\n'
            "\n"
            "Summary: two files affected."
        )
        result = self.sanitize(raw)
        assert "[Tool:" not in result
        assert "Summary:" in result

    def test_strips_standalone_tool_header(self):
        """A lone [Tool: Bash] line with no input block is also removed."""
        raw = "Analysis:\n\n[Tool: Bash]\n\nConclusion: fix applied."
        result = self.sanitize(raw)
        assert "[Tool:" not in result
        assert "Conclusion:" in result

    def test_collapses_excess_blank_lines(self):
        """After stripping, runs of 3+ blank lines are collapsed to 2."""
        raw = "Line one.\n\n\n\n\nLine two."
        result = self.sanitize(raw)
        assert "\n\n\n" not in result
        assert "Line one." in result
        assert "Line two." in result

    def test_empty_string_returns_empty(self):
        assert self.sanitize("") == ""

    def test_only_tool_blocks_returns_empty(self):
        """A report consisting entirely of tool-call artifacts becomes empty."""
        raw = "[Tool: Bash]\nInput: {\"command\": \"pwd\"}\n\n"
        result = self.sanitize(raw)
        assert result == ""

    @_PATCH_WORKER
    @_PATCH_EMBED
    def test_report_stored_without_tool_artifacts(self, _e, _f):
        """End-to-end: investigate() stores a sanitized report on the ticket."""
        from unittest.mock import MagicMock, patch

        dirty_report = (
            "[Tool: Bash]\n"
            'Input: {"command": "git log --oneline -5"}\n'
            "\n"
            "Root Cause: Null pointer in auth handler caused exception propagation.\n\n"
            "Affected Files: src/auth/handler.py, src/swe_team/investigator.py\n\n"
            "Fix Plan: Add defensive null checks, tighten report validation, and ensure downstream automation only receives structured investigation output."
        )

        agent = _make_investigator()
        agent._program_cache = "Investigate: {error_log} Module: {source_module}"
        ticket = _make_ticket()

        engine_result = MagicMock()
        engine_result.success = True
        engine_result.stdout = dirty_report
        engine_result.stderr = ""
        engine_result.returncode = 0
        engine_result.cost_usd = None
        engine_result.input_tokens = None
        engine_result.output_tokens = None
        engine_result.cache_read_tokens = None
        engine_result.cache_creation_tokens = None
        engine_result.num_turns = None
        engine_result.duration_api_ms = None
        engine_result.session_id = None

        with patch.object(agent._engine, "run", return_value=engine_result), \
             patch.object(agent, "_comment_on_issue"), \
             patch.object(agent, "_notify_investigation"):
            ok = agent.investigate(ticket)

        assert ok is True
        assert "[Tool:" not in (ticket.investigation_report or "")
        assert "Root Cause:" in (ticket.investigation_report or "")

    def test_is_valid_report_rejects_error_json(self):
        error_json = (
            '{"type":"result","subtype":"error_during_execution",'
            '"stop_reason":"error","result":"proxy timeout"}'
        )
        assert self.is_valid(error_json) is False

    def test_is_valid_report_accepts_sectioned_report(self):
        assert self.is_valid(_VALID_REPORT) is True

    def test_is_valid_report_rejects_short_text(self):
        assert self.is_valid("Root Cause: short.\nAffected Files: x\nFix Plan: y") is False
