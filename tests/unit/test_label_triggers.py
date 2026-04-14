"""
Tests for /api/github/label-triggers endpoints — GitHub label trigger management.

Covers:
  - GET /api/github/label-triggers — list triggers
  - POST /api/github/label-triggers — create/update trigger
  - DELETE /api/github/label-triggers/<label> — remove trigger
  - POST /api/github/label-triggers/test — test trigger matching
  - CRUD persistence via JSON file
  - Validation (missing label, invalid severity)
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════


def _make_handler(tmp_path):
    """Build a mock DashboardHandler with label-trigger methods wired."""
    from scripts.ops.dashboard_server import DashboardHandler

    handler = MagicMock(spec=DashboardHandler)
    handler.headers = {"Content-Length": "0"}

    # Wire real label-trigger methods
    handler._load_label_triggers = DashboardHandler._load_label_triggers.__get__(handler)
    handler._save_label_triggers = DashboardHandler._save_label_triggers.__get__(handler)
    handler._handle_list_label_triggers = DashboardHandler._handle_list_label_triggers.__get__(handler)
    handler._handle_create_label_trigger = DashboardHandler._handle_create_label_trigger.__get__(handler)
    handler._handle_delete_label_trigger = DashboardHandler._handle_delete_label_trigger.__get__(handler)
    handler._handle_test_label_trigger = DashboardHandler._handle_test_label_trigger.__get__(handler)
    handler._read_post_body = DashboardHandler._read_post_body.__get__(handler)
    handler._json_response = MagicMock()

    return handler


def _set_body(handler, body_dict):
    raw = json.dumps(body_dict).encode()
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)


def _capture(handler):
    """Return a list that collects (data, status) from _json_response calls."""
    results = []

    def _save(data, status=200, **kwargs):
        results.append((data, status))

    handler._json_response = _save
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Tests: GET /api/github/label-triggers (list)
# ══════════════════════════════════════════════════════════════════════════════


class TestListLabelTriggers:
    def test_empty_list_when_no_file(self, tmp_path):
        handler = _make_handler(tmp_path)
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", tmp_path / "triggers.json"):
            handler._handle_list_label_triggers()

        assert len(results) == 1
        data, status = results[0]
        assert status == 200
        assert data == {"triggers": []}

    def test_returns_saved_triggers(self, tmp_path):
        triggers_file = tmp_path / "triggers.json"
        triggers_file.write_text(json.dumps([
            {"label": "swe-squad", "severity": "high", "auto_assign": True, "enabled": True},
            {"label": "auto-triage", "severity": "medium", "auto_assign": False, "enabled": False},
        ]))

        handler = _make_handler(tmp_path)
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_file):
            handler._handle_list_label_triggers()

        data, status = results[0]
        assert status == 200
        assert len(data["triggers"]) == 2
        assert data["triggers"][0]["label"] == "swe-squad"
        assert data["triggers"][1]["label"] == "auto-triage"

    def test_handles_corrupt_file(self, tmp_path):
        triggers_file = tmp_path / "triggers.json"
        triggers_file.write_text("NOT VALID JSON")

        handler = _make_handler(tmp_path)
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_file):
            handler._handle_list_label_triggers()

        data, status = results[0]
        assert status == 200
        assert data == {"triggers": []}


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/github/label-triggers (create/update)
# ══════════════════════════════════════════════════════════════════════════════


class TestCreateLabelTrigger:
    def test_create_new_trigger(self, tmp_path):
        triggers_file = tmp_path / "triggers.json"
        handler = _make_handler(tmp_path)
        results = _capture(handler)

        _set_body(handler, {"label": "swe-squad", "severity": "high", "auto_assign": True, "enabled": True})

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_file):
            handler._handle_create_label_trigger()

        data, status = results[0]
        assert status == 200
        assert data["ok"] is True
        assert data["trigger"]["label"] == "swe-squad"
        assert data["trigger"]["severity"] == "high"

        # Verify persistence
        saved = json.loads(triggers_file.read_text())
        assert len(saved) == 1
        assert saved[0]["label"] == "swe-squad"

    def test_update_existing_trigger(self, tmp_path):
        triggers_file = tmp_path / "triggers.json"
        triggers_file.write_text(json.dumps([
            {"label": "swe-squad", "severity": "medium", "auto_assign": False, "enabled": True},
        ]))

        handler = _make_handler(tmp_path)
        results = _capture(handler)
        _set_body(handler, {"label": "swe-squad", "severity": "critical", "auto_assign": True, "enabled": False})

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_file):
            handler._handle_create_label_trigger()

        data, status = results[0]
        assert status == 200
        assert data["trigger"]["severity"] == "critical"
        assert data["trigger"]["auto_assign"] is True
        assert data["trigger"]["enabled"] is False

        # Still only one trigger
        saved = json.loads(triggers_file.read_text())
        assert len(saved) == 1
        assert saved[0]["severity"] == "critical"

    def test_missing_label_returns_400(self, tmp_path):
        handler = _make_handler(tmp_path)
        results = _capture(handler)
        _set_body(handler, {"severity": "high"})

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", tmp_path / "triggers.json"):
            handler._handle_create_label_trigger()

        data, status = results[0]
        assert status == 400
        assert "label" in data.get("error", "").lower()

    def test_empty_label_returns_400(self, tmp_path):
        handler = _make_handler(tmp_path)
        results = _capture(handler)
        _set_body(handler, {"label": "  ", "severity": "high"})

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", tmp_path / "triggers.json"):
            handler._handle_create_label_trigger()

        data, status = results[0]
        assert status == 400

    def test_invalid_severity_returns_400(self, tmp_path):
        handler = _make_handler(tmp_path)
        results = _capture(handler)
        _set_body(handler, {"label": "test", "severity": "ultra"})

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", tmp_path / "triggers.json"):
            handler._handle_create_label_trigger()

        data, status = results[0]
        assert status == 400
        assert "severity" in data.get("error", "").lower()

    def test_label_normalized_to_lowercase(self, tmp_path):
        triggers_file = tmp_path / "triggers.json"
        handler = _make_handler(tmp_path)
        results = _capture(handler)
        _set_body(handler, {"label": "  SWE-Squad  ", "severity": "high"})

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_file):
            handler._handle_create_label_trigger()

        data, status = results[0]
        assert data["trigger"]["label"] == "swe-squad"

    def test_defaults_severity_to_medium(self, tmp_path):
        triggers_file = tmp_path / "triggers.json"
        handler = _make_handler(tmp_path)
        results = _capture(handler)
        _set_body(handler, {"label": "test-label"})

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_file):
            handler._handle_create_label_trigger()

        data, status = results[0]
        assert status == 200
        assert data["trigger"]["severity"] == "medium"
        assert data["trigger"]["auto_assign"] is True
        assert data["trigger"]["enabled"] is True


# ══════════════════════════════════════════════════════════════════════════════
# Tests: DELETE /api/github/label-triggers/<label>
# ══════════════════════════════════════════════════════════════════════════════


class TestDeleteLabelTrigger:
    def test_delete_existing_trigger(self, tmp_path):
        triggers_file = tmp_path / "triggers.json"
        triggers_file.write_text(json.dumps([
            {"label": "swe-squad", "severity": "high", "auto_assign": True, "enabled": True},
            {"label": "auto-triage", "severity": "medium", "auto_assign": False, "enabled": True},
        ]))

        handler = _make_handler(tmp_path)
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_file):
            handler._handle_delete_label_trigger("swe-squad")

        data, status = results[0]
        assert status == 200
        assert data["ok"] is True
        assert data["deleted"] == "swe-squad"

        # Verify file updated
        saved = json.loads(triggers_file.read_text())
        assert len(saved) == 1
        assert saved[0]["label"] == "auto-triage"

    def test_delete_nonexistent_returns_404(self, tmp_path):
        triggers_file = tmp_path / "triggers.json"
        triggers_file.write_text(json.dumps([]))

        handler = _make_handler(tmp_path)
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_file):
            handler._handle_delete_label_trigger("nonexistent")

        data, status = results[0]
        assert status == 404

    def test_delete_normalizes_label(self, tmp_path):
        triggers_file = tmp_path / "triggers.json"
        triggers_file.write_text(json.dumps([
            {"label": "swe-squad", "severity": "high", "auto_assign": True, "enabled": True},
        ]))

        handler = _make_handler(tmp_path)
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_file):
            handler._handle_delete_label_trigger("  SWE-Squad  ")

        data, status = results[0]
        assert status == 200
        assert data["deleted"] == "swe-squad"


# ══════════════════════════════════════════════════════════════════════════════
# Tests: POST /api/github/label-triggers/test
# ══════════════════════════════════════════════════════════════════════════════


class TestTestLabelTrigger:
    def test_missing_label_returns_400(self, tmp_path):
        handler = _make_handler(tmp_path)
        results = _capture(handler)
        _set_body(handler, {})

        handler._handle_test_label_trigger()

        data, status = results[0]
        assert status == 400
        assert "label" in data.get("error", "").lower()

    def test_no_repos_configured(self, tmp_path):
        handler = _make_handler(tmp_path)
        results = _capture(handler)
        _set_body(handler, {"label": "swe-squad"})

        mock_cfg = MagicMock()
        mock_cfg.raw = {"repos": []}

        with patch("scripts.ops.dashboard_server.load_config", return_value=mock_cfg):
            handler._handle_test_label_trigger()

        data, status = results[0]
        assert status == 200
        assert data["matching_issues"] == 0
        assert data["issues"] == []

    def test_matching_issues_returned(self, tmp_path):
        handler = _make_handler(tmp_path)
        results = _capture(handler)
        _set_body(handler, {"label": "bug"})

        mock_cfg = MagicMock()
        mock_cfg.raw = {"repos": [{"name": "owner/repo"}]}

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps([
            {"number": 42, "title": "Fix login bug"},
            {"number": 99, "title": "Another bug"},
        ])

        with patch("scripts.ops.dashboard_server.load_config", return_value=mock_cfg), \
             patch("subprocess.run", return_value=mock_result):
            handler._handle_test_label_trigger()

        data, status = results[0]
        assert status == 200
        assert data["matching_issues"] == 2
        assert data["issues"][0]["number"] == 42
        assert data["issues"][0]["repo"] == "owner/repo"

    def test_gh_cli_failure_returns_empty(self, tmp_path):
        handler = _make_handler(tmp_path)
        results = _capture(handler)
        _set_body(handler, {"label": "bug"})

        mock_cfg = MagicMock()
        mock_cfg.raw = {"repos": [{"name": "owner/repo"}]}

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("scripts.ops.dashboard_server.load_config", return_value=mock_cfg), \
             patch("subprocess.run", return_value=mock_result):
            handler._handle_test_label_trigger()

        data, status = results[0]
        assert status == 200
        assert data["matching_issues"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Persistence round-trip
# ══════════════════════════════════════════════════════════════════════════════


class TestLabelTriggerPersistence:
    def test_create_list_delete_roundtrip(self, tmp_path):
        triggers_file = tmp_path / "triggers.json"

        handler = _make_handler(tmp_path)

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_file):
            # Create two triggers
            results1 = _capture(handler)
            _set_body(handler, {"label": "swe-squad", "severity": "high"})
            handler._handle_create_label_trigger()
            assert results1[0][1] == 200

            results2 = _capture(handler)
            _set_body(handler, {"label": "auto-triage", "severity": "low"})
            handler._handle_create_label_trigger()
            assert results2[0][1] == 200

            # List
            results3 = _capture(handler)
            handler._handle_list_label_triggers()
            assert len(results3[0][0]["triggers"]) == 2

            # Delete one
            results4 = _capture(handler)
            handler._handle_delete_label_trigger("swe-squad")
            assert results4[0][1] == 200

            # List again — should be 1
            results5 = _capture(handler)
            handler._handle_list_label_triggers()
            assert len(results5[0][0]["triggers"]) == 1
            assert results5[0][0]["triggers"][0]["label"] == "auto-triage"

    def test_non_list_json_returns_empty(self, tmp_path):
        """If the JSON file contains an object instead of a list, return empty."""
        triggers_file = tmp_path / "triggers.json"
        triggers_file.write_text('{"not": "a list"}')

        handler = _make_handler(tmp_path)
        results = _capture(handler)

        with patch("scripts.ops.dashboard_server._LABEL_TRIGGERS_PATH", triggers_file):
            handler._handle_list_label_triggers()

        data, status = results[0]
        assert status == 200
        assert data["triggers"] == []
