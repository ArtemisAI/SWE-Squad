from __future__ import annotations

from src.swe_team.workflow.executor import PipelineExecutor
from src.swe_team.workflow.models import (
    WorkflowConnection,
    WorkflowDefinition,
    WorkflowNode,
)


def _basic_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        id="wf-1",
        name="Test",
        team_id="team-a",
        nodes=[
            WorkflowNode(id="monitor", type="monitor"),
            WorkflowNode(id="triage", type="triage"),
            WorkflowNode(id="notify", type="notify"),
        ],
        connections=[
            WorkflowConnection(source_node_id="monitor", source_output=0, target_node_id="triage"),
            WorkflowConnection(source_node_id="triage", source_output=1, target_node_id="notify"),
        ],
    )


def test_topological_order_returns_connected_sequence() -> None:
    executor = PipelineExecutor(_basic_workflow())
    assert executor.topological_order() == ["monitor", "triage", "notify"]


def test_execute_routes_to_error_branch() -> None:
    seen: list[str] = []
    workflow = _basic_workflow()
    executor = PipelineExecutor(
        workflow,
        handlers={
            "monitor": lambda _p, _c: True,
            "triage": lambda _p, _c: False,
            "notify": lambda _p, _c: True,
        },
        hooks={"on_node_complete": lambda node_id, _payload: seen.append(node_id)},
    )

    result = executor.execute()
    assert result.success is False
    assert result.visited_nodes == ["monitor", "triage", "notify"]
    assert seen[-1] == "notify"


def test_execute_retries_failed_node_when_configured() -> None:
    attempts = {"triage": 0}
    workflow = WorkflowDefinition(
        nodes=[
            WorkflowNode(id="monitor", type="monitor"),
            WorkflowNode(id="triage", type="triage", parameters={"max_retries": 1}),
        ],
        connections=[WorkflowConnection(source_node_id="monitor", source_output=0, target_node_id="triage")],
    )

    def triage_handler(_params, _ctx):
        attempts["triage"] += 1
        return attempts["triage"] > 1

    executor = PipelineExecutor(
        workflow,
        handlers={
            "monitor": lambda _p, _c: True,
            "triage": triage_handler,
        },
    )

    result = executor.execute()
    assert result.success is True
    assert attempts["triage"] == 2


def test_execute_subworkflow_node() -> None:
    child = WorkflowDefinition(
        nodes=[WorkflowNode(id="child-monitor", type="monitor")],
        connections=[],
    )
    parent = WorkflowDefinition(
        nodes=[WorkflowNode(id="call-child", type="subworkflow", parameters={"workflow": child})],
        connections=[],
    )
    called = {"count": 0}
    executor = PipelineExecutor(
        parent,
        handlers={"monitor": lambda _p, _c: called.__setitem__("count", called["count"] + 1) or True},
    )

    result = executor.execute()
    assert result.success is True
    assert called["count"] == 1
