"""Workflow data model — pipeline definitions as data, not code."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import json
import uuid
import logging


logger = logging.getLogger(__name__)


@dataclass
class WorkflowNode:
    """A single step in a workflow pipeline."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    type: str = ""           # "monitor", "triage", "investigate", "develop", "review", "notify", "conditional", "loop"
    name: str = ""           # Human-readable name
    parameters: Dict[str, Any] = field(default_factory=dict)
    position: tuple = (0, 0)  # For UI canvas rendering


@dataclass
class WorkflowConnection:
    """A connection between two nodes."""
    source_node_id: str = ""
    source_output: int = 0   # 0 = success, 1 = error
    target_node_id: str = ""
    target_input: int = 0


@dataclass
class WorkflowDefinition:
    """A complete workflow pipeline definition."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Default Pipeline"
    description: str = ""
    team_id: str = ""
    nodes: List[WorkflowNode] = field(default_factory=list)
    connections: List[WorkflowConnection] = field(default_factory=list)
    settings: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = True

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "team_id": self.team_id,
            "nodes": [
                {
                    "id": n.id,
                    "type": n.type,
                    "name": n.name,
                    "parameters": n.parameters,
                    "position": list(n.position),
                }
                for n in self.nodes
            ],
            "connections": [
                {
                    "source_node_id": c.source_node_id,
                    "source_output": c.source_output,
                    "target_node_id": c.target_node_id,
                    "target_input": c.target_input,
                }
                for c in self.connections
            ],
            "settings": self.settings,
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WorkflowDefinition:
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            name=data.get("name", ""),
            description=data.get("description", ""),
            team_id=data.get("team_id", ""),
            nodes=[
                WorkflowNode(
                    id=n.get("id", str(uuid.uuid4())[:8]),
                    type=n.get("type", ""),
                    name=n.get("name", ""),
                    parameters=n.get("parameters", {}),
                    position=tuple(n.get("position", [0, 0])),
                )
                for n in data.get("nodes", [])
            ],
            connections=[
                WorkflowConnection(
                    source_node_id=c.get("source_node_id", ""),
                    source_output=c.get("source_output", 0),
                    target_node_id=c.get("target_node_id", ""),
                    target_input=c.get("target_input", 0),
                )
                for c in data.get("connections", [])
            ],
            settings=data.get("settings", {}),
            is_active=data.get("is_active", True),
        )


def create_default_pipeline(team_id: str = "") -> WorkflowDefinition:
    """Create the default SWE-Squad pipeline matching the current hardcoded one."""
    monitor = WorkflowNode(id="monitor", type="monitor", name="Monitor Logs", position=(100, 200))
    triage = WorkflowNode(id="triage", type="triage", name="Triage & Classify", position=(300, 200))
    gate = WorkflowNode(id="gate", type="stability_gate", name="Stability Gate", position=(500, 200))
    investigate = WorkflowNode(id="investigate", type="investigate", name="Investigate", position=(700, 200))
    develop = WorkflowNode(id="develop", type="develop", name="Develop Fix", position=(900, 200))
    review = WorkflowNode(id="review", type="review", name="Code Review", position=(1100, 200))
    notify = WorkflowNode(id="notify", type="notify", name="Notify", position=(500, 400))

    return WorkflowDefinition(
        name="Default SWE Pipeline",
        description="Standard monitor → triage → gate → investigate → develop → review pipeline",
        team_id=team_id,
        nodes=[monitor, triage, gate, investigate, develop, review, notify],
        connections=[
            WorkflowConnection(source_node_id="monitor", source_output=0, target_node_id="triage"),
            WorkflowConnection(source_node_id="triage", source_output=0, target_node_id="gate"),
            WorkflowConnection(source_node_id="gate", source_output=0, target_node_id="investigate"),
            WorkflowConnection(source_node_id="gate", source_output=1, target_node_id="notify"),  # blocked → notify
            WorkflowConnection(source_node_id="investigate", source_output=0, target_node_id="develop"),
            WorkflowConnection(source_node_id="develop", source_output=0, target_node_id="review"),
            WorkflowConnection(source_node_id="develop", source_output=1, target_node_id="notify"),  # failed → notify
        ],
    )


def load_workflow_definition(team_id: str = "", store: Optional[Any] = None) -> WorkflowDefinition:
    """Load a workflow definition for a team, falling back to the default pipeline.

    The loader is intentionally defensive: if a workflow backend is unavailable,
    misconfigured, or returns malformed data, the system keeps running with the
    built-in default workflow.
    """
    if store and hasattr(store, "get_workflow_definition"):
        try:
            loaded = store.get_workflow_definition(team_id=team_id)
            if isinstance(loaded, WorkflowDefinition):
                if not loaded.team_id:
                    loaded.team_id = team_id
                return loaded
            if isinstance(loaded, dict):
                wf = WorkflowDefinition.from_dict(loaded)
                if not wf.team_id:
                    wf.team_id = team_id
                return wf
        except Exception as exc:
            logger.warning("Failed to load workflow definition for team %s: %s", team_id or "default", exc)

    return create_default_pipeline(team_id=team_id)
