"""Tests for session tagging module."""
from __future__ import annotations

from src.swe_team.session import make_session_tag, session_header


class TestMakeSessionTag:
    def test_issue_number_priority(self):
        """issue_number has highest priority."""
        tag = make_session_tag(issue_number=42, ticket_id="t-abc", cycle=True)
        assert "ISSUE#42" in tag
        assert "[trace:" in tag

    def test_ticket_id_priority_over_cycle(self):
        """ticket_id takes priority over cycle flag."""
        tag = make_session_tag(ticket_id="abcdef123456", cycle=True)
        assert "TICKET-abcdef123456" in tag
        assert "CYCLE" not in tag
        assert "[trace:" in tag

    def test_cycle_flag(self):
        """cycle=True generates a CYCLE tag."""
        tag = make_session_tag(cycle=True)
        assert "CYCLE" in tag
        assert "[trace:" in tag

    def test_fallback_session(self):
        """No args → SESSION tag."""
        tag = make_session_tag()
        assert "SESSION" in tag
        assert "[trace:" in tag

    def test_trace_suffix_always_appended(self):
        """[trace:...] is always present regardless of type."""
        for kwargs in [
            {"issue_number": 1},
            {"ticket_id": "abc"},
            {"cycle": True},
            {},
        ]:
            tag = make_session_tag(**kwargs)
            assert "[trace:" in tag

    def test_ticket_id_truncated_to_12(self):
        """ticket_id is truncated to 12 characters."""
        tag = make_session_tag(ticket_id="a" * 20)
        assert "TICKET-" + "a" * 12 in tag

    def test_session_header_contains_tag(self):
        """session_header() embeds the tag."""
        tag = make_session_tag(issue_number=7)
        header = session_header(tag)
        assert tag in header
        assert "SWE-Squad" in header
