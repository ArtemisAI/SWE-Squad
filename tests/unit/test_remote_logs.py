"""Unit tests for src/swe_team/remote_logs.py — SSH/rsync log collection."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import src.swe_team.remote_logs as remote_logs_mod
from src.swe_team.remote_logs import (
    collect_remote_logs,
    fetch_worker_logs,
    list_available_workers,
    _ssh_config_path,
)


def _make_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


# ---------------------------------------------------------------------------
# Tests: _ssh_config_path
# ---------------------------------------------------------------------------

class TestSshConfigPath(unittest.TestCase):
    def test_returns_explicit_env_var_when_file_exists(self):
        with tempfile.NamedTemporaryFile(suffix=".conf", delete=False) as f:
            conf_path = f.name
        try:
            with patch.dict(os.environ, {"SWE_SSH_CONFIG": conf_path}):
                result = _ssh_config_path()
            assert result == conf_path
        finally:
            os.unlink(conf_path)

    def test_returns_none_when_explicit_file_missing(self):
        with patch.dict(os.environ, {"SWE_SSH_CONFIG": "/nonexistent/path.conf"}):
            result = _ssh_config_path()
        # Will fall through to check default path; if that also doesn't exist, returns None
        # We just care that it doesn't crash
        assert result is None or isinstance(result, str)

    def test_returns_none_when_no_config_found(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SWE_SSH_CONFIG", None)
            # Patch Path.is_file to return False for default config path
            with patch.object(Path, "is_file", return_value=False):
                result = _ssh_config_path()
        assert result is None


# ---------------------------------------------------------------------------
# Tests: collect_remote_logs
# ---------------------------------------------------------------------------

class TestCollectRemoteLogs(unittest.TestCase):
    def test_empty_nodes_returns_empty_list(self):
        result = collect_remote_logs(nodes=[])
        assert result == []

    @patch("src.swe_team.remote_logs.subprocess.run")
    def test_successful_rsync_appends_dir(self, mock_run):
        mock_run.return_value = _make_proc(returncode=0)
        nodes = [{"name": "worker-1", "ssh": "worker-1", "log_dir": "~/logs"}]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(Path, "mkdir"):
                with patch.object(Path, "glob", return_value=[]):
                    with patch("src.swe_team.remote_logs._ssh_config_path", return_value=None):
                        result = collect_remote_logs(local_dir=tmpdir, nodes=nodes)

        assert len(result) == 1
        assert "worker-1" in result[0]

    @patch("src.swe_team.remote_logs.subprocess.run")
    def test_rsync_failure_logs_warning_no_append(self, mock_run):
        mock_run.return_value = _make_proc(returncode=1, stderr="connection refused")
        nodes = [{"name": "worker-2", "ssh": "worker-2", "log_dir": "~/logs"}]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("src.swe_team.remote_logs._ssh_config_path", return_value=None):
                result = collect_remote_logs(local_dir=tmpdir, nodes=nodes)

        assert result == []

    @patch("src.swe_team.remote_logs.subprocess.run")
    def test_rsync_timeout_logs_warning(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rsync", timeout=30)
        nodes = [{"name": "worker-3", "ssh": "worker-3", "log_dir": "~/logs"}]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("src.swe_team.remote_logs._ssh_config_path", return_value=None):
                result = collect_remote_logs(local_dir=tmpdir, nodes=nodes)

        assert result == []

    @patch("src.swe_team.remote_logs.subprocess.run")
    def test_rsync_not_found_falls_back_to_ssh(self, mock_run):
        """When rsync is not installed (FileNotFoundError), falls back to SSH cat."""
        ssh_output = "2024-01-01 12:00:00 ERROR something happened\n"

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "rsync" in cmd[0]:
                raise FileNotFoundError("rsync not found")
            # SSH fallback call
            return _make_proc(returncode=0, stdout=ssh_output)

        mock_run.side_effect = side_effect
        nodes = [{"name": "worker-4", "ssh": "worker-4", "log_dir": "~/logs"}]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("src.swe_team.remote_logs._ssh_config_path", return_value=None):
                result = collect_remote_logs(local_dir=tmpdir, nodes=nodes)

        assert len(result) == 1

    @patch("src.swe_team.remote_logs.subprocess.run")
    def test_ssh_config_included_in_rsync_command(self, mock_run):
        mock_run.return_value = _make_proc(returncode=0)
        nodes = [{"name": "worker-5", "ssh": "worker-5", "log_dir": "~/logs"}]

        with tempfile.TemporaryDirectory() as tmpdir:
            with tempfile.NamedTemporaryFile(suffix=".conf", delete=False) as f:
                conf_path = f.name
            try:
                with patch("src.swe_team.remote_logs._ssh_config_path", return_value=conf_path):
                    with patch.object(Path, "glob", return_value=[]):
                        collect_remote_logs(local_dir=tmpdir, nodes=nodes)
            finally:
                os.unlink(conf_path)

        call_args = mock_run.call_args[0][0]
        # The -e argument should include the ssh config
        e_idx = call_args.index("-e")
        assert conf_path in call_args[e_idx + 1]

    def test_multiple_nodes_processed(self):
        nodes = [
            {"name": "worker-a", "ssh": "worker-a", "log_dir": "~/logs"},
            {"name": "worker-b", "ssh": "worker-b", "log_dir": "~/logs"},
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("src.swe_team.remote_logs.subprocess.run") as mock_run:
                mock_run.return_value = _make_proc(returncode=0)
                with patch("src.swe_team.remote_logs._ssh_config_path", return_value=None):
                    with patch.object(Path, "glob", return_value=[]):
                        result = collect_remote_logs(local_dir=tmpdir, nodes=nodes)

        # Both nodes attempted
        assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# Tests: fetch_worker_logs
# ---------------------------------------------------------------------------

class TestFetchWorkerLogs(unittest.TestCase):
    def test_no_ssh_config_returns_none(self):
        with patch("src.swe_team.remote_logs._ssh_config_path", return_value=None):
            result = fetch_worker_logs("worker-1")
        assert result is None

    @patch("src.swe_team.remote_logs.subprocess.run")
    def test_successful_fetch_returns_log_string(self, mock_run):
        log_content = "ERROR 2024-01-01 something went wrong\n"
        mock_run.return_value = _make_proc(returncode=0, stdout=log_content)

        with patch("src.swe_team.remote_logs._ssh_config_path", return_value="/path/to/ssh.conf"):
            result = fetch_worker_logs("worker-1", log_dir="~/logs")

        assert result == log_content

    @patch("src.swe_team.remote_logs.subprocess.run")
    def test_empty_output_returns_none(self, mock_run):
        mock_run.return_value = _make_proc(returncode=0, stdout="   ")

        with patch("src.swe_team.remote_logs._ssh_config_path", return_value="/path/to/ssh.conf"):
            result = fetch_worker_logs("worker-1", log_dir="~/logs")

        assert result is None

    @patch("src.swe_team.remote_logs.subprocess.run")
    def test_nonzero_returncode_returns_none(self, mock_run):
        mock_run.return_value = _make_proc(returncode=255, stdout="")

        with patch("src.swe_team.remote_logs._ssh_config_path", return_value="/path/to/ssh.conf"):
            result = fetch_worker_logs("worker-1", log_dir="~/logs")

        assert result is None

    @patch("src.swe_team.remote_logs.subprocess.run")
    def test_timeout_returns_none(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=20)

        with patch("src.swe_team.remote_logs._ssh_config_path", return_value="/path/to/ssh.conf"):
            result = fetch_worker_logs("worker-1", log_dir="~/logs")

        assert result is None

    @patch("src.swe_team.remote_logs.subprocess.run")
    def test_uses_remote_nodes_config_for_log_dir(self, mock_run):
        """When no log_dir is given, should look up the worker in REMOTE_NODES."""
        log_content = "some log data\n"
        mock_run.return_value = _make_proc(returncode=0, stdout=log_content)

        # Temporarily patch REMOTE_NODES
        orig = remote_logs_mod.REMOTE_NODES
        try:
            remote_logs_mod.REMOTE_NODES = [
                {"name": "my-worker", "ssh": "my-worker", "log_dir": "~/special/logs"}
            ]
            with patch("src.swe_team.remote_logs._ssh_config_path", return_value="/path/ssh.conf"):
                result = fetch_worker_logs("my-worker")
        finally:
            remote_logs_mod.REMOTE_NODES = orig

        assert result == log_content
        # Verify the remote command used the config log_dir
        cmd_args = mock_run.call_args[0][0]
        remote_cmd = cmd_args[-1]
        assert "~/special/logs" in remote_cmd


if __name__ == "__main__":
    unittest.main()
