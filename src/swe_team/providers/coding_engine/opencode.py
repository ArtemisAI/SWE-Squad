"""OpenCode CLI coding engine -- CodingEngine implementation for OpenCode.

Wraps ``opencode`` (or a custom path) as a subprocess.  Extends
:class:`GenericCLIEngine` with OpenCode-specific defaults and flags.

OpenCode is an open-source terminal coding agent.  It uses its own config
for model selection by default (empty ``default_model``), but supports
``--model`` for per-invocation overrides.

Registered in swe_team.yaml under providers.coding_engine.provider: opencode.
"""
from __future__ import annotations

import logging
import shutil
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)


class OpenCodeEngine(GenericCLIEngine):
    """OpenCode CLI engine -- wraps ``opencode``.

    Adds OpenCode-specific argument construction including the ``run``
    subcommand, ``--yes`` for autonomous operation, and optional
    ``--json`` output mode.

    Implements the :class:`CodingEngine` protocol.
    """

    def __init__(
        self,
        *,
        default_model: str = "",
        default_timeout: int = 300,
        binary: str | None = None,
        model_flag: str = "--model",
        env_vars: Dict[str, str] | None = None,
        auto_approve: bool = True,
        output_json: bool = False,
        **kwargs,
    ) -> None:
        resolved_binary = binary or shutil.which("opencode") or "/usr/local/bin/opencode"

        # Build args template: opencode run -m "{prompt}"
        args_template: List[str] = ["run", "-m", "{prompt}"]
        if auto_approve:
            args_template.append("--yes")
        if output_json:
            args_template.append("--json")

        output_format = "json" if output_json else "text"

        super().__init__(
            binary=resolved_binary,
            default_model=default_model,
            default_timeout=default_timeout,
            args_template=args_template,
            model_flag=model_flag,
            prompt_via="args",
            output_format=output_format,
            session_flag=None,
            env_vars=env_vars,
        )

    # -- CodingEngine protocol override ----------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "opencode"
