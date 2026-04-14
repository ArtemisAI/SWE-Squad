from __future__ import annotations

from types import SimpleNamespace

from scripts.ops.swe_team_runner import _resolve_workflow_phase_plan
from src.swe_team.workflow.models import WorkflowConnection, WorkflowDefinition, WorkflowNode


class _WorkflowStore:
    def get_workflow_definition(self, team_id: str):
        return WorkflowDefinition(
            id="wf-custom",
            name="Custom",
            team_id=team_id,
            nodes=[
                WorkflowNode(id="monitor", type="monitor"),
                WorkflowNode(id="triage", type="triage"),
                WorkflowNode(id="review", type="review"),
            ],
            connections=[
                WorkflowConnection(source_node_id="monitor", source_output=0, target_node_id="triage"),
                WorkflowConnection(source_node_id="triage", source_output=0, target_node_id="review"),
            ],
        )


def test_resolve_workflow_phase_plan_uses_loaded_workflow() -> None:
    cfg = SimpleNamespace(team_id="swe-squad-alpha")
    workflow, phase_order, phase_set = _resolve_workflow_phase_plan(cfg, _WorkflowStore())
    assert workflow.id == "wf-custom"
    assert phase_order == ["monitor", "triage", "review"]
    assert phase_set == {"monitor", "triage", "review"}
