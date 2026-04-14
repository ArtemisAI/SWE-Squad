"""Windsurf CLI coding engine -- CodingEngine implementation for Codeium Windsurf.

Windsurf is Codeium's AI-powered coding agent featuring the Cascade system
for multi-step autonomous workflows (code generation, refactoring, debugging).

This engine wraps the Windsurf CLI as a subprocess, implementing the
CodingEngine protocol for use in SWE-Squad agent workflows.

Registered in swe_team.yaml under providers.coding_engine.provider: windsurf.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult

logger = logging.getLogger(__name__)


class WindsurfCLIEngine:
    """Windsurf CLI engine -- wraps Codeium Windsurf for autonomous coding.

    Windsurf is Codeium's AI coding agent with Cascade workflows for:
    - Code generation
    - Refactoring
    - Debugging
    - Multi-step autonomous workflows

    The CLI can be invoked as `windsurf` or `cascade` depending on installation.

    Implements the :class:`CodingEngine` protocol.
    """

    def __init__(
        self,
        *,
        default_model: str = "",
        default_timeout: int = 300,
        binary: str | None = None,
        model_flag: str = "",
        args_template: List[str] | None = None,
        env_vars: Dict[str, str] | None = None,
        cascade_mode: bool = False,
    ) -> None:
        """Initialize the Windsurf CLI engine.

        Args:
            default_model: Default model name (Windsurf uses Codeium models,
                typically not user-selectable via CLI).
            default_timeout: Default subprocess timeout in seconds.
            binary: Path to the Windsurf binary. If None, searches for
                `windsurf` or `cascade` on PATH.
            model_flag: CLI flag for model selection (not typically used by Windsurf).
            args_template: Additional CLI arguments to include.
            env_vars: Extra environment variables to pass to subprocess.
            cascade_mode: If True, uses `cascade` subcommand for direct Cascade
                workflow execution. If False, uses `windsurf` command.
        """
        self._default_model = default_model or ""
        self._default_timeout = default_timeout
        self._cascade_mode = cascade_mode

        # Resolve binary: check windsurf, then cascade, then fallback
        resolved_binary = binary
        if not resolved_binary:
            if cascade_mode:
                resolved_binary = shutil.which("cascade") or shutil.which("windsurf") or "/usr/bin/cascade"
            else:
                resolved_binary = shutil.which("windsurf") or shutil.which("cascade") or "/usr/bin/windsurf"

        self._binary = resolved_binary
        self._model_flag = model_flag or ""
        self._args_template: List[str] = args_template or []
        self._env_vars: Dict[str, str] = env_vars or {}

    # -- CodingEngine protocol -------------------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "windsurf"

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
        """Execute Windsurf CLI with *prompt* and return an :class:`EngineResult`.

        Parameters match the :class:`CodingEngine` protocol.

        The prompt is sent via stdin. Windsurf typically processes the prompt
        and outputs the result to stdout.

        Args:
            prompt: The task description or prompt to send to Windsurf.
            model: Model name override (not typically used by Windsurf).
            timeout: Override timeout in seconds.
            cwd: Working directory for execution.
            env: Extra environment variables.
            session_id: Session identifier (not currently used by Windsurf CLI).

        Returns:
            EngineResult with stdout, stderr, returncode, and model fields.
        """
        effective_timeout = timeout or self._default_timeout
        cmd = self._build_cmd(prompt, model)

        # Merge environment
        run_env: dict | None = None
        if self._env_vars or env:
            run_env = dict(os.environ)
            run_env.update(self._env_vars)
            if env:
                run_env.update(env)

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=effective_timeout,
                cwd=cwd,
                env=run_env,
            )
            return EngineResult(
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
                model=model or self._default_model,
                metadata={"cascade_mode": self._cascade_mode},
            )
        except subprocess.TimeoutExpired:
            logger.warning("Windsurf CLI timed out after %ds", effective_timeout)
            raise
        except FileNotFoundError:
            logger.error("Windsurf CLI binary not found: %s", self._binary)
            return EngineResult(
                stdout="",
                stderr=f"Binary not found: {self._binary}",
                returncode=-1,
                model=model or self._default_model,
            )

    def health_check(self) -> bool:
        """Return True if the Windsurf CLI binary is found."""
        return self.is_available()

    # -- Command builder -------------------------------------------------------

    def _build_cmd(
        self,
        prompt: str,
        model: str | None = None,
    ) -> List[str]:
        """Build the subprocess argument list for a Windsurf invocation.

        Args:
            prompt: The prompt text (not used in cmd building, sent via stdin).
            model: Optional model name (not typically used by Windsurf).

        Returns:
            List of command-line arguments.
        """
        cmd: List[str] = [self._binary]

        # Add cascade subcommand if in cascade mode and binary is windsurf
        if self._cascade_mode and "windsurf" in self._binary:
            cmd.append("cascade")

        # Model flag (for future compatibility)
        if model and self._model_flag:
            cmd.extend([self._model_flag, model])

        # Add configured args template
        cmd.extend(self._args_template)

        return cmd

    # -- Convenience -----------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the Windsurf CLI binary exists."""
        return bool(
            shutil.which("windsurf")
            or shutil.which("cascade")
            or (self._binary and (Path(self._binary).exists() if self._binary else False))
        )

    def model(self) -> str:
        """Return the default model name (typically empty for Windsurf)."""
        return self._default_model
