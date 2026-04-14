"""Gorilla CLI coding engine -- CodingEngine implementation for API specialist agent.

Wraps ``gorilla`` (or a custom path) as a subprocess.  Extends
:class:`GenericCLIEngine` with Gorilla-specific defaults.  Gorilla reads
prompts from stdin.

Registered in swe_team.yaml under providers.coding_engine.provider: gorilla.
"""
from __future__ import annotations

import logging
import shutil
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)


class GorillaCLIEngine(GenericCLIEngine):
    """Gorilla CLI engine -- wraps ``gorilla`` (API specialist).

    Gorilla reads prompts from stdin rather than command-line arguments.
    No model selection is supported (model_flag is empty).

    Implements the :class:`CodingEngine` protocol.
    """

    def __init__(
        self,
        *,
        default_model: str = "",
        default_timeout: int = 300,
        binary: str | None = None,
        env_vars: Dict[str, str] | None = None,
        **kwargs,
    ) -> None:
        resolved_binary = binary or shutil.which("gorilla") or "/usr/local/bin/gorilla"

        # Gorilla reads from stdin; args_template contains {prompt} as placeholder
        # but prompt_via="stdin" means the prompt is piped, not substituted
        args_template: List[str] = ["{prompt}"]

        super().__init__(
            binary=resolved_binary,
            default_model=default_model,
            default_timeout=default_timeout,
            args_template=args_template,
            model_flag="",
            prompt_via="stdin",
            output_format="text",
            session_flag=None,
            env_vars=env_vars,
        )

    # -- CodingEngine protocol override ----------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "gorilla"

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
        """Execute Gorilla CLI with the given prompt via stdin."""
        return super().run(
            prompt,
            model=model,
            timeout=timeout,
            cwd=cwd,
            env=env,
            session_id=session_id,
        )
