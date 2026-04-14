from __future__ import annotations

from src.swe_team.workflow.models import WorkflowDefinition, load_workflow_definition


class _StoreDict:
    def get_workflow_definition(self, team_id: str):
        return {
            "id": "wf-db",
            "name": "DB Workflow",
            "team_id": team_id,
            "nodes": [{"id": "monitor", "type": "monitor", "name": "Monitor"}],
            "connections": [],
            "settings": {"source": "db"},
        }


class _StoreObject:
    def get_workflow_definition(self, team_id: str):
        return WorkflowDefinition(
            id="wf-obj",
            name="Object Workflow",
            team_id=team_id,
        )


class _StoreError:
    def get_workflow_definition(self, team_id: str):
        raise RuntimeError("db down")


def test_load_workflow_definition_from_dict_store() -> None:
    wf = load_workflow_definition(team_id="alpha", store=_StoreDict())
    assert wf.id == "wf-db"
    assert wf.team_id == "alpha"
    assert wf.settings["source"] == "db"


def test_load_workflow_definition_from_object_store() -> None:
    wf = load_workflow_definition(team_id="beta", store=_StoreObject())
    assert wf.id == "wf-obj"
    assert wf.team_id == "beta"


def test_load_workflow_definition_falls_back_to_default() -> None:
    wf = load_workflow_definition(team_id="gamma", store=_StoreError())
    assert wf.name == "Default SWE Pipeline"
    assert wf.team_id == "gamma"
