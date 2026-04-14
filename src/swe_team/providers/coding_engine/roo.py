"""Roo Code CLI coding engine -- CodingEngine implementation for Roo Code.

Wraps ``roo`` CLI as a subprocess.  Extends
:class:`GenericCLIEngine` with Roo-specific defaults.

Registered in swe_team.yaml under providers.coding_engine.provider: roo.
"""
from __future__ import annotations

import logging
import shutil
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)


class RooCLIEngine(GenericCLIEngine):
    """Roo Code CLI engine -- wraps ``roo``.

    Roo Code is an AI coding assistant CLI. This engine wraps
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
        resolved_binary = binary or shutil.which("roo") or "/usr/local/bin/roo"

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
        return "roo"
