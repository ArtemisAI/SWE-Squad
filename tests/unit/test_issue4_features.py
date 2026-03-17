"""
Tests for issue #4 features: progress log, stall detection, model config.
"""

from __future__ import annotations

import logging
logging.logAsyncioTasks = False

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.config import ModelConfig, SWETeamConfig, load_config
from src.swe_team.ticket_store import TicketStore


# ======================================================================
# ModelConfig
# ======================================================================


class TestModelConfig:
    def test_defaults(self):
        mc = ModelConfig()
        assert mc.t1_heavy == "opus"
        assert mc.t2_standard == "sonnet"
        assert mc.t3_fast == "haiku"

    def test_from_dict(self):
        mc = ModelConfig.from_dict({
            "t1_heavy": "claude-opus-4",
            "t2_standard": "claude-sonnet-4",
            "t3_fast": "claude-haiku-3",
        })
        assert mc.t1_heavy == "claude-opus-4"
        assert mc.t2_standard == "claude-sonnet-4"
        assert mc.t3_fast == "claude-haiku-3"

    def test_from_dict_defaults(self):
        mc = ModelConfig.from_dict({})
        assert mc.t1_heavy == "opus"
        assert mc.t2_standard == "sonnet"
        assert mc.t3_fast == "haiku"

    def test_to_dict(self):
        mc = ModelConfig(t1_heavy="a", t2_standard="b", t3_fast="c")
        d = mc.to_dict()
        assert d == {"t1_heavy": "a", "t2_standard": "b", "t3_fast": "c"}

    def test_env_overrides(self):
        mc = ModelConfig()
        env = {"T1_MODEL": "gpt-5", "T2_MODEL": "gpt-4", "T3_MODEL": "gpt-3"}
        orig = {k: os.environ.get(k) for k in env}
        try:
            os.environ.update(env)
            mc.apply_env_overrides()
            assert mc.t1_heavy == "gpt-5"
            assert mc.t2_standard == "gpt-4"
            assert mc.t3_fast == "gpt-3"
        finally:
            for k, v in orig.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_partial_env_overrides(self):
        mc = ModelConfig()
        orig_t2 = os.environ.get("T2_MODEL")
        try:
            os.environ["T2_MODEL"] = "custom-sonnet"
            # Ensure T1 and T3 are not set
            os.environ.pop("T1_MODEL", None)
            os.environ.pop("T3_MODEL", None)
            mc.apply_env_overrides()
            assert mc.t1_heavy == "opus"  # unchanged
            assert mc.t2_standard == "custom-sonnet"
            assert mc.t3_fast == "haiku"  # unchanged
        finally:
            if orig_t2 is None:
                os.environ.pop("T2_MODEL", None)
            else:
                os.environ["T2_MODEL"] = orig_t2


class TestModelConfigInSWETeamConfig:
    def test_models_field_exists(self):
        cfg = SWETeamConfig()
        assert hasattr(cfg, "models")
        assert isinstance(cfg.models, ModelConfig)
        assert cfg.models.t1_heavy == "opus"

    def test_from_dict_with_models(self):
        cfg = SWETeamConfig.from_dict({
            "models": {
                "t1_heavy": "custom-opus",
                "t2_standard": "custom-sonnet",
                "t3_fast": "custom-haiku",
            }
        })
        assert cfg.models.t1_heavy == "custom-opus"
        assert cfg.models.t2_standard == "custom-sonnet"
        assert cfg.models.t3_fast == "custom-haiku"

    def test_to_dict_includes_models(self):
        cfg = SWETeamConfig()
        d = cfg.to_dict()
        assert "models" in d
        assert d["models"]["t1_heavy"] == "opus"

    def test_load_config_from_yaml(self):
        """Config loaded from actual YAML should include model tiers."""
        cfg = load_config("config/swe_team.yaml")
        assert cfg.models.t1_heavy == "opus"
        assert cfg.models.t2_standard == "sonnet"
        assert cfg.models.t3_fast == "haiku"


# ======================================================================
# Stall Detection
# ======================================================================


class TestStallDetection:
    """Test heartbeat stall detection logic (extracted from runner)."""

    def _make_ticket(self, status, heartbeat_iso=None, updated_at=None):
        t = SWETicket(
            title="Test ticket",
            description="desc",
            severity=TicketSeverity.HIGH,
        )
        t.status = status
        if updated_at:
            t.updated_at = updated_at
        if heartbeat_iso:
            t.metadata["last_heartbeat"] = heartbeat_iso
        return t

    def test_stalled_investigating_ticket(self):
        """A ticket investigating for >2 hours with no heartbeat should be detected."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        t = self._make_ticket(TicketStatus.INVESTIGATING, updated_at=old_time)

        with tempfile.TemporaryDirectory() as td:
            store = TicketStore(os.path.join(td, "tickets.json"))
            store.add(t)

            # Simulate stall detection logic (same as runner)
            stalled = self._detect_stalled(store)
            assert len(stalled) == 1
            assert stalled[0].status == TicketStatus.OPEN
            assert "stall_detected" in stalled[0].metadata

    def test_stalled_in_development_ticket(self):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        t = self._make_ticket(TicketStatus.IN_DEVELOPMENT, updated_at=old_time)

        with tempfile.TemporaryDirectory() as td:
            store = TicketStore(os.path.join(td, "tickets.json"))
            store.add(t)

            stalled = self._detect_stalled(store)
            assert len(stalled) == 1
            assert stalled[0].status == TicketStatus.OPEN

    def test_not_stalled_recent_heartbeat(self):
        """A ticket with a recent heartbeat should NOT be flagged."""
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        t = self._make_ticket(
            TicketStatus.INVESTIGATING,
            heartbeat_iso=recent_time,
            updated_at=(datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
        )

        with tempfile.TemporaryDirectory() as td:
            store = TicketStore(os.path.join(td, "tickets.json"))
            store.add(t)

            stalled = self._detect_stalled(store)
            assert len(stalled) == 0

    def test_not_stalled_open_status(self):
        """Tickets in OPEN status are not checked for stall."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        t = self._make_ticket(TicketStatus.OPEN, updated_at=old_time)

        with tempfile.TemporaryDirectory() as td:
            store = TicketStore(os.path.join(td, "tickets.json"))
            store.add(t)

            stalled = self._detect_stalled(store)
            assert len(stalled) == 0

    def test_stall_metadata_records_previous_status(self):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        t = self._make_ticket(TicketStatus.IN_DEVELOPMENT, updated_at=old_time)

        with tempfile.TemporaryDirectory() as td:
            store = TicketStore(os.path.join(td, "tickets.json"))
            store.add(t)

            stalled = self._detect_stalled(store)
            assert stalled[0].metadata["stall_detected"]["previous_status"] == "in_development"

    @staticmethod
    def _detect_stalled(store):
        """Replicate the stall detection logic from the runner for testing."""
        _STALL_THRESHOLD_HOURS = 2
        stalled = []
        now = datetime.now(timezone.utc)
        stall_statuses = {TicketStatus.INVESTIGATING, TicketStatus.IN_DEVELOPMENT}

        for ticket in store.list_all():
            if ticket.status not in stall_statuses:
                continue
            heartbeat_iso = ticket.metadata.get("last_heartbeat") or ticket.updated_at
            try:
                heartbeat = datetime.fromisoformat(heartbeat_iso)
            except (ValueError, TypeError):
                continue
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            hours_since = (now - heartbeat).total_seconds() / 3600
            if hours_since > _STALL_THRESHOLD_HOURS:
                ticket.metadata["stall_detected"] = {
                    "previous_status": ticket.status.value,
                    "stalled_hours": round(hours_since, 2),
                    "detected_at": now.isoformat(),
                }
                ticket.transition(TicketStatus.OPEN)
                store.add(ticket)
                stalled.append(ticket)
        return stalled


# ======================================================================
# Progress Log
# ======================================================================


class TestProgressLog:
    def test_append_creates_file(self):
        """Progress log append should create the file if it doesn't exist."""
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "swe_progress.txt"
            result = {
                "new_tickets": 3,
                "open_tickets": 7,
                "gate_verdict": "pass",
            }
            self._append_progress(log_path, result)
            assert log_path.exists()
            content = log_path.read_text()
            assert "CYCLE" in content
            assert "Tickets: 3/7" in content
            assert "Gate: pass" in content

    def test_append_is_additive(self):
        """Multiple appends should accumulate entries, not overwrite."""
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "swe_progress.txt"
            r1 = {"new_tickets": 1, "open_tickets": 2, "gate_verdict": "pass"}
            r2 = {"new_tickets": 5, "open_tickets": 10, "gate_verdict": "block"}
            self._append_progress(log_path, r1, done="First cycle")
            self._append_progress(log_path, r2, done="Second cycle")
            content = log_path.read_text()
            assert content.count("--- CYCLE") == 2
            assert "First cycle" in content
            assert "Second cycle" in content

    def test_entry_format(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "swe_progress.txt"
            result = {"new_tickets": 0, "open_tickets": 0, "gate_verdict": "N/A"}
            self._append_progress(
                log_path,
                result,
                done="Nothing happened",
                next_step="Wait",
                blockers="Network down",
            )
            content = log_path.read_text()
            assert "DONE: Nothing happened" in content
            assert "NEXT: Wait" in content
            assert "BLOCKERS: Network down" in content

    @staticmethod
    def _append_progress(log_path, result, *, done="", next_step="", blockers=""):
        """Replicate the progress log logic from the runner for testing."""
        ts = datetime.now(timezone.utc).isoformat()
        new_count = result.get("new_tickets", 0)
        open_count = result.get("open_tickets", 0)
        verdict = result.get("gate_verdict", "N/A")

        entry = (
            f"--- CYCLE {ts} | Tickets: {new_count}/{open_count} | Gate: {verdict}\n"
            f"DONE: {done or 'Cycle completed'}\n"
            f"NEXT: {next_step or 'Continue monitoring'}\n"
            f"BLOCKERS: {blockers or 'None'}\n"
            f"---\n"
        )

        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(entry)


# ======================================================================
# Agent model selection with ModelConfig
# ======================================================================


class TestInvestigatorModelSelection:
    def test_critical_uses_t1_heavy(self):
        from src.swe_team.investigator import InvestigatorAgent

        mc = ModelConfig(t1_heavy="my-opus", t2_standard="my-sonnet")
        agent = InvestigatorAgent(model_config=mc)
        ticket = SWETicket(
            title="Critical bug",
            description="desc",
            severity=TicketSeverity.CRITICAL,
        )
        assert agent._select_model(ticket) == "my-opus"

    def test_high_uses_t2_standard(self):
        from src.swe_team.investigator import InvestigatorAgent

        mc = ModelConfig(t1_heavy="my-opus", t2_standard="my-sonnet")
        agent = InvestigatorAgent(model_config=mc)
        ticket = SWETicket(
            title="High bug",
            description="desc",
            severity=TicketSeverity.HIGH,
        )
        assert agent._select_model(ticket) == "my-sonnet"

    def test_failed_investigation_escalates_to_t1(self):
        from src.swe_team.investigator import InvestigatorAgent

        mc = ModelConfig(t1_heavy="my-opus", t2_standard="my-sonnet")
        agent = InvestigatorAgent(model_config=mc)
        ticket = SWETicket(
            title="High bug",
            description="desc",
            severity=TicketSeverity.HIGH,
            metadata={"investigation": {"status": "failed"}},
        )
        assert agent._select_model(ticket) == "my-opus"

    def test_no_model_config_falls_back(self):
        from src.swe_team.investigator import InvestigatorAgent

        agent = InvestigatorAgent()
        ticket = SWETicket(
            title="Critical bug",
            description="desc",
            severity=TicketSeverity.CRITICAL,
        )
        assert agent._select_model(ticket) == "opus"


class TestDeveloperModelSelection:
    def test_critical_uses_t1_heavy(self):
        from src.swe_team.developer import DeveloperAgent

        mc = ModelConfig(t1_heavy="dev-opus", t2_standard="dev-sonnet")
        agent = DeveloperAgent(model_config=mc)
        ticket = SWETicket(
            title="Critical bug",
            description="desc",
            severity=TicketSeverity.CRITICAL,
        )
        assert agent._select_model(ticket) == "dev-opus"

    def test_high_uses_t2_standard(self):
        from src.swe_team.developer import DeveloperAgent

        mc = ModelConfig(t1_heavy="dev-opus", t2_standard="dev-sonnet")
        agent = DeveloperAgent(model_config=mc)
        ticket = SWETicket(
            title="High bug",
            description="desc",
            severity=TicketSeverity.HIGH,
        )
        assert agent._select_model(ticket) == "dev-sonnet"

    def test_escalation_after_failures(self):
        from src.swe_team.developer import DeveloperAgent

        mc = ModelConfig(t1_heavy="dev-opus", t2_standard="dev-sonnet")
        agent = DeveloperAgent(model_config=mc)
        ticket = SWETicket(
            title="High bug",
            description="desc",
            severity=TicketSeverity.HIGH,
            metadata={"attempts": [
                {"result": "fail"},
                {"result": "fail"},
            ]},
        )
        assert agent._select_model(ticket) == "dev-opus"

    def test_no_model_config_falls_back(self):
        from src.swe_team.developer import DeveloperAgent

        agent = DeveloperAgent()
        ticket = SWETicket(
            title="High bug",
            description="desc",
            severity=TicketSeverity.HIGH,
        )
        assert agent._select_model(ticket) == "sonnet"


# ======================================================================
# Claude Code hooks settings.json
# ======================================================================


class TestClaudeSettings:
    def test_settings_json_exists(self):
        settings_path = Path(".")
        # Walk up to find .claude/settings.json relative to repo root
        repo_root = Path(__file__).resolve().parent.parent.parent
        p = repo_root / ".claude" / "settings.json"
        assert p.exists(), f".claude/settings.json not found at {p}"

    def test_settings_json_valid(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        p = repo_root / ".claude" / "settings.json"
        with open(p) as fh:
            data = json.load(fh)
        assert "hooks" in data
        assert "PreToolUse" in data["hooks"]
        hooks = data["hooks"]["PreToolUse"]
        assert len(hooks) >= 1
        assert hooks[0]["matcher"] == "Bash"

    def test_blocked_patterns_in_hook(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        p = repo_root / ".claude" / "settings.json"
        with open(p) as fh:
            data = json.load(fh)
        hook_cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        for pattern in ["rm -rf", "git push --force", "git push -f", "git reset --hard"]:
            assert pattern in hook_cmd, f"Missing blocked pattern: {pattern}"
