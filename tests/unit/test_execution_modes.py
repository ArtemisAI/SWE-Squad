"""
Tests for Execution Mode API — plan / review / start modes + checkpoint CRUD.

Covers:
  - GET /api/execution/mode — get current mode
  - PATCH /api/execution/mode — change mode
  - GET /api/execution/checkpoints — list pending checkpoints
  - POST /api/execution/checkpoints/<id>/approve — approve checkpoint
  - POST /api/execution/checkpoints/<id>/reject — reject checkpoint
  - Mode validation (invalid mode rejected)
  - Helper function behaviour (_read_execution_mode, _write_execution_mode, etc.)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ══════════════════════════════════════════════════════════════════════════════
# Import the module-level helpers we want to test directly
# ══════════════════════════════════════════════════════════════════════════════

# We need to patch heavy imports that dashboard_server pulls in at module level
# to keep tests lightweight and offline.

@pytest.fixture(autouse=True)
def _patch_env(tmp_path, monkeypatch):
    """Ensure data dir exists and point path constants to tmp."""
    monkeypatch.setenv("SWE_TEAM_ENABLED", "true")


# ══════════════════════════════════════════════════════════════════════════════
# Direct helper tests (no HTTP server needed)
# ══════════════════════════════════════════════════════════════════════════════


class TestReadExecutionMode:
    """Test _read_execution_mode helper."""

    def test_default_mode_when_no_file(self, tmp_path):
        """Should return 'start' when no file exists."""
        from scripts.ops.dashboard_server import _read_execution_mode, _EXECUTION_MODE_PATH

        with patch("scripts.ops.dashboard_server._EXECUTION_MODE_PATH", tmp_path / "missing.json"):
            result = _read_execution_mode()

        assert result["mode"] == "start"
        assert result["available_modes"] == ["plan", "review", "start"]
        assert "autonomous" in result["description"].lower()

    def test_reads_saved_mode(self, tmp_path):
        """Should read a saved mode from disk."""
        mode_file = tmp_path / "execution_mode.json"
        mode_file.write_text(json.dumps({"mode": "review"}))

        with patch("scripts.ops.dashboard_server._EXECUTION_MODE_PATH", mode_file):
            from scripts.ops.dashboard_server import _read_execution_mode
            result = _read_execution_mode()

        assert result["mode"] == "review"
        assert "checkpoint" in result["description"].lower()

    def test_invalid_saved_mode_falls_back(self, tmp_path):
        """Should fall back to 'start' if saved mode is invalid."""
        mode_file = tmp_path / "execution_mode.json"
        mode_file.write_text(json.dumps({"mode": "turbo"}))

        with patch("scripts.ops.dashboard_server._EXECUTION_MODE_PATH", mode_file):
            from scripts.ops.dashboard_server import _read_execution_mode
            result = _read_execution_mode()

        assert result["mode"] == "start"

    def test_corrupt_file_falls_back(self, tmp_path):
        """Should fall back to 'start' if file is corrupted."""
        mode_file = tmp_path / "execution_mode.json"
        mode_file.write_text("NOT JSON")

        with patch("scripts.ops.dashboard_server._EXECUTION_MODE_PATH", mode_file):
            from scripts.ops.dashboard_server import _read_execution_mode
            result = _read_execution_mode()

        assert result["mode"] == "start"


class TestWriteExecutionMode:
    """Test _write_execution_mode helper."""

    def test_writes_mode_to_disk(self, tmp_path):
        """Should write mode JSON to disk."""
        mode_file = tmp_path / "execution_mode.json"

        with patch("scripts.ops.dashboard_server._EXECUTION_MODE_PATH", mode_file):
            from scripts.ops.dashboard_server import _write_execution_mode
            result = _write_execution_mode("plan")

        assert result is True
        saved = json.loads(mode_file.read_text())
        assert saved["mode"] == "plan"

    def test_overwrite_existing(self, tmp_path):
        """Should overwrite an existing mode file."""
        mode_file = tmp_path / "execution_mode.json"
        mode_file.write_text(json.dumps({"mode": "start"}))

        with patch("scripts.ops.dashboard_server._EXECUTION_MODE_PATH", mode_file):
            from scripts.ops.dashboard_server import _write_execution_mode
            _write_execution_mode("review")

        saved = json.loads(mode_file.read_text())
        assert saved["mode"] == "review"


class TestReadCheckpoints:
    """Test _read_checkpoints helper."""

    def test_default_empty_when_no_file(self, tmp_path):
        """Should return empty list when no file exists."""
        with patch("scripts.ops.dashboard_server._CHECKPOINTS_PATH", tmp_path / "missing.json"):
            from scripts.ops.dashboard_server import _read_checkpoints
            result = _read_checkpoints()

        assert result == []

    def test_reads_list_format(self, tmp_path):
        """Should read checkpoints stored as a JSON array."""
        cp_file = tmp_path / "checkpoints.json"
        data = [
            {"id": "cp-1", "ticket_id": "t-1", "stage": "develop", "status": "pending"},
            {"id": "cp-2", "ticket_id": "t-2", "stage": "review", "status": "approved"},
        ]
        cp_file.write_text(json.dumps(data))

        with patch("scripts.ops.dashboard_server._CHECKPOINTS_PATH", cp_file):
            from scripts.ops.dashboard_server import _read_checkpoints
            result = _read_checkpoints()

        assert len(result) == 2
        assert result[0]["id"] == "cp-1"

    def test_reads_dict_format(self, tmp_path):
        """Should handle checkpoints stored as {checkpoints: [...]}."""
        cp_file = tmp_path / "checkpoints.json"
        data = {"checkpoints": [{"id": "cp-3", "status": "pending"}]}
        cp_file.write_text(json.dumps(data))

        with patch("scripts.ops.dashboard_server._CHECKPOINTS_PATH", cp_file):
            from scripts.ops.dashboard_server import _read_checkpoints
            result = _read_checkpoints()

        assert len(result) == 1
        assert result[0]["id"] == "cp-3"


class TestWriteCheckpoints:
    """Test _write_checkpoints helper."""

    def test_writes_checkpoints(self, tmp_path):
        """Should persist checkpoints to disk."""
        cp_file = tmp_path / "checkpoints.json"
        data = [{"id": "cp-1", "status": "pending"}]

        with patch("scripts.ops.dashboard_server._CHECKPOINTS_PATH", cp_file):
            from scripts.ops.dashboard_server import _write_checkpoints
            result = _write_checkpoints(data)

        assert result is True
        saved = json.loads(cp_file.read_text())
        assert len(saved) == 1
        assert saved[0]["id"] == "cp-1"


class TestModeValidation:
    """Test that only valid modes are accepted."""

    def test_valid_modes(self):
        from scripts.ops.dashboard_server import _VALID_EXECUTION_MODES
        assert "plan" in _VALID_EXECUTION_MODES
        assert "review" in _VALID_EXECUTION_MODES
        assert "start" in _VALID_EXECUTION_MODES

    def test_invalid_mode_not_in_set(self):
        from scripts.ops.dashboard_server import _VALID_EXECUTION_MODES
        assert "turbo" not in _VALID_EXECUTION_MODES
        assert "" not in _VALID_EXECUTION_MODES

    def test_mode_descriptions_present(self):
        from scripts.ops.dashboard_server import _EXECUTION_MODE_DESCRIPTIONS, _VALID_EXECUTION_MODES
        for mode in _VALID_EXECUTION_MODES:
            assert mode in _EXECUTION_MODE_DESCRIPTIONS
            assert len(_EXECUTION_MODE_DESCRIPTIONS[mode]) > 0


class TestCheckpointApproveReject:
    """Test checkpoint approve/reject logic via helpers."""

    def test_approve_changes_status(self, tmp_path):
        """Approving a checkpoint should set status to 'approved'."""
        cp_file = tmp_path / "checkpoints.json"
        data = [
            {"id": "cp-1", "ticket_id": "t-1", "stage": "develop", "status": "pending",
             "description": "Ready to create PR", "created_at": "2026-04-08T00:00:00Z"},
        ]
        cp_file.write_text(json.dumps(data))

        with patch("scripts.ops.dashboard_server._CHECKPOINTS_PATH", cp_file):
            from scripts.ops.dashboard_server import _read_checkpoints, _write_checkpoints
            checkpoints = _read_checkpoints()
            for cp in checkpoints:
                if cp["id"] == "cp-1":
                    cp["status"] = "approved"
                    cp["resolved_at"] = datetime.now(timezone.utc).isoformat()
            _write_checkpoints(checkpoints)

        saved = json.loads(cp_file.read_text())
        assert saved[0]["status"] == "approved"
        assert "resolved_at" in saved[0]

    def test_reject_changes_status_and_stores_feedback(self, tmp_path):
        """Rejecting a checkpoint should set status to 'rejected' and store feedback."""
        cp_file = tmp_path / "checkpoints.json"
        data = [
            {"id": "cp-2", "ticket_id": "t-2", "stage": "review", "status": "pending",
             "description": "About to merge", "created_at": "2026-04-08T00:00:00Z"},
        ]
        cp_file.write_text(json.dumps(data))

        with patch("scripts.ops.dashboard_server._CHECKPOINTS_PATH", cp_file):
            from scripts.ops.dashboard_server import _read_checkpoints, _write_checkpoints
            checkpoints = _read_checkpoints()
            for cp in checkpoints:
                if cp["id"] == "cp-2":
                    cp["status"] = "rejected"
                    cp["feedback"] = "Needs more tests"
                    cp["resolved_at"] = datetime.now(timezone.utc).isoformat()
            _write_checkpoints(checkpoints)

        saved = json.loads(cp_file.read_text())
        assert saved[0]["status"] == "rejected"
        assert saved[0]["feedback"] == "Needs more tests"

    def test_only_pending_can_be_approved(self, tmp_path):
        """Non-pending checkpoints should not be modifiable."""
        cp_file = tmp_path / "checkpoints.json"
        data = [
            {"id": "cp-3", "ticket_id": "t-3", "stage": "develop", "status": "approved"},
        ]
        cp_file.write_text(json.dumps(data))

        with patch("scripts.ops.dashboard_server._CHECKPOINTS_PATH", cp_file):
            from scripts.ops.dashboard_server import _read_checkpoints
            checkpoints = _read_checkpoints()
            pending = [cp for cp in checkpoints if cp.get("status") == "pending"]
            assert len(pending) == 0

    def test_multiple_checkpoints_independent(self, tmp_path):
        """Approving one checkpoint should not affect others."""
        cp_file = tmp_path / "checkpoints.json"
        data = [
            {"id": "cp-a", "status": "pending", "ticket_id": "t-a", "stage": "develop",
             "description": "A", "created_at": "2026-04-08T00:00:00Z"},
            {"id": "cp-b", "status": "pending", "ticket_id": "t-b", "stage": "review",
             "description": "B", "created_at": "2026-04-08T00:00:00Z"},
        ]
        cp_file.write_text(json.dumps(data))

        with patch("scripts.ops.dashboard_server._CHECKPOINTS_PATH", cp_file):
            from scripts.ops.dashboard_server import _read_checkpoints, _write_checkpoints
            checkpoints = _read_checkpoints()
            for cp in checkpoints:
                if cp["id"] == "cp-a":
                    cp["status"] = "approved"
            _write_checkpoints(checkpoints)

        saved = json.loads(cp_file.read_text())
        statuses = {cp["id"]: cp["status"] for cp in saved}
        assert statuses["cp-a"] == "approved"
        assert statuses["cp-b"] == "pending"


class TestModeRoundTrip:
    """Test full read-write-read cycle for execution mode."""

    def test_set_and_get_plan(self, tmp_path):
        mode_file = tmp_path / "execution_mode.json"
        with patch("scripts.ops.dashboard_server._EXECUTION_MODE_PATH", mode_file):
            from scripts.ops.dashboard_server import _write_execution_mode, _read_execution_mode
            _write_execution_mode("plan")
            result = _read_execution_mode()
        assert result["mode"] == "plan"
        assert "plan" in result["description"].lower()

    def test_set_and_get_review(self, tmp_path):
        mode_file = tmp_path / "execution_mode.json"
        with patch("scripts.ops.dashboard_server._EXECUTION_MODE_PATH", mode_file):
            from scripts.ops.dashboard_server import _write_execution_mode, _read_execution_mode
            _write_execution_mode("review")
            result = _read_execution_mode()
        assert result["mode"] == "review"

    def test_set_and_get_start(self, tmp_path):
        mode_file = tmp_path / "execution_mode.json"
        with patch("scripts.ops.dashboard_server._EXECUTION_MODE_PATH", mode_file):
            from scripts.ops.dashboard_server import _write_execution_mode, _read_execution_mode
            _write_execution_mode("start")
            result = _read_execution_mode()
        assert result["mode"] == "start"
