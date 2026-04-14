"""
Unit tests for MonitorAgent (#113).

Covers:
  - Traceback header lines are filtered and do not produce tickets
  - Stack-frame lines (File "...", line N) are filtered
  - Whitespace-only lines are filtered
  - Lines containing only ^ caret markers are filtered
  - Raise / ExceptionName: lines are filtered
  - Normal ERROR lines still produce tickets
  - In-cycle fingerprint deduplication prevents duplicate tickets from the
    same log line appearing twice in a single scan
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.config import MonitorConfig
from src.swe_team.monitor_agent import MonitorAgent, _fingerprint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(log_dir: str, enabled: bool = True) -> MonitorConfig:
    return MonitorConfig(
        log_directories=[log_dir],
        log_patterns=["ERROR", "CRITICAL", "Traceback", "FAILED"],
        exclude_patterns=["swe_team"],
        scan_interval_minutes=1,
        dedup_window_hours=24,
        enabled=enabled,
    )


def _write_log(path: Path, lines: list[str]) -> Path:
    """Write a .log file with the given lines and a recent mtime."""
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# LOG_ARTIFACT_RE filter tests (#113)
# ---------------------------------------------------------------------------

class TestLogArtifactFilter:
    """Lines that are formatting noise must not become tickets."""

    def test_traceback_header_is_filtered(self, tmp_path):
        log_file = _write_log(
            tmp_path / "app.log",
            [
                "Traceback (most recent call last):",
                '  File "app.py", line 42, in run',
                "    result = do_thing()",
                "ValueError: something went wrong",
            ],
        )
        agent = MonitorAgent(_make_config(str(tmp_path)))
        tickets = agent.scan()
        titles = [t.title for t in tickets]
        # None of the traceback-frame or header lines should become a ticket
        assert all("Traceback (most recent call last)" not in t for t in titles)
        assert all('File "app.py"' not in t for t in titles)

    def test_file_frame_line_is_filtered(self, tmp_path):
        log_file = _write_log(
            tmp_path / "app.log",
            ['  File "/home/user/project/module.py", line 99, in handler'],
        )
        agent = MonitorAgent(_make_config(str(tmp_path)))
        # The line doesn't match any error pattern so no ticket; but confirm the
        # artifact regex itself correctly matches the line.
        assert MonitorAgent._LOG_ARTIFACT_RE.search(
            '  File "/home/user/project/module.py", line 99, in handler'
        )

    def test_whitespace_only_line_is_filtered(self, tmp_path):
        log_file = _write_log(
            tmp_path / "app.log",
            [
                "ERROR: disk full",
                "   ",      # whitespace only
                "",         # empty
            ],
        )
        agent = MonitorAgent(_make_config(str(tmp_path)))
        tickets = agent.scan()
        # Only the real ERROR line should produce a ticket
        assert len(tickets) == 1
        assert "disk full" in tickets[0].title

    def test_caret_marker_line_is_filtered(self, tmp_path):
        log_file = _write_log(
            tmp_path / "app.log",
            [
                "ERROR: syntax problem",
                "        ^^^",   # Python SyntaxError caret indicator
                "   ^^^^   ",    # with surrounding whitespace
            ],
        )
        agent = MonitorAgent(_make_config(str(tmp_path)))
        tickets = agent.scan()
        # Only the real ERROR line should produce a ticket; ^ lines are noise
        assert len(tickets) == 1

    def test_raise_line_is_filtered(self, tmp_path):
        # "raise SomeError" lines sometimes match ERROR via content but should
        # be filtered as artifact lines.
        line = "    raise RuntimeError('boom')"
        assert MonitorAgent._LOG_ARTIFACT_RE.search(line)

    def test_exception_name_line_is_filtered(self, tmp_path):
        line = "    ValueError: unexpected value"
        assert MonitorAgent._LOG_ARTIFACT_RE.search(line)

    def test_real_error_line_still_produces_ticket(self, tmp_path):
        log_file = _write_log(
            tmp_path / "app.log",
            ["2026-03-21 10:00:00 ERROR Cannot connect to database"],
        )
        agent = MonitorAgent(_make_config(str(tmp_path)))
        tickets = agent.scan()
        assert len(tickets) == 1
        assert "Cannot connect to database" in tickets[0].title

    def test_critical_line_still_produces_ticket(self, tmp_path):
        log_file = _write_log(
            tmp_path / "app.log",
            ["CRITICAL system is down"],
        )
        agent = MonitorAgent(_make_config(str(tmp_path)))
        tickets = agent.scan()
        assert len(tickets) == 1


# ---------------------------------------------------------------------------
# In-cycle fingerprint deduplication (#113)
# ---------------------------------------------------------------------------

class TestInCycleDedup:
    """The same error line must produce at most one ticket per scan cycle."""

    def test_duplicate_error_lines_produce_one_ticket(self, tmp_path):
        log_file = _write_log(
            tmp_path / "app.log",
            [
                "ERROR: connection refused",
                "ERROR: connection refused",   # exact duplicate
                "ERROR: connection refused",   # third occurrence
            ],
        )
        agent = MonitorAgent(_make_config(str(tmp_path)))
        tickets = agent.scan()
        assert len(tickets) == 1, (
            f"Expected 1 ticket (deduped), got {len(tickets)}: {[t.title for t in tickets]}"
        )

    def test_distinct_errors_each_produce_a_ticket(self, tmp_path):
        log_file = _write_log(
            tmp_path / "app.log",
            [
                "ERROR: connection refused",
                "ERROR: out of memory",
            ],
        )
        agent = MonitorAgent(_make_config(str(tmp_path)))
        tickets = agent.scan()
        assert len(tickets) == 2

    def test_previously_known_fingerprint_is_skipped(self, tmp_path):
        """A fingerprint already in known_fingerprints must not create a new ticket."""
        log_file = _write_log(
            tmp_path / "app.log",
            ["ERROR: repeated problem"],
        )
        fp = _fingerprint(str(tmp_path / "app.log"), "ERROR: repeated problem")
        agent = MonitorAgent(_make_config(str(tmp_path)), known_fingerprints={fp})
        tickets = agent.scan()
        assert len(tickets) == 0, "Pre-known fingerprint should be skipped"

    def test_new_fingerprint_added_to_known_after_scan(self, tmp_path):
        """After a scan the new fingerprint is added so the next scan skips it."""
        log_file = _write_log(
            tmp_path / "app.log",
            ["ERROR: one time error"],
        )
        agent = MonitorAgent(_make_config(str(tmp_path)))
        tickets_first = agent.scan()
        assert len(tickets_first) == 1

        # Second scan of the same file — fingerprint is now known
        tickets_second = agent.scan()
        assert len(tickets_second) == 0, "Second scan should not re-file the same error"


# ---------------------------------------------------------------------------
# Internal-line filter (pre-existing behaviour, regression guard)
# ---------------------------------------------------------------------------

class TestInternalLineFilter:
    """SWE Squad internal log noise must never become tickets."""

    def test_info_lines_are_skipped(self, tmp_path):
        log_file = _write_log(
            tmp_path / "app.log",
            ["[INFO] SWE Team cycle started — ERROR counts: 0"],
        )
        agent = MonitorAgent(_make_config(str(tmp_path)))
        tickets = agent.scan()
        assert len(tickets) == 0

    def test_stability_gate_lines_are_skipped(self, tmp_path):
        log_file = _write_log(
            tmp_path / "app.log",
            ["Stability gate ERROR verdict: pass"],
        )
        agent = MonitorAgent(_make_config(str(tmp_path)))
        tickets = agent.scan()
        assert len(tickets) == 0


# ---------------------------------------------------------------------------
# Disabled agent
# ---------------------------------------------------------------------------

def test_disabled_agent_returns_empty(tmp_path):
    log_file = _write_log(tmp_path / "app.log", ["ERROR: fatal"])
    config = _make_config(str(tmp_path), enabled=False)
    agent = MonitorAgent(config)
    assert agent.scan() == []
