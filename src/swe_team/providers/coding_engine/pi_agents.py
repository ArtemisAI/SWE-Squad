"""Pi Agents CLI coding engine -- CodingEngine implementation for Pi framework.

Wraps ``pi`` (or a custom path) as a subprocess.  Extends
:class:`GenericCLIEngine` with Pi-specific defaults.

Registered in swe_team.yaml under providers.coding_engine.provider: pi_agents.
"""
from __future__ import annotations

import logging
import shutil
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)


class PiAgentsCLIEngine(GenericCLIEngine):
    """Pi Agents CLI engine -- wraps ``pi`` (Pi framework).

    Uses ``pi run --prompt "{prompt}"`` to execute tasks.  No model selection
    is supported (model_flag is empty).

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
        resolved_binary = binary or shutil.which("pi") or "/usr/local/bin/pi"

        # Pi uses: pi run --prompt "{prompt}"
        args_template: List[str] = ["run", "--prompt", "{prompt}"]

        super().__init__(
            binary=resolved_binary,
            default_model=default_model,
            default_timeout=default_timeout,
            args_template=args_template,
            model_flag="",
            prompt_via="args",
            output_format="text",
            session_flag=None,
            env_vars=env_vars,
        )

    # -- CodingEngine protocol override ----------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "pi_agents"

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
        """Execute Pi Agents CLI with the given prompt."""
        return super().run(
            prompt,
            model=model,
            timeout=timeout,
            cwd=cwd,
            env=env,
            session_id=session_id,
        )
