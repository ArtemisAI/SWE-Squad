"""Shell-GPT (sgpt) CLI coding engine -- CodingEngine implementation for sgpt.

Shell-GPT is a lightweight, pragmatic CLI tool that brings LLM capabilities to
the terminal. It generates shell commands, code snippets, and text from natural
language. Focused on simplicity and quick interactions rather than autonomous
agent behavior.

Registered in swe_team.yaml under providers.coding_engine.provider: sgpt.
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


class SgpTCLEngine:
    """Shell-GPT CLI engine -- wraps ``sgpt`` for terminal LLM interactions.

    Shell-GPT provides:
    - Shell command generation from natural language
    - Code snippet generation
    - General text completion
    - Quick, single-turn interactions

    The CLI is invoked as ``sgpt`` and processes prompts via stdin or args.

    Implements the :class:`CodingEngine` protocol.
    """

    def __init__(
        self,
        *,
        default_model: str = "",
        default_timeout: int = 300,
        binary: str | None = None,
        model_flag: str = "--model",
        args_template: List[str] | None = None,
        env_vars: Dict[str, str] | None = None,
    ) -> None:
        """Initialize Shell-GPT CLI engine.

        Args:
            default_model: Default model name (e.g., 'gpt-4o', 'gpt-4-turbo').
                Empty string uses sgpt's default model.
            default_timeout: Default subprocess timeout in seconds.
            binary: Path to sgpt binary. If None, searches for
                ``sgpt`` on PATH.
            model_flag: CLI flag for model selection (default: --model).
                Shell-GPT also supports ``-m`` as a shorthand.
            args_template: Additional CLI arguments to include.
            env_vars: Extra environment variables to pass to subprocess.
        """
        self._default_model = default_model or ""
        self._default_timeout = default_timeout

        # Resolve binary: check sgpt on PATH, then fallback
        resolved_binary = binary
        if not resolved_binary:
            resolved_binary = shutil.which("sgpt") or "/usr/local/bin/sgpt"

        self._binary = resolved_binary
        self._model_flag = model_flag or "--model"
        self._args_template: List[str] = args_template or []
        self._env_vars: Dict[str, str] = env_vars or {}

    # -- CodingEngine protocol -------------------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "sgpt"

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
        """Execute sgpt CLI with *prompt* and return an :class:`EngineResult`.

        Parameters match the :class:`CodingEngine` protocol.

        The prompt is sent via stdin. Shell-GPT processes the prompt and outputs
        the result to stdout.

        Args:
            prompt: The task description or prompt to send to Shell-GPT.
            model: Model name override (uses --model flag if provided).
            timeout: Override timeout in seconds.
            cwd: Working directory for execution.
            env: Extra environment variables.
            session_id: Session identifier (not currently used by sgpt).

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
            )
        except subprocess.TimeoutExpired:
            logger.warning("Shell-GPT CLI timed out after %ds", effective_timeout)
            raise
        except FileNotFoundError:
            logger.error("Shell-GPT CLI binary not found: %s", self._binary)
            return EngineResult(
                stdout="",
                stderr=f"Binary not found: {self._binary}",
                returncode=-1,
                model=model or self._default_model,
            )

    def health_check(self) -> bool:
        """Return True if Shell-GPT CLI binary is found."""
        return self.is_available()

    # -- Command builder -------------------------------------------------------

    def _build_cmd(
        self,
        prompt: str,
        model: str | None = None,
    ) -> List[str]:
        """Build subprocess argument list for a Shell-GPT invocation.

        Args:
            prompt: The prompt text (not used in cmd building, sent via stdin).
            model: Optional model name (used with --model flag).

        Returns:
            List of command-line arguments.
        """
        cmd: List[str] = [self._binary]

        # Model flag
        if model and self._model_flag:
            cmd.extend([self._model_flag, model])

        # Add configured args template
        cmd.extend(self._args_template)

        return cmd

    # -- Convenience -----------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if Shell-GPT CLI binary exists."""
        return bool(
            shutil.which("sgpt")
            or (self._binary and (Path(self._binary).exists() if self._binary else False))
        )

    def model(self) -> str:
        """Return the default model name."""
        return self._default_model
