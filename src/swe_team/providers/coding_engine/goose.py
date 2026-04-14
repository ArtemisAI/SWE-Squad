"""Goose CLI coding engine -- CodingEngine implementation for Goose.

Wraps ``goose`` CLI as a subprocess.  Extends
:class:`GenericCLIEngine` with Goose-specific defaults and optional
recipe support.

Registered in swe_team.yaml under providers.coding_engine.provider: goose.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)


class GooseCLIEngine(GenericCLIEngine):
    """Goose CLI engine -- wraps ``goose``.

    Goose is an open-source AI coding agent. This engine wraps
    it as a subprocess, implementing the CodingEngine protocol.

    Supports an optional ``recipe`` parameter that adds
    ``--recipe <name>`` to the command.

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
        recipe: str | None = None,
        **kwargs,
    ) -> None:
        resolved_binary = binary or shutil.which("goose") or "/usr/local/bin/goose"

        # Build args template: goose run --text "{prompt}" [--recipe <recipe>]
        args_template: List[str] = ["run", "--text", "{prompt}"]
        if recipe:
            args_template.extend(["--recipe", recipe])

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
        self._recipe = recipe

    # -- CodingEngine protocol override ----------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "goose"

    def is_available(self) -> bool:
        """Return True when Goose is installed and responds to ``--version``."""
        binary_path = Path(self._binary)
        if binary_path.is_absolute():
            if not binary_path.exists():
                return False
        elif not shutil.which(self._binary):
            return False
        try:
            result = subprocess.run(
                [self._binary, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0
