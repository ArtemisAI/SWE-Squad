"""Bolt CLI coding engine -- CodingEngine implementation for Bolt (StackBlitz).

Wraps ``bolt`` CLI from StackBlitz as a subprocess. Bolt is an AI-powered
full-stack web development agent that scaffolds entire web applications
from prompts, handling frontend, backend, and deployment configuration.

Registered in swe_team.yaml under providers.coding_engine.provider: bolt.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult

logger = logging.getLogger(__name__)


class BoltCLIEngine:
    """Bolt CLI engine -- wraps Bolt (StackBlitz) for web scaffolding.

    Bolt is an AI-powered full-stack web development agent that scaffolds
    entire web applications from prompts. This engine wraps the Bolt CLI
    as a subprocess, implementing the CodingEngine protocol.

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
    ) -> None:
        self._default_model = default_model or ""
        self._default_timeout = default_timeout
        self._binary = binary or shutil.which("bolt") or "/usr/local/bin/bolt"
        self._model_flag = model_flag or ""
        self._args_template = args_template or []
        self._env_vars: Dict[str, str] = env_vars or {}

    # -- CodingEngine protocol -------------------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "bolt"

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
        """Execute Bolt CLI with *prompt* and return an :class:`EngineResult`.

        Parameters match the :class:`CodingEngine` protocol.
        """
        effective_timeout = timeout or self._default_timeout
        cmd = self._build_cmd(prompt, model)

        # Merge environment
        run_env: dict | None = None
        if self._env_vars or env:
            import os
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
            )
        except subprocess.TimeoutExpired:
            logger.warning("Bolt CLI timed out after %ds", effective_timeout)
            raise
        except FileNotFoundError:
            logger.error("Bolt CLI binary not found: %s", self._binary)
            return EngineResult(
                stdout="",
                stderr=f"Binary not found: {self._binary}",
                returncode=-1,
                model=model or self._default_model,
            )

    def health_check(self) -> bool:
        """Return True if the Bolt CLI binary is found."""
        return self.is_available()

    # -- Command builder -------------------------------------------------------

    def _build_cmd(
        self,
        prompt: str,
        model: str | None = None,
    ) -> List[str]:
        """Build the subprocess argument list for a Bolt invocation."""
        cmd: List[str] = [self._binary]

        # Bolt typically doesn't use model flags like other CLIs,
        # but we include the mechanism for future compatibility
        if model and self._model_flag:
            cmd.extend([self._model_flag, model])

        # Add any configured args template
        cmd.extend(self._args_template)

        return cmd

    # -- Convenience -----------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the Bolt CLI binary exists."""
        return bool(shutil.which("bolt") or (self._binary and shutil.which(self._binary)))

    def model(self) -> str:
        """Return the default model name (empty for Bolt)."""
        return self._default_model
