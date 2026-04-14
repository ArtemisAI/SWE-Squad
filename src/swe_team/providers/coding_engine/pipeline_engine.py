"""Pipeline coding engine -- chain multiple CodingEngine providers.

Allows a workflow-style handover where each stage can use a different engine
provider and pass context to the next stage.
"""
from __future__ import annotations

import logging
import inspect
from typing import Any, Dict, List, Optional

from src.swe_team.providers.coding_engine.base import EngineResult

logger = logging.getLogger(__name__)


class PipelineEngine:
    """Run a configured sequence of coding-engine stages."""

    def __init__(
        self,
        *,
        workflow_name: str = "default",
        default_model: str = "sonnet",
        default_timeout: int = 300,
        stages: Optional[List[Dict[str, Any]]] = None,
        stop_on_first_failure: bool = True,
        continue_on_skip: bool = True,
    ) -> None:
        self._workflow_name = workflow_name
        self._default_model = default_model
        self._default_timeout = default_timeout
        self._stages: List[Dict[str, Any]] = stages or []
        self._stop_on_first_failure = stop_on_first_failure
        self._continue_on_skip = continue_on_skip

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "pipeline"

    def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout: int | None = None,
        cwd: Optional[str] = None,
        env: dict | None = None,
        session_id: str | None = None,
    ) -> EngineResult:
        """Execute configured workflow stages in order."""
        if not self._stages:
            return EngineResult(
                stdout=prompt,
                stderr="",
                returncode=0,
                model=model or self._default_model,
                metadata={
                    "workflow_name": self._workflow_name,
                    "stages": [],
                    "had_failures": False,
                },
            )

        from src.swe_team.providers.coding_engine import resolve_engine

        previous_output = prompt
        stage_records: List[Dict[str, Any]] = []
        total_cost = 0.0
        had_failures = False

        for index, stage in enumerate(self._stages):
            stage_name = str(stage.get("name") or f"stage_{index + 1}")
            provider = stage.get("provider")
            if not provider:
                msg = f"Stage '{stage_name}' missing provider"
                stage_records.append(
                    {"name": stage_name, "provider": None, "status": "skipped", "reason": msg}
                )
                if self._continue_on_skip:
                    continue
                return EngineResult(
                    stdout=previous_output,
                    stderr=msg,
                    returncode=1,
                    model=model or self._default_model,
                    metadata={
                        "workflow_name": self._workflow_name,
                        "stages": stage_records,
                        "had_failures": True,
                        "failed_stage": stage_name,
                    },
                )

            stage_prompt = self._build_stage_prompt(stage, prompt, previous_output, stage_name)
            stage_cfg = dict(stage.get("config") or {})
            stage_model = stage.get("model") or model or stage_cfg.get("default_model") or self._default_model
            if "timeout_seconds" in stage and "timeout" not in stage:
                logger.warning(
                    "Stage '%s' uses deprecated key 'timeout_seconds'; use 'timeout' instead",
                    stage_name,
                )
            stage_timeout = int(
                stage.get("timeout", stage.get("timeout_seconds", timeout or self._default_timeout))
            )

            try:
                engine = resolve_engine(str(provider), stage_cfg)
            except Exception as exc:
                msg = f"Failed to resolve engine '{provider}' for stage '{stage_name}': {exc}"
                logger.warning(msg)
                stage_records.append(
                    {"name": stage_name, "provider": provider, "status": "skipped", "reason": str(exc)}
                )
                if self._continue_on_skip:
                    continue
                return EngineResult(
                    stdout=previous_output,
                    stderr=msg,
                    returncode=1,
                    model=stage_model,
                    metadata={
                        "workflow_name": self._workflow_name,
                        "stages": stage_records,
                        "had_failures": True,
                        "failed_stage": stage_name,
                    },
                )

            result = self._run_stage_engine(
                engine,
                stage_prompt,
                model=stage_model,
                timeout=stage_timeout,
                cwd=cwd,
                env=env,
                session_id=session_id,
            )
            if result.cost_usd is not None:
                total_cost += float(result.cost_usd)

            stage_records.append(
                {
                    "name": stage_name,
                    "provider": provider,
                    "status": "success" if result.success else "failed",
                    "returncode": result.returncode,
                    "model": result.model,
                }
            )

            if result.success:
                previous_output = result.stdout
                continue

            had_failures = True
            if self._stop_on_first_failure:
                return EngineResult(
                    stdout=previous_output,
                    stderr=result.stderr or f"Stage '{stage_name}' failed",
                    returncode=result.returncode or 1,
                    model=result.model or stage_model,
                    cost_usd=total_cost if total_cost > 0 else None,
                    session_id=result.session_id,
                    metadata={
                        "workflow_name": self._workflow_name,
                        "stages": stage_records,
                        "had_failures": True,
                        "failed_stage": stage_name,
                    },
                )

        return EngineResult(
            stdout=previous_output,
            stderr="",
            returncode=1 if had_failures else 0,
            model=model or self._default_model,
            cost_usd=total_cost if total_cost > 0 else None,
            metadata={
                "workflow_name": self._workflow_name,
                "stages": stage_records,
                "had_failures": had_failures,
            },
        )

    def health_check(self) -> bool:
        """Return True when all configured stage engines are healthy."""
        from src.swe_team.providers.coding_engine import resolve_engine

        for stage in self._stages:
            provider = stage.get("provider")
            if not provider:
                if self._continue_on_skip:
                    continue
                return False
            try:
                engine = resolve_engine(str(provider), dict(stage.get("config") or {}))
            except Exception:
                return False
            if not engine.health_check():
                return False
        return True

    @staticmethod
    def _build_stage_prompt(
        stage: Dict[str, Any],
        root_prompt: str,
        previous_output: str,
        stage_name: str,
    ) -> str:
        template = str(stage.get("prompt_template", "{prompt}"))
        try:
            return template.format(
                prompt=root_prompt,
                previous_output=previous_output,
                stage_name=stage_name,
            )
        except Exception as exc:
            logger.warning("Invalid prompt_template for stage '%s': %s", stage_name, exc)
            return previous_output or root_prompt

    @staticmethod
    def _run_stage_engine(
        engine: Any,
        prompt: str,
        *,
        model: str,
        timeout: int,
        cwd: Optional[str],
        env: Optional[dict],
        session_id: Optional[str],
    ) -> EngineResult:
        """Invoke stage engine while remaining compatible with minimal protocol."""
        run_kwargs: Dict[str, Any] = {
            "model": model,
            "timeout": timeout,
            "cwd": cwd,
        }
        try:
            signature = inspect.signature(engine.run)
        except (TypeError, ValueError):
            signature = None

        accepts_var_kwargs = bool(
            signature and any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
        )
        if env is not None and (accepts_var_kwargs or (signature and "env" in signature.parameters)):
            run_kwargs["env"] = env
        if session_id is not None and (accepts_var_kwargs or (signature and "session_id" in signature.parameters)):
            run_kwargs["session_id"] = session_id

        return engine.run(prompt, **run_kwargs)
