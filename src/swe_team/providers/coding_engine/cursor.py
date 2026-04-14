"""Cursor CLI coding engine -- CodingEngine implementation for Cursor.

Wraps ``cursor`` CLI as a subprocess.  Extends
:class:`GenericCLIEngine` with Cursor-specific defaults.

Registered in swe_team.yaml under providers.coding_engine.provider: cursor.
"""
from __future__ import annotations

import logging
import shutil
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)


class CursorCLIEngine(GenericCLIEngine):
    """Cursor CLI engine -- wraps ``cursor``.

    Cursor is an AI-powered code editor with CLI support. This engine wraps
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
        env_vars: Dict[str, str] | None = None,
        **kwargs,
    ) -> None:
        resolved_binary = binary or shutil.which("cursor") or "/usr/local/bin/cursor"

        args_template: List[str] = ["--message", "{prompt}"]

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

    # -- CodingEngine protocol override ----------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "cursor"
