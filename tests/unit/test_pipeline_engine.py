"""Tests for the PipelineEngine coding connector."""
from __future__ import annotations

import pytest

from src.swe_team.providers.coding_engine import resolve_engine
from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.pipeline_engine import PipelineEngine


class _MockSuccessEngine:
    def __init__(self, name: str, *, suffix: str = "", cost: float | None = None) -> None:
        self._name = name
        self._suffix = suffix
        self._cost = cost

    @property
    def name(self) -> str:
        return self._name

    def run(self, prompt, *, model, timeout, cwd=None, env=None, session_id=None):
        return EngineResult(
            stdout=f"{prompt}{self._suffix}",
            stderr="",
            returncode=0,
            cost_usd=self._cost,
            model=model,
        )

    def health_check(self):
        return True


class _MockFailEngine:
    @property
    def name(self) -> str:
        return "failing"

    def run(self, prompt, *, model, timeout, cwd=None, env=None, session_id=None):
        return EngineResult(stdout="", stderr="boom", returncode=2, model=model)

    def health_check(self):
        return True


def test_pipeline_engine_is_coding_engine_protocol():
    engine = PipelineEngine()
    assert isinstance(engine, CodingEngine)
    assert engine.name == "pipeline"


def test_pipeline_engine_resolves_from_registry():
    engine = resolve_engine("pipeline", {"stages": []})
    assert isinstance(engine, PipelineEngine)
    assert engine.name == "pipeline"


def test_mock_engines_satisfy_protocol():
    assert isinstance(_MockSuccessEngine("ok"), CodingEngine)
    assert isinstance(_MockFailEngine(), CodingEngine)


def test_pipeline_engine_empty_stages_returns_input():
    engine = PipelineEngine(stages=[])
    result = engine.run("hello", model="sonnet", timeout=30)
    assert result.success is True
    assert result.stdout == "hello"
    assert result.metadata["workflow_name"] == "default"
    assert result.metadata["stages"] == []


def test_pipeline_engine_chains_stage_outputs(monkeypatch):
    from src.swe_team.providers import coding_engine as registry

    def _fake_resolve(provider_name: str, config=None):
        if provider_name == "s1":
            return _MockSuccessEngine("s1", suffix="::investigated", cost=0.1)
        if provider_name == "s2":
            return _MockSuccessEngine("s2", suffix="::fixed", cost=0.2)
        raise ValueError(provider_name)

    monkeypatch.setattr(registry, "resolve_engine", _fake_resolve)

    engine = PipelineEngine(
        workflow_name="handover",
        stages=[
            {"name": "investigate", "provider": "s1"},
            {"name": "develop", "provider": "s2", "prompt_template": "context={previous_output}"},
        ],
    )
    result = engine.run("ticket prompt", model="sonnet", timeout=30)

    assert result.success is True
    assert result.stdout == "context=ticket prompt::investigated::fixed"
    assert result.cost_usd == pytest.approx(0.3)
    assert len(result.metadata["stages"]) == 2
    assert result.metadata["had_failures"] is False


def test_pipeline_engine_stops_on_first_failure(monkeypatch):
    from src.swe_team.providers import coding_engine as registry

    def _fake_resolve(provider_name: str, config=None):
        if provider_name == "ok":
            return _MockSuccessEngine("ok", suffix="::ok")
        if provider_name == "fail":
            return _MockFailEngine()
        raise ValueError(provider_name)

    monkeypatch.setattr(registry, "resolve_engine", _fake_resolve)

    engine = PipelineEngine(
        stages=[
            {"name": "a", "provider": "ok"},
            {"name": "b", "provider": "fail"},
            {"name": "c", "provider": "ok"},
        ],
        stop_on_first_failure=True,
    )

    result = engine.run("x", model="m", timeout=10)
    assert result.success is False
    assert result.returncode == 2
    assert result.metadata["failed_stage"] == "b"


def test_pipeline_engine_skips_missing_provider_when_allowed(monkeypatch):
    from src.swe_team.providers import coding_engine as registry

    monkeypatch.setattr(
        registry,
        "resolve_engine",
        lambda provider_name, config=None: _MockSuccessEngine(provider_name, suffix="::done"),
    )

    engine = PipelineEngine(
        continue_on_skip=True,
        stages=[
            {"name": "missing"},  # no provider
            {"name": "actual", "provider": "ok"},
        ],
    )
    result = engine.run("base", model="m", timeout=10)
    assert result.success is True
    assert result.stdout == "base::done"
    assert result.metadata["stages"][0]["status"] == "skipped"


def test_pipeline_engine_health_check(monkeypatch):
    from src.swe_team.providers import coding_engine as registry

    monkeypatch.setattr(
        registry,
        "resolve_engine",
        lambda provider_name, config=None: _MockSuccessEngine(provider_name),
    )
    engine = PipelineEngine(stages=[{"provider": "s1"}, {"provider": "s2"}])
    assert engine.health_check() is True


def test_pipeline_engine_invalid_template_falls_back_to_previous_output(monkeypatch):
    from src.swe_team.providers import coding_engine as registry

    monkeypatch.setattr(
        registry,
        "resolve_engine",
        lambda provider_name, config=None: _MockSuccessEngine(provider_name, suffix="::ok"),
    )

    engine = PipelineEngine(
        stages=[
            {"name": "a", "provider": "s1", "prompt_template": "{missing_placeholder}"},
        ]
    )
    result = engine.run("seed", model="m", timeout=10)
    assert result.success is True
    assert result.stdout == "seed::ok"
