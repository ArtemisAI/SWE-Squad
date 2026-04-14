"""Antigravity CLI coding engine -- CodingEngine implementation for Google Gemini agent.

Wraps ``antigravity`` (or a custom path) as a subprocess.  Extends
:class:`GenericCLIEngine` with Antigravity-specific defaults and model routing.

Registered in swe_team.yaml under providers.coding_engine.provider: antigravity.
"""
from __future__ import annotations

import logging
import shutil
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)


class AntigravityCLIEngine(GenericCLIEngine):
    """Antigravity CLI engine -- wraps ``antigravity`` (Google Gemini agent).

    Extends GenericCLIEngine with Antigravity-specific defaults for the
    Google Gemini agent CLI.

    Implements the :class:`CodingEngine` protocol.
    """

    def __init__(
        self,
        *,
        default_model: str = "gemini-2.5-pro",
        default_timeout: int = 300,
        binary: str | None = None,
        model_flag: str = "--model",
        env_vars: Dict[str, str] | None = None,
        **kwargs,
    ) -> None:
        resolved_binary = binary or shutil.which("antigravity") or "/usr/local/bin/antigravity"

        # Antigravity takes prompt as positional argument
        args_template: List[str] = ["{prompt}"]

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
        return "antigravity"

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
        """Execute Antigravity CLI with the given prompt."""
        return super().run(
            prompt,
            model=model,
            timeout=timeout,
            cwd=cwd,
            env=env,
            session_id=session_id,
        )
