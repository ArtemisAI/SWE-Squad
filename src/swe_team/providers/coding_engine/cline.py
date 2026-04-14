"""Cline CLI coding engine -- CodingEngine implementation for Cline.

Wraps ``cline`` CLI as a subprocess.  Extends
:class:`GenericCLIEngine` with Cline-specific defaults.

Registered in swe_team.yaml under providers.coding_engine.provider: cline.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)


class ClineCLIEngine(GenericCLIEngine):
    """Cline CLI engine -- wraps ``cline``.

    Cline is an autonomous AI coding agent CLI. This engine wraps
    it as a subprocess, implementing the CodingEngine protocol.

    Implements the :class:`CodingEngine` protocol.
    """

    def __init__(
        self,
        *,
        default_model: str = "",
        default_timeout: int = 300,
        binary: str | None = None,
        model_flag: str = "--model",
        task_flag: str = "--task",
        approval_flag: str = "--auto-approve",
        auto_approve: bool = True,
        no_edit: bool = False,
        no_edit_flag: str = "--no-edit",
        output_format: str = "json",
        session_flag: str | None = "--session",
        cwd_flag: str | None = "--cwd",
        include_cwd_flag: bool = True,
        env_vars: Dict[str, str] | None = None,
        **kwargs,
    ) -> None:
        resolved_binary = binary or shutil.which("cline") or "/usr/local/bin/cline"

        args_template: List[str] = [task_flag, "{prompt}"]
        if auto_approve:
            args_template.append(approval_flag)
        if no_edit:
            args_template.append(no_edit_flag)

        self._cwd_flag = cwd_flag if include_cwd_flag else None

        super().__init__(
            binary=resolved_binary,
            default_model=default_model,
            default_timeout=default_timeout,
            args_template=args_template,
            model_flag=model_flag,
            prompt_via="args",
            output_format=output_format,
            session_flag=session_flag,
            env_vars=env_vars,
        )

    # -- CodingEngine protocol override ----------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "cline"

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
        """Execute Cline with optional session and headless flags."""
        effective_model = model or self._default_model
        effective_timeout = timeout or self._default_timeout
        cmd = self._build_cmd(prompt, effective_model, session_id=session_id)
        if cwd and self._cwd_flag:
            cmd.extend([self._cwd_flag, cwd])

        run_env: dict | None = None
        if self._env_vars or env:
            run_env = dict(os.environ)
            run_env.update(self._env_vars)
            if env:
                run_env.update(env)

        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=effective_timeout,
                cwd=cwd,
                env=run_env,
            )
            return self._build_engine_result(
                result.stdout, result.stderr, result.returncode, effective_model,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "%s timed out after %ds", self.name, effective_timeout,
            )
            raise
        except FileNotFoundError:
            logger.error("CLI binary not found: %s", self._binary)
            return EngineResult(
                stdout="",
                stderr=f"Binary not found: {self._binary}",
                returncode=-1,
                model=effective_model,
            )

    def _build_engine_result(
        self,
        raw_stdout: str,
        stderr: str,
        returncode: int,
        model: str,
    ) -> EngineResult:
        """Extract summary, tool-use log, session, and usage metadata from Cline output."""
        parsed: dict = {}
        text = raw_stdout.strip()
        if self._output_format == "json" and text:
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    parsed = data
            except (json.JSONDecodeError, ValueError):
                parsed = {}

        summary = text
        used_summary_key = False
        if parsed:
            for key in ("result", "summary", "output", "message"):
                value = parsed.get(key)
                if value is not None:
                    summary = value if isinstance(value, str) else str(value)
                    used_summary_key = True
                    break

        parsed_usage = parsed.get("usage")
        usage = parsed_usage if isinstance(parsed_usage, dict) else {}
        cost = self._extract_cost(parsed=parsed, usage=usage)
        input_tokens = self._extract_with_aliases(
            usage=usage, parsed=parsed, primary_key="input_tokens", aliases=(),
        )
        output_tokens = self._extract_with_aliases(
            usage=usage, parsed=parsed, primary_key="output_tokens", aliases=(),
        )
        cache_read_tokens = usage.get("cache_read_input_tokens", usage.get("cache_read_tokens"))
        cache_creation_tokens = usage.get("cache_creation_input_tokens", usage.get("cache_creation_tokens"))
        session_id = parsed.get("session_id", parsed.get("sessionId"))

        metadata: Dict[str, object] = {}
        # Cline telemetry format can vary by mode/version; support common key styles.
        for key in ("tool_use", "tool_calls", "tools", "toolUse"):
            if key in parsed:
                metadata["tool_use"] = parsed[key]
                break
        if parsed and not used_summary_key:
            metadata["summary_fallback"] = "raw_stdout"

        return EngineResult(
            stdout=summary,
            stderr=stderr,
            returncode=returncode,
            cost_usd=self._to_float(cost),
            model=model,
            input_tokens=self._to_int(input_tokens),
            output_tokens=self._to_int(output_tokens),
            cache_read_tokens=self._to_int(cache_read_tokens),
            cache_creation_tokens=self._to_int(cache_creation_tokens),
            session_id=session_id if isinstance(session_id, str) else None,
            metadata=metadata,
        )

    @staticmethod
    def _to_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_cost(*, parsed: dict, usage: dict) -> object:
        """Cost priority: usage.cost_usd → parsed.cost_usd → parsed.cost."""
        usage_cost = usage.get("cost_usd")
        if usage_cost is not None:
            return usage_cost
        parsed_cost_usd = parsed.get("cost_usd")
        if parsed_cost_usd is not None:
            return parsed_cost_usd
        return parsed.get("cost")

    @staticmethod
    def _extract_with_aliases(
        *,
        usage: dict,
        parsed: dict,
        primary_key: str,
        aliases: tuple[str, ...],
    ) -> object:
        if primary_key in usage:
            return usage[primary_key]
        for key in aliases:
            if key in usage:
                return usage[key]
        if primary_key in parsed:
            return parsed[primary_key]
        for key in aliases:
            if key in parsed:
                return parsed[key]
        return None
