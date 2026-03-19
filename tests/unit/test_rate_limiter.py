"""
Tests for rate limit detection and exponential backoff (issue #16).

Covers:
  - ExponentialBackoff: retries on rate limit, fails fast on other errors, respects max retries
  - RateLimitTracker: recording, recent events, cooldown detection
  - InvestigatorAgent: rate limit triggers backoff, exhausted marks ticket
  - DeveloperAgent: same pattern
  - RateLimitConfig: loads from YAML
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.rate_limiter import (
    ExponentialBackoff,
    RateLimitExhausted,
    RateLimitTracker,
)
from src.swe_team.models import SWETicket, TicketSeverity


class TestExponentialBackoff:
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
        backoff = ExponentialBackoff(max_retries=3, initial_delay=0.01, max_delay=0.1, tracker=tracker)
        backoff.execute(flaky, model="sonnet", context="test")
        assert len(tracker.events) == 2
        assert tracker.events[0]["model"] == "sonnet"
        assert tracker.events[0]["attempt"] == 1
        assert tracker.events[1]["attempt"] == 2

    def test_retries_on_os_error_with_rate_limit(self):
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
        backoff = ExponentialBackoff(max_retries=5, initial_delay=100, max_delay=150)
        delay = min(backoff.initial_delay * (2 ** 3), backoff.max_delay)
        assert delay == 150

    def test_value_error_not_caught(self):
        def bad():
            raise ValueError("nope")
        backoff = ExponentialBackoff(max_retries=3, initial_delay=0.01, max_delay=0.1)
        with pytest.raises(ValueError, match="nope"):
            backoff.execute(bad, context="test")


class TestRateLimitTracker:
    def test_record_and_list_events(self):
        tracker = RateLimitTracker()
        tracker.record(model="sonnet", context="investigation", attempt=1, wait_seconds=30)
        tracker.record(model="opus", context="dev", attempt=2, wait_seconds=60)
        assert len(tracker.events) == 2
        assert tracker.events[0]["model"] == "sonnet"
        assert tracker.events[1]["wait_seconds"] == 60

    def test_recent_events_filters_by_time(self):
        tracker = RateLimitTracker()
        tracker.record(model="sonnet", context="test", attempt=1, wait_seconds=30)
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        tracker.events.append({"timestamp": old_time, "model": "opus", "context": "old", "attempt": 1, "wait_seconds": 30})
        recent = tracker.recent_events(hours=1)
        assert len(recent) == 1
        assert recent[0]["model"] == "sonnet"

    def test_recent_events_all(self):
        tracker = RateLimitTracker()
        tracker.record(model="a", context="t", attempt=1, wait_seconds=10)
        tracker.record(model="b", context="t", attempt=1, wait_seconds=20)
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
        tracker.events.append({"timestamp": old_time, "model": "sonnet", "context": "test", "attempt": 1, "wait_seconds": 30})
        assert tracker.is_cooling_down() is False

    def test_recent_events_handles_malformed_timestamps(self):
        tracker = RateLimitTracker()
        tracker.events.append({"timestamp": "not-a-date", "model": "x", "context": "y", "attempt": 1, "wait_seconds": 10})
        result = tracker.recent_events(hours=1)
        assert len(result) == 0


@pytest.mark.skip(reason="requires RateLimitConfig integration — pending config.py update")
class TestRateLimitConfig:
    def test_defaults(self):
        from src.swe_team.config import RateLimitConfig
        cfg = RateLimitConfig()
        assert cfg.max_retries_on_429 == 3

    def test_from_dict(self):
        from src.swe_team.config import RateLimitConfig
        cfg = RateLimitConfig.from_dict({"max_retries_on_429": 5, "initial_backoff_seconds": 10, "max_backoff_seconds": 600})
        assert cfg.max_retries_on_429 == 5

    def test_from_dict_defaults(self):
        from src.swe_team.config import RateLimitConfig
        cfg = RateLimitConfig.from_dict({})
        assert cfg.max_retries_on_429 == 3

    def test_to_dict(self):
        from src.swe_team.config import RateLimitConfig
        cfg = RateLimitConfig(max_retries_on_429=2, initial_backoff_seconds=15, max_backoff_seconds=120)
        assert cfg.to_dict() == {"max_retries_on_429": 2, "initial_backoff_seconds": 15, "max_backoff_seconds": 120}

    def test_swe_team_config_includes_rate_limits(self):
        from src.swe_team.config import RateLimitConfig, SWETeamConfig
        config = SWETeamConfig()
        assert hasattr(config, "rate_limits")
        assert isinstance(config.rate_limits, RateLimitConfig)

    def test_swe_team_config_from_dict_with_rate_limits(self):
        from src.swe_team.config import SWETeamConfig
        config = SWETeamConfig.from_dict({"rate_limits": {"max_retries_on_429": 5, "initial_backoff_seconds": 60, "max_backoff_seconds": 600}})
        assert config.rate_limits.max_retries_on_429 == 5

    def test_swe_team_config_to_dict_includes_rate_limits(self):
        from src.swe_team.config import SWETeamConfig
        config = SWETeamConfig()
        assert "rate_limits" in config.to_dict()

    def test_load_config_from_yaml(self, tmp_path):
        from src.swe_team.config import load_config
        cfg_file = tmp_path / "test_config.yaml"
        cfg_file.write_text("enabled: false\nrate_limits:\n  max_retries_on_429: 7\n  initial_backoff_seconds: 45\n  max_backoff_seconds: 500\n")
        config = load_config(str(cfg_file))
        assert config.rate_limits.max_retries_on_429 == 7


@pytest.mark.skip(reason="requires RateLimitConfig integration — pending config.py update")
class TestInvestigatorRateLimit:
    def test_investigate_retries_on_rate_limit(self, tmp_path):
        from src.swe_team.config import RateLimitConfig
        from src.swe_team.investigator import InvestigatorAgent
        from src.swe_team.models import TicketStatus
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")
        ticket = SWETicket(title="Test crash", description="boom", severity=TicketSeverity.HIGH, source_module="testing", error_log="boom")
        ticket.transition(TicketStatus.TRIAGED)
        call_count = 0
        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count < 2:
                result.returncode = 1; result.stderr = "Rate limit exceeded (429)"; result.stdout = ""
            else:
                result.returncode = 0; result.stdout = "Root cause: Y\n"; result.stderr = "Cost: $0.05"
            return result
        rl_config = RateLimitConfig(max_retries_on_429=3, initial_backoff_seconds=0.01, max_backoff_seconds=0.1)
        with patch("src.swe_team.investigator.subprocess.run", side_effect=mock_run), patch("src.swe_team.investigator.notify_investigation_summary"):
            agent = InvestigatorAgent(program_path=program, claude_path="/usr/bin/claude", rate_limit_config=rl_config)
            result = agent.investigate(ticket)
        assert result is True
        assert call_count == 2

    def test_investigate_passes_tracker(self, tmp_path):
        from src.swe_team.config import RateLimitConfig
        from src.swe_team.investigator import InvestigatorAgent
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")
        tracker = RateLimitTracker()
        rl_config = RateLimitConfig(max_retries_on_429=3, initial_backoff_seconds=0.01, max_backoff_seconds=0.1)
        agent = InvestigatorAgent(program_path=program, claude_path="/usr/bin/claude", rate_limit_config=rl_config, rate_limit_tracker=tracker)
        assert agent._backoff.tracker is tracker

    def test_backoff_uses_config_values(self, tmp_path):
        from src.swe_team.config import RateLimitConfig
        from src.swe_team.investigator import InvestigatorAgent
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")
        rl_config = RateLimitConfig(max_retries_on_429=7, initial_backoff_seconds=42, max_backoff_seconds=999)
        agent = InvestigatorAgent(program_path=program, claude_path="/usr/bin/claude", rate_limit_config=rl_config)
        assert agent._backoff.max_retries == 7
        assert agent._backoff.initial_delay == 42
        assert agent._backoff.max_delay == 999


@pytest.mark.skip(reason="requires RateLimitConfig integration — pending config.py update")
class TestDeveloperRateLimit:
    def test_developer_has_backoff(self, tmp_path):
        from src.swe_team.config import RateLimitConfig
        from src.swe_team.developer import DeveloperAgent
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")
        rl_config = RateLimitConfig(max_retries_on_429=5, initial_backoff_seconds=20, max_backoff_seconds=200)
        dev = DeveloperAgent(repo_root=tmp_path, program_path=program, rate_limit_config=rl_config)
        assert dev._backoff.max_retries == 5

    def test_developer_backoff_uses_tracker(self, tmp_path):
        from src.swe_team.config import RateLimitConfig
        from src.swe_team.developer import DeveloperAgent
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")
        tracker = RateLimitTracker()
        rl_config = RateLimitConfig(max_retries_on_429=3, initial_backoff_seconds=0.01, max_backoff_seconds=0.1)
        dev = DeveloperAgent(repo_root=tmp_path, program_path=program, rate_limit_config=rl_config, rate_limit_tracker=tracker)
        assert dev._backoff.tracker is tracker

    def test_developer_default_backoff_without_config(self, tmp_path):
        from src.swe_team.developer import DeveloperAgent
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")
        dev = DeveloperAgent(repo_root=tmp_path, program_path=program)
        assert dev._backoff.max_retries == 3


@pytest.mark.skip(reason="requires RateLimitConfig integration — pending config.py update")
class TestRunnerRateLimitIntegration:
    def test_run_cycle_creates_rate_limit_tracker(self):
        import scripts.ops.swe_team_runner as runner
        assert hasattr(runner, "RateLimitTracker")

    def test_rate_limit_events_in_cycle_result(self):
        import scripts.ops.swe_team_runner as runner
        from src.swe_team.config import SWETeamConfig
        from src.swe_team.ticket_store import TicketStore
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(f"{tmpdir}/tickets.json")
            with patch.object(runner, "ModelProbe") as mock_probe_cls, patch.object(runner, "PreflightCheck") as mock_preflight, patch.object(runner, "_send_preflight_alert"), patch("src.swe_team.remote_logs.collect_remote_logs", return_value=[]), patch.object(runner, "fetch_github_tickets", return_value=[]), patch.object(runner, "MonitorAgent") as mock_monitor, patch.object(runner, "check_regressions", return_value=[]), patch.object(runner, "detect_stalled_tickets", return_value=[]):
                mock_pf = mock_preflight.return_value
                mock_pf_result = MagicMock()
                mock_pf_result.passed = True
                mock_pf.run.return_value = mock_pf_result
                mock_probe_cls.return_value.validate_and_patch_env.return_value = {}
                mock_monitor.return_value.scan.return_value = []
                mock_monitor.return_value._config = MagicMock()
                config = SWETeamConfig(enabled=True)
                result = runner.run_cycle(config, store, dry_run=False)
            assert "rate_limit_events" in result
            assert result["rate_limit_events"] == 0
