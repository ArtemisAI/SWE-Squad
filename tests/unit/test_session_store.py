"""Tests for session lifecycle store (SessionStore + SessionRecord)."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.session_store import SessionRecord, SessionStore


# ---------------------------------------------------------------------------
# SessionRecord dataclass
# ---------------------------------------------------------------------------


class TestSessionRecord:
    def test_to_dict(self):
        rec = SessionRecord(
            session_id="swe-investigator-abc12345-ff",
            ticket_id="T-001",
            agent_type="investigator",
            created_at=1000.0,
            last_active=1000.0,
            status="active",
            metadata={"key": "val"},
        )
        d = rec.to_dict()
        assert d["session_id"] == "swe-investigator-abc12345-ff"
        assert d["ticket_id"] == "T-001"
        assert d["metadata"] == {"key": "val"}

    def test_from_dict(self):
        data = {
            "session_id": "swe-developer-xyz-aa",
            "ticket_id": "T-002",
            "agent_type": "developer",
            "created_at": 2000.0,
            "last_active": 2000.0,
            "status": "suspended",
        }
        rec = SessionRecord.from_dict(data)
        assert rec.session_id == "swe-developer-xyz-aa"
        assert rec.status == "suspended"
        assert rec.metadata == {}

    def test_roundtrip(self):
        original = SessionRecord(
            session_id="swe-investigator-test1234-ab",
            ticket_id="T-005",
            agent_type="investigator",
            created_at=5000.0,
            last_active=5000.0,
            status="completed",
            metadata={"attempts": 3},
        )
        reconstructed = SessionRecord.from_dict(original.to_dict())
        assert reconstructed.session_id == original.session_id
        assert reconstructed.metadata == original.metadata


# ---------------------------------------------------------------------------
# SessionStore — creation
# ---------------------------------------------------------------------------


class TestSessionStoreCreate:
    def test_create_generates_unique_ids(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        r1 = store.create("T-001", "investigator")
        r2 = store.create("T-001", "investigator")
        assert r1.session_id != r2.session_id

    def test_create_session_id_is_valid_uuid(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-ABC12345", "developer")
        # Session IDs must be valid UUID4 so Claude CLI accepts them
        parsed = uuid.UUID(rec.session_id)
        assert parsed.version == 4
        assert rec.status == "active"
        assert rec.agent_type == "developer"
        assert rec.ticket_id == "T-ABC12345"

    def test_create_with_metadata(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator", metadata={"model": "sonnet"})
        # Caller-supplied keys are preserved; display_name is auto-added.
        assert rec.metadata["model"] == "sonnet"
        assert "display_name" in rec.metadata

    def test_create_sets_timestamps(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        before = time.time()
        rec = store.create("T-001", "investigator")
        after = time.time()
        assert before <= rec.created_at <= after
        assert before <= rec.last_active <= after


# ---------------------------------------------------------------------------
# SessionStore — get and get_by_ticket
# ---------------------------------------------------------------------------


class TestSessionStoreGet:
    def test_get_existing(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        fetched = store.get(rec.session_id)
        assert fetched is not None
        assert fetched.session_id == rec.session_id

    def test_get_missing(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        assert store.get("nonexistent") is None

    def test_get_by_ticket_returns_matching(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        store.create("T-001", "investigator")
        store.create("T-001", "developer")
        store.create("T-002", "investigator")
        results = store.get_by_ticket("T-001")
        assert len(results) == 2
        assert all(r.ticket_id == "T-001" for r in results)

    def test_get_by_ticket_newest_first(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        r1 = store.create("T-001", "investigator")
        time.sleep(0.01)
        r2 = store.create("T-001", "developer")
        results = store.get_by_ticket("T-001")
        assert results[0].session_id == r2.session_id

    def test_get_by_ticket_empty(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        assert store.get_by_ticket("T-NONE") == []


# ---------------------------------------------------------------------------
# SessionStore — update_status
# ---------------------------------------------------------------------------


class TestSessionStoreUpdateStatus:
    def test_update_status(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        store.update_status(rec.session_id, "suspended")
        fetched = store.get(rec.session_id)
        assert fetched is not None
        assert fetched.status == "suspended"

    def test_update_status_persists(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        store = SessionStore(path=path)
        rec = store.create("T-001", "investigator")
        store.update_status(rec.session_id, "completed")
        # Reload from disk
        store2 = SessionStore(path=path)
        fetched = store2.get(rec.session_id)
        assert fetched is not None
        assert fetched.status == "completed"

    def test_update_status_invalid_raises(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        with pytest.raises(ValueError, match="Invalid status"):
            store.update_status(rec.session_id, "bogus")

    def test_update_status_missing_logs_warning(self, tmp_path):
        """update_status on a missing session logs a warning and returns gracefully."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        # Should not raise — just log a warning
        store.update_status("nonexistent", "active")

    def test_update_status_updates_last_active(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        original_active = rec.last_active
        time.sleep(0.01)
        store.update_status(rec.session_id, "suspended")
        fetched = store.get(rec.session_id)
        assert fetched is not None
        assert fetched.last_active > original_active


# ---------------------------------------------------------------------------
# SessionStore — cleanup_stale
# ---------------------------------------------------------------------------


class TestSessionStoreCleanup:
    def test_cleanup_removes_old_sessions(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        # Artificially age the session
        rec.last_active = time.time() - (25 * 3600)
        store._save()
        removed = store.cleanup_stale(max_age_hours=24.0)
        assert removed == 1
        assert store.get(rec.session_id) is None

    def test_cleanup_keeps_recent_sessions(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        removed = store.cleanup_stale(max_age_hours=24.0)
        assert removed == 0
        assert store.get(rec.session_id) is not None

    def test_cleanup_returns_count(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        for i in range(3):
            r = store.create(f"T-{i:03d}", "investigator")
            r.last_active = time.time() - (48 * 3600)
        store._save()
        store.create("T-RECENT", "developer")  # This one stays
        removed = store.cleanup_stale(max_age_hours=24.0)
        assert removed == 3
        assert len(store.list_all()) == 1


# ---------------------------------------------------------------------------
# SessionStore — list_active
# ---------------------------------------------------------------------------


class TestSessionStoreListActive:
    def test_list_active_filters_by_status(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        r1 = store.create("T-001", "investigator")
        r2 = store.create("T-002", "developer")
        store.update_status(r2.session_id, "completed")
        active = store.list_active()
        assert len(active) == 1
        assert active[0].session_id == r1.session_id

    def test_list_active_empty(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        assert store.list_active() == []


# ---------------------------------------------------------------------------
# SessionStore — persistence
# ---------------------------------------------------------------------------


class TestSessionStorePersistence:
    def test_persist_and_reload(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        store = SessionStore(path=path)
        store.create("T-001", "investigator")
        store.create("T-002", "developer")

        store2 = SessionStore(path=path)
        assert len(store2.list_all()) == 2

    def test_handles_corrupt_file(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text("not valid json")
        store = SessionStore(path=str(path))
        assert len(store.list_all()) == 0

    def test_handles_missing_file(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "nonexistent.json"))
        assert len(store.list_all()) == 0


# ---------------------------------------------------------------------------
# SessionStore — touch
# ---------------------------------------------------------------------------


class TestSessionStoreUpdateSessionId:
    def test_rekey_session(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        old_id = rec.session_id
        new_id = str(uuid.uuid4())
        updated = store.update_session_id(old_id, new_id)
        assert updated.session_id == new_id
        assert store.get(old_id) is None
        assert store.get(new_id) is not None
        assert store.get(new_id).ticket_id == "T-001"

    def test_rekey_persists(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        store = SessionStore(path=path)
        rec = store.create("T-001", "investigator")
        old_id = rec.session_id
        new_id = str(uuid.uuid4())
        store.update_session_id(old_id, new_id)
        # Reload from disk
        store2 = SessionStore(path=path)
        assert store2.get(new_id) is not None
        assert store2.get(old_id) is None

    def test_rekey_missing_raises(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        with pytest.raises(KeyError):
            store.update_session_id("nonexistent", "new-id")

    def test_rekey_updates_last_active(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        original = rec.last_active
        time.sleep(0.01)
        new_id = str(uuid.uuid4())
        updated = store.update_session_id(rec.session_id, new_id)
        assert updated.last_active > original


# ---------------------------------------------------------------------------
# SessionStore — touch
# ---------------------------------------------------------------------------


class TestSessionStoreTouch:
    def test_touch_updates_last_active(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        # Re-read persisted value to avoid in-flight timing skew
        original = store.get(rec.session_id).last_active
        time.sleep(0.05)
        store.touch(rec.session_id)
        fetched = store.get(rec.session_id)
        assert fetched is not None
        assert fetched.last_active > original

    def test_touch_missing_raises(self, tmp_path):
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        with pytest.raises(KeyError):
            store.touch("nonexistent")


# ---------------------------------------------------------------------------
# ClaudeCodeEngine session integration
# ---------------------------------------------------------------------------


class TestClaudeCodeEngineSession:
    # Claude CLI requires session IDs to be valid UUIDs
    _TEST_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_run_with_session_id(self):
        from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine

        engine = ClaudeCodeEngine(binary="/bin/echo")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="ok", stderr="", returncode=0
            )
            engine.run("hello", session_id=self._TEST_UUID)
            cmd = mock_run.call_args[0][0]
            assert "--session-id" in cmd
            idx = cmd.index("--session-id")
            assert cmd[idx + 1] == self._TEST_UUID

    def test_run_without_session_id(self):
        from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine

        engine = ClaudeCodeEngine(binary="/bin/echo")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="ok", stderr="", returncode=0
            )
            engine.run("hello")
            cmd = mock_run.call_args[0][0]
            assert "--session-id" not in cmd
            assert "--session" not in cmd

    def test_resume_adds_flags(self):
        from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine

        engine = ClaudeCodeEngine(binary="/bin/echo")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="resumed", stderr="", returncode=0
            )
            result = engine.resume(self._TEST_UUID, "continue")
            cmd = mock_run.call_args[0][0]
            assert "--resume" in cmd
            idx = cmd.index("--resume")
            assert cmd[idx + 1] == self._TEST_UUID
            assert result.stdout == "resumed"

    def test_resume_timeout(self):
        from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine
        import subprocess as sp

        engine = ClaudeCodeEngine(binary="/bin/echo", default_timeout=1)
        # Default: raise_on_timeout=True — TimeoutExpired is re-raised
        with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="claude", timeout=1)):
            with pytest.raises(sp.TimeoutExpired):
                engine.resume("sid", "prompt")

    def test_resume_timeout_legacy(self):
        from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine
        import subprocess as sp

        engine = ClaudeCodeEngine(binary="/bin/echo", default_timeout=1)
        # With raise_on_timeout=False, legacy EngineResult(-1) is returned
        with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="claude", timeout=1)):
            result = engine.resume("sid", "prompt", raise_on_timeout=False)
            assert result.returncode == -1
            assert "Timeout" in result.stderr
            assert result.metadata.get("error_type") == "timeout"


# ---------------------------------------------------------------------------
# Investigator session wiring (mocked)
# ---------------------------------------------------------------------------


class TestInvestigatorSessionWiring:
    def _make_ticket(self):
        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
        return SWETicket(
            ticket_id="T-SESSION-001",
            title="Test session wiring",
            description="Test session wiring desc",
            severity=TicketSeverity.HIGH,
            status=TicketStatus.TRIAGED,
            source_module="test_module",
        )

    @patch("src.swe_team.investigator.InvestigatorAgent._run_claude")
    @patch("src.swe_team.investigator.InvestigatorAgent._build_prompt")
    @patch("src.swe_team.investigator.embed_ticket")
    def test_investigate_creates_session_with_engine(
        self, mock_embed, mock_build, mock_run, tmp_path
    ):
        """When engine is set, investigate() creates a session record."""
        from src.swe_team.investigator import InvestigatorAgent
        from src.swe_team.session_store import SessionStore

        mock_build.return_value = "test prompt"
        mock_run.return_value = (
            "Root Cause: A retry loop silently swallowed a timeout from the upstream proxy, "
            "so the worker emitted incomplete diagnostics and the orchestrator persisted an invalid response.\n\n"
            "Affected Files: src/swe_team/investigator.py, src/swe_team/developer.py, tests/unit/test_investigator.py\n\n"
            "Fix Plan: Validate report structure before persistence, reject known Claude error envelopes, "
            "require minimum report length, and keep sectioned investigation output for downstream automation.",
            "",
        )

        engine = MagicMock()
        engine.name = "claude"

        agent = InvestigatorAgent(engine=engine)
        session_store = SessionStore(path=str(tmp_path / "sessions.json"))
        agent._session_store = session_store

        ticket = self._make_ticket()
        result = agent.investigate(ticket)

        assert result is True
        sessions = session_store.get_by_ticket("T-SESSION-001")
        assert len(sessions) == 1
        assert sessions[0].status == "completed"

    @patch("src.swe_team.investigator.InvestigatorAgent._run_claude")
    @patch("src.swe_team.investigator.InvestigatorAgent._build_prompt")
    @patch("src.swe_team.investigator.embed_ticket")
    def test_investigate_resumes_suspended_session(
        self, mock_embed, mock_build, mock_run, tmp_path
    ):
        """When a suspended session exists, investigate() resumes it."""
        from src.swe_team.investigator import InvestigatorAgent
        from src.swe_team.session_store import SessionStore

        mock_build.return_value = "test prompt"
        mock_run.return_value = (
            "Root Cause: A retry loop silently swallowed a timeout from the upstream proxy, "
            "so the worker emitted incomplete diagnostics and the orchestrator persisted an invalid response.\n\n"
            "Affected Files: src/swe_team/investigator.py, src/swe_team/developer.py, tests/unit/test_investigator.py\n\n"
            "Fix Plan: Validate report structure before persistence, reject known Claude error envelopes, "
            "require minimum report length, and keep sectioned investigation output for downstream automation.",
            "",
        )

        engine = MagicMock()
        engine.name = "claude"

        agent = InvestigatorAgent(engine=engine)
        session_store = SessionStore(path=str(tmp_path / "sessions.json"))
        agent._session_store = session_store

        # Pre-create a suspended session
        rec = session_store.create("T-SESSION-001", "investigator")
        session_store.update_status(rec.session_id, "suspended")

        ticket = self._make_ticket()
        result = agent.investigate(ticket)

        assert result is True
        # The suspended session should now be completed
        fetched = session_store.get(rec.session_id)
        assert fetched is not None
        assert fetched.status == "completed"

    @patch("src.swe_team.investigator.InvestigatorAgent._run_claude")
    @patch("src.swe_team.investigator.InvestigatorAgent._build_prompt")
    @patch("src.swe_team.investigator.embed_ticket")
    def test_investigate_captures_claude_session_uuid(
        self, mock_embed, mock_build, mock_run, tmp_path
    ):
        """After a successful run, the real Claude CLI session UUID should be
        stored in the session record and ticket metadata."""
        from src.swe_team.investigator import InvestigatorAgent
        from src.swe_team.session_store import SessionStore
        from src.swe_team.providers.coding_engine.base import EngineResult

        real_claude_uuid = str(uuid.uuid4())
        mock_build.return_value = "test prompt"
        mock_run.return_value = (
            "Root Cause: A retry loop silently swallowed a timeout from the upstream proxy, "
            "so the worker emitted incomplete diagnostics and the orchestrator persisted an invalid response.\n\n"
            "Affected Files: src/swe_team/investigator.py, src/swe_team/developer.py, tests/unit/test_investigator.py\n\n"
            "Fix Plan: Validate report structure before persistence, reject known Claude error envelopes, "
            "require minimum report length, and keep sectioned investigation output for downstream automation.",
            "",
        )

        engine = MagicMock()
        engine.name = "claude"

        agent = InvestigatorAgent(engine=engine)
        session_store = SessionStore(path=str(tmp_path / "sessions.json"))
        agent._session_store = session_store
        # Simulate the engine result having a Claude CLI session UUID
        agent._last_engine_result = EngineResult(
            stdout=(
                "Root Cause: A retry loop silently swallowed a timeout from the upstream proxy, "
                "so the worker emitted incomplete diagnostics and the orchestrator persisted an invalid response.\n\n"
                "Affected Files: src/swe_team/investigator.py, src/swe_team/developer.py, tests/unit/test_investigator.py\n\n"
                "Fix Plan: Validate report structure before persistence, reject known Claude error envelopes, "
                "require minimum report length, and keep sectioned investigation output for downstream automation."
            ), stderr="", returncode=0,
            session_id=real_claude_uuid,
        )

        ticket = self._make_ticket()
        result = agent.investigate(ticket)

        assert result is True
        # Session should be re-keyed under the real Claude UUID
        sessions = session_store.get_by_ticket("T-SESSION-001")
        assert len(sessions) == 1
        assert sessions[0].session_id == real_claude_uuid
        assert sessions[0].status == "completed"
        # Ticket metadata should contain the Claude session UUID
        assert ticket.metadata.get("claude_session_id") == real_claude_uuid

    @patch("src.swe_team.investigator.InvestigatorAgent._run_claude")
    @patch("src.swe_team.investigator.InvestigatorAgent._build_prompt")
    def test_investigate_suspends_on_failure(
        self, mock_build, mock_run, tmp_path
    ):
        """On failure, the session should be suspended for later resumption."""
        from src.swe_team.investigator import InvestigatorAgent
        from src.swe_team.session_store import SessionStore

        mock_build.return_value = "test prompt"
        mock_run.side_effect = RuntimeError("CLI failed")

        engine = MagicMock()
        engine.name = "claude"

        agent = InvestigatorAgent(engine=engine)
        session_store = SessionStore(path=str(tmp_path / "sessions.json"))
        agent._session_store = session_store

        ticket = self._make_ticket()
        result = agent.investigate(ticket)

        assert result is False
        sessions = session_store.get_by_ticket("T-SESSION-001")
        assert len(sessions) == 1
        assert sessions[0].status == "suspended"


# ---------------------------------------------------------------------------
# Concurrent write safety (#315)
# ---------------------------------------------------------------------------


class TestSessionStoreConcurrentSafety:
    def test_has_thread_lock(self, tmp_path):
        """SessionStore must have a threading.Lock for concurrent access."""
        import threading
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        assert hasattr(store, "_lock")
        assert isinstance(store._lock, type(threading.Lock()))

    def test_concurrent_creates_no_corruption(self, tmp_path):
        """Multiple threads creating sessions must not corrupt the file."""
        import threading
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        errors = []

        def create_session(idx):
            try:
                store.create(f"T-{idx:03d}", "developer")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=create_session, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(store.list_all()) == 10


# ---------------------------------------------------------------------------
# #264 — Session naming (rename + generate_session_name + display_name)
# ---------------------------------------------------------------------------


class TestSessionNaming:
    def test_create_sets_display_name(self, tmp_path):
        """create() should store a display_name in metadata automatically."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("TICKET-001", "investigator")
        assert "display_name" in rec.metadata
        assert "TICKET-001" in rec.metadata["display_name"]

    def test_display_name_format(self, tmp_path):
        """display_name should follow SWE-<type>-<ticket>-<date> pattern."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("TICKET-042", "developer")
        name = rec.metadata["display_name"]
        assert name.startswith("SWE-")
        assert "TICKET-042" in name
        # Date portion: YYYY-MM-DD
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2}$", name)

    def test_create_does_not_overwrite_provided_display_name(self, tmp_path):
        """If caller passes display_name in metadata, it should be preserved."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator", metadata={"display_name": "my-custom-name"})
        assert rec.metadata["display_name"] == "my-custom-name"

    def test_generate_session_name_format(self):
        """generate_session_name should produce the correct format."""
        name = SessionStore.generate_session_name("ticket123", "investigator")
        assert name.startswith("SWE-")
        assert "ticket123" in name
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2}$", name)

    def test_generate_session_name_with_developer(self):
        """generate_session_name works for developer agent type."""
        name = SessionStore.generate_session_name("T-999", "developer")
        assert "T-999" in name
        assert name.startswith("SWE-")

    def test_rename_updates_display_name(self, tmp_path):
        """rename() should update the display_name in metadata."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        store.rename(rec.session_id, "my-renamed-session")
        fetched = store.get(rec.session_id)
        assert fetched is not None
        assert fetched.metadata["display_name"] == "my-renamed-session"

    def test_rename_persists_to_disk(self, tmp_path):
        """rename() change survives a store reload."""
        path = str(tmp_path / "sessions.json")
        store = SessionStore(path=path)
        rec = store.create("T-001", "investigator")
        store.rename(rec.session_id, "persisted-name")
        store2 = SessionStore(path=path)
        fetched = store2.get(rec.session_id)
        assert fetched is not None
        assert fetched.metadata["display_name"] == "persisted-name"

    def test_rename_missing_raises(self, tmp_path):
        """rename() should raise KeyError for unknown session_id."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        with pytest.raises(KeyError):
            store.rename("nonexistent", "new-name")

    def test_rename_updates_last_active(self, tmp_path):
        """rename() should refresh last_active."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        original_active = rec.last_active
        time.sleep(0.01)
        store.rename(rec.session_id, "new-name")
        fetched = store.get(rec.session_id)
        assert fetched is not None
        assert fetched.last_active > original_active


# ---------------------------------------------------------------------------
# #265 — Session ID collector (find_resumable, find_by_status, mark_for_escalation)
# ---------------------------------------------------------------------------


class TestSessionIdCollector:
    def test_find_resumable_returns_active(self, tmp_path):
        """find_resumable should return an active session for a ticket."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        found = store.find_resumable("T-001")
        assert found is not None
        assert found.session_id == rec.session_id

    def test_find_resumable_returns_suspended(self, tmp_path):
        """find_resumable should return a suspended session for a ticket."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        store.update_status(rec.session_id, "suspended")
        found = store.find_resumable("T-001")
        assert found is not None
        assert found.session_id == rec.session_id

    def test_find_resumable_skips_completed(self, tmp_path):
        """find_resumable should ignore completed sessions."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        store.update_status(rec.session_id, "completed")
        found = store.find_resumable("T-001")
        assert found is None

    def test_find_resumable_returns_most_recent(self, tmp_path):
        """find_resumable should return the most recently active session."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        r1 = store.create("T-001", "investigator")
        store.update_status(r1.session_id, "suspended")
        time.sleep(0.01)
        r2 = store.create("T-001", "investigator")
        found = store.find_resumable("T-001")
        assert found is not None
        assert found.session_id == r2.session_id

    def test_find_resumable_no_match(self, tmp_path):
        """find_resumable returns None when no active/suspended session exists."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        assert store.find_resumable("T-NONE") is None

    def test_find_by_status_active(self, tmp_path):
        """find_by_status('active') returns only active sessions."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        r1 = store.create("T-001", "investigator")
        r2 = store.create("T-002", "developer")
        store.update_status(r2.session_id, "completed")
        results = store.find_by_status("active")
        assert len(results) == 1
        assert results[0].session_id == r1.session_id

    def test_find_by_status_completed(self, tmp_path):
        """find_by_status('completed') returns only completed sessions."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        r1 = store.create("T-001", "investigator")
        r2 = store.create("T-002", "developer")
        store.update_status(r1.session_id, "completed")
        results = store.find_by_status("completed")
        assert len(results) == 1
        assert results[0].session_id == r1.session_id

    def test_find_by_status_sorted_newest_first(self, tmp_path):
        """find_by_status should return results sorted newest-first."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        r1 = store.create("T-001", "investigator")
        time.sleep(0.01)
        r2 = store.create("T-002", "developer")
        results = store.find_by_status("active")
        assert results[0].session_id == r2.session_id

    def test_find_by_status_invalid_raises(self, tmp_path):
        """find_by_status raises ValueError for unrecognised status."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        with pytest.raises(ValueError, match="Invalid status"):
            store.find_by_status("bogus_status")

    def test_mark_for_escalation_sets_status(self, tmp_path):
        """mark_for_escalation should set status to 'escalated'."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        store.mark_for_escalation(rec.session_id, "Too many retries")
        fetched = store.get(rec.session_id)
        assert fetched is not None
        assert fetched.status == "escalated"

    def test_mark_for_escalation_stores_reason(self, tmp_path):
        """mark_for_escalation should store the reason in metadata."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        store.mark_for_escalation(rec.session_id, "circuit breaker triggered")
        fetched = store.get(rec.session_id)
        assert fetched is not None
        assert fetched.metadata.get("escalation_reason") == "circuit breaker triggered"

    def test_mark_for_escalation_persists(self, tmp_path):
        """Escalation status and reason survive a store reload."""
        path = str(tmp_path / "sessions.json")
        store = SessionStore(path=path)
        rec = store.create("T-001", "investigator")
        store.mark_for_escalation(rec.session_id, "HITL required")
        store2 = SessionStore(path=path)
        fetched = store2.get(rec.session_id)
        assert fetched is not None
        assert fetched.status == "escalated"
        assert fetched.metadata.get("escalation_reason") == "HITL required"

    def test_mark_for_escalation_missing_raises(self, tmp_path):
        """mark_for_escalation raises KeyError for unknown session_id."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        with pytest.raises(KeyError):
            store.mark_for_escalation("nonexistent", "some reason")

    def test_find_by_status_escalated(self, tmp_path):
        """find_by_status('escalated') returns escalated sessions."""
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        store.mark_for_escalation(rec.session_id, "test")
        results = store.find_by_status("escalated")
        assert len(results) == 1
        assert results[0].session_id == rec.session_id


# ---------------------------------------------------------------------------
# #266 — build_session_header
# ---------------------------------------------------------------------------


class TestBuildSessionHeader:
    def _make_ticket(self, ticket_id="T-HEADER-001", severity="HIGH", module="src/swe_team/developer.py"):
        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
        sev_map = {
            "CRITICAL": TicketSeverity.CRITICAL,
            "HIGH": TicketSeverity.HIGH,
            "MEDIUM": TicketSeverity.MEDIUM,
            "LOW": TicketSeverity.LOW,
        }
        return SWETicket(
            ticket_id=ticket_id,
            title="Header test",
            description="Header test description",
            severity=sev_map.get(severity, TicketSeverity.HIGH),
            status=TicketStatus.INVESTIGATING,
            source_module=module,
        )

    def test_header_contains_session_id(self, tmp_path):
        """build_session_header output must include the session_id."""
        from src.swe_team.session_store import build_session_header
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-HEADER-001", "investigator")
        ticket = self._make_ticket()
        header = build_session_header(rec, ticket)
        assert rec.session_id in header

    def test_header_contains_ticket_id(self, tmp_path):
        """build_session_header output must include the ticket_id."""
        from src.swe_team.session_store import build_session_header
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-HEADER-001", "investigator")
        ticket = self._make_ticket()
        header = build_session_header(rec, ticket)
        assert "T-HEADER-001" in header

    def test_header_session_line_format(self, tmp_path):
        """[SESSION] line should include id=, ticket=, agent=, attempt= tags."""
        from src.swe_team.session_store import build_session_header
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator", metadata={"attempt": 2})
        ticket = self._make_ticket(ticket_id="T-001")
        header = build_session_header(rec, ticket)
        session_line = header.splitlines()[0]
        assert session_line.startswith("[SESSION]")
        assert "id=" in session_line
        assert "ticket=T-001" in session_line
        assert "agent=investigator" in session_line
        assert "attempt=2" in session_line

    def test_header_context_line_format(self, tmp_path):
        """[CONTEXT] line should include severity= and module= tags."""
        from src.swe_team.session_store import build_session_header
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "developer")
        ticket = self._make_ticket(severity="HIGH", module="src/swe_team/developer.py")
        header = build_session_header(rec, ticket)
        context_line = header.splitlines()[1]
        assert context_line.startswith("[CONTEXT]")
        assert "severity=HIGH" in context_line
        assert "module=src/swe_team/developer.py" in context_line

    def test_header_default_attempt_is_1(self, tmp_path):
        """When attempt is not in metadata, build_session_header defaults to 1."""
        from src.swe_team.session_store import build_session_header
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        ticket = self._make_ticket(ticket_id="T-001")
        header = build_session_header(rec, ticket)
        assert "attempt=1" in header

    def test_header_two_lines(self, tmp_path):
        """build_session_header must produce exactly two lines."""
        from src.swe_team.session_store import build_session_header
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        ticket = self._make_ticket(ticket_id="T-001")
        header = build_session_header(rec, ticket)
        lines = header.splitlines()
        assert len(lines) == 2

    def test_header_severity_uppercased(self, tmp_path):
        """Severity value in [CONTEXT] should always be uppercase."""
        from src.swe_team.session_store import build_session_header
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "investigator")
        ticket = self._make_ticket(severity="CRITICAL")
        header = build_session_header(rec, ticket)
        assert "severity=CRITICAL" in header

    def test_header_empty_module(self, tmp_path):
        """build_session_header should not crash when source_module is empty."""
        from src.swe_team.session_store import build_session_header
        store = SessionStore(path=str(tmp_path / "sessions.json"))
        rec = store.create("T-001", "developer")
        ticket = self._make_ticket(module="")
        header = build_session_header(rec, ticket)
        assert "module=" in header
