"""Unit tests for src.swe_team.github_invites."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call, patch

import pytest

from src.swe_team.github_invites import (
    accept_pending_invites,
    accept_pending_invites_from_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_invite(
    invite_id: int = 1,
    repo_full: str = "your-org/SWE-Sandbox",
    inviter_login: str = "alice",
    created_at: str = "2026-03-28T10:00:00Z",
) -> dict:
    owner = repo_full.split("/")[0] if "/" in repo_full else repo_full
    return {
        "id": invite_id,
        "repository": {
            "full_name": repo_full,
            "owner": {"login": owner},
        },
        "inviter": {"login": inviter_login},
        "created_at": created_at,
    }


def _proc(stdout: str = "[]", returncode: int = 0, stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# Tests: accept all when no allowlist
# ---------------------------------------------------------------------------

class TestAcceptAll:
    def test_accepts_all_pending_invites(self):
        invites = [
            _make_invite(1, "your-org/SWE-Sandbox", "alice"),
            _make_invite(2, "OtherOrg/some-repo", "bob"),
        ]

        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            # First call: list; subsequent calls: PATCH per invite
            mock_run.side_effect = [
                _proc(stdout=json.dumps(invites)),  # list
                _proc(stdout="", returncode=0),     # accept invite 1
                _proc(stdout="", returncode=0),     # accept invite 2
            ]

            result = accept_pending_invites("swe-squad-alpha")

        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[0]["repo"] == "your-org/SWE-Sandbox"
        assert result[0]["inviter"] == "alice"
        assert result[1]["id"] == 2
        assert result[1]["repo"] == "OtherOrg/some-repo"

    def test_returns_empty_when_no_invites(self):
        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.return_value = _proc(stdout="[]")
            result = accept_pending_invites("swe-squad-alpha")

        assert result == []
        # Only one call (list) — no PATCH calls
        assert mock_run.call_count == 1

    def test_returns_empty_on_gh_api_failure(self):
        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.return_value = _proc(returncode=1, stderr="not logged in")
            result = accept_pending_invites("swe-squad-alpha")

        assert result == []


# ---------------------------------------------------------------------------
# Tests: allowlist filtering
# ---------------------------------------------------------------------------

class TestAllowlistFiltering:
    def test_accepts_only_allowlisted_org(self):
        invites = [
            _make_invite(1, "your-org/SWE-Sandbox", "alice"),
            _make_invite(2, "OtherOrg/some-repo", "bob"),
            _make_invite(3, "your-org/another-repo", "carol"),
        ]

        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _proc(stdout=json.dumps(invites)),  # list
                _proc(stdout="", returncode=0),     # accept invite 1
                _proc(stdout="", returncode=0),     # accept invite 3
            ]

            result = accept_pending_invites(
                "swe-squad-alpha",
                allowlist=["your-org"],
            )

        assert len(result) == 2
        repos = {r["repo"] for r in result}
        assert repos == {"your-org/SWE-Sandbox", "your-org/another-repo"}
        # OtherOrg/some-repo must not appear
        owners = {r["owner"] for r in result}
        assert "OtherOrg" not in owners

    def test_allowlist_is_case_insensitive(self):
        invites = [_make_invite(1, "your-org/SWE-Sandbox", "alice")]

        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _proc(stdout=json.dumps(invites)),
                _proc(stdout="", returncode=0),
            ]

            result = accept_pending_invites(
                "swe-squad-alpha",
                allowlist=["your-org"],   # lowercase
            )

        assert len(result) == 1

    def test_empty_allowlist_accepts_all(self):
        invites = [
            _make_invite(1, "your-org/SWE-Sandbox"),
            _make_invite(2, "OtherOrg/repo"),
        ]

        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _proc(stdout=json.dumps(invites)),
                _proc(stdout="", returncode=0),
                _proc(stdout="", returncode=0),
            ]

            result = accept_pending_invites("swe-squad-alpha", allowlist=[])

        assert len(result) == 2

    def test_no_invites_accepted_when_none_match_allowlist(self):
        invites = [_make_invite(1, "OtherOrg/repo", "bob")]

        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _proc(stdout=json.dumps(invites)),
                # No PATCH calls expected
            ]

            result = accept_pending_invites(
                "swe-squad-alpha",
                allowlist=["your-org"],
            )

        assert result == []
        # Only the list call — no PATCH
        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# Tests: dry_run mode
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_returns_invites_without_patching(self):
        invites = [
            _make_invite(1, "your-org/SWE-Sandbox"),
            _make_invite(2, "your-org/other"),
        ]

        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.return_value = _proc(stdout=json.dumps(invites))

            result = accept_pending_invites(
                "swe-squad-alpha",
                dry_run=True,
            )

        # Only the list call — no PATCH calls in dry_run
        assert mock_run.call_count == 1
        assert len(result) == 2

    def test_dry_run_with_allowlist_filters_but_does_not_patch(self):
        invites = [
            _make_invite(1, "your-org/SWE-Sandbox"),
            _make_invite(2, "OtherOrg/repo"),
        ]

        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.return_value = _proc(stdout=json.dumps(invites))

            result = accept_pending_invites(
                "swe-squad-alpha",
                allowlist=["your-org"],
                dry_run=True,
            )

        assert mock_run.call_count == 1   # list only
        assert len(result) == 1
        assert result[0]["owner"] == "your-org"


# ---------------------------------------------------------------------------
# Tests: accept_pending_invites_from_config
# ---------------------------------------------------------------------------

class TestFromConfig:
    def test_disabled_by_default(self):
        """auto_accept_invites defaults to False — nothing should be called."""
        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            result = accept_pending_invites_from_config({})

        assert result == []
        mock_run.assert_not_called()

    def test_disabled_explicitly(self):
        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            result = accept_pending_invites_from_config(
                {"auto_accept_invites": False, "github_account": "swe-squad-alpha"}
            )

        assert result == []
        mock_run.assert_not_called()

    def test_enabled_calls_accept(self):
        invites = [_make_invite(1, "your-org/SWE-Sandbox")]

        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _proc(stdout=json.dumps(invites)),
                _proc(stdout="", returncode=0),
            ]

            result = accept_pending_invites_from_config(
                {
                    "auto_accept_invites": True,
                    "github_account": "swe-squad-alpha",
                    "invite_allowlist": ["your-org"],
                }
            )

        assert len(result) == 1
        assert result[0]["repo"] == "your-org/SWE-Sandbox"

    def test_enabled_no_allowlist_accepts_all(self):
        invites = [
            _make_invite(1, "your-org/SWE-Sandbox"),
            _make_invite(2, "OtherOrg/repo"),
        ]

        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _proc(stdout=json.dumps(invites)),
                _proc(stdout="", returncode=0),
                _proc(stdout="", returncode=0),
            ]

            result = accept_pending_invites_from_config(
                {"auto_accept_invites": True, "github_account": "swe-squad-alpha"}
            )

        assert len(result) == 2

    def test_enabled_dry_run_no_patch_calls(self):
        invites = [_make_invite(1, "your-org/SWE-Sandbox")]

        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.return_value = _proc(stdout=json.dumps(invites))

            result = accept_pending_invites_from_config(
                {
                    "auto_accept_invites": True,
                    "github_account": "swe-squad-alpha",
                    "dry_run": True,
                }
            )

        assert mock_run.call_count == 1   # list only
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Tests: failed PATCH is not returned in accepted list
# ---------------------------------------------------------------------------

class TestPatchFailure:
    def test_failed_patch_excluded_from_result(self):
        invites = [
            _make_invite(1, "your-org/SWE-Sandbox"),
            _make_invite(2, "your-org/other"),
        ]

        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _proc(stdout=json.dumps(invites)),    # list
                _proc(returncode=0),                  # accept 1 — ok
                _proc(returncode=1, stderr="error"),  # accept 2 — fail
            ]

            result = accept_pending_invites("swe-squad-alpha")

        assert len(result) == 1
        assert result[0]["id"] == 1


# ---------------------------------------------------------------------------
# Tests: gh API subprocess commands use correct arguments
# ---------------------------------------------------------------------------

class TestSubprocessCommands:
    def test_list_command_args(self):
        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.return_value = _proc(stdout="[]")
            accept_pending_invites("swe-squad-alpha")

        list_call_args = mock_run.call_args_list[0][0][0]
        assert list_call_args == ["gh", "api", "/user/repository_invitations"]

    def test_patch_command_args(self):
        invites = [_make_invite(42, "your-org/SWE-Sandbox")]

        with patch("src.swe_team.github_invites.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _proc(stdout=json.dumps(invites)),
                _proc(returncode=0),
            ]
            accept_pending_invites("swe-squad-alpha")

        patch_call_args = mock_run.call_args_list[1][0][0]
        assert patch_call_args == [
            "gh", "api", "-X", "PATCH",
            "/user/repository_invitations/42",
        ]
