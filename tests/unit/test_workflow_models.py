"""Tests for the workflow data model (Phase 1: Workflow-as-Data)."""
from __future__ import annotations
import json
import pytest

from src.swe_team.workflow.models import (
    WorkflowNode,
    WorkflowConnection,
    WorkflowDefinition,
    create_default_pipeline,
)


class TestWorkflowNode:
    def test_default_creation(self):
        node = WorkflowNode()
        assert node.id  # auto-generated
        assert node.type == ""
        assert node.name == ""
        assert node.parameters == {}
        assert node.position == (0, 0)

    def test_explicit_creation(self):
        node = WorkflowNode(id="monitor", type="monitor", name="Monitor Logs", position=(100, 200))
        assert node.id == "monitor"
        assert node.type == "monitor"
        assert node.name == "Monitor Logs"
        assert node.position == (100, 200)

    def test_parameters(self):
        node = WorkflowNode(type="investigate", parameters={"max_attempts": 3, "timeout": 60})
        assert node.parameters["max_attempts"] == 3
        assert node.parameters["timeout"] == 60

    def test_auto_id_is_unique(self):
        n1 = WorkflowNode()
        n2 = WorkflowNode()
        assert n1.id != n2.id

    def test_auto_id_length(self):
        node = WorkflowNode()
        assert len(node.id) == 8


class TestWorkflowConnection:
    def test_default_creation(self):
        conn = WorkflowConnection()
        assert conn.source_node_id == ""
        assert conn.source_output == 0
        assert conn.target_node_id == ""
        assert conn.target_input == 0

    def test_explicit_creation(self):
        conn = WorkflowConnection(
            source_node_id="gate",
            source_output=1,
            target_node_id="notify",
            target_input=0,
        )
        assert conn.source_node_id == "gate"
        assert conn.source_output == 1
        assert conn.target_node_id == "notify"
        assert conn.target_input == 0


class TestWorkflowDefinition:
    def test_default_creation(self):
        wf = WorkflowDefinition()
        assert wf.id  # auto-generated UUID
        assert wf.name == "Default Pipeline"
        assert wf.description == ""
        assert wf.team_id == ""
        assert wf.nodes == []
        assert wf.connections == []
        assert wf.settings == {}
        assert wf.is_active is True

    def test_to_dict_structure(self):
        wf = WorkflowDefinition(
            id="test-id",
            name="Test Pipeline",
            description="A test",
            team_id="team-alpha",
            nodes=[WorkflowNode(id="n1", type="monitor", name="Monitor", position=(10, 20))],
            connections=[WorkflowConnection(source_node_id="n1", source_output=0, target_node_id="n2")],
            settings={"foo": "bar"},
            is_active=True,
        )
        d = wf.to_dict()
        assert d["id"] == "test-id"
        assert d["name"] == "Test Pipeline"
        assert d["description"] == "A test"
        assert d["team_id"] == "team-alpha"
        assert len(d["nodes"]) == 1
        assert d["nodes"][0]["id"] == "n1"
        assert d["nodes"][0]["type"] == "monitor"
        assert d["nodes"][0]["position"] == [10, 20]
        assert len(d["connections"]) == 1
        assert d["connections"][0]["source_node_id"] == "n1"
        assert d["settings"] == {"foo": "bar"}
        assert d["is_active"] is True

    def test_to_json_is_valid_json(self):
        wf = WorkflowDefinition(id="wf-1", name="JSON Test")
        raw = wf.to_json()
        parsed = json.loads(raw)
        assert parsed["id"] == "wf-1"
        assert parsed["name"] == "JSON Test"

    def test_from_dict_round_trip(self):
        original = WorkflowDefinition(
            id="round-trip-id",
            name="Round Trip",
            description="desc",
            team_id="team-beta",
            nodes=[WorkflowNode(id="a", type="triage", name="Triage", position=(50, 100))],
            connections=[
                WorkflowConnection(source_node_id="a", source_output=0, target_node_id="b", target_input=0)
            ],
            settings={"key": "value"},
            is_active=False,
        )
        d = original.to_dict()
        restored = WorkflowDefinition.from_dict(d)

        assert restored.id == original.id
        assert restored.name == original.name
        assert restored.description == original.description
        assert restored.team_id == original.team_id
        assert restored.is_active == original.is_active
        assert restored.settings == original.settings

        assert len(restored.nodes) == 1
        assert restored.nodes[0].id == "a"
        assert restored.nodes[0].type == "triage"
        assert restored.nodes[0].name == "Triage"
        assert restored.nodes[0].position == (50, 100)

        assert len(restored.connections) == 1
        assert restored.connections[0].source_node_id == "a"
        assert restored.connections[0].source_output == 0
        assert restored.connections[0].target_node_id == "b"
        assert restored.connections[0].target_input == 0

    def test_from_dict_missing_fields_use_defaults(self):
        wf = WorkflowDefinition.from_dict({})
        assert wf.id  # generated
        assert wf.name == ""
        assert wf.nodes == []
        assert wf.connections == []
        assert wf.is_active is True

    def test_json_round_trip(self):
        original = WorkflowDefinition(id="json-rt", name="JSON Round Trip", team_id="t1")
        json_str = original.to_json()
        restored = WorkflowDefinition.from_dict(json.loads(json_str))
        assert restored.id == original.id
        assert restored.name == original.name
        assert restored.team_id == original.team_id


class TestCreateDefaultPipeline:
    def test_returns_workflow_definition(self):
        pipeline = create_default_pipeline()
        assert isinstance(pipeline, WorkflowDefinition)

    def test_name(self):
        pipeline = create_default_pipeline()
        assert pipeline.name == "Default SWE Pipeline"

    def test_is_active(self):
        pipeline = create_default_pipeline()
        assert pipeline.is_active is True

    def test_team_id_propagated(self):
        pipeline = create_default_pipeline(team_id="swe-alpha")
        assert pipeline.team_id == "swe-alpha"

    def test_node_count(self):
        pipeline = create_default_pipeline()
        assert len(pipeline.nodes) == 7

    def test_connection_count(self):
        pipeline = create_default_pipeline()
        assert len(pipeline.connections) == 7

    def test_node_types(self):
        pipeline = create_default_pipeline()
        types = {n.type for n in pipeline.nodes}
        assert "monitor" in types
        assert "triage" in types
        assert "stability_gate" in types
        assert "investigate" in types
        assert "develop" in types
        assert "review" in types
        assert "notify" in types

    def test_node_ids_unique(self):
        pipeline = create_default_pipeline()
        ids = [n.id for n in pipeline.nodes]
        assert len(ids) == len(set(ids))

    def test_expected_node_ids(self):
        pipeline = create_default_pipeline()
        ids = {n.id for n in pipeline.nodes}
        assert ids == {"monitor", "triage", "gate", "investigate", "develop", "review", "notify"}

    def test_connection_source_ids_valid(self):
        pipeline = create_default_pipeline()
        node_ids = {n.id for n in pipeline.nodes}
        for conn in pipeline.connections:
            assert conn.source_node_id in node_ids, f"Invalid source: {conn.source_node_id}"
            assert conn.target_node_id in node_ids, f"Invalid target: {conn.target_node_id}"

    def test_monitor_connects_to_triage(self):
        pipeline = create_default_pipeline()
        conn = next(c for c in pipeline.connections if c.source_node_id == "monitor")
        assert conn.target_node_id == "triage"
        assert conn.source_output == 0

    def test_gate_blocked_connects_to_notify(self):
        pipeline = create_default_pipeline()
        gate_conns = [c for c in pipeline.connections if c.source_node_id == "gate"]
        blocked = next(c for c in gate_conns if c.source_output == 1)
        assert blocked.target_node_id == "notify"

    def test_develop_failure_connects_to_notify(self):
        pipeline = create_default_pipeline()
        develop_conns = [c for c in pipeline.connections if c.source_node_id == "develop"]
        failed = next(c for c in develop_conns if c.source_output == 1)
        assert failed.target_node_id == "notify"

    def test_serializable_to_json(self):
        pipeline = create_default_pipeline(team_id="test-team")
        raw = pipeline.to_json()
        parsed = json.loads(raw)
        assert parsed["name"] == "Default SWE Pipeline"
        assert len(parsed["nodes"]) == 7
        assert len(parsed["connections"]) == 7

    def test_round_trip_preserves_structure(self):
        original = create_default_pipeline(team_id="my-team")
        restored = WorkflowDefinition.from_dict(original.to_dict())
        assert len(restored.nodes) == len(original.nodes)
        assert len(restored.connections) == len(original.connections)
        assert restored.team_id == "my-team"
        orig_ids = {n.id for n in original.nodes}
        rest_ids = {n.id for n in restored.nodes}
        assert orig_ids == rest_ids
