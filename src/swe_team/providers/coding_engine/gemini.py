"""Gemini CLI coding engine -- CodingEngine implementation for Google Gemini CLI.

Wraps ``/usr/bin/gemini`` (or a custom path) as a subprocess.  Extends
:class:`GenericCLIEngine` with Gemini-specific defaults and output parsing.

Registered in swe_team.yaml under providers.coding_engine.provider: gemini.
"""
from __future__ import annotations

import logging
import shutil
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)

# Sensitive keywords that must never be sent through Gemini CLI
# (Google data retention policy -- see gemini_cli_adapter.py)
_UNSAFE_KEYWORDS = (
    "password", "secret", "token", "api_key", "credential",
    "private_key", "access_key",
)


class GeminiCLIEngine(GenericCLIEngine):
    """Gemini CLI engine -- wraps ``/usr/bin/gemini``.

    Adds Gemini-specific output parsing, model defaults, and a safety
    filter that blocks prompts containing sensitive keywords (passwords,
    API keys, etc.) per Google's data retention policy.

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
        session_flag: str | None = None,
        sandbox_mode: bool = False,
        **kwargs,
    ) -> None:
        resolved_binary = binary or shutil.which("gemini") or "/usr/bin/gemini"

        # Build args template: gemini -p "{prompt}"
        args_template: List[str] = ["-p", "{prompt}"]
        if sandbox_mode:
            args_template.append("--sandbox")

        super().__init__(
            binary=resolved_binary,
            default_model=default_model,
            default_timeout=default_timeout,
            args_template=args_template,
            model_flag=model_flag,
            prompt_via="args",
            output_format="text",
            session_flag=session_flag,
            env_vars=env_vars,
        )

    # -- CodingEngine protocol override ----------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "gemini"

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
        """Execute Gemini CLI with safety filtering.

        Blocks prompts containing sensitive keywords (passwords, tokens, etc.)
        to prevent data leakage through Google's data retention pipeline.
        """
        # Safety filter -- never forward prompts with credentials
        prompt_lower = prompt.lower()
        for kw in _UNSAFE_KEYWORDS:
            if kw in prompt_lower:
                logger.warning(
                    "gemini: prompt contains sensitive keyword '%s' -- blocked",
                    kw,
                )
                return EngineResult(
                    stdout="",
                    stderr=f"Blocked: prompt contains sensitive keyword '{kw}'",
                    returncode=-1,
                    model=model or self._default_model,
                    metadata={"error_type": "safety_block", "blocked_keyword": kw},
                )

        return super().run(
            prompt,
            model=model,
            timeout=timeout,
            cwd=cwd,
            env=env,
            session_id=session_id,
        )
