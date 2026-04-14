"""Unit tests for the LogQueryProvider protocol and LocalFileProvider."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from src.swe_team.providers.log_query.base import LogEntry, LogQueryProvider
from src.swe_team.providers.log_query.local import LocalFileProvider
from src.swe_team.providers.log_query import create_log_query_provider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def log_dir(tmp_path: Path) -> Path:
    """Create a temp directory with sample log files."""
    # Plain text log
    text_log = tmp_path / "app.log"
    text_log.write_text(
        "2025-01-15 10:00:00,123 [ERROR] Database connection failed\n"
        "2025-01-15 10:00:01,456 [INFO] Retrying connection\n"
        "2025-01-15 10:00:02,789 [WARNING] Slow query detected\n"
        "[ERROR] Something broke with no timestamp\n"
        "DEBUG: low-level trace message\n"
        "Just a plain line with no format\n"
    )

    # JSON log
    json_log = tmp_path / "service.log"
    json_log.write_text(
        json.dumps({"timestamp": "2025-01-15T10:00:03", "level": "error", "message": "Disk full", "source": "storage", "disk": "/dev/sda1"}) + "\n"
        + json.dumps({"timestamp": "2025-01-15T10:00:04", "level": "info", "message": "Cleanup started", "service": "janitor"}) + "\n"
    )

    return tmp_path


@pytest.fixture()
def provider(log_dir: Path) -> LocalFileProvider:
    """Create a LocalFileProvider pointing at the temp log dir."""
    return LocalFileProvider({
        "log_directories": [str(log_dir)],
        "remote_collection": False,
    })


# ---------------------------------------------------------------------------
# LogEntry dataclass
# ---------------------------------------------------------------------------

class TestLogEntry:
    def test_defaults(self):
        entry = LogEntry(timestamp="now", level="INFO", message="hi", source="test")
        assert entry.metadata == {}

    def test_with_metadata(self):
        entry = LogEntry(timestamp="t", level="ERROR", message="m", source="s", metadata={"k": "v"})
        assert entry.metadata == {"k": "v"}


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_local_provider_is_protocol_instance(self, provider: LocalFileProvider):
        assert isinstance(provider, LogQueryProvider)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestFactory:
    def test_none_config_returns_none(self):
        assert create_log_query_provider(None) is None

    def test_local_provider(self, log_dir: Path):
        p = create_log_query_provider({
            "provider": "local",
            "log_directories": [str(log_dir)],
        })
        assert isinstance(p, LocalFileProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown log_query provider"):
            create_log_query_provider({"provider": "elasticsearch"})


# ---------------------------------------------------------------------------
# LocalFileProvider — query_logs
# ---------------------------------------------------------------------------

class TestQueryLogs:
    def test_returns_all_entries(self, provider: LocalFileProvider):
        entries = provider.query_logs(since_minutes=999999)
        # 6 lines in text log + 2 in JSON log = 8
        assert len(entries) == 8

    def test_filter_by_level(self, provider: LocalFileProvider):
        entries = provider.query_logs(level="ERROR", since_minutes=999999)
        assert all(e.level == "ERROR" for e in entries)
        assert len(entries) == 3  # 2 text ERROR + 1 JSON error

    def test_filter_by_service(self, provider: LocalFileProvider):
        entries = provider.query_logs(service="app", since_minutes=999999)
        assert len(entries) == 6  # from app.log (filename stem)

    def test_limit(self, provider: LocalFileProvider):
        entries = provider.query_logs(limit=3, since_minutes=999999)
        assert len(entries) == 3

    def test_sorted_newest_first(self, provider: LocalFileProvider):
        entries = provider.query_logs(since_minutes=999999)
        timestamps = [e.timestamp for e in entries if e.timestamp]
        assert timestamps == sorted(timestamps, reverse=True)


# ---------------------------------------------------------------------------
# LocalFileProvider — search_logs
# ---------------------------------------------------------------------------

class TestSearchLogs:
    def test_substring_search(self, provider: LocalFileProvider):
        entries = provider.search_logs("connection", since_minutes=999999)
        assert len(entries) == 2  # "Database connection failed" + "Retrying connection"

    def test_regex_search(self, provider: LocalFileProvider):
        entries = provider.search_logs(r"Disk\s+full", since_minutes=999999)
        assert len(entries) == 1
        assert "Disk full" in entries[0].message

    def test_search_with_service_filter(self, provider: LocalFileProvider):
        entries = provider.search_logs("started", service="janitor", since_minutes=999999)
        assert len(entries) == 1
        assert entries[0].message == "Cleanup started"

    def test_invalid_regex_falls_back_to_substring(self, provider: LocalFileProvider):
        # Invalid regex — should not raise, falls back to substring
        entries = provider.search_logs("[invalid", since_minutes=999999)
        assert isinstance(entries, list)


# ---------------------------------------------------------------------------
# LocalFileProvider — health_check
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_healthy_when_dir_exists(self, provider: LocalFileProvider):
        assert provider.health_check() is True

    def test_unhealthy_when_no_dirs(self):
        p = LocalFileProvider({"log_directories": ["/nonexistent/path/xyz"]})
        assert p.health_check() is False

    def test_unhealthy_when_empty_config(self):
        p = LocalFileProvider({})
        assert p.health_check() is False


# ---------------------------------------------------------------------------
# LocalFileProvider — parsing
# ---------------------------------------------------------------------------

class TestParsing:
    def test_json_log_entry(self, provider: LocalFileProvider):
        entries = provider.query_logs(service="storage", since_minutes=999999)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.level == "ERROR"
        assert entry.message == "Disk full"
        assert entry.source == "storage"
        assert entry.metadata.get("disk") == "/dev/sda1"

    def test_json_service_field(self, provider: LocalFileProvider):
        entries = provider.query_logs(service="janitor", since_minutes=999999)
        assert len(entries) == 1
        assert entries[0].source == "janitor"

    def test_bracket_level_format(self, provider: LocalFileProvider):
        entries = provider.search_logs("Something broke", since_minutes=999999)
        assert len(entries) == 1
        assert entries[0].level == "ERROR"
        assert entries[0].timestamp == ""

    def test_colon_level_format(self, provider: LocalFileProvider):
        entries = provider.search_logs("low-level trace", since_minutes=999999)
        assert len(entries) == 1
        assert entries[0].level == "DEBUG"

    def test_plain_line_becomes_info(self, provider: LocalFileProvider):
        entries = provider.search_logs("plain line with no format", since_minutes=999999)
        assert len(entries) == 1
        assert entries[0].level == "INFO"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_directory(self, tmp_path: Path):
        p = LocalFileProvider({"log_directories": [str(tmp_path)]})
        assert p.query_logs(since_minutes=999999) == []

    def test_nonexistent_directory(self):
        p = LocalFileProvider({"log_directories": ["/does/not/exist"]})
        assert p.query_logs(since_minutes=999999) == []

    def test_empty_log_file(self, tmp_path: Path):
        (tmp_path / "empty.log").write_text("")
        p = LocalFileProvider({"log_directories": [str(tmp_path)]})
        assert p.query_logs(since_minutes=999999) == []

    def test_since_minutes_filters_old_files(self, tmp_path: Path):
        log_file = tmp_path / "old.log"
        log_file.write_text("[ERROR] Old error\n")
        # Set mtime to 2 hours ago
        old_time = os.path.getmtime(str(log_file)) - 7200
        os.utime(str(log_file), (old_time, old_time))
        p = LocalFileProvider({"log_directories": [str(tmp_path)]})
        assert p.query_logs(since_minutes=60) == []
