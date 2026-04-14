"""Tests for critical untested functions in swe_team_runner.py."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, mock_open, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# We need to mock heavy imports that swe_team_runner pulls in at module level
# before we can import the runner itself.
# ---------------------------------------------------------------------------

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Lightweight stubs for SWE models needed by the runner functions
# ---------------------------------------------------------------------------
from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus


def _make_ticket(
    ticket_id: str = "test-001",
    severity: TicketSeverity = TicketSeverity.HIGH,
    status: TicketStatus = TicketStatus.OPEN,
    investigation_report: str = "",
    **meta,
) -> SWETicket:
    """Create a minimal SWETicket for testing."""
    t = SWETicket(
        title=f"Test ticket {ticket_id}",
        description="Unit test ticket",
        severity=severity,
        source_module="test_module",
        ticket_id=ticket_id,
        status=status,
    )
    t.investigation_report = investigation_report
    t.metadata["fingerprint"] = f"fp-{ticket_id}"
    t.metadata.update(meta)
    return t


# ============================================================================
# 1. append_progress_log
# ============================================================================

class TestAppendProgressLog:
    """Tests for append_progress_log()."""

    def test_writes_structured_entry(self, tmp_path):
        """Appends a correctly formatted progress entry to the file."""
        from scripts.ops.swe_team_runner import append_progress_log

        log_file = tmp_path / "swe_progress.txt"

        with patch("scripts.ops.swe_team_runner._PROGRESS_LOG_PATH", log_file):
            result = {"new_tickets": 3, "open_tickets": 5, "gate_verdict": "PASS"}
            append_progress_log(
                result,
                done="Fixed auth bug",
                next_step="Monitor logs",
                blockers="None",
            )

        content = log_file.read_text()
        assert "Tickets: 3/5" in content
        assert "Gate: PASS" in content
        assert "DONE: Fixed auth bug" in content
        assert "NEXT: Monitor logs" in content
        assert "BLOCKERS: None" in content

    def test_defaults_for_missing_fields(self, tmp_path):
        """Uses default text when done/next_step/blockers are empty."""
        from scripts.ops.swe_team_runner import append_progress_log

        log_file = tmp_path / "swe_progress.txt"

        with patch("scripts.ops.swe_team_runner._PROGRESS_LOG_PATH", log_file):
            append_progress_log({})

        content = log_file.read_text()
        assert "Tickets: 0/0" in content
        assert "Gate: N/A" in content
        assert "DONE: Cycle completed" in content
        assert "NEXT: Continue monitoring" in content
        assert "BLOCKERS: None" in content

    def test_handles_file_error_gracefully(self):
        """Logs a warning instead of crashing on OSError."""
        from scripts.ops.swe_team_runner import append_progress_log

        bad_path = Path("/nonexistent/dir/progress.txt")
        with patch("scripts.ops.swe_team_runner._PROGRESS_LOG_PATH", bad_path):
            # Should not raise
            append_progress_log({"new_tickets": 1})

    def test_appends_multiple_entries(self, tmp_path):
        """Multiple calls append, not overwrite."""
        from scripts.ops.swe_team_runner import append_progress_log

        log_file = tmp_path / "swe_progress.txt"

        with patch("scripts.ops.swe_team_runner._PROGRESS_LOG_PATH", log_file):
            append_progress_log({"new_tickets": 1, "gate_verdict": "PASS"})
            append_progress_log({"new_tickets": 2, "gate_verdict": "WARN"})

        content = log_file.read_text()
        assert content.count("--- CYCLE") == 2
        assert "Gate: PASS" in content
        assert "Gate: WARN" in content


# ============================================================================
# 2. setup_logging
# ============================================================================

class TestSetupLogging:
    """Tests for setup_logging()."""

    @patch("scripts.ops.swe_team_runner.PROJECT_ROOT")
    def test_creates_log_directory(self, mock_root, tmp_path):
        """Creates the logs/ directory if it does not exist."""
        from scripts.ops.swe_team_runner import setup_logging

        mock_root.__truediv__ = lambda self, other: tmp_path / other
        # Provide a real Path for the log dir
        log_dir = tmp_path / "logs"
        assert not log_dir.exists()

        with patch("scripts.ops.swe_team_runner.PROJECT_ROOT", tmp_path):
            setup_logging(verbose=False)

        assert log_dir.exists()
        assert log_dir.is_dir()

    @patch("scripts.ops.swe_team_runner.PROJECT_ROOT")
    def test_verbose_sets_debug_level(self, mock_root, tmp_path):
        """verbose=True sets the root logger to DEBUG."""
        from scripts.ops.swe_team_runner import setup_logging

        with patch("scripts.ops.swe_team_runner.PROJECT_ROOT", tmp_path):
            setup_logging(verbose=True)

        root = logging.getLogger()
        assert root.level == logging.DEBUG

    @patch("scripts.ops.swe_team_runner.PROJECT_ROOT")
    def test_non_verbose_sets_info_level(self, mock_root, tmp_path):
        """verbose=False sets the root logger to INFO."""
        from scripts.ops.swe_team_runner import setup_logging

        with patch("scripts.ops.swe_team_runner.PROJECT_ROOT", tmp_path):
            setup_logging(verbose=False)

        root = logging.getLogger()
        assert root.level == logging.INFO

    @patch("scripts.ops.swe_team_runner.PROJECT_ROOT")
    def test_creates_rotating_file_and_stream_handlers(self, mock_root, tmp_path):
        """Sets up a RotatingFileHandler and StreamHandler."""
        from scripts.ops.swe_team_runner import setup_logging

        with patch("scripts.ops.swe_team_runner.PROJECT_ROOT", tmp_path):
            setup_logging(verbose=False)

        root = logging.getLogger()
        handler_types = {type(h).__name__ for h in root.handlers}
        assert "RotatingFileHandler" in handler_types
        assert "StreamHandler" in handler_types

    @patch("scripts.ops.swe_team_runner.PROJECT_ROOT")
    def test_clears_pre_existing_handlers(self, mock_root, tmp_path):
        """Clears pre-existing handlers to prevent duplicate log lines."""
        from scripts.ops.swe_team_runner import setup_logging

        root = logging.getLogger()
        # Add a dummy handler
        dummy = logging.StreamHandler()
        root.addHandler(dummy)
        count_before = len(root.handlers)

        with patch("scripts.ops.swe_team_runner.PROJECT_ROOT", tmp_path):
            setup_logging(verbose=False)

        # Should have exactly the 2 handlers setup_logging creates (file + stream)
        assert len(root.handlers) == 2

    @patch("scripts.ops.swe_team_runner.PROJECT_ROOT")
    def test_log_file_created(self, mock_root, tmp_path):
        """The swe_team.log file is created inside logs/."""
        from scripts.ops.swe_team_runner import setup_logging

        with patch("scripts.ops.swe_team_runner.PROJECT_ROOT", tmp_path):
            setup_logging(verbose=False)

        log_file = tmp_path / "logs" / "swe_team.log"
        assert log_file.exists()

    @patch("scripts.ops.swe_team_runner.PROJECT_ROOT")
    def test_early_basicConfig_handlers_cleared(self, mock_root, tmp_path):
        """Early basicConfig handlers are fully replaced by setup_logging.

        The runner calls logging.basicConfig() at module level for early
        error capture. setup_logging() must clear those and replace with
        RotatingFileHandler + StreamHandler to avoid duplicate lines.
        """
        from scripts.ops.swe_team_runner import setup_logging

        root = logging.getLogger()
        # Simulate the early basicConfig that runs at module import
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S UTC",
        )
        early_count = len(root.handlers)
        assert early_count >= 1, "basicConfig should add at least one handler"

        with patch("scripts.ops.swe_team_runner.PROJECT_ROOT", tmp_path):
            setup_logging(verbose=False)

        # After setup_logging, only its own 2 handlers should remain
        assert len(root.handlers) == 2
        handler_types = {type(h).__name__ for h in root.handlers}
        assert "RotatingFileHandler" in handler_types
        assert "StreamHandler" in handler_types

    @patch("scripts.ops.swe_team_runner.PROJECT_ROOT")
    def test_file_handler_is_rotating(self, mock_root, tmp_path):
        """setup_logging uses RotatingFileHandler with size limits."""
        from scripts.ops.swe_team_runner import setup_logging

        with patch("scripts.ops.swe_team_runner.PROJECT_ROOT", tmp_path):
            setup_logging(verbose=False)

        root = logging.getLogger()
        file_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        fh = file_handlers[0]
        assert fh.maxBytes == 5 * 1024 * 1024  # 5 MB
        assert fh.backupCount == 3


# ============================================================================
# Self-heal logging (no FileHandler to avoid double-write with cron redirect)

# ============================================================================
class TestSelfHealLogging:
    """Tests for self_heal.py logging configuration."""

    def test_no_file_handler(self):
        """self_heal.py should not have a FileHandler (cron redirects to file)."""
        # Import self_heal and check that the logger has no FileHandler
        import importlib
        import scripts.ops.self_heal as sh_module
        importlib.reload(sh_module)
        logger = sh_module.logger
        file_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.FileHandler)
        ]
        # The root logger is what basicConfig configures; self_heal logger
        # should not add its own FileHandler
        assert len(file_handlers) == 0


# ============================================================================
# 3. bootstrap_cycle
# ============================================================================

class TestBootstrapCycle:
    """Tests for bootstrap_cycle()."""

    def test_no_issues_returns_zero_acknowledged(self):
        """When the monitor finds no issues, returns acknowledged=0."""
        from scripts.ops.swe_team_runner import bootstrap_cycle

        config = MagicMock()
        store = MagicMock()
        store.known_fingerprints = set()

        with patch("scripts.ops.swe_team_runner.MonitorAgent") as MockMonitor:
            MockMonitor.return_value.scan.return_value = []
            result = bootstrap_cycle(config, store)

        assert result == {"acknowledged": 0}
        store.add.assert_not_called()

    def test_acknowledges_baseline_tickets(self):
        """Scanned tickets are triaged, acknowledged, and persisted."""
        from scripts.ops.swe_team_runner import bootstrap_cycle

        config = MagicMock()
        store = MagicMock()
        store.known_fingerprints = set()

        ticket1 = _make_ticket("boot-1")
        ticket2 = _make_ticket("boot-2")

        with (
            patch("scripts.ops.swe_team_runner.MonitorAgent") as MockMonitor,
            patch("scripts.ops.swe_team_runner.TriageAgent") as MockTriage,
        ):
            MockMonitor.return_value.scan.return_value = [ticket1, ticket2]
            MockTriage.return_value.triage_batch.return_value = [ticket1, ticket2]

            result = bootstrap_cycle(config, store)

        assert result == {"acknowledged": 2}
        assert store.add.call_count == 2
        # Verify tickets were transitioned to ACKNOWLEDGED
        assert ticket1.status == TicketStatus.ACKNOWLEDGED
        assert ticket2.status == TicketStatus.ACKNOWLEDGED
        assert "bootstrap" in ticket1.metadata
        assert "bootstrap" in ticket2.metadata

    def test_dry_run_does_not_persist(self):
        """In dry_run mode, tickets are not written to the store."""
        from scripts.ops.swe_team_runner import bootstrap_cycle

        config = MagicMock()
        store = MagicMock()
        store.known_fingerprints = set()

        ticket = _make_ticket("dry-1")

        with (
            patch("scripts.ops.swe_team_runner.MonitorAgent") as MockMonitor,
            patch("scripts.ops.swe_team_runner.TriageAgent") as MockTriage,
        ):
            MockMonitor.return_value.scan.return_value = [ticket]
            MockTriage.return_value.triage_batch.return_value = [ticket]

            result = bootstrap_cycle(config, store, dry_run=True)

        assert result == {"acknowledged": 1}
        store.add.assert_not_called()

    def test_triage_failure_falls_back_to_baseline(self):
        """If triage raises, bootstrap still acknowledges baseline tickets."""
        from scripts.ops.swe_team_runner import bootstrap_cycle

        config = MagicMock()
        store = MagicMock()
        store.known_fingerprints = set()

        ticket = _make_ticket("fail-triage-1")

        with (
            patch("scripts.ops.swe_team_runner.MonitorAgent") as MockMonitor,
            patch("scripts.ops.swe_team_runner.TriageAgent") as MockTriage,
        ):
            MockMonitor.return_value.scan.return_value = [ticket]
            MockTriage.return_value.triage_batch.side_effect = RuntimeError("triage broke")

            result = bootstrap_cycle(config, store)

        assert result == {"acknowledged": 1}
        assert ticket.status == TicketStatus.ACKNOWLEDGED
        store.add.assert_called_once()

    def test_triage_returns_empty_falls_back_to_baseline(self):
        """If triage returns empty list, fallback to baseline tickets."""
        from scripts.ops.swe_team_runner import bootstrap_cycle

        config = MagicMock()
        store = MagicMock()
        store.known_fingerprints = set()

        ticket = _make_ticket("empty-triage-1")

        with (
            patch("scripts.ops.swe_team_runner.MonitorAgent") as MockMonitor,
            patch("scripts.ops.swe_team_runner.TriageAgent") as MockTriage,
        ):
            MockMonitor.return_value.scan.return_value = [ticket]
            MockTriage.return_value.triage_batch.return_value = []

            result = bootstrap_cycle(config, store)

        assert result == {"acknowledged": 1}
        assert ticket.status == TicketStatus.ACKNOWLEDGED

    def test_store_add_failure_does_not_crash(self):
        """If store.add raises, bootstrap continues without crashing."""
        from scripts.ops.swe_team_runner import bootstrap_cycle

        config = MagicMock()
        store = MagicMock()
        store.known_fingerprints = set()
        store.add.side_effect = Exception("DB down")

        ticket = _make_ticket("store-fail-1")

        with (
            patch("scripts.ops.swe_team_runner.MonitorAgent") as MockMonitor,
            patch("scripts.ops.swe_team_runner.TriageAgent") as MockTriage,
        ):
            MockMonitor.return_value.scan.return_value = [ticket]
            MockTriage.return_value.triage_batch.return_value = [ticket]

            # Should not raise
            result = bootstrap_cycle(config, store)

        assert result == {"acknowledged": 1}


# ============================================================================
# 3b. dependency filtering helper
# ============================================================================

class TestDependencyFiltering:
    def test_filters_blocked_ticket_from_stage_batch(self):
        from scripts.ops.swe_team_runner import _filter_dependency_ready_tickets

        blocker = _make_ticket("A", status=TicketStatus.OPEN)
        ready = _make_ticket("B", status=TicketStatus.OPEN)
        blocked = _make_ticket("C", status=TicketStatus.OPEN)
        blocked.blocked_by = ["A"]

        filtered = _filter_dependency_ready_tickets(
            all_tickets=[blocker, ready, blocked],
            candidates=[ready, blocked],
            stage="Investigation",
        )

        assert [t.ticket_id for t in filtered] == ["B"]


# ============================================================================
# 4. _run_parallel_investigations
# ============================================================================

class TestRunParallelInvestigations:
    """Tests for _run_parallel_investigations()."""

    def _make_config(self, mode="fixed"):
        """Build a minimal config mock for parallel investigations."""
        config = MagicMock()
        config.execution.mode = mode
        config.memory.store_on_investigation_complete = False
        return config

    def test_empty_pending_returns_empty(self):
        """Returns empty list when no pending tickets."""
        from scripts.ops.swe_team_runner import _run_parallel_investigations

        result = _run_parallel_investigations(
            config=self._make_config(),
            store=MagicMock(),
            investigator=MagicMock(),
            pending=[],
            swe_events=[],
        )
        assert result == []

    @patch("scripts.ops.swe_team_runner._queued_dispatcher", None)
    @patch("scripts.ops.swe_team_runner._get_or_create_executor")
    def test_submits_investigations_to_executor(self, mock_get_executor):
        """Each pending ticket is submitted to the executor."""
        from scripts.ops.swe_team_runner import _run_parallel_investigations
        from src.swe_team.parallel_executor import TaskResult

        ticket1 = _make_ticket("inv-1", investigation_report="found bug")
        ticket2 = _make_ticket("inv-2", investigation_report="found crash")

        # Mock executor
        mock_executor = MagicMock()
        mock_executor.active_profile_name = "base"
        mock_executor.active_profile.max_concurrent_investigations = 4
        mock_get_executor.return_value = mock_executor

        # Mock futures and results
        result1 = TaskResult(
            ticket_id="inv-1", task_type="investigation",
            success=True, duration_s=10.0, ticket=ticket1,
        )
        result2 = TaskResult(
            ticket_id="inv-2", task_type="investigation",
            success=True, duration_s=12.0, ticket=ticket2,
        )
        mock_executor.collect_results.return_value = [result1, result2]

        store = MagicMock()
        swe_events: list = []
        investigator = MagicMock()

        investigated = _run_parallel_investigations(
            config=self._make_config(),
            store=store,
            investigator=investigator,
            pending=[ticket1, ticket2],
            swe_events=swe_events,
        )

        assert len(investigated) == 2
        assert mock_executor.submit_investigation.call_count == 2
        assert store.add.call_count == 2
        assert len(swe_events) == 2

    @patch("scripts.ops.swe_team_runner._queued_dispatcher", None)
    @patch("scripts.ops.swe_team_runner._get_or_create_executor")
    def test_failed_investigation_not_in_results(self, mock_get_executor):
        """Failed investigations are excluded from the returned list."""
        from scripts.ops.swe_team_runner import _run_parallel_investigations
        from src.swe_team.parallel_executor import TaskResult

        ticket = _make_ticket("inv-fail-1")

        mock_executor = MagicMock()
        mock_executor.active_profile_name = "base"
        mock_executor.active_profile.max_concurrent_investigations = 4
        mock_get_executor.return_value = mock_executor

        fail_result = TaskResult(
            ticket_id="inv-fail-1", task_type="investigation",
            success=False, duration_s=5.0, error="model timeout",
        )
        mock_executor.collect_results.return_value = [fail_result]

        store = MagicMock()
        swe_events: list = []

        investigated = _run_parallel_investigations(
            config=self._make_config(),
            store=store,
            investigator=MagicMock(),
            pending=[ticket],
            swe_events=swe_events,
        )

        assert investigated == []
        store.add.assert_not_called()
        assert len(swe_events) == 0

    @patch("scripts.ops.swe_team_runner._queued_dispatcher", None)
    @patch("scripts.ops.swe_team_runner._get_or_create_executor")
    def test_submit_failure_logged_not_crashed(self, mock_get_executor):
        """If submit_investigation raises, it logs a warning and continues."""
        from scripts.ops.swe_team_runner import _run_parallel_investigations
        from src.swe_team.parallel_executor import TaskResult

        ticket1 = _make_ticket("inv-sub-fail")
        ticket2 = _make_ticket("inv-sub-ok", investigation_report="ok")

        mock_executor = MagicMock()
        mock_executor.active_profile_name = "base"
        mock_executor.active_profile.max_concurrent_investigations = 4
        mock_get_executor.return_value = mock_executor

        # First submit fails, second succeeds
        mock_executor.submit_investigation.side_effect = [
            RuntimeError("pool full"),
            MagicMock(),
        ]
        result_ok = TaskResult(
            ticket_id="inv-sub-ok", task_type="investigation",
            success=True, duration_s=8.0, ticket=ticket2,
        )
        mock_executor.collect_results.return_value = [result_ok]

        store = MagicMock()
        swe_events: list = []

        investigated = _run_parallel_investigations(
            config=self._make_config(),
            store=store,
            investigator=MagicMock(),
            pending=[ticket1, ticket2],
            swe_events=swe_events,
        )

        assert len(investigated) == 1
        assert investigated[0].ticket_id == "inv-sub-ok"

    @patch("scripts.ops.swe_team_runner._queued_dispatcher", None)
    @patch("scripts.ops.swe_team_runner._get_or_create_executor")
    def test_store_persist_failure_does_not_crash(self, mock_get_executor):
        """If store.add raises during persist, the function continues."""
        from scripts.ops.swe_team_runner import _run_parallel_investigations
        from src.swe_team.parallel_executor import TaskResult

        ticket = _make_ticket("inv-persist-fail", investigation_report="found it")

        mock_executor = MagicMock()
        mock_executor.active_profile_name = "base"
        mock_executor.active_profile.max_concurrent_investigations = 4
        mock_get_executor.return_value = mock_executor

        result = TaskResult(
            ticket_id="inv-persist-fail", task_type="investigation",
            success=True, duration_s=10.0, ticket=ticket,
        )
        mock_executor.collect_results.return_value = [result]

        store = MagicMock()
        store.add.side_effect = Exception("DB write failed")
        swe_events: list = []

        # Should not raise
        investigated = _run_parallel_investigations(
            config=self._make_config(),
            store=store,
            investigator=MagicMock(),
            pending=[ticket],
            swe_events=swe_events,
        )

        # Ticket is still in the investigated list (it succeeded)
        assert len(investigated) == 1

    @patch("scripts.ops.swe_team_runner._queued_dispatcher", None)
    @patch("scripts.ops.swe_team_runner._get_or_create_executor")
    def test_adaptive_mode_resolves_profile(self, mock_get_executor):
        """In adaptive mode, resolves the right execution profile."""
        from scripts.ops.swe_team_runner import _run_parallel_investigations
        from src.swe_team.parallel_executor import TaskResult

        ticket = _make_ticket("inv-adapt", investigation_report="bug")

        mock_executor = MagicMock()
        mock_executor.active_profile_name = "burst"
        mock_executor.active_profile.max_concurrent_investigations = 8
        mock_executor.resolve_adaptive_profile.return_value = "burst"
        mock_get_executor.return_value = mock_executor

        result = TaskResult(
            ticket_id="inv-adapt", task_type="investigation",
            success=True, duration_s=5.0, ticket=ticket,
        )
        mock_executor.collect_results.return_value = [result]

        store = MagicMock()
        store.list_open.return_value = [_make_ticket(f"open-{i}") for i in range(10)]
        swe_events: list = []

        _run_parallel_investigations(
            config=self._make_config(mode="adaptive"),
            store=store,
            investigator=MagicMock(),
            pending=[ticket],
            swe_events=swe_events,
        )

        mock_executor.resolve_adaptive_profile.assert_called_once_with(backlog_size=10)
        mock_executor.scale_to.assert_called_once_with("burst")


# ============================================================================
# 5. _run_parallel_developments
# ============================================================================

class TestRunParallelDevelopments:
    """Tests for _run_parallel_developments()."""

    def _make_config(self, mode="fixed"):
        config = MagicMock()
        config.execution.mode = mode
        config.execution.profiles = {"base": MagicMock(), "max": MagicMock()}
        config.models = MagicMock()
        config.rate_limits = MagicMock()
        return config

    def _make_effective_cycle(self, max_dev=5):
        ec = MagicMock()
        ec.max_developments_per_cycle = max_dev
        ec.max_reinvestigations = 1
        return ec

    def test_empty_investigated_returns_early(self):
        """No candidates means the function returns immediately."""
        from scripts.ops.swe_team_runner import _run_parallel_developments

        store = MagicMock()
        config = self._make_config()

        with patch("scripts.ops.swe_team_runner._get_or_create_executor") as mock_exec:
            _run_parallel_developments(
                config=config,
                store=store,
                effective_cycle=self._make_effective_cycle(),
                investigated=[],
                rate_limit_tracker=MagicMock(),
            )
            mock_exec.assert_not_called()

    def test_filters_by_severity_and_report(self):
        """Only tickets with investigation_report and valid severity are developed."""
        from scripts.ops.swe_team_runner import _run_parallel_developments

        # HIGH with report -> candidate
        t_good = _make_ticket("dev-good", severity=TicketSeverity.HIGH, investigation_report="root cause found")
        # LOW with report -> excluded
        t_low = _make_ticket("dev-low", severity=TicketSeverity.LOW, investigation_report="minor")
        # HIGH without report -> excluded
        t_no_report = _make_ticket("dev-noreport", severity=TicketSeverity.HIGH, investigation_report="")

        config = self._make_config()
        store = MagicMock()

        mock_executor = MagicMock()
        mock_executor.active_profile_name = "base"
        mock_wt_mgr = MagicMock()

        with (
            patch("scripts.ops.swe_team_runner._get_or_create_executor", return_value=mock_executor),
            patch("scripts.ops.swe_team_runner._get_or_create_worktree_manager", return_value=mock_wt_mgr),
            patch("scripts.ops.swe_team_runner.CircuitBreaker"),
            patch("scripts.ops.swe_team_runner.DeveloperAgent", create=True),
            patch("scripts.ops.swe_team_runner.sandbox_repos_map", {}, create=True),
        ):
            _run_parallel_developments(
                config=config,
                store=store,
                effective_cycle=self._make_effective_cycle(),
                investigated=[t_good, t_low, t_no_report],
                rate_limit_tracker=MagicMock(),
            )

            # Only t_good should get a worktree acquired
            assert mock_wt_mgr.acquire.call_count == 1

    def test_blocks_development_when_dependency_unresolved(self):
        """Dependency-blocked tickets are skipped in development batch."""
        from scripts.ops.swe_team_runner import _run_parallel_developments

        blocker = _make_ticket("dev-blocker", severity=TicketSeverity.HIGH, investigation_report="still open")
        blocked = _make_ticket("dev-blocked", severity=TicketSeverity.HIGH, investigation_report="report")
        blocked.blocked_by = [blocker.ticket_id]

        config = self._make_config()
        store = MagicMock()
        store.list_all.return_value = [blocker, blocked]

        mock_executor = MagicMock()
        mock_executor.active_profile_name = "base"
        mock_wt_mgr = MagicMock()

        with (
            patch("scripts.ops.swe_team_runner._get_or_create_executor", return_value=mock_executor),
            patch("scripts.ops.swe_team_runner._get_or_create_worktree_manager", return_value=mock_wt_mgr),
            patch("scripts.ops.swe_team_runner.CircuitBreaker"),
            patch("scripts.ops.swe_team_runner.DeveloperAgent", create=True),
            patch("scripts.ops.swe_team_runner.sandbox_repos_map", {}, create=True),
        ):
            _run_parallel_developments(
                config=config,
                store=store,
                effective_cycle=self._make_effective_cycle(),
                investigated=[blocked],
                rate_limit_tracker=MagicMock(),
            )

        mock_wt_mgr.acquire.assert_not_called()

    @patch("scripts.ops.swe_team_runner.sandbox_repos_map", {}, create=True)
    @patch("scripts.ops.swe_team_runner._get_or_create_worktree_manager")
    @patch("scripts.ops.swe_team_runner._get_or_create_executor")
    @patch("scripts.ops.swe_team_runner.CircuitBreaker")
    def test_respects_max_developments_limit(self, mock_cb, mock_get_executor, mock_get_wt):
        """Limits the number of dev candidates to max_developments_per_cycle."""
        from scripts.ops.swe_team_runner import _run_parallel_developments

        tickets = [
            _make_ticket(f"dev-{i}", severity=TicketSeverity.HIGH, investigation_report=f"report-{i}")
            for i in range(10)
        ]

        config = self._make_config()
        store = MagicMock()

        mock_executor = MagicMock()
        mock_executor.active_profile_name = "base"
        mock_get_executor.return_value = mock_executor
        mock_wt_mgr = MagicMock()
        mock_get_wt.return_value = mock_wt_mgr

        with patch("scripts.ops.swe_team_runner.DeveloperAgent", create=True):
            _run_parallel_developments(
                config=config,
                store=store,
                effective_cycle=self._make_effective_cycle(max_dev=3),
                investigated=tickets,
                rate_limit_tracker=MagicMock(),
            )

        # Only 3 worktrees should be acquired (limited by max_developments_per_cycle)
        assert mock_wt_mgr.acquire.call_count == 3

    @patch("scripts.ops.swe_team_runner.sandbox_repos_map", {}, create=True)
    @patch("scripts.ops.swe_team_runner._get_or_create_worktree_manager")
    @patch("scripts.ops.swe_team_runner._get_or_create_executor")
    @patch("scripts.ops.swe_team_runner.CircuitBreaker")
    def test_worktree_acquire_failure_continues(self, mock_cb, mock_get_executor, mock_get_wt):
        """If worktree acquire fails for one ticket, others still proceed."""
        from scripts.ops.swe_team_runner import _run_parallel_developments

        t1 = _make_ticket("dev-wt-fail", severity=TicketSeverity.HIGH, investigation_report="report")
        t2 = _make_ticket("dev-wt-ok", severity=TicketSeverity.HIGH, investigation_report="report")

        config = self._make_config()
        store = MagicMock()

        mock_executor = MagicMock()
        mock_executor.active_profile_name = "base"
        mock_get_executor.return_value = mock_executor

        mock_wt_mgr = MagicMock()
        # First acquire fails, second succeeds
        mock_wt = MagicMock()
        mock_wt.path = Path("/tmp/worktree")
        mock_wt_mgr.acquire.side_effect = [RuntimeError("no slots"), mock_wt]
        mock_get_wt.return_value = mock_wt_mgr

        mock_future = MagicMock()
        mock_result = MagicMock()
        mock_result.ticket = t2
        mock_result.success = True
        mock_result.ticket.metadata = {"attempts": [{"branch": "swe-fix/test", "pushed": True}]}
        mock_future.result.return_value = mock_result
        mock_executor.submit_development.return_value = mock_future

        with patch("scripts.ops.swe_team_runner.DeveloperAgent", create=True):
            # Should not crash
            _run_parallel_developments(
                config=config,
                store=store,
                effective_cycle=self._make_effective_cycle(),
                investigated=[t1, t2],
                rate_limit_tracker=MagicMock(),
            )

        # Only one submit should have happened (second ticket)
        assert mock_executor.submit_development.call_count == 1

    @patch("scripts.ops.swe_team_runner.sandbox_repos_map", {}, create=True)
    @patch("scripts.ops.swe_team_runner._get_or_create_worktree_manager")
    @patch("scripts.ops.swe_team_runner._get_or_create_executor")
    @patch("scripts.ops.swe_team_runner.CircuitBreaker")
    def test_critical_ticket_triggers_orchestration(self, mock_cb, mock_get_executor, mock_get_wt):
        """CRITICAL tickets trigger orchestration planning before development."""
        from scripts.ops.swe_team_runner import _run_parallel_developments

        ticket = _make_ticket(
            "dev-critical",
            severity=TicketSeverity.CRITICAL,
            investigation_report="critical failure",
        )

        config = self._make_config()
        store = MagicMock()

        mock_executor = MagicMock()
        mock_executor.active_profile_name = "base"
        mock_get_executor.return_value = mock_executor

        mock_wt_mgr = MagicMock()
        mock_wt = MagicMock()
        mock_wt.path = Path("/tmp/worktree")
        mock_wt_mgr.acquire.return_value = mock_wt
        mock_get_wt.return_value = mock_wt_mgr

        mock_future = MagicMock()
        mock_result = MagicMock()
        mock_result.ticket = ticket
        mock_result.success = False
        mock_result.ticket.metadata = {}
        mock_future.result.return_value = mock_result
        mock_executor.submit_development.return_value = mock_future

        mock_plan = MagicMock()
        mock_plan.to_checklist.return_value = "- [ ] Step 1\n- [ ] Step 2"
        mock_plan.sub_tasks = ["task1", "task2"]

        with (
            patch("scripts.ops.swe_team_runner.DeveloperAgent", create=True),
            patch("src.swe_team.orchestrator.OrchestratorAgent") as MockOrch,
        ):
            MockOrch.return_value.plan.return_value = mock_plan

            _run_parallel_developments(
                config=config,
                store=store,
                effective_cycle=self._make_effective_cycle(),
                investigated=[ticket],
                rate_limit_tracker=MagicMock(),
            )

        MockOrch.return_value.plan.assert_called_once_with(ticket)


class TestResetBlockedTickets:
    def test_unblocks_when_blocker_is_resolved(self, tmp_path):
        from scripts.ops.swe_team_runner import _reset_blocked_tickets
        from src.swe_team.ticket_store import TicketStore

        store = TicketStore(path=str(tmp_path / "tickets.json"))
        blocker = _make_ticket("blocker-resolved", status=TicketStatus.RESOLVED, severity=TicketSeverity.LOW)
        blocked = _make_ticket("blocked-1", status=TicketStatus.BLOCKED)
        blocked.blocked_by = [blocker.ticket_id]
        store.add(blocker)
        store.add(blocked)

        touched = _reset_blocked_tickets(store, blocked_ticket_timeout_hours=4, blocked_ticket_escalation_hours=24)

        refreshed = store.get(blocked.ticket_id)
        assert len(touched) == 1
        assert refreshed is not None
        assert refreshed.status == TicketStatus.TRIAGED
        assert refreshed.blocked_by == []

    def test_unblocks_when_blocker_is_stuck_past_timeout(self, tmp_path):
        from scripts.ops.swe_team_runner import _reset_blocked_tickets
        from src.swe_team.ticket_store import TicketStore

        store = TicketStore(path=str(tmp_path / "tickets.json"))
        now = datetime.now(timezone.utc)
        blocker = _make_ticket("blocker-stuck", status=TicketStatus.INVESTIGATING)
        blocker.updated_at = (now - timedelta(hours=5)).isoformat()
        blocked = _make_ticket("blocked-2", status=TicketStatus.BLOCKED)
        blocked.blocked_by = [blocker.ticket_id]
        blocked.updated_at = (now - timedelta(hours=1)).isoformat()
        store.add(blocker)
        store.add(blocked)

        touched = _reset_blocked_tickets(store, blocked_ticket_timeout_hours=4, blocked_ticket_escalation_hours=24)

        refreshed = store.get(blocked.ticket_id)
        assert len(touched) == 1
        assert refreshed is not None
        assert refreshed.status == TicketStatus.TRIAGED
        assert refreshed.blocked_by == []
        assert refreshed.metadata.get("blocked_timeout_events")

    def test_escalates_to_hitl_when_blocked_too_long(self, tmp_path):
        from scripts.ops.swe_team_runner import _reset_blocked_tickets
        from src.swe_team.ticket_store import TicketStore

        store = TicketStore(path=str(tmp_path / "tickets.json"))
        now = datetime.now(timezone.utc)
        blocker = _make_ticket("blocker-active", status=TicketStatus.INVESTIGATING)
        blocker.updated_at = (now - timedelta(hours=1)).isoformat()
        blocked = _make_ticket("blocked-3", status=TicketStatus.BLOCKED)
        blocked.blocked_by = [blocker.ticket_id]
        blocked.updated_at = (now - timedelta(hours=25)).isoformat()
        store.add(blocker)
        store.add(blocked)

        touched = _reset_blocked_tickets(store, blocked_ticket_timeout_hours=4, blocked_ticket_escalation_hours=24)

        refreshed = store.get(blocked.ticket_id)
        assert len(touched) == 1
        assert refreshed is not None
        assert refreshed.status == TicketStatus.BLOCKED
        assert refreshed.metadata.get("needs_hitl") is True
        assert "Blocked for" in refreshed.metadata.get("hitl_reason", "")
