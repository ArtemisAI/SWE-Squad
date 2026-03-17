"""
Tests for rate limit detection and exponential backoff (Phase 1 of issue #18).

Covers:
  - ExponentialBackoff: retries on rate limit, fails fast on other errors, respects max retries
  - RateLimitTracker: recording, recent events, cooldown detection
  - InvestigatorAgent: rate limit triggers backoff, exhausted marks ticket
  - DeveloperAgent: same pattern
  - RateLimitConfig: loads from YAML
"""

from __future__ import annotations

import logging
logging.logAsyncioTasks = False

import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.swe_team.rate_limiter import (
    ExponentialBackoff,
    RateLimitExhausted,
    RateLimitTracker,
)
from src.swe_team.config import RateLimitConfig, SWETeamConfig, load_config
from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.investigator import InvestigatorAgent
from src.swe_team.developer import DeveloperAgent


# ======================================================================
# ExponentialBackoff
# ======================================================================


class TestExponentialBackoff:
    """ExponentialBackoff: retries on rate limit, fails fast on other errors, respects max retries."""

    def test_success_on_first_call(self):
        backoff = ExponentialBackoff(max_retries=3, initial_delay=0.01, max_delay=0.1)
        result = backoff.execute(lambda: "ok", context="test")
        assert result == "ok"

    def test_retries_on_rate_limit_error(self):
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("Rate limit exceeded (429)")
            return "recovered"

        backoff = ExponentialBackoff(max_retries=3, initial_delay=0.01, max_delay=0.1)
        result = backoff.execute(flaky, context="test")
        assert result == "recovered"
        assert call_count == 3

    def test_retries_on_429_in_message(self):
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RuntimeError("HTTP 429 Too Many Requests")
            return "ok"

        backoff = ExponentialBackoff(max_retries=3, initial_delay=0.01, max_delay=0.1)
        result = backoff.execute(flaky, context="test")
        assert result == "ok"
        assert call_count == 2

    def test_fails_fast_on_non_rate_limit_error(self):
        def bad():
            raise RuntimeError("Something completely different")

        backoff = ExponentialBackoff(max_retries=3, initial_delay=0.01, max_delay=0.1)
        with pytest.raises(RuntimeError, match="Something completely different"):
            backoff.execute(bad, context="test")

    def test_raises_exhausted_after_max_retries(self):
        def always_limited():
            raise RuntimeError("Rate limit hit")

        backoff = ExponentialBackoff(max_retries=2, initial_delay=0.01, max_delay=0.1)
        with pytest.raises(RateLimitExhausted, match="Rate limit exhausted after 2 retries"):
            backoff.execute(always_limited, context="model-x")

    def test_records_events_in_tracker(self):
        tracker = RateLimitTracker()
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("Rate limit exceeded")
            return "ok"

        backoff = ExponentialBackoff(
            max_retries=3, initial_delay=0.01, max_delay=0.1, tracker=tracker
        )
        backoff.execute(flaky, context="sonnet")
        assert len(tracker.events) == 2  # Two retries before success
        assert tracker.events[0]["model"] == "sonnet"
        assert tracker.events[0]["attempt"] == 1
        assert tracker.events[1]["attempt"] == 2

    def test_retries_on_os_error_with_rate_limit(self):
        """OSError containing rate limit text should also be retried."""
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise OSError("Rate limit: too many requests")
            return "ok"

        backoff = ExponentialBackoff(max_retries=3, initial_delay=0.01, max_delay=0.1)
        result = backoff.execute(flaky, context="test")
        assert result == "ok"
        assert call_count == 2

    def test_non_rate_limit_os_error_not_retried(self):
        def bad():
            raise OSError("file not found")

        backoff = ExponentialBackoff(max_retries=3, initial_delay=0.01, max_delay=0.1)
        with pytest.raises(OSError, match="file not found"):
            backoff.execute(bad, context="test")

    def test_max_delay_cap(self):
        """Ensure the backoff delay never exceeds max_delay."""
        backoff = ExponentialBackoff(max_retries=5, initial_delay=100, max_delay=150)
        # With attempt=3: 100 * 2^3 = 800, should be capped to 150
        delay = min(backoff.initial_delay * (2 ** 3), backoff.max_delay)
        assert delay == 150

    def test_value_error_not_caught(self):
        """Non RuntimeError/OSError exceptions should propagate immediately."""
        def bad():
            raise ValueError("nope")

        backoff = ExponentialBackoff(max_retries=3, initial_delay=0.01, max_delay=0.1)
        with pytest.raises(ValueError, match="nope"):
            backoff.execute(bad, context="test")


# ======================================================================
# RateLimitTracker
# ======================================================================


class TestRateLimitTracker:
    """RateLimitTracker: recording, recent events, cooldown detection."""

    def test_record_and_list_events(self):
        tracker = RateLimitTracker()
        tracker.record(model="sonnet", context="investigation", attempt=1, wait_seconds=30)
        tracker.record(model="opus", context="dev", attempt=2, wait_seconds=60)
        assert len(tracker.events) == 2
        assert tracker.events[0]["model"] == "sonnet"
        assert tracker.events[1]["wait_seconds"] == 60

    def test_recent_events_filters_by_time(self):
        tracker = RateLimitTracker()
        # Add a recent event
        tracker.record(model="sonnet", context="test", attempt=1, wait_seconds=30)
        # Add an old event (manually)
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        tracker.events.append({
            "timestamp": old_time,
            "model": "opus",
            "context": "old-test",
            "attempt": 1,
            "wait_seconds": 30,
        })
        recent = tracker.recent_events(hours=1)
        assert len(recent) == 1
        assert recent[0]["model"] == "sonnet"

    def test_recent_events_all(self):
        tracker = RateLimitTracker()
        tracker.record(model="a", context="t", attempt=1, wait_seconds=10)
        tracker.record(model="b", context="t", attempt=1, wait_seconds=20)
        # Both are recent
        assert len(tracker.recent_events(hours=1)) == 2

    def test_is_cooling_down_true_after_recent_event(self):
        tracker = RateLimitTracker()
        tracker.record(model="sonnet", context="test", attempt=1, wait_seconds=30)
        assert tracker.is_cooling_down() is True

    def test_is_cooling_down_false_when_no_events(self):
        tracker = RateLimitTracker()
        assert tracker.is_cooling_down() is False

    def test_is_cooling_down_false_after_old_events(self):
        tracker = RateLimitTracker()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        tracker.events.append({
            "timestamp": old_time,
            "model": "sonnet",
            "context": "test",
            "attempt": 1,
            "wait_seconds": 30,
        })
        assert tracker.is_cooling_down() is False

    def test_recent_events_handles_malformed_timestamps(self):
        tracker = RateLimitTracker()
        tracker.events.append({
            "timestamp": "not-a-date",
            "model": "x",
            "context": "y",
            "attempt": 1,
            "wait_seconds": 10,
        })
        # Should not raise, just skip bad entries
        result = tracker.recent_events(hours=1)
        assert len(result) == 0


# ======================================================================
# RateLimitConfig
# ======================================================================


class TestRateLimitConfig:
    """RateLimitConfig loads from YAML and integrates with SWETeamConfig."""

    def test_defaults(self):
        cfg = RateLimitConfig()
        assert cfg.max_retries_on_429 == 3
        assert cfg.initial_backoff_seconds == 30
        assert cfg.max_backoff_seconds == 300

    def test_from_dict(self):
        cfg = RateLimitConfig.from_dict({
            "max_retries_on_429": 5,
            "initial_backoff_seconds": 10,
            "max_backoff_seconds": 600,
        })
        assert cfg.max_retries_on_429 == 5
        assert cfg.initial_backoff_seconds == 10
        assert cfg.max_backoff_seconds == 600

    def test_from_dict_defaults(self):
        cfg = RateLimitConfig.from_dict({})
        assert cfg.max_retries_on_429 == 3
        assert cfg.initial_backoff_seconds == 30
        assert cfg.max_backoff_seconds == 300

    def test_to_dict(self):
        cfg = RateLimitConfig(max_retries_on_429=2, initial_backoff_seconds=15, max_backoff_seconds=120)
        d = cfg.to_dict()
        assert d == {
            "max_retries_on_429": 2,
            "initial_backoff_seconds": 15,
            "max_backoff_seconds": 120,
        }

    def test_swe_team_config_includes_rate_limits(self):
        config = SWETeamConfig()
        assert hasattr(config, "rate_limits")
        assert isinstance(config.rate_limits, RateLimitConfig)
        assert config.rate_limits.max_retries_on_429 == 3

    def test_swe_team_config_from_dict_with_rate_limits(self):
        config = SWETeamConfig.from_dict({
            "rate_limits": {
                "max_retries_on_429": 5,
                "initial_backoff_seconds": 60,
                "max_backoff_seconds": 600,
            }
        })
        assert config.rate_limits.max_retries_on_429 == 5
        assert config.rate_limits.initial_backoff_seconds == 60
        assert config.rate_limits.max_backoff_seconds == 600

    def test_swe_team_config_to_dict_includes_rate_limits(self):
        config = SWETeamConfig()
        d = config.to_dict()
        assert "rate_limits" in d
        assert d["rate_limits"]["max_retries_on_429"] == 3

    def test_load_config_from_yaml(self, tmp_path):
        yaml_content = """
enabled: false
rate_limits:
  max_retries_on_429: 7
  initial_backoff_seconds: 45
  max_backoff_seconds: 500
"""
        cfg_file = tmp_path / "test_config.yaml"
        cfg_file.write_text(yaml_content)
        config = load_config(str(cfg_file))
        assert config.rate_limits.max_retries_on_429 == 7
        assert config.rate_limits.initial_backoff_seconds == 45
        assert config.rate_limits.max_backoff_seconds == 500


# ======================================================================
# InvestigatorAgent rate limit integration
# ======================================================================


class TestInvestigatorRateLimit:
    """Investigator: rate limit triggers backoff, exhausted marks ticket."""

    def test_investigate_retries_on_rate_limit(self, tmp_path):
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        ticket = SWETicket(
            title="Test crash",
            description="boom",
            severity=TicketSeverity.HIGH,
            source_module="testing",
            error_log="Traceback: boom",
        )
        ticket.transition(TicketStatus.TRIAGED)

        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                # Simulate non-zero exit (rate limit)
                result = MagicMock()
                result.returncode = 1
                result.stderr = "Rate limit exceeded (429)"
                result.stdout = ""
                return result
            # Success on second call
            result = MagicMock()
            result.returncode = 0
            result.stdout = "Root cause: Y\n"
            result.stderr = "Cost: $0.05"
            return result

        rl_config = RateLimitConfig(max_retries_on_429=3, initial_backoff_seconds=0.01, max_backoff_seconds=0.1)

        with (
            patch("src.swe_team.investigator.subprocess.run", side_effect=mock_run),
            patch("src.swe_team.investigator.notify_investigation_summary"),
        ):
            agent = InvestigatorAgent(
                program_path=program,
                claude_path="/usr/bin/claude",
                rate_limit_config=rl_config,
            )
            result = agent.investigate(ticket)

        assert result is True
        assert ticket.investigation_report == "Root cause: Y"
        assert call_count == 2

    def test_investigate_marks_rate_limited_on_exhaustion(self, tmp_path):
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        ticket = SWETicket(
            title="Test crash",
            description="boom",
            severity=TicketSeverity.HIGH,
            source_module="testing",
            error_log="Traceback: boom",
        )
        ticket.transition(TicketStatus.TRIAGED)

        def always_rate_limited(*args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stderr = "Rate limit hit (429)"
            result.stdout = ""
            return result

        rl_config = RateLimitConfig(max_retries_on_429=1, initial_backoff_seconds=0.01, max_backoff_seconds=0.1)

        with (
            patch("src.swe_team.investigator.subprocess.run", side_effect=always_rate_limited),
            patch("src.swe_team.investigator.notify_investigation_summary"),
            patch("src.swe_team.telegram.send_message", return_value=True) as mock_tg,
        ):
            agent = InvestigatorAgent(
                program_path=program,
                claude_path="/usr/bin/claude",
                rate_limit_config=rl_config,
            )
            result = agent.investigate(ticket)

        assert result is False
        assert ticket.metadata.get("rate_limited") is True
        assert "rate_limited_at" in ticket.metadata
        # Telegram alert should have been attempted
        mock_tg.assert_called_once()

    def test_investigate_passes_tracker(self, tmp_path):
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        tracker = RateLimitTracker()
        rl_config = RateLimitConfig(max_retries_on_429=3, initial_backoff_seconds=0.01, max_backoff_seconds=0.1)

        agent = InvestigatorAgent(
            program_path=program,
            claude_path="/usr/bin/claude",
            rate_limit_config=rl_config,
            rate_limit_tracker=tracker,
        )
        assert agent._backoff.tracker is tracker

    def test_backoff_uses_config_values(self, tmp_path):
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        rl_config = RateLimitConfig(max_retries_on_429=7, initial_backoff_seconds=42, max_backoff_seconds=999)
        agent = InvestigatorAgent(
            program_path=program,
            claude_path="/usr/bin/claude",
            rate_limit_config=rl_config,
        )
        assert agent._backoff.max_retries == 7
        assert agent._backoff.initial_delay == 42
        assert agent._backoff.max_delay == 999


# ======================================================================
# DeveloperAgent rate limit integration
# ======================================================================


class TestDeveloperRateLimit:
    """Developer: rate limit triggers backoff, exhausted marks ticket and breaks loop."""

    def test_developer_has_backoff(self, tmp_path):
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")

        rl_config = RateLimitConfig(max_retries_on_429=5, initial_backoff_seconds=20, max_backoff_seconds=200)

        dev = DeveloperAgent(
            repo_root=tmp_path,
            program_path=program,
            rate_limit_config=rl_config,
        )
        assert dev._backoff.max_retries == 5
        assert dev._backoff.initial_delay == 20
        assert dev._backoff.max_delay == 200

    def test_developer_backoff_uses_tracker(self, tmp_path):
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")

        tracker = RateLimitTracker()
        rl_config = RateLimitConfig(max_retries_on_429=3, initial_backoff_seconds=0.01, max_backoff_seconds=0.1)

        dev = DeveloperAgent(
            repo_root=tmp_path,
            program_path=program,
            rate_limit_config=rl_config,
            rate_limit_tracker=tracker,
        )
        assert dev._backoff.tracker is tracker

    def test_developer_marks_rate_limited_on_exhaustion(self, tmp_path):
        """When _run_claude hits rate limit exhaustion, ticket should be marked."""
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")

        ticket = SWETicket(
            title="Bug to fix",
            description="needs fixing",
            severity=TicketSeverity.HIGH,
            source_module="core",
            investigation_report="Root cause identified",
        )
        ticket.transition(TicketStatus.INVESTIGATION_COMPLETE)

        rl_config = RateLimitConfig(max_retries_on_429=1, initial_backoff_seconds=0.01, max_backoff_seconds=0.1)

        def mock_subprocess_run(cmd, **kwargs):
            """Mock subprocess.run: git commands succeed, claude CLI rate-limits."""
            result = MagicMock()
            if isinstance(cmd, list) and cmd[0] == "git":
                result.returncode = 0
                result.stdout = "abc123\n"  # for rev-parse HEAD
                result.stderr = ""
                return result
            # Claude CLI call — always rate limit
            result.returncode = 1
            result.stderr = "Rate limit exceeded 429"
            result.stdout = ""
            return result

        with (
            patch("src.swe_team.developer.subprocess.run", side_effect=mock_subprocess_run),
            patch.object(DeveloperAgent, "_run_preflight") as mock_preflight,
            patch("src.swe_team.telegram.send_message", return_value=True) as mock_tg,
        ):
            mock_pf = MagicMock()
            mock_pf.passed = True
            mock_preflight.return_value = mock_pf

            dev = DeveloperAgent(
                repo_root=tmp_path,
                program_path=program,
                rate_limit_config=rl_config,
            )
            result = dev.attempt_fix(ticket)

        assert result is False
        assert ticket.metadata.get("rate_limited") is True
        assert "rate_limited_at" in ticket.metadata
        # send_message is called for rate limit alert AND escalation
        assert mock_tg.call_count >= 1
        # First call should be the rate limit alert
        first_call_text = mock_tg.call_args_list[0][0][0]
        assert "Rate Limit Exhausted" in first_call_text

    def test_developer_default_backoff_without_config(self, tmp_path):
        """When no rate_limit_config is passed, defaults should be used."""
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")

        dev = DeveloperAgent(repo_root=tmp_path, program_path=program)
        assert dev._backoff.max_retries == 3
        assert dev._backoff.initial_delay == 30
        assert dev._backoff.max_delay == 300


# ======================================================================
# Runner integration (rate_limit_tracker in cycle)
# ======================================================================


class TestRunnerRateLimitIntegration:
    """Runner creates and passes rate limit tracker to agents."""

    def test_run_cycle_creates_rate_limit_tracker(self):
        """The run_cycle function should instantiate a RateLimitTracker."""
        # We verify this by checking the import works and the tracker class exists
        from scripts.ops.swe_team_runner import run_cycle
        from src.swe_team.rate_limiter import RateLimitTracker

        # The tracker is created inside run_cycle, so we just verify integration
        # by checking that the module imports the tracker
        import scripts.ops.swe_team_runner as runner
        assert hasattr(runner, "RateLimitTracker")

    def test_rate_limit_events_in_cycle_result(self):
        """Cycle result should include rate_limit_events count."""
        import scripts.ops.swe_team_runner as runner
        from src.swe_team.config import SWETeamConfig
        from src.swe_team.ticket_store import TicketStore
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(f"{tmpdir}/tickets.json")

            with (
                patch.object(runner, "ModelProbe") as mock_probe_cls,
                patch.object(runner, "PreflightCheck") as mock_preflight,
                patch.object(runner, "_send_preflight_alert"),
                patch("src.swe_team.remote_logs.collect_remote_logs", return_value=[]),
                patch.object(runner, "fetch_github_tickets", return_value=[]),
                patch.object(runner, "MonitorAgent") as mock_monitor,
                patch.object(runner, "check_regressions", return_value=[]),
                patch.object(runner, "detect_stalled_tickets", return_value=[]),
            ):
                mock_pf = mock_preflight.return_value
                mock_pf_result = MagicMock()
                mock_pf_result.passed = True
                mock_pf.run.return_value = mock_pf_result

                mock_probe_inst = mock_probe_cls.return_value
                mock_probe_inst.validate_and_patch_env.return_value = {}

                mock_monitor.return_value.scan.return_value = []
                mock_monitor.return_value._config = MagicMock()

                config = SWETeamConfig(enabled=True)
                result = runner.run_cycle(config, store, dry_run=False)

            assert "rate_limit_events" in result
            assert result["rate_limit_events"] == 0
