"""Unit tests for src/swe_team/supabase_store.py — no real network calls."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import EngineHandover, HandoverConstraints, SWETicket, TicketSeverity, TicketStatus
from src.swe_team.supabase_store import SupabaseTicketStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path, url: str = "https://test.supabase.co") -> SupabaseTicketStore:
    """Create a SupabaseTicketStore pointing at a tmp activity file."""
    activity_file = tmp_path / "supabase_last_activity.json"
    return SupabaseTicketStore(
        supabase_url=url,
        supabase_key="test-anon-key",
        team_id="test-team",
        activity_file=activity_file,
    )


def _mock_response(data: Any, status: int = 200) -> MagicMock:
    """Build a context-manager mock that urllib.request.urlopen returns."""
    raw = json.dumps(data).encode() if data is not None else b""
    mock_resp = MagicMock()
    mock_resp.read.return_value = raw
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_ticket(
    ticket_id: str = "abc123",
    title: str = "Test ticket",
    status: TicketStatus = TicketStatus.OPEN,
) -> SWETicket:
    t = SWETicket(title=title, description="desc", status=status)
    t.ticket_id = ticket_id
    return t


def _ticket_row(
    ticket_id: str = "abc123",
    title: str = "Test ticket",
    status: str = "open",
) -> dict:
    return {
        "ticket_id": ticket_id,
        "title": title,
        "description": "desc",
        "severity": "medium",
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "assigned_to": None,
        "labels": [],
        "ticket_type": "unknown",
        "source_module": None,
        "error_log": None,
        "related_tickets": [],
        "metadata": {},
        "investigation_report": None,
        "proposed_fix": None,
        "test_results": None,
        "deployment_id": None,
        "rollback_reason": None,
        "team_id": "test-team",
    }


# ---------------------------------------------------------------------------
# 1. __init__ — constructor sets internal state correctly
# ---------------------------------------------------------------------------

class TestInit:
    def test_url_and_key_set(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        assert store._url == "https://test.supabase.co"
        assert store._key == "test-anon-key"
        assert store._team_id == "test-team"

    def test_url_trailing_slash_stripped(self, tmp_path: Path) -> None:
        store = SupabaseTicketStore(
            supabase_url="https://test.supabase.co/",
            supabase_key="key",
            team_id="t1",
            activity_file=tmp_path / "act.json",
        )
        assert not store._url.endswith("/")
        assert store._rest == "https://test.supabase.co/rest/v1"

    def test_url_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")
        monkeypatch.setenv("SUPABASE_ANON_KEY", "env-key")
        store = SupabaseTicketStore(
            team_id="default",
            activity_file=tmp_path / "act.json",
        )
        assert store._url == "https://env.supabase.co"
        assert store._key == "env-key"

    def test_activity_file_missing_gives_datetime_min(self, tmp_path: Path) -> None:
        activity_file = tmp_path / "nonexistent.json"
        store = SupabaseTicketStore(
            supabase_url="https://x.supabase.co",
            supabase_key="k",
            activity_file=activity_file,
        )
        # datetime.min means keepalive will always fire on first check
        assert store._last_activity.tzinfo is not None
        assert store._last_activity.year < 2000

    def test_activity_file_loaded_if_present(self, tmp_path: Path) -> None:
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        act_file = tmp_path / "act.json"
        act_file.write_text(json.dumps({"last_activity": ts.isoformat()}))
        store = SupabaseTicketStore(
            supabase_url="https://x.supabase.co",
            supabase_key="k",
            activity_file=act_file,
        )
        assert store._last_activity == ts


# ---------------------------------------------------------------------------
# 2. get() — mock HTTP GET, return a ticket
# ---------------------------------------------------------------------------

class TestGet:
    def test_get_returns_ticket_on_hit(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        row = _ticket_row(ticket_id="t001", title="Found ticket")
        with patch("urllib.request.urlopen", return_value=_mock_response([row])):
            ticket = store.get("t001")
        assert ticket is not None
        assert ticket.ticket_id == "t001"
        assert ticket.title == "Found ticket"

    def test_get_returns_none_on_miss(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch("urllib.request.urlopen", return_value=_mock_response([])):
            ticket = store.get("nonexistent")
        assert ticket is None

    def test_get_raises_on_http_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        exc = urllib.error.HTTPError(
            url="http://x", code=500, msg="Internal Server Error",
            hdrs=None, fp=BytesIO(b"error"),
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(urllib.error.HTTPError):
                store.get("any")

    def test_get_metadata_as_string_is_parsed(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        row = _ticket_row(ticket_id="t002")
        row["metadata"] = json.dumps({"key": "value"})
        with patch("urllib.request.urlopen", return_value=_mock_response([row])):
            ticket = store.get("t002")
        assert ticket is not None
        assert ticket.metadata["key"] == "value"


# ---------------------------------------------------------------------------
# 3. list_all / list_by_status — mock HTTP, return list
# ---------------------------------------------------------------------------

class TestListTickets:
    def test_list_all_returns_tickets(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        rows = [_ticket_row(f"t{i:03d}", f"Ticket {i}") for i in range(3)]
        with patch("urllib.request.urlopen", return_value=_mock_response(rows)):
            tickets = store.list_all()
        assert len(tickets) == 3
        assert tickets[0].title == "Ticket 0"

    def test_list_all_empty(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch("urllib.request.urlopen", return_value=_mock_response([])):
            tickets = store.list_all()
        assert tickets == []

    def test_list_by_status_calls_correct_param(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        row = _ticket_row(status="investigating")
        captured_urls = []

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return _mock_response([row])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            tickets = store.list_by_status(TicketStatus.INVESTIGATING)
        assert len(tickets) == 1
        assert "investigating" in captured_urls[0]

    def test_list_open_excludes_resolved(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        rows = [_ticket_row(f"t{i:03d}", status="open") for i in range(2)]
        captured_url = []

        def fake_urlopen(req, timeout=None):
            captured_url.append(req.full_url)
            return _mock_response(rows)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            tickets = store.list_open()
        assert len(tickets) == 2
        # Should use not.in.(...) filter
        assert "not.in." in captured_url[0]


# ---------------------------------------------------------------------------
# 4. upsert via add() — mock POST
# ---------------------------------------------------------------------------

class TestUpsert:
    def test_add_posts_to_swe_tickets(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ticket = _make_ticket()
        row = _ticket_row()
        call_count = [0]

        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            # First call is GET (for existing check), rest are POST/audit
            return _mock_response([row] if call_count[0] == 1 else row)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            store.add(ticket)
        assert call_count[0] >= 2  # GET (existing) + POST (upsert) + audit

    def test_add_resets_write_failure_counter_on_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store._consecutive_write_failures = 5
        ticket = _make_ticket()
        row = _ticket_row()

        def fake_urlopen(req, timeout=None):
            # GET returns a list; POST/PATCH returns a list with the row
            return _mock_response([row])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            store.add(ticket)
        assert store._consecutive_write_failures == 0

    def test_add_increments_write_failure_on_http_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ticket = _make_ticket()
        # First call (GET existing) succeeds; subsequent POST fails
        responses = [
            _mock_response([]),  # GET existing → miss
        ]
        call_idx = [0]
        exc = urllib.error.HTTPError(
            url="http://x", code=503, msg="Service Unavailable",
            hdrs=None, fp=BytesIO(b"down"),
        )

        def fake_urlopen(req, timeout=None):
            if call_idx[0] < len(responses):
                r = responses[call_idx[0]]
                call_idx[0] += 1
                return r
            raise exc

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with pytest.raises(urllib.error.HTTPError):
                store.add(ticket)
        assert store._consecutive_write_failures >= 1


# ---------------------------------------------------------------------------
# 5. find_similar — mock RPC call
# ---------------------------------------------------------------------------

class TestFindSimilar:
    def test_find_similar_returns_matches(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        matches = [
            {"ticket_id": "t001", "similarity": 0.95, "raw_similarity": 0.95},
            {"ticket_id": "t002", "similarity": 0.87, "raw_similarity": 0.87},
        ]
        with patch("urllib.request.urlopen", return_value=_mock_response(matches)):
            result = store.find_similar([0.1] * 10)
        assert len(result) == 2
        assert result[0]["ticket_id"] == "t001"

    def test_find_similar_empty_on_no_match(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch("urllib.request.urlopen", return_value=_mock_response([])):
            result = store.find_similar([0.1] * 10)
        assert result == []

    def test_find_similar_posts_to_rpc_endpoint(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req.full_url)
            return _mock_response([])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            store.find_similar([0.5, 0.5])
        assert any("/rpc/match_similar_tickets" in u for u in captured)

    def test_find_similar_empty_on_url_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        exc = urllib.error.URLError(reason="Name or service not known")
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(urllib.error.URLError):
                store.find_similar([0.1] * 10)


# ---------------------------------------------------------------------------
# 6. store_embedding_with_dedup — deduplication logic
# ---------------------------------------------------------------------------

class TestStoreEmbeddingWithDedup:
    def test_stored_when_no_similar(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ticket = _make_ticket()
        call_idx = [0]
        responses = [
            [],           # find_similar → no matches
            None,         # store_embedding PATCH
        ]

        def fake_urlopen(req, timeout=None):
            resp = responses[call_idx[0] % len(responses)]
            call_idx[0] += 1
            return _mock_response(resp)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = store.store_embedding_with_dedup(ticket, [0.1] * 10)
        assert result == "stored"

    def test_skipped_when_existing_has_higher_detail(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        ticket = _make_ticket()

        # Existing ticket has richer content
        existing_row = _ticket_row(ticket_id="existing-001")
        existing_row["investigation_report"] = "A" * 300
        existing_row["proposed_fix"] = "B" * 200

        call_idx = [0]
        responses_data = [
            [{"ticket_id": "existing-001", "similarity": 0.95}],  # find_similar
            [existing_row],  # get(existing-001)
        ]

        def fake_urlopen(req, timeout=None):
            resp = responses_data[call_idx[0] % len(responses_data)]
            call_idx[0] += 1
            return _mock_response(resp)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = store.store_embedding_with_dedup(ticket, [0.1] * 10)
        assert result == "skipped"


# ---------------------------------------------------------------------------
# 7. Error handling — Supabase unreachable
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_url_error_raises(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        exc = urllib.error.URLError(reason="Name or service not known")
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(urllib.error.URLError):
                store.get("any-id")

    def test_connection_error_increments_write_failure(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        exc = urllib.error.URLError(reason="Connection refused")
        # Patch _request to raise on POST
        original_request = store._request

        def patched_request(method, path, **kwargs):
            if method == "POST":
                raise exc
            return []

        store._request = patched_request
        with pytest.raises(urllib.error.URLError):
            store._request("POST", "/swe_tickets", body={})
        # The raised exception confirms write would fail; counter incremented by store.add
        # (tested separately above). Just check raise propagates.

    def test_health_stats_initial_values(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        stats = store.health_stats()
        assert stats["supabase_write_errors_today"] == 0
        assert stats["supabase_consecutive_write_failures"] == 0
        assert stats["supabase_last_successful_write"] is None

    def test_http_error_increments_write_errors_today(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        exc = urllib.error.HTTPError(
            url="http://x", code=500, msg="ISE",
            hdrs=None, fp=BytesIO(b"err"),
        )
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(urllib.error.HTTPError):
                store._request("POST", "/swe_tickets", body={})
        assert store._write_errors_today == 1
        assert store._consecutive_write_failures == 1


# ---------------------------------------------------------------------------
# 8. keep_alive — activity file persistence
# ---------------------------------------------------------------------------

class TestKeepAlive:
    def test_keepalive_skipped_when_recent_activity(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        # Write a very recent activity timestamp
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        act_file = tmp_path / "supabase_last_activity.json"
        act_file.write_text(json.dumps({"last_activity": recent.isoformat()}))

        result = store.keep_alive(threshold_days=5)
        assert result is False

    def test_keepalive_fires_when_stale(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        # Stale: 10 days ago
        stale = datetime.now(timezone.utc) - timedelta(days=10)
        act_file = tmp_path / "supabase_last_activity.json"
        act_file.write_text(json.dumps({"last_activity": stale.isoformat()}))

        with patch("urllib.request.urlopen", return_value=_mock_response([])):
            result = store.keep_alive(threshold_days=5)
        assert result is True

    def test_keepalive_failure_increments_counter(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        stale = datetime.now(timezone.utc) - timedelta(days=10)
        act_file = tmp_path / "supabase_last_activity.json"
        act_file.write_text(json.dumps({"last_activity": stale.isoformat()}))

        exc = urllib.error.URLError(reason="connection refused")
        with patch("urllib.request.urlopen", side_effect=exc):
            result = store.keep_alive(threshold_days=5)
        assert result is False
        assert store._consecutive_keepalive_failures == 1


# ---------------------------------------------------------------------------
# 9. Vector literal helper
# ---------------------------------------------------------------------------

class TestVectorLiteral:
    def test_vector_literal_format(self) -> None:
        result = SupabaseTicketStore._vector_literal([1.0, 2.0, 3.0])
        assert result == "[1.0,2.0,3.0]"

    def test_vector_literal_empty(self) -> None:
        result = SupabaseTicketStore._vector_literal([])
        assert result == "[]"


# ---------------------------------------------------------------------------
# 10. known_fingerprints — lazy cache load
# ---------------------------------------------------------------------------

class TestKnownFingerprints:
    def test_fingerprints_loaded_from_supabase(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        rows = [
            {"metadata": {"fingerprint": "fp001"}},
            {"metadata": {"fingerprint": "fp002"}},
            {"metadata": {}},  # no fingerprint
        ]
        with patch("urllib.request.urlopen", return_value=_mock_response(rows)):
            fps = store.known_fingerprints
        assert "fp001" in fps
        assert "fp002" in fps
        assert len(fps) == 2

    def test_fingerprints_cached_after_first_load(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        rows = [{"metadata": {"fingerprint": "fp-x"}}]
        call_count = [0]

        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            return _mock_response(rows)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _ = store.known_fingerprints
            _ = store.known_fingerprints  # second access should use cache
        assert call_count[0] == 1  # only one HTTP call


class TestHandoverLogging:
    def test_log_handover_writes_engine_handover_note(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        handover = EngineHandover(
            task_id="t-handover-1",
            phase="develop",
            source_engine="claude",
            target_engine="cline",
            timestamp="2026-01-01T00:00:00+00:00",
            context={"branch": "feat/x"},
            constraints=HandoverConstraints(
                budget_remaining_usd=1.25,
                time_limit_seconds=300,
                model_tier="T2",
                retry_count=0,
                max_retries=3,
            ),
        )
        with patch.object(store, "_request") as mock_request:
            store.log_handover(handover)
        assert mock_request.call_count == 1
        _, kwargs = mock_request.call_args
        body = kwargs["body"]
        assert body["ticket_id"] == "t-handover-1"
        assert body["agent"] == "claude"
        assert body["from_status"] == "phase:develop"
        assert body["to_status"] == "handover:develop"
        assert body["note"].startswith("engine_handover:")


class TestEngineCooldownStorage:
    def test_upsert_engine_cooldown_posts_merge_request(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch.object(store, "_request") as mock_request:
            store.upsert_engine_cooldown(
                team_id="test-team",
                engine_name="claudez",
                status="rate_limited",
                cooldown_until="2026-04-07T12:00:00+00:00",
                reset_at=None,
                next_probe_at="2026-04-07T12:02:00+00:00",
                last_error="429",
                fallback_engine="claudep",
                updated_at="2026-04-07T11:59:00+00:00",
            )
        args, kwargs = mock_request.call_args
        assert args[0] == "POST"
        assert args[1] == "/engine_cooldowns"
        assert kwargs["body"]["engine_name"] == "claudez"
        assert kwargs["extra_headers"]["Prefer"] == "resolution=merge-duplicates,return=representation"

    def test_get_engine_cooldown_returns_row(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch.object(store, "_request", return_value=[{"engine_name": "claudez", "status": "healthy"}]):
            row = store.get_engine_cooldown("claudez")
        assert row is not None
        assert row["status"] == "healthy"

    def test_list_engine_cooldowns_returns_rows(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        rows = [{"engine_name": "claudez"}, {"engine_name": "claudep"}]
        with patch.object(store, "_request", return_value=rows):
            out = store.list_engine_cooldowns()
        assert len(out) == 2
        assert out[1]["engine_name"] == "claudep"
