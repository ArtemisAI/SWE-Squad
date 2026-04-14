"""
Unit tests for src/swe_team/distiller.py — TrajectoryDistiller and AutomationRecord.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.swe_team.distiller import AutomationRecord, TrajectoryDistiller
from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ticket(fingerprint: str | None = "fp-abc123") -> SWETicket:
    t = SWETicket(title="Test", description="A bug", severity=TicketSeverity.HIGH)
    if fingerprint is not None:
        t.metadata["fingerprint"] = fingerprint
    return t


def _make_distiller(tmp_path: Path) -> TrajectoryDistiller:
    return TrajectoryDistiller(automations_dir=tmp_path / "automations")


# ---------------------------------------------------------------------------
# AutomationRecord: to_dict / from_dict roundtrip
# ---------------------------------------------------------------------------

class TestAutomationRecordRoundtrip:
    def test_to_dict_has_expected_keys(self):
        rec = AutomationRecord(
            fingerprint="fp-001",
            steps=[["echo", "hello"]],
            success_count=3,
            failure_count=1,
            success_rate=0.75,
        )
        d = rec.to_dict()
        assert d["fingerprint"] == "fp-001"
        assert d["steps"] == [["echo", "hello"]]
        assert d["success_count"] == 3
        assert d["failure_count"] == 1
        assert d["success_rate"] == 0.75
        assert "updated_at" in d

    def test_from_dict_restores_all_fields(self):
        rec = AutomationRecord(
            fingerprint="fp-002",
            steps=[["git", "apply", "fix.patch"]],
            success_count=5,
            failure_count=2,
            success_rate=0.71,
        )
        restored = AutomationRecord.from_dict(rec.to_dict())
        assert restored.fingerprint == "fp-002"
        assert restored.steps == [["git", "apply", "fix.patch"]]
        assert restored.success_count == 5
        assert restored.failure_count == 2
        assert abs(restored.success_rate - 0.71) < 0.01

    def test_from_dict_defaults_for_missing_fields(self):
        d = {"fingerprint": "fp-min", "steps": []}
        rec = AutomationRecord.from_dict(d)
        assert rec.success_count == 0
        assert rec.failure_count == 0
        assert rec.success_rate == 0.0

    def test_steps_are_lists_of_lists(self):
        d = {
            "fingerprint": "fp-003",
            "steps": [["cmd", "arg1"], ["cmd2"]],
        }
        rec = AutomationRecord.from_dict(d)
        assert rec.steps[0] == ["cmd", "arg1"]
        assert rec.steps[1] == ["cmd2"]


# ---------------------------------------------------------------------------
# get_automation() — missing records
# ---------------------------------------------------------------------------

class TestGetAutomationMissing:
    def test_returns_none_for_unknown_fingerprint(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        result = distiller.get_automation("nonexistent-fp")
        assert result is None

    def test_returns_none_after_empty_dir_init(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        assert distiller.get_automation("fp-xyz") is None

    def test_returns_none_for_corrupt_json(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        path = distiller.automation_path("bad-fp")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        result = distiller.get_automation("bad-fp")
        assert result is None


# ---------------------------------------------------------------------------
# record_success() and get_automation()
# ---------------------------------------------------------------------------

class TestRecordSuccess:
    def test_record_success_stores_retrievable_record(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        ticket = _ticket("fp-store-test")
        steps = [["python", "-c", "print('fix')"]]
        rec = distiller.record_success(ticket, steps)
        assert rec is not None
        assert rec.fingerprint == "fp-store-test"

        retrieved = distiller.get_automation("fp-store-test")
        assert retrieved is not None
        assert retrieved.steps == steps

    def test_record_success_increments_count(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        ticket = _ticket("fp-count-test")
        steps = [["echo", "ok"]]
        r1 = distiller.record_success(ticket, steps)
        assert r1.success_count == 1
        r2 = distiller.record_success(ticket, steps)
        assert r2.success_count == 2

    def test_record_success_no_fingerprint_returns_none(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        ticket = _ticket(fingerprint=None)
        result = distiller.record_success(ticket, [["echo", "x"]])
        assert result is None

    def test_record_success_updates_steps(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        ticket = _ticket("fp-update")
        distiller.record_success(ticket, [["old", "step"]])
        new_steps = [["new", "step"]]
        distiller.record_success(ticket, new_steps)
        retrieved = distiller.get_automation("fp-update")
        assert retrieved.steps == new_steps

    def test_success_rate_computed_correctly(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        ticket = _ticket("fp-rate")
        steps = [["echo", "ok"]]
        # Record 3 successes; no failures initially
        distiller.record_success(ticket, steps)
        distiller.record_success(ticket, steps)
        distiller.record_success(ticket, steps)
        rec = distiller.get_automation("fp-rate")
        assert rec.success_rate == 1.0


# ---------------------------------------------------------------------------
# record_patch()
# ---------------------------------------------------------------------------

class TestRecordPatch:
    def test_record_patch_creates_automation(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        ticket = _ticket("fp-patch-001")
        patch_text = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n+x = 1\n"
        rec = distiller.record_patch(ticket, patch_text)
        assert rec is not None

    def test_record_patch_creates_patch_file(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        ticket = _ticket("fp-patch-002")
        patch_text = "--- a/foo.py\n+++ b/foo.py"
        distiller.record_patch(ticket, patch_text)
        patch_files = list((tmp_path / "automations").glob("*.patch"))
        assert len(patch_files) == 1
        assert patch_files[0].read_text(encoding="utf-8") == patch_text

    def test_record_patch_empty_text_returns_none(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        ticket = _ticket("fp-patch-empty")
        result = distiller.record_patch(ticket, "   ")
        assert result is None

    def test_record_patch_no_fingerprint_returns_none(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        ticket = _ticket(fingerprint=None)
        result = distiller.record_patch(ticket, "diff content")
        assert result is None

    def test_record_patch_step_points_to_patch_file(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        ticket = _ticket("fp-patch-003")
        distiller.record_patch(ticket, "some diff")
        rec = distiller.get_automation("fp-patch-003")
        assert rec is not None
        assert len(rec.steps) == 1
        step = rec.steps[0]
        assert step[0] == "git"
        assert step[1] == "apply"
        assert ".patch" in step[2]


# ---------------------------------------------------------------------------
# Persistence: new instance reads saved records
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_new_instance_reads_saved_records(self, tmp_path):
        automations_dir = tmp_path / "automations"
        distiller1 = TrajectoryDistiller(automations_dir=automations_dir)
        ticket = _ticket("fp-persist")
        steps = [["echo", "persisted"]]
        distiller1.record_success(ticket, steps)

        # Create brand-new instance pointing at same dir
        distiller2 = TrajectoryDistiller(automations_dir=automations_dir)
        rec = distiller2.get_automation("fp-persist")
        assert rec is not None
        assert rec.steps == steps
        assert rec.success_count == 1

    def test_record_written_as_valid_json(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        ticket = _ticket("fp-json")
        distiller.record_success(ticket, [["cmd", "arg"]])
        path = distiller.automation_path("fp-json")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["fingerprint"] == "fp-json"
        assert isinstance(data["steps"], list)

    def test_automations_dir_created_automatically(self, tmp_path):
        new_dir = tmp_path / "deep" / "nested" / "automations"
        assert not new_dir.exists()
        TrajectoryDistiller(automations_dir=new_dir)
        assert new_dir.exists()


# ---------------------------------------------------------------------------
# automation_path() and _safe_filename()
# ---------------------------------------------------------------------------

class TestAutomationPath:
    def test_path_uses_safe_filename(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        path = distiller.automation_path("some/weird:fp?name!")
        assert path.suffix == ".json"
        # Safe chars only in stem
        assert "/" not in path.stem
        assert "?" not in path.stem

    def test_path_for_normal_fingerprint(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        path = distiller.automation_path("fp-normal-123")
        assert path.name == "fp-normal-123.json"

    def test_all_special_chars_get_fallback_hash(self, tmp_path):
        distiller = _make_distiller(tmp_path)
        # A fingerprint made entirely of unsafe chars falls back to sha256 digest
        path = distiller.automation_path("!!!???///")
        assert path.stem.startswith("unknown_")
