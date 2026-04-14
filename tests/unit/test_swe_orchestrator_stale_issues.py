"""Tests for detect_stale_github_issues + close_stale_github_issues in swe_orchestrator.py."""

from __future__ import annotations

import json
from subprocess import TimeoutExpired
from unittest.mock import MagicMock, call, patch

import pytest

# Import the orchestrator classes (no network / DB required — we mock everything)
from scripts.ops.swe_orchestrator import (
    Finding,
    OrchestratorActions,
    OrchestratorConfig,
    PipelineIntelligence,
    SupabaseClient,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _proc(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _make_resolved_ticket(issue_num: int, repo: str, status: str = "resolved") -> dict:
    return {
        "ticket_id": f"T-{issue_num}",
        "status": status,
        "metadata": {"github_issue": issue_num, "repo": repo},
        "updated_at": "2026-03-29T10:00:00Z",
    }


def _make_config(auto_fix: bool = True, dry_run: bool = False) -> OrchestratorConfig:
    cfg = OrchestratorConfig()
    cfg.auto_fix = auto_fix
    cfg.dry_run = dry_run
    return cfg


# ---------------------------------------------------------------------------
# PipelineIntelligence.detect_stale_github_issues
# ---------------------------------------------------------------------------

class TestDetectStaleGithubIssues:
    def _make_intel(self, tickets_by_status: dict) -> PipelineIntelligence:
        """Build a PipelineIntelligence with a mocked DB."""
        db = MagicMock(spec=SupabaseClient)

        def _query(filters=""):
            for status, rows in tickets_by_status.items():
                if f"status=eq.{status}" in filters:
                    return rows
            return []

        db.query_tickets.side_effect = _query
        return PipelineIntelligence(_make_config(), db)

    def test_returns_empty_when_no_db(self):
        intel = PipelineIntelligence(_make_config(), db=None)
        findings = intel.detect_stale_github_issues()
        assert findings == []

    def test_returns_empty_when_no_resolved_tickets(self):
        intel = self._make_intel({"resolved": [], "closed": []})
        findings = intel.detect_stale_github_issues()
        assert findings == []

    def test_returns_empty_when_issue_already_closed(self):
        intel = self._make_intel({
            "resolved": [_make_resolved_ticket(42, "owner/repo")],
            "closed": [],
        })
        closed_response = json.dumps({"state": "CLOSED", "number": 42})
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0, stdout=closed_response)
            findings = intel.detect_stale_github_issues()
        assert findings == []

    def test_detects_open_issue_for_resolved_ticket(self):
        intel = self._make_intel({
            "resolved": [_make_resolved_ticket(42, "owner/repo")],
            "closed": [],
        })
        open_response = json.dumps({"state": "open", "number": 42})
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0, stdout=open_response)
            findings = intel.detect_stale_github_issues()

        assert len(findings) == 1
        f = findings[0]
        assert f.category == "stale_github_issues"
        assert f.auto_fixable is True
        assert len(f.evidence["stale_issues"]) == 1
        assert f.evidence["stale_issues"][0]["github_issue"] == 42

    def test_detects_open_issue_for_closed_ticket(self):
        intel = self._make_intel({
            "resolved": [],
            "closed": [_make_resolved_ticket(99, "org/proj", status="closed")],
        })
        open_response = json.dumps({"state": "open", "number": 99})
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0, stdout=open_response)
            findings = intel.detect_stale_github_issues()

        assert len(findings) == 1
        stale = findings[0].evidence["stale_issues"][0]
        assert stale["github_issue"] == 99
        assert stale["repo"] == "org/proj"

    def test_skips_ticket_without_github_issue(self):
        ticket_no_issue = {
            "ticket_id": "T-no-issue",
            "status": "resolved",
            "metadata": {"repo": "owner/repo"},  # no github_issue key
        }
        intel = self._make_intel({"resolved": [ticket_no_issue], "closed": []})
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            findings = intel.detect_stale_github_issues()
        mock_run.assert_not_called()
        assert findings == []

    def test_skips_ticket_without_repo(self):
        ticket_no_repo = {
            "ticket_id": "T-no-repo",
            "status": "resolved",
            "metadata": {"github_issue": 55},  # no repo key
        }
        intel = self._make_intel({"resolved": [ticket_no_repo], "closed": []})
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            findings = intel.detect_stale_github_issues()
        mock_run.assert_not_called()
        assert findings == []

    def test_skips_when_gh_command_fails(self):
        intel = self._make_intel({
            "resolved": [_make_resolved_ticket(42, "owner/repo")],
            "closed": [],
        })
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=1, stderr="not found")
            findings = intel.detect_stale_github_issues()
        assert findings == []

    def test_handles_multiple_stale_issues(self):
        intel = self._make_intel({
            "resolved": [
                _make_resolved_ticket(1, "owner/repo"),
                _make_resolved_ticket(2, "owner/repo"),
            ],
            "closed": [
                _make_resolved_ticket(3, "owner/repo", status="closed"),
            ],
        })
        open_response = json.dumps({"state": "open", "number": 0})
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0, stdout=open_response)
            findings = intel.detect_stale_github_issues()

        assert len(findings) == 1
        assert len(findings[0].evidence["stale_issues"]) == 3

    def test_finding_severity_is_warning(self):
        intel = self._make_intel({
            "resolved": [_make_resolved_ticket(10, "owner/repo")],
            "closed": [],
        })
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0, stdout=json.dumps({"state": "open"}))
            findings = intel.detect_stale_github_issues()

        assert findings[0].severity == "warning"

    def test_metadata_as_json_string_is_parsed(self):
        """Supabase may return metadata as a JSON string; we must parse it."""
        ticket = {
            "ticket_id": "T-str-meta",
            "status": "resolved",
            "metadata": json.dumps({"github_issue": 77, "repo": "org/r"}),
        }
        intel = self._make_intel({"resolved": [ticket], "closed": []})
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=0, stdout=json.dumps({"state": "open"}))
            findings = intel.detect_stale_github_issues()

        assert len(findings) == 1
        assert findings[0].evidence["stale_issues"][0]["github_issue"] == 77


# ---------------------------------------------------------------------------
# OrchestratorActions.close_stale_github_issues
# ---------------------------------------------------------------------------

class TestCloseStaleGithubIssues:
    def _make_actions(self, dry_run: bool = False) -> OrchestratorActions:
        return OrchestratorActions(config=_make_config(), db=None, dry_run=dry_run)

    def _stale_finding(self, stale_issues: list) -> Finding:
        return Finding(
            category="stale_github_issues",
            severity="warning",
            title="test",
            description="test",
            evidence={"stale_issues": stale_issues},
            auto_fixable=True,
        )

    def test_returns_zero_for_empty_stale_list(self):
        actions = self._make_actions()
        finding = self._stale_finding([])
        assert actions.close_stale_github_issues(finding) == 0

    def test_dry_run_returns_zero_disabled(self):
        """Disabled function returns 0 even in dry-run mode with stale issues."""
        actions = self._make_actions(dry_run=True)
        finding = self._stale_finding([
            {"github_issue": 1, "repo": "o/r", "ticket_id": "T-1", "ticket_status": "resolved"},
            {"github_issue": 2, "repo": "o/r", "ticket_id": "T-2", "ticket_status": "closed"},
        ])
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            result = actions.close_stale_github_issues(finding)
        mock_run.assert_not_called()
        assert result == 0

    def test_disabled_does_not_close_issues(self):
        """Function is disabled — must return 0 and never call subprocess."""
        actions = self._make_actions()
        finding = self._stale_finding([
            {"github_issue": 42, "repo": "owner/repo", "ticket_id": "T-42", "ticket_status": "resolved"},
        ])
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            result = actions.close_stale_github_issues(finding)

        assert result == 0
        mock_run.assert_not_called()

    def test_disabled_logs_warning_with_count(self):
        """Disabled function logs a warning including the count of stale issues."""
        actions = self._make_actions()
        finding = self._stale_finding([
            {"github_issue": 9, "repo": "o/r", "ticket_id": "T-9", "ticket_status": "resolved"},
        ])
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            result = actions.close_stale_github_issues(finding)

        mock_run.assert_not_called()
        assert result == 0

    def test_skips_items_without_issue_num(self):
        actions = self._make_actions()
        finding = self._stale_finding([
            {"github_issue": None, "repo": "o/r", "ticket_id": "T-x", "ticket_status": "resolved"},
        ])
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            result = actions.close_stale_github_issues(finding)
        mock_run.assert_not_called()
        assert result == 0

    def test_skips_items_without_repo(self):
        actions = self._make_actions()
        finding = self._stale_finding([
            {"github_issue": 5, "repo": "", "ticket_id": "T-5", "ticket_status": "resolved"},
        ])
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            result = actions.close_stale_github_issues(finding)
        mock_run.assert_not_called()
        assert result == 0

    def test_disabled_returns_zero_regardless_of_issues(self):
        """Disabled function always returns 0, no matter how many stale issues."""
        actions = self._make_actions()
        finding = self._stale_finding([
            {"github_issue": 55, "repo": "o/r", "ticket_id": "T-55", "ticket_status": "resolved"},
        ])
        with patch("scripts.ops.swe_orchestrator.subprocess.run") as mock_run:
            result = actions.close_stale_github_issues(finding)

        assert result == 0
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# execute() dispatches to close_stale_github_issues
# ---------------------------------------------------------------------------

class TestExecuteDispatch:
    def test_execute_dispatches_stale_issues(self):
        actions = OrchestratorActions(config=_make_config(), db=None, dry_run=False)
        finding = Finding(
            category="stale_github_issues",
            severity="warning",
            title="test",
            description="test",
            evidence={"stale_issues": [
                {"github_issue": 1, "repo": "o/r", "ticket_id": "T-1", "ticket_status": "resolved"},
            ]},
            auto_fixable=True,
        )
        with patch.object(actions, "close_stale_github_issues", return_value=1) as mock_close:
            result = actions.execute(finding)
        mock_close.assert_called_once_with(finding)
        assert result is True

    def test_execute_returns_false_when_none_closed(self):
        actions = OrchestratorActions(config=_make_config(), db=None, dry_run=False)
        finding = Finding(
            category="stale_github_issues",
            severity="warning",
            title="test",
            description="test",
            evidence={"stale_issues": []},
            auto_fixable=True,
        )
        result = actions.execute(finding)
        assert result is False
