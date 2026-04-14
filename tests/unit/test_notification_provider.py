"""Tests for plugin boundary enforcement (Issue #132).

Verifies that core agents use injected NotificationProvider / IssueTracker
protocol objects instead of importing concrete implementations directly.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(severity: str = "critical", **overrides) -> SWETicket:
    defaults = dict(
        title="Test ticket",
        description="Test",
        severity=TicketSeverity(severity),
        source_module="test_mod",
    )
    defaults.update(overrides)
    return SWETicket(**defaults)


def _mock_notifier() -> MagicMock:
    """Return a mock that satisfies NotificationProvider protocol."""
    m = MagicMock()
    m.name = "mock_notifier"
    m.send_alert.return_value = True
    m.send_daily_summary.return_value = True
    m.send_hitl_escalation.return_value = True
    m.health_check.return_value = True
    return m


def _mock_issue_tracker() -> MagicMock:
    """Return a mock that satisfies IssueTracker protocol."""
    m = MagicMock()
    m.name = "mock_tracker"
    m.comment.return_value = True
    m.health_check.return_value = True
    return m


# ---------------------------------------------------------------------------
# TelegramNotificationProvider unit tests
# ---------------------------------------------------------------------------

class TestTelegramNotificationProvider:
    def test_satisfies_protocol(self):
        from src.swe_team.providers.notification.base import NotificationProvider
        from src.swe_team.providers.notification.telegram_provider import TelegramNotificationProvider
        provider = TelegramNotificationProvider(token="tok", chat_id="123")
        assert isinstance(provider, NotificationProvider)

    def test_name_property(self):
        from src.swe_team.providers.notification.telegram_provider import TelegramNotificationProvider
        p = TelegramNotificationProvider()
        assert p.name == "telegram"

    def test_health_check_no_credentials(self):
        from src.swe_team.providers.notification.telegram_provider import TelegramNotificationProvider
        p = TelegramNotificationProvider()
        assert p.health_check() is False

    def test_health_check_with_credentials(self):
        from src.swe_team.providers.notification.telegram_provider import TelegramNotificationProvider
        p = TelegramNotificationProvider(token="tok", chat_id="123")
        assert p.health_check() is True


# ---------------------------------------------------------------------------
# GitHubIssueTracker unit tests
# ---------------------------------------------------------------------------

class TestGitHubIssueTracker:
    def test_satisfies_protocol(self):
        from src.swe_team.providers.issue_tracker.base import IssueTracker
        from src.swe_team.providers.issue_tracker.github_provider import GitHubIssueTracker
        tracker = GitHubIssueTracker(repo="owner/repo")
        assert isinstance(tracker, IssueTracker)

    def test_name_property(self):
        from src.swe_team.providers.issue_tracker.github_provider import GitHubIssueTracker
        t = GitHubIssueTracker(repo="o/r")
        assert t.name == "github"

    def test_health_check_no_repo(self):
        from src.swe_team.providers.issue_tracker.github_provider import GitHubIssueTracker
        t = GitHubIssueTracker()
        assert t.health_check() is False

    def test_health_check_with_repo(self):
        from src.swe_team.providers.issue_tracker.github_provider import GitHubIssueTracker
        t = GitHubIssueTracker(repo="o/r")
        assert t.health_check() is True

    @patch("src.swe_team.github_integration.comment_on_issue", return_value=True)
    def test_comment_delegates(self, mock_comment):
        from src.swe_team.providers.issue_tracker.github_provider import GitHubIssueTracker
        t = GitHubIssueTracker(repo="o/r")
        result = t.comment("42", "body")
        assert result is True
        mock_comment.assert_called_once_with(42, "body", repo="o/r")


# ---------------------------------------------------------------------------
# InvestigatorAgent uses injected NotificationProvider
# ---------------------------------------------------------------------------

class TestInvestigatorWithNotifier:
    def test_investigator_uses_injected_notifier(self, tmp_path):
        """When a NotificationProvider is injected, the investigator calls it
        instead of the legacy notifier module."""
        from src.swe_team.investigator import InvestigatorAgent

        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        ticket = _make_ticket(severity="critical")
        _valid_report = (
            "Root Cause: A retry loop silently swallowed a timeout from the upstream proxy, "
            "so the worker emitted incomplete diagnostics and the orchestrator persisted an invalid response.\n\n"
            "Affected Files: src/swe_team/investigator.py, src/swe_team/developer.py, tests/unit/test_investigator.py\n\n"
            "Fix Plan: Validate report structure before persistence, reject known Claude error envelopes, "
            "require minimum report length, and keep sectioned investigation output for downstream automation."
        )
        mock_result = type("R", (), {"returncode": 0, "stdout": _valid_report + "\n", "stderr": "Cost: $0.01"})()
        notifier = _mock_notifier()

        with patch("src.swe_team.investigator.subprocess.run", return_value=mock_result):
            agent = InvestigatorAgent(
                program_path=program,
                claude_path="/usr/bin/echo",
                notifier=notifier,
            )
            result = agent.investigate(ticket)

        assert result is True
        # The notifier should have been called (for critical tickets)
        notifier.send_alert.assert_called()

    def test_investigator_uses_injected_issue_tracker(self, tmp_path):
        """When an IssueTracker is injected, the investigator comments via it."""
        from src.swe_team.investigator import InvestigatorAgent

        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        ticket = _make_ticket(severity="critical")
        ticket.metadata["github_issue"] = 42
        _valid_report = (
            "Root Cause: A retry loop silently swallowed a timeout from the upstream proxy, "
            "so the worker emitted incomplete diagnostics and the orchestrator persisted an invalid response.\n\n"
            "Affected Files: src/swe_team/investigator.py, src/swe_team/developer.py, tests/unit/test_investigator.py\n\n"
            "Fix Plan: Validate report structure before persistence, reject known Claude error envelopes, "
            "require minimum report length, and keep sectioned investigation output for downstream automation."
        )
        mock_result = type("R", (), {"returncode": 0, "stdout": _valid_report + "\n", "stderr": "Cost: $0.01"})()
        tracker = _mock_issue_tracker()

        with patch("src.swe_team.investigator.subprocess.run", return_value=mock_result):
            agent = InvestigatorAgent(
                program_path=program,
                claude_path="/usr/bin/echo",
                issue_tracker=tracker,
            )
            result = agent.investigate(ticket)

        assert result is True
        tracker.comment.assert_called_once()
        call_args = tracker.comment.call_args
        assert call_args[0][0] == "42"  # issue_id as string


# ---------------------------------------------------------------------------
# DeveloperAgent uses injected NotificationProvider
# ---------------------------------------------------------------------------

class TestDeveloperWithNotifier:
    def test_developer_send_telegram_uses_notifier(self):
        """When a NotificationProvider is injected, _send_telegram uses it."""
        from src.swe_team.developer import DeveloperAgent

        notifier = _mock_notifier()
        dev = DeveloperAgent(
            repo_root="/tmp",
            claude_path="/usr/bin/echo",
            notifier=notifier,
        )
        dev._send_telegram("test message")
        notifier.send_alert.assert_called_once_with("test message", level="info")

    def test_developer_rate_limit_alert_uses_notifier(self):
        """_send_rate_limit_alert uses the injected provider."""
        from src.swe_team.developer import DeveloperAgent

        notifier = _mock_notifier()
        dev = DeveloperAgent(
            repo_root="/tmp",
            claude_path="/usr/bin/echo",
            notifier=notifier,
        )
        ticket = _make_ticket()
        dev._send_rate_limit_alert(ticket, RuntimeError("429"))
        notifier.send_alert.assert_called_once()
        assert "Rate Limit" in notifier.send_alert.call_args[0][0]


# ---------------------------------------------------------------------------
# OrchestratorAgent uses injected IssueTracker
# ---------------------------------------------------------------------------

class TestOrchestratorWithIssueTracker:
    def test_orchestrator_update_progress_uses_tracker(self):
        """When an IssueTracker is injected, update_progress uses it."""
        from src.swe_team.orchestrator import OrchestratorAgent, OrchestrationPlan, SubTask

        tracker = _mock_issue_tracker()
        tracker.update_comment = MagicMock(return_value=True)

        agent = OrchestratorAgent(
            claude_path="/usr/bin/echo",
            repo_root=Path("/tmp"),
            issue_tracker=tracker,
        )
        plan = OrchestrationPlan(
            ticket_id="t-1",
            session_tag="tag",
            sub_tasks=[SubTask(id="1", description="Work", status="completed")],
        )
        agent.update_progress(plan, comment_id=123, repo="owner/repo")
        tracker.update_comment.assert_called_once()


# ---------------------------------------------------------------------------
# SupabaseTicketStore uses injected NotificationProvider
# ---------------------------------------------------------------------------

class TestSupabaseStoreWithNotifier:
    def test_alert_uses_injected_notifier(self):
        """_alert delegates to the injected NotificationProvider."""
        notifier = _mock_notifier()

        # We can't easily construct a SupabaseTicketStore without real creds,
        # but we can test _alert on an instance with mocked internals.
        from src.swe_team.supabase_store import SupabaseTicketStore
        store = object.__new__(SupabaseTicketStore)
        store._notifier = notifier
        store._alert("test alert message")
        notifier.send_alert.assert_called_once_with("test alert message", level="warning")

    def test_alert_falls_back_to_legacy(self):
        """Without an injected notifier, _alert falls back to notifier._send."""
        from src.swe_team.supabase_store import SupabaseTicketStore
        store = object.__new__(SupabaseTicketStore)
        store._notifier = None
        with patch("src.swe_team.notifier._send") as mock_send:
            store._alert("fallback message")
            mock_send.assert_called_once_with("fallback message")
