"""OpenAI Codex CLI coding engine -- CodingEngine implementation for OpenAI Codex CLI.

Wraps ``codex`` (or a custom path) as a subprocess.  Extends
:class:`GenericCLIEngine` with Codex-specific defaults: ``--full-auto`` approval
mode for autonomous operation, optional ``--sandbox`` isolation, and ``--quiet``
flag to suppress interactive UI output.

Registered in swe_team.yaml under providers.coding_engine.provider: codex.
"""
from __future__ import annotations

import logging
import shutil
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)

# Valid approval modes for Codex CLI
_VALID_APPROVAL_MODES = ("full-auto", "auto-edit", "suggest")


class CodexCLIEngine(GenericCLIEngine):
    """OpenAI Codex CLI engine -- wraps ``codex``.

    Adds Codex-specific flags for approval mode (``--full-auto``,
    ``--auto-edit``, ``--suggest``), optional Docker/SSH sandboxing,
    and quiet mode for non-interactive operation.

    Implements the :class:`CodingEngine` protocol.
    """

    def __init__(
        self,
        *,
        default_model: str = "o4-mini",
        default_timeout: int = 300,
        binary: str | None = None,
        model_flag: str = "--model",
        env_vars: Dict[str, str] | None = None,
        approval_mode: str = "full-auto",
        sandbox: str | None = None,
        quiet: bool = True,
        **kwargs,
    ) -> None:
        if approval_mode not in _VALID_APPROVAL_MODES:
            raise ValueError(
                f"approval_mode must be one of {_VALID_APPROVAL_MODES}; "
                f"got {approval_mode!r}"
            )
        if sandbox is not None and sandbox not in ("docker", "ssh"):
            raise ValueError(
                f"sandbox must be None, 'docker', or 'ssh'; got {sandbox!r}"
            )

        resolved_binary = binary or shutil.which("codex") or "/usr/local/bin/codex"

        # Build args template: codex [flags] "{prompt}"
        args_template: List[str] = []

        # Approval mode flag
        args_template.append(f"--{approval_mode}")

        # Quiet mode -- suppress interactive UI
        if quiet:
            args_template.append("--quiet")

        # Sandbox isolation
        if sandbox is not None:
            args_template.extend(["--sandbox", sandbox])

        # Prompt as positional argument
        args_template.append("{prompt}")

        super().__init__(
            binary=resolved_binary,
            default_model=default_model,
            default_timeout=default_timeout,
            args_template=args_template,
            model_flag=model_flag,
            prompt_via="args",
            output_format="text",
            session_flag=None,
            env_vars=env_vars,
        )

        # Store for introspection in tests
        self._approval_mode = approval_mode
        self._sandbox = sandbox
        self._quiet = quiet

    # -- CodingEngine protocol override ----------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "codex"

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
        """Execute Codex CLI with the given prompt.

        Codex does not support session tracking, so *session_id* is
        accepted for interface compatibility but ignored.
        """
        return super().run(
            prompt,
            model=model,
            timeout=timeout,
            cwd=cwd,
            env=env,
            session_id=session_id,
        )
