"""GitHub Copilot CLI coding engine -- CodingEngine implementation for Copilot CLI.

Wraps the standalone ``copilot`` binary (GitHub Copilot's autonomous coding
agent) as a subprocess.  Extends :class:`GenericCLIEngine` with
Copilot-specific defaults, permission flags, and output parsing.

The ``copilot`` binary is a full autonomous agent (like Claude Code) with
tool use, file editing, and shell execution capabilities.  It is NOT the
older ``gh copilot suggest`` helper — it is the standalone agent available
at https://github.com/features/copilot/cli.

Installation: ``gh copilot`` auto-downloads the binary, or install
standalone from GitHub.  Binary location: ``/usr/local/bin/copilot`` or
``~/.local/share/gh/copilot``.

Registered in swe_team.yaml under providers.coding_engine.provider: copilot.
"""
from __future__ import annotations

import logging
import shutil
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)


class CopilotCLIEngine(GenericCLIEngine):
    """GitHub Copilot CLI engine -- wraps the ``copilot`` autonomous agent.

    Uses ``copilot -p "<prompt>"`` for non-interactive (scripted) execution.
    The ``copilot`` binary is a full autonomous coding agent with tool use,
    file editing, and shell execution — NOT the legacy ``gh copilot suggest``.

    Parameters
    ----------
    default_model:
        Model identifier passed via ``--model``.  Copilot supports both
        OpenAI models (``gpt-5.2``, ``gpt-4.1``) and Anthropic models
        (``claude-sonnet-4.5``, ``claude-opus-4.5``) via GitHub routing.
        Set to ``""`` to use the Copilot-selected default.
    default_timeout:
        Default subprocess timeout in seconds.  Copilot is a full agent,
        so 600+ is recommended for complex tasks.
    binary:
        Path to the ``copilot`` binary.  Resolves via shutil.which, then
        falls back to ``/usr/local/bin/copilot``.
    env_vars:
        Extra environment variables.  Useful for BYOK mode:
        ``COPILOT_PROVIDER_BASE_URL``, ``COPILOT_PROVIDER_API_KEY``, etc.
    allow_all:
        If True, pass ``--allow-all`` (alias ``--yolo``) to auto-approve
        all tools, paths, and URLs.  Required for fully autonomous operation.
    no_ask_user:
        If True, pass ``--no-ask-user`` to disable clarifying questions.
    silent:
        If True, pass ``-s`` (silent) to output only agent response text,
        no stats or progress.
    no_auto_update:
        If True, pass ``--no-auto-update`` to prevent update checks during
        CI/automation runs.
    no_custom_instructions:
        If True, pass ``--no-custom-instructions`` to ignore AGENTS.md.
    autopilot:
        If True, pass ``--autopilot`` to enable autonomous continuation.
    max_autopilot_continues:
        If set, pass ``--max-autopilot-continues <n>`` to cap turns.
    effort:
        Reasoning effort level: ``"low"``, ``"medium"``, ``"high"``,
        ``"xhigh"``.  If None, uses the Copilot default.

    Implements the :class:`CodingEngine` protocol.
    """

    _VALID_EFFORTS = ("low", "medium", "high", "xhigh")

    def __init__(
        self,
        *,
        default_model: str = "",
        default_timeout: int = 600,
        binary: str | None = None,
        model_flag: str = "--model",
        env_vars: Dict[str, str] | None = None,
        allow_all: bool = True,
        no_ask_user: bool = True,
        silent: bool = True,
        no_auto_update: bool = True,
        no_custom_instructions: bool = False,
        autopilot: bool = True,
        max_autopilot_continues: int | None = None,
        effort: str | None = None,
        **kwargs,
    ) -> None:
        if effort is not None and effort not in self._VALID_EFFORTS:
            raise ValueError(
                f"effort must be one of {self._VALID_EFFORTS}; got {effort!r}"
            )

        resolved_binary = (
            binary
            or shutil.which("copilot")
            or "/usr/local/bin/copilot"
        )

        # Build args template for non-interactive mode:
        #   copilot -p "{prompt}" --allow-all --no-ask-user -s ...
        args_template: List[str] = ["-p", "{prompt}"]

        if allow_all:
            args_template.append("--allow-all")
        if no_ask_user:
            args_template.append("--no-ask-user")
        if silent:
            args_template.append("-s")
        if no_auto_update:
            args_template.append("--no-auto-update")
        if no_custom_instructions:
            args_template.append("--no-custom-instructions")
        if autopilot:
            args_template.append("--autopilot")
        if max_autopilot_continues is not None:
            args_template.extend([
                "--max-autopilot-continues",
                str(max_autopilot_continues),
            ])
        if effort:
            args_template.extend(["--effort", effort])

        self._allow_all = allow_all
        self._no_ask_user = no_ask_user
        self._silent = silent
        self._no_auto_update = no_auto_update
        self._no_custom_instructions = no_custom_instructions
        self._autopilot = autopilot
        self._max_autopilot_continues = max_autopilot_continues
        self._effort = effort

        super().__init__(
            binary=resolved_binary,
            default_model=default_model,
            default_timeout=default_timeout,
            args_template=args_template,
            model_flag=model_flag,
            prompt_via="args",
            output_format="text",
            session_flag="--resume",
            env_vars=env_vars,
        )

    # -- CodingEngine protocol override ----------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "copilot"

    def resume(
        self,
        session_id: str,
        prompt: str,
        *,
        model: str | None = None,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict | None = None,
    ) -> EngineResult:
        """Resume a previous Copilot session.

        Uses ``--resume=<session_id>`` to continue an existing conversation.
        """
        return self.run(
            prompt,
            model=model,
            timeout=timeout,
            cwd=cwd,
            env=env,
            session_id=session_id,
        )
