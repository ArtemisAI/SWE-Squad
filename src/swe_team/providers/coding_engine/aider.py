"""Aider coding engine -- CodingEngine implementation for Aider.

Wraps the ``aider`` CLI (the most popular open-source git-native pair
programming agent) as a subprocess.  Extends :class:`GenericCLIEngine`
with Aider-specific defaults and flags for autonomous operation.

Registered in swe_team.yaml under providers.coding_engine.provider: aider.
"""
from __future__ import annotations

import logging
import shutil
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)


class AiderEngine(GenericCLIEngine):
    """Aider CLI engine -- wraps the ``aider`` binary.

    Configures Aider for autonomous non-interactive operation with
    SWE-Squad managing git operations.  By default, git integration
    and auto-commits are disabled so SWE-Squad retains full control
    over the repository.

    Implements the :class:`CodingEngine` protocol.
    """

    def __init__(
        self,
        *,
        default_model: str = "sonnet",
        default_timeout: int = 300,
        binary: str | None = None,
        model_flag: str = "--model",
        env_vars: Dict[str, str] | None = None,
        auto_commits: bool = False,
        no_git: bool = True,
        yes_always: bool = True,
        edit_format: str | None = None,
        **kwargs,
    ) -> None:
        resolved_binary = binary or shutil.which("aider") or "/usr/local/bin/aider"

        # Build args template: aider --message "{prompt}" [flags]
        args_template: List[str] = ["--message", "{prompt}"]

        # Git control flags -- SWE-Squad manages git itself
        if no_git:
            args_template.append("--no-git")
        elif auto_commits:
            args_template.append("--auto-commits")
        else:
            args_template.append("--no-auto-commits")

        # Auto-approve for autonomous operation
        if yes_always:
            args_template.append("--yes-always")

        # Optional edit format (diff, whole, udiff, etc.)
        if edit_format:
            args_template.extend(["--edit-format", edit_format])

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
        return "aider"
