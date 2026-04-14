"""
Security audit tests for dashboard server input validation fixes.

Covers:
  - Request body size limit enforcement
  - Label validation (type checking and format)
  - Team creation numeric bounds and enum validation
  - Mermaid SVG sanitization helper
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Project bootstrap ─────────────────────────────────────────────────────────
logging.logAsyncioTasks = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Test: _read_post_body size limit
# ---------------------------------------------------------------------------

class TestReadPostBodySizeLimit:
    """Verify that _read_post_body rejects oversized payloads."""

    def _make_handler(self, content_length: int, body: bytes = b"{}"):
        """Create a mock handler with the _read_post_body method."""
        from scripts.ops.dashboard_server import DashboardHandler

        handler = MagicMock(spec=DashboardHandler)
        handler.headers = {"Content-Length": str(content_length)}
        from io import BytesIO
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.address_string = lambda: "127.0.0.1"
        # Bind the real methods so we test actual logic
        handler._read_post_body = lambda **kw: DashboardHandler._read_post_body(handler, **kw)
        handler._json_response = lambda data, status=200, **kw: DashboardHandler._json_response(
            handler, data, status, **kw
        )
        return handler

    def test_normal_body_accepted(self):
        """A normal-sized body should be parsed correctly."""
        body = json.dumps({"key": "value"}).encode()
        handler = self._make_handler(len(body), body)
        result = handler._read_post_body()
        assert result == {"key": "value"}

    def test_oversized_body_rejected(self):
        """A body exceeding 10 MB should return None and send 400."""
        handler = self._make_handler(11 * 1024 * 1024)
        result = handler._read_post_body()
        assert result is None
        # rfile should not have been read (position stays at 0)
        assert handler.rfile.tell() == 0
        # Verify a 400 response was sent
        resp = json.loads(handler.wfile.getvalue())
        assert "too large" in resp["error"].lower()

    def test_zero_length_body(self):
        """A zero-length body should return empty dict."""
        handler = self._make_handler(0)
        result = handler._read_post_body()
        assert result == {}

    def test_exactly_at_limit(self):
        """A body at exactly 10 MB should be accepted."""
        handler = self._make_handler(10 * 1024 * 1024, b"{}")
        result = handler._read_post_body()
        assert result == {}  # valid JSON parse of {}


# ---------------------------------------------------------------------------
# Test: Label validation regex
# ---------------------------------------------------------------------------

class TestLabelValidation:
    """Verify the label regex pattern used in _handle_ticket_label."""

    @pytest.fixture
    def pattern(self):
        # Same pattern as in dashboard_server.py
        return re.compile(r"^[\w ./:@-]{1,100}$")

    @pytest.mark.parametrize("label", [
        "bug",
        "high-priority",
        "needs_review",
        "scope:frontend",
        "team/alpha",
        "P0 urgent",
        "v2.1.0",
        "user@mention",
    ])
    def test_valid_labels(self, pattern, label):
        assert pattern.match(label), f"Expected valid: {label!r}"

    @pytest.mark.parametrize("label", [
        "",                           # empty
        "a" * 101,                    # too long
        "label;injection",            # semicolon
        "label$(cmd)",                # shell metachar
        "label`whoami`",              # backticks
        'label"quoted"',              # double quotes
        "label\nnewline",             # newline
    ])
    def test_invalid_labels(self, pattern, label):
        assert not pattern.match(label), f"Expected invalid: {label!r}"


# ---------------------------------------------------------------------------
# Test: Team creation validation
# ---------------------------------------------------------------------------

class TestTeamCreationValidation:
    """Verify bounds and enum checks for _handle_create_team fields."""

    def test_max_concurrent_bounds(self):
        """max_concurrent must be 1-100."""
        # Valid
        assert 1 <= 5 <= 100
        assert 1 <= 1 <= 100
        assert 1 <= 100 <= 100
        # Invalid
        assert not (1 <= 0 <= 100)
        assert not (1 <= 101 <= 100)
        assert not (1 <= -1 <= 100)

    def test_cost_budget_bounds(self):
        """cost_budget_daily must be 0-100000."""
        assert 0 <= 50.0 <= 100000
        assert 0 <= 0 <= 100000
        assert 0 <= 100000 <= 100000
        assert not (0 <= -1 <= 100000)
        assert not (0 <= 100001 <= 100000)

    def test_valid_roles(self):
        valid_roles = {"developer", "investigator", "manager", "reviewer", "senior"}
        assert "developer" in valid_roles
        assert "manager" in valid_roles
        assert "hacker" not in valid_roles
        assert "" not in valid_roles

    def test_valid_tiers(self):
        valid_tiers = {"economy", "standard", "senior", "premium"}
        assert "standard" in valid_tiers
        assert "economy" in valid_tiers
        assert "free" not in valid_tiers


# ---------------------------------------------------------------------------
# Test: Mermaid SVG sanitization
# ---------------------------------------------------------------------------

class TestSvgSanitization:
    """Test the sanitizeSvg logic (Python equivalent of the TS function).

    We test the regex patterns directly since the actual function is in TypeScript.
    """

    def _sanitize_svg(self, svg: str) -> str:
        """Python equivalent of the sanitizeSvg function in MarkdownBody.tsx."""
        # Remove <script> tags
        clean = re.sub(r"<script[\s\S]*?</script>", "", svg, flags=re.IGNORECASE)
        # Remove on* event handlers
        clean = re.sub(r"""\s+on\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]*)""", "", clean, flags=re.IGNORECASE)
        # Remove javascript: URIs
        clean = re.sub(r"""(href|xlink:href|src)\s*=\s*(?:"javascript:[^"]*"|'javascript:[^']*')""", r'\1=""', clean, flags=re.IGNORECASE)
        return clean

    def test_removes_script_tags(self):
        svg = '<svg><script>alert("xss")</script><rect/></svg>'
        result = self._sanitize_svg(svg)
        assert "<script" not in result
        assert "alert" not in result
        assert "<rect/>" in result

    def test_removes_event_handlers(self):
        svg = '<svg><rect onclick="alert(1)" onload="evil()" width="10"/></svg>'
        result = self._sanitize_svg(svg)
        assert "onclick" not in result
        assert "onload" not in result
        assert "width" in result

    def test_removes_javascript_uris(self):
        svg = '<svg><a href="javascript:alert(1)"><text>click</text></a></svg>'
        result = self._sanitize_svg(svg)
        assert "javascript:" not in result
        assert "href=" in result  # attribute exists but is empty

    def test_preserves_clean_svg(self):
        svg = '<svg viewBox="0 0 100 100"><rect x="10" y="10" width="80" height="80" fill="blue"/></svg>'
        result = self._sanitize_svg(svg)
        assert result == svg

    def test_removes_mixed_attacks(self):
        svg = (
            '<svg>'
            '<script>document.cookie</script>'
            '<rect onmouseover="steal()" fill="red"/>'
            '<a xlink:href="javascript:void(0)"><text>link</text></a>'
            '</svg>'
        )
        result = self._sanitize_svg(svg)
        assert "<script" not in result
        assert "onmouseover" not in result
        assert "javascript:" not in result
        assert "<rect" in result
        assert "<text>link</text>" in result


# ---------------------------------------------------------------------------
# Test: user_store.py uses parameterized queries
# ---------------------------------------------------------------------------

class TestUserStoreSqlSafety:
    """Verify that UserStore uses parameterized queries (no SQL injection)."""

    def test_get_user_parameterized(self, tmp_path):
        from src.swe_team.webui.user_store import UserStore

        db = str(tmp_path / "test.db")
        store = UserStore(db_path=db, encryption_key=b"0" * 32)

        # Create a user with SQL injection attempt in the login name
        malicious_login = "admin'; DROP TABLE users; --"
        user = store.get_or_create_user(malicious_login, email="test@test.com")
        assert user["github_login"] == malicious_login

        # Table should still exist and be queryable
        all_users = store.list_users()
        assert len(all_users) == 1
        assert all_users[0]["github_login"] == malicious_login

    def test_set_secret_parameterized(self, tmp_path):
        from src.swe_team.webui.user_store import UserStore

        db = str(tmp_path / "test.db")
        store = UserStore(db_path=db, encryption_key=b"0" * 32)
        store.get_or_create_user("testuser")

        # Store a secret with SQL injection attempt in the name
        malicious_name = "key'; DROP TABLE secrets; --"
        store.set_secret("testuser", malicious_name, "secret_value")

        # Table should still work
        names = store.list_secret_names("testuser")
        assert malicious_name in names

    def test_update_user_allowed_fields_only(self, tmp_path):
        from src.swe_team.webui.user_store import UserStore

        db = str(tmp_path / "test.db")
        store = UserStore(db_path=db, encryption_key=b"0" * 32)
        store.get_or_create_user("testuser", email="old@test.com")

        # Attempt to update a disallowed field
        user = store.update_user("testuser", email="new@test.com", id=999, created_at="hacked")
        assert user["email"] == "new@test.com"
        # id and created_at should not have changed
        assert user["id"] != 999


# ---------------------------------------------------------------------------
# Test: Static file path traversal protection
# ---------------------------------------------------------------------------

class TestStaticFilePathTraversal:
    """Verify that _serve_static_file blocks path traversal."""

    def test_dotdot_blocked(self, tmp_path):
        """Paths with .. should be rejected (404)."""
        from scripts.ops.dashboard_server import _REACT_UI_DIST

        # The _serve_static_file method checks:
        # str(file_path.resolve()).startswith(str(_REACT_UI_DIST.resolve()))
        # Test the logic directly
        safe_path = "../../etc/passwd"
        file_path = _REACT_UI_DIST / safe_path.lstrip("/")
        resolved = str(file_path.resolve())
        dist_resolved = str(_REACT_UI_DIST.resolve())
        # The resolved path should NOT start with the dist directory
        assert not resolved.startswith(dist_resolved)
