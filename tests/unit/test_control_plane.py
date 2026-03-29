"""Tests for the SWE-Squad Control Plane."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.swe_team.control_plane import (
    ConfigStore,
    ControlPlane,
    PipelineState,
    ProjectConfig,
    QueuedTicket,
    QueueStore,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def config_path(tmp_dir):
    return tmp_dir / "control_plane.yaml"


@pytest.fixture
def queue_path(tmp_dir):
    return tmp_dir / "queue.json"


@pytest.fixture
def sample_config(config_path):
    data = {
        "pipeline": {
            "paused": False,
            "cycle_interval_minutes": 15,
            "model_routing": {
                "t1_heavy": "opus",
                "t2_standard": "sonnet",
                "t3_fast": "haiku",
            },
        },
        "projects": {
            "example-org/my-app": {
                "max_concurrent_agents": 3,
                "budget_cap_daily": 50.0,
                "budget_cap_weekly": 200.0,
                "priority_weight": 0.7,
                "model_tier": "T2",
                "cycle_interval_minutes": 15,
                "enabled": True,
            },
            "ArtemisAI/SWE-Squad-DEV": {
                "max_concurrent_agents": 2,
                "budget_cap_daily": 30.0,
                "budget_cap_weekly": 150.0,
                "priority_weight": 0.3,
                "model_tier": "T2",
                "cycle_interval_minutes": 30,
                "enabled": True,
            },
        },
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(data, f)
    return config_path


@pytest.fixture
def control_plane(sample_config, queue_path):
    return ControlPlane(
        config_path=str(sample_config),
        queue_path=str(queue_path),
    )


# ---------------------------------------------------------------------------
# ProjectConfig tests
# ---------------------------------------------------------------------------

class TestProjectConfig:
    def test_from_dict_defaults(self):
        cfg = ProjectConfig.from_dict({})
        assert cfg.max_concurrent_agents == 2
        assert cfg.budget_cap_daily == 50.0
        assert cfg.model_tier == "T2"
        assert cfg.enabled is True

    def test_from_dict_custom(self):
        cfg = ProjectConfig.from_dict({
            "max_concurrent_agents": 5,
            "budget_cap_daily": 100.0,
            "priority_weight": 0.9,
        })
        assert cfg.max_concurrent_agents == 5
        assert cfg.budget_cap_daily == 100.0
        assert cfg.priority_weight == 0.9

    def test_to_dict(self):
        cfg = ProjectConfig(max_concurrent_agents=4, model_tier="T1")
        d = cfg.to_dict()
        assert d["max_concurrent_agents"] == 4
        assert d["model_tier"] == "T1"


# ---------------------------------------------------------------------------
# PipelineState tests
# ---------------------------------------------------------------------------

class TestPipelineState:
    def test_from_dict_defaults(self):
        state = PipelineState.from_dict({})
        assert state.paused is False
        assert state.cycle_interval_minutes == 15

    def test_from_dict_custom(self):
        state = PipelineState.from_dict({
            "paused": True,
            "cycle_interval_minutes": 5,
            "model_routing": {"t1_heavy": "o3"},
        })
        assert state.paused is True
        assert state.cycle_interval_minutes == 5
        assert state.model_routing["t1_heavy"] == "o3"

    def test_to_dict(self):
        state = PipelineState(paused=True)
        d = state.to_dict()
        assert d["paused"] is True
        assert "model_routing" in d


# ---------------------------------------------------------------------------
# QueuedTicket tests
# ---------------------------------------------------------------------------

class TestQueuedTicket:
    def test_from_dict(self):
        t = QueuedTicket.from_dict({
            "title": "Fix login",
            "severity": "high",
            "priority": 20,
        })
        assert t.title == "Fix login"
        assert t.severity == "high"
        assert t.priority == 20

    def test_to_dict(self):
        t = QueuedTicket(title="Test", severity="low")
        d = t.to_dict()
        assert d["title"] == "Test"
        assert "ticket_id" in d
        assert "created_at" in d

    def test_default_values(self):
        t = QueuedTicket()
        assert t.status == "queued"
        assert t.source == "api"
        assert len(t.ticket_id) == 12


# ---------------------------------------------------------------------------
# QueueStore tests
# ---------------------------------------------------------------------------

class TestQueueStore:
    def test_load_empty(self, tmp_dir):
        store = QueueStore(tmp_dir / "q.json")
        assert store.load_all() == []

    def test_upsert_and_load(self, tmp_dir):
        store = QueueStore(tmp_dir / "q.json")
        t = QueuedTicket(title="Test ticket")
        store.upsert(t)
        loaded = store.load_all()
        assert len(loaded) == 1
        assert loaded[0].title == "Test ticket"

    def test_upsert_update(self, tmp_dir):
        store = QueueStore(tmp_dir / "q.json")
        t = QueuedTicket(ticket_id="abc123", title="Original")
        store.upsert(t)
        t.title = "Updated"
        store.upsert(t)
        loaded = store.load_all()
        assert len(loaded) == 1
        assert loaded[0].title == "Updated"

    def test_remove(self, tmp_dir):
        store = QueueStore(tmp_dir / "q.json")
        t = QueuedTicket(ticket_id="remove-me")
        store.upsert(t)
        assert store.remove("remove-me") is True
        assert store.load_all() == []

    def test_remove_nonexistent(self, tmp_dir):
        store = QueueStore(tmp_dir / "q.json")
        assert store.remove("nope") is False

    def test_corrupt_file(self, tmp_dir):
        path = tmp_dir / "q.json"
        path.write_text("not json at all {{{")
        store = QueueStore(path)
        assert store.load_all() == []


# ---------------------------------------------------------------------------
# ConfigStore tests
# ---------------------------------------------------------------------------

class TestConfigStore:
    def test_load_from_yaml(self, sample_config):
        store = ConfigStore(sample_config)
        store.reload()
        state = store.get_pipeline_state()
        assert state.paused is False
        projects = store.get_projects()
        assert "example-org/my-app" in projects
        assert projects["example-org/my-app"].priority_weight == 0.7

    def test_get_project(self, sample_config):
        store = ConfigStore(sample_config)
        cfg = store.get_project("example-org/my-app")
        assert cfg is not None
        assert cfg.max_concurrent_agents == 3

    def test_get_project_not_found(self, sample_config):
        store = ConfigStore(sample_config)
        assert store.get_project("nonexistent") is None

    def test_update_pipeline(self, sample_config):
        store = ConfigStore(sample_config)
        state = store.update_pipeline({"paused": True, "cycle_interval_minutes": 5})
        assert state.paused is True
        assert state.cycle_interval_minutes == 5
        # Verify persistence
        store2 = ConfigStore(sample_config)
        state2 = store2.get_pipeline_state()
        assert state2.paused is True

    def test_update_project(self, sample_config):
        store = ConfigStore(sample_config)
        cfg = store.update_project("example-org/my-app", {"priority_weight": 0.9})
        assert cfg.priority_weight == 0.9
        # Verify persistence
        store2 = ConfigStore(sample_config)
        cfg2 = store2.get_project("example-org/my-app")
        assert cfg2.priority_weight == 0.9

    def test_update_new_project(self, sample_config):
        store = ConfigStore(sample_config)
        cfg = store.update_project("NewOrg/NewRepo", {"max_concurrent_agents": 5})
        assert cfg.max_concurrent_agents == 5
        store2 = ConfigStore(sample_config)
        assert store2.get_project("NewOrg/NewRepo") is not None

    def test_hot_reload_on_mtime_change(self, sample_config):
        store = ConfigStore(sample_config)
        store.reload()
        assert store.get_pipeline_state().paused is False
        # Write new config directly
        with open(sample_config) as f:
            raw = yaml.safe_load(f)
        raw["pipeline"]["paused"] = True
        with open(sample_config, "w") as f:
            yaml.dump(raw, f)
        # Force mtime change detection
        store._last_mtime = 0
        state = store.get_pipeline_state()
        assert state.paused is True

    def test_missing_file_uses_defaults(self, tmp_dir):
        store = ConfigStore(tmp_dir / "missing.yaml")
        state = store.get_pipeline_state()
        assert state.paused is False
        assert state.cycle_interval_minutes == 15

    def test_save_creates_file(self, tmp_dir):
        path = tmp_dir / "new_config.yaml"
        store = ConfigStore(path)
        store.reload()
        store.update_pipeline({"paused": True})
        assert path.exists()
        with open(path) as f:
            raw = yaml.safe_load(f)
        assert raw["pipeline"]["paused"] is True


# ---------------------------------------------------------------------------
# ControlPlane tests
# ---------------------------------------------------------------------------

class TestControlPlane:
    def test_get_projects(self, control_plane):
        projects = control_plane.get_projects()
        assert "example-org/my-app" in projects
        assert "ArtemisAI/SWE-Squad-DEV" in projects

    def test_get_project(self, control_plane):
        cfg = control_plane.get_project("example-org/my-app")
        assert cfg is not None
        assert cfg.max_concurrent_agents == 3

    def test_update_project(self, control_plane):
        cfg = control_plane.update_project("example-org/my-app", {
            "priority_weight": 0.95,
            "max_concurrent_agents": 5,
        })
        assert cfg.priority_weight == 0.95
        assert cfg.max_concurrent_agents == 5

    def test_update_projects_bulk(self, control_plane):
        results = control_plane.update_projects_bulk({
            "example-org/my-app": {"priority_weight": 0.8},
            "ArtemisAI/SWE-Squad-DEV": {"priority_weight": 0.2},
        })
        assert results["example-org/my-app"].priority_weight == 0.8
        assert results["ArtemisAI/SWE-Squad-DEV"].priority_weight == 0.2

    def test_pause_resume(self, control_plane):
        state = control_plane.pause_pipeline()
        assert state.paused is True
        assert control_plane.is_paused is True
        state = control_plane.resume_pipeline()
        assert state.paused is False
        assert control_plane.is_paused is False

    def test_set_cycle_interval(self, control_plane):
        state = control_plane.set_cycle_interval(5)
        assert state.cycle_interval_minutes == 5

    def test_set_cycle_interval_invalid(self, control_plane):
        with pytest.raises(ValueError):
            control_plane.set_cycle_interval(0)

    def test_set_model_routing(self, control_plane):
        state = control_plane.set_model_routing({"t1_heavy": "o3"})
        assert state.model_routing["t1_heavy"] == "o3"
        assert state.model_routing["t2_standard"] == "sonnet"  # unchanged

    def test_set_model_routing_invalid_tier(self, control_plane):
        with pytest.raises(ValueError, match="Invalid model tier key"):
            control_plane.set_model_routing({"t4_ultra": "magic"})

    def test_get_status(self, control_plane):
        status = control_plane.get_status()
        assert "pipeline" in status
        assert "queue_depth" in status
        assert "active_agents" in status
        assert "timestamp" in status

    def test_submit_urgent_ticket(self, control_plane):
        ticket = control_plane.submit_urgent_ticket({
            "title": "Production down",
            "description": "All endpoints returning 500",
            "severity": "critical",
            "project": "example-org/my-app",
        })
        assert ticket.priority == 0
        assert ticket.source == "urgent"
        assert ticket.severity == "critical"
        assert ticket.title == "Production down"
        # Verify it's in the queue
        queue = control_plane.get_queue()
        assert any(t.ticket_id == ticket.ticket_id for t in queue)

    def test_submit_urgent_with_executor(self, sample_config, queue_path):
        executed = []
        def mock_executor(ticket):
            executed.append(ticket.ticket_id)

        cp = ControlPlane(
            config_path=str(sample_config),
            queue_path=str(queue_path),
            executor=mock_executor,
        )
        ticket = cp.submit_urgent_ticket({"title": "Urgent fix"})
        # Wait for background thread
        import time
        time.sleep(0.2)
        assert ticket.ticket_id in executed

    def test_submit_urgent_paused_no_execute(self, sample_config, queue_path):
        executed = []
        def mock_executor(ticket):
            executed.append(ticket.ticket_id)

        cp = ControlPlane(
            config_path=str(sample_config),
            queue_path=str(queue_path),
            executor=mock_executor,
        )
        cp.pause_pipeline()
        ticket = cp.submit_urgent_ticket({"title": "While paused"})
        import time
        time.sleep(0.1)
        assert ticket.ticket_id not in executed
        assert ticket.status == "queued"

    def test_add_ticket(self, control_plane):
        ticket = control_plane.add_ticket({
            "title": "Refactor auth module",
            "severity": "medium",
            "project": "example-org/my-app",
        })
        assert ticket.priority == 50
        assert ticket.source == "api"

    def test_add_ticket_severity_priority_mapping(self, control_plane):
        t_critical = control_plane.add_ticket({"title": "c", "severity": "critical"})
        t_high = control_plane.add_ticket({"title": "h", "severity": "high"})
        t_medium = control_plane.add_ticket({"title": "m", "severity": "medium"})
        t_low = control_plane.add_ticket({"title": "l", "severity": "low"})
        assert t_critical.priority < t_high.priority < t_medium.priority < t_low.priority

    def test_get_queue_sorted(self, control_plane):
        control_plane.add_ticket({"title": "Low", "severity": "low"})
        control_plane.add_ticket({"title": "Critical", "severity": "critical"})
        control_plane.add_ticket({"title": "High", "severity": "high"})
        queue = control_plane.get_queue()
        assert queue[0].title == "Critical"
        assert queue[-1].title == "Low"

    def test_update_ticket_priority(self, control_plane):
        ticket = control_plane.add_ticket({"title": "Test"})
        updated = control_plane.update_ticket_priority(ticket.ticket_id, 5)
        assert updated.priority == 5

    def test_update_ticket_priority_clamped(self, control_plane):
        ticket = control_plane.add_ticket({"title": "Test"})
        updated = control_plane.update_ticket_priority(ticket.ticket_id, -10)
        assert updated.priority == 0
        updated = control_plane.update_ticket_priority(ticket.ticket_id, 200)
        assert updated.priority == 100

    def test_update_ticket_priority_not_found(self, control_plane):
        assert control_plane.update_ticket_priority("nonexistent", 5) is None

    def test_promote_ticket(self, control_plane):
        ticket = control_plane.add_ticket({"title": "Test", "severity": "low"})
        assert ticket.priority == 70
        promoted = control_plane.promote_ticket(ticket.ticket_id)
        assert promoted.priority == 0

    def test_remove_ticket(self, control_plane):
        ticket = control_plane.add_ticket({"title": "Remove me"})
        assert control_plane.remove_ticket(ticket.ticket_id) is True
        assert control_plane.get_ticket(ticket.ticket_id) is None

    def test_remove_ticket_not_found(self, control_plane):
        assert control_plane.remove_ticket("nonexistent") is False

    def test_get_ticket(self, control_plane):
        ticket = control_plane.add_ticket({"title": "Find me"})
        found = control_plane.get_ticket(ticket.ticket_id)
        assert found is not None
        assert found.title == "Find me"

    def test_reload_config(self, control_plane):
        # Should not raise
        control_plane.reload_config()


# ---------------------------------------------------------------------------
# ControlPlane API handler tests
# ---------------------------------------------------------------------------

class TestControlPlaneAPI:
    """Test the HTTP API handler functions with mock request objects."""

    def _make_handler(self, method="GET", path="/", body=None):
        """Create a mock BaseHTTPRequestHandler."""
        import io
        handler = MagicMock()
        handler.path = path
        handler.command = method
        handler.headers = {"Content-Type": "application/json"}
        if body:
            encoded = json.dumps(body).encode("utf-8")
            handler.headers["Content-Length"] = str(len(encoded))
            handler.rfile = io.BytesIO(encoded)
        else:
            handler.headers["Content-Length"] = "0"
            handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()

        # Make send_response etc. actually write to wfile
        def mock_send_response(code):
            handler._response_code = code
        handler.send_response = mock_send_response
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def _get_response(self, handler):
        handler.wfile.seek(0)
        return json.loads(handler.wfile.read().decode("utf-8"))

    def test_handle_get_status(self, control_plane):
        from src.swe_team.control_plane_api import handle_get
        handler = self._make_handler(path="/api/control/status")
        assert handle_get(handler, control_plane) is True
        resp = self._get_response(handler)
        assert "pipeline" in resp

    def test_handle_get_projects(self, control_plane):
        from src.swe_team.control_plane_api import handle_get
        handler = self._make_handler(path="/api/config/projects")
        assert handle_get(handler, control_plane) is True
        resp = self._get_response(handler)
        assert "example-org/my-app" in resp

    def test_handle_get_queue(self, control_plane):
        from src.swe_team.control_plane_api import handle_get
        control_plane.add_ticket({"title": "Test"})
        handler = self._make_handler(path="/api/queue")
        assert handle_get(handler, control_plane) is True
        resp = self._get_response(handler)
        assert isinstance(resp, list)
        assert len(resp) == 1

    def test_handle_get_single_project(self, control_plane):
        from src.swe_team.control_plane_api import handle_get
        handler = self._make_handler(path="/api/config/projects/example-org/my-app")
        assert handle_get(handler, control_plane) is True
        resp = self._get_response(handler)
        assert "example-org/my-app" in resp

    def test_handle_get_unmatched(self, control_plane):
        from src.swe_team.control_plane_api import handle_get
        handler = self._make_handler(path="/api/unknown")
        assert handle_get(handler, control_plane) is False

    def test_handle_post_urgent(self, control_plane):
        from src.swe_team.control_plane_api import handle_post
        handler = self._make_handler(
            method="POST",
            path="/api/tickets/urgent",
            body={"title": "Server down", "severity": "critical"},
        )
        assert handle_post(handler, control_plane) is True
        resp = self._get_response(handler)
        assert "ticket_id" in resp
        assert resp["status"] == "queued"

    def test_handle_post_urgent_no_title(self, control_plane):
        from src.swe_team.control_plane_api import handle_post
        handler = self._make_handler(
            method="POST",
            path="/api/tickets/urgent",
            body={"description": "missing title"},
        )
        assert handle_post(handler, control_plane) is True
        resp = self._get_response(handler)
        assert "error" in resp

    def test_handle_post_pause(self, control_plane):
        from src.swe_team.control_plane_api import handle_post
        handler = self._make_handler(method="POST", path="/api/control/pause")
        assert handle_post(handler, control_plane) is True
        resp = self._get_response(handler)
        assert resp["status"] == "paused"

    def test_handle_post_resume(self, control_plane):
        from src.swe_team.control_plane_api import handle_post
        handler = self._make_handler(method="POST", path="/api/control/resume")
        assert handle_post(handler, control_plane) is True
        resp = self._get_response(handler)
        assert resp["status"] == "resumed"

    def test_handle_post_promote(self, control_plane):
        from src.swe_team.control_plane_api import handle_post
        ticket = control_plane.add_ticket({"title": "Test", "severity": "low"})
        handler = self._make_handler(
            method="POST",
            path=f"/api/queue/{ticket.ticket_id}/promote",
        )
        assert handle_post(handler, control_plane) is True
        resp = self._get_response(handler)
        assert resp["priority"] == 0

    def test_handle_put_projects_bulk(self, control_plane):
        from src.swe_team.control_plane_api import handle_put
        handler = self._make_handler(
            method="PUT",
            path="/api/config/projects",
            body={"example-org/my-app": {"priority_weight": 0.99}},
        )
        assert handle_put(handler, control_plane) is True
        resp = self._get_response(handler)
        assert resp["example-org/my-app"]["priority_weight"] == 0.99

    def test_handle_put_cycle_interval(self, control_plane):
        from src.swe_team.control_plane_api import handle_put
        handler = self._make_handler(
            method="PUT",
            path="/api/control/cycle-interval",
            body={"cycle_interval_minutes": 10},
        )
        assert handle_put(handler, control_plane) is True
        resp = self._get_response(handler)
        assert resp["pipeline"]["cycle_interval_minutes"] == 10

    def test_handle_put_model_routing(self, control_plane):
        from src.swe_team.control_plane_api import handle_put
        handler = self._make_handler(
            method="PUT",
            path="/api/control/model-routing",
            body={"t1_heavy": "o3"},
        )
        assert handle_put(handler, control_plane) is True
        resp = self._get_response(handler)
        assert resp["pipeline"]["model_routing"]["t1_heavy"] == "o3"

    def test_handle_put_ticket_priority(self, control_plane):
        from src.swe_team.control_plane_api import handle_put
        ticket = control_plane.add_ticket({"title": "Test"})
        handler = self._make_handler(
            method="PUT",
            path=f"/api/queue/{ticket.ticket_id}/priority",
            body={"priority": 5},
        )
        assert handle_put(handler, control_plane) is True
        resp = self._get_response(handler)
        assert resp["priority"] == 5

    def test_handle_delete_ticket(self, control_plane):
        from src.swe_team.control_plane_api import handle_delete
        ticket = control_plane.add_ticket({"title": "Delete me"})
        handler = self._make_handler(
            method="DELETE",
            path=f"/api/queue/{ticket.ticket_id}",
        )
        assert handle_delete(handler, control_plane) is True
        resp = self._get_response(handler)
        assert "removed" in resp.get("message", "").lower() or "message" in resp

    def test_handle_delete_not_found(self, control_plane):
        from src.swe_team.control_plane_api import handle_delete
        handler = self._make_handler(
            method="DELETE",
            path="/api/queue/nonexistent",
        )
        assert handle_delete(handler, control_plane) is True
        resp = self._get_response(handler)
        assert "error" in resp


# ---------------------------------------------------------------------------
# WebUI panel render test
# ---------------------------------------------------------------------------

class TestControlPanelWebUI:
    def test_render_control_panel(self, control_plane):
        from src.swe_team.control_plane_api import render_control_panel
        html = render_control_panel(control_plane)
        assert "Control Plane" in html
        assert "Pipeline Status" in html
        assert "example-org/my-app" in html
        assert "API Reference" in html
        assert "/api/tickets/urgent" in html

    def test_render_with_queue(self, control_plane):
        from src.swe_team.control_plane_api import render_control_panel
        control_plane.add_ticket({"title": "Test ticket in queue", "severity": "high"})
        html = render_control_panel(control_plane)
        assert "Test ticket in queue" in html

    def test_render_paused_state(self, control_plane):
        from src.swe_team.control_plane_api import render_control_panel
        control_plane.pause_pipeline()
        html = render_control_panel(control_plane)
        assert "PAUSED" in html
