"""Amazon Q Developer CLI coding engine -- CodingEngine implementation for Amazon Q CLI.

Wraps ``q`` (or a custom path) as a subprocess.  Extends
:class:`GenericCLIEngine` with Amazon Q-specific defaults, profile support,
and a safety filter for AWS credential patterns.

Registered in swe_team.yaml under providers.coding_engine.provider: amazon_q.
"""
from __future__ import annotations

import logging
import shutil
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)

# AWS credential patterns that must never be forwarded through Amazon Q CLI.
# Amazon Q may log or retain prompt data -- block prompts containing raw creds.
_UNSAFE_AWS_KEYWORDS = (
    "access_key_id",
    "secret_access_key",
    "aws_session_token",
    "aws_secret_key",
    "credential",
    "private_key",
    "password",
    "secret",
)


class AmazonQCLIEngine(GenericCLIEngine):
    """Amazon Q Developer CLI engine -- wraps ``q``.

    Adds Amazon Q-specific argument handling, optional AWS profile selection,
    autonomous tool trust, and a safety filter that blocks prompts containing
    AWS credential patterns.

    Implements the :class:`CodingEngine` protocol.
    """

    def __init__(
        self,
        *,
        default_model: str = "",
        default_timeout: int = 300,
        binary: str | None = None,
        env_vars: Dict[str, str] | None = None,
        trust_all_tools: bool = True,
        profile: str | None = None,
        **kwargs,
    ) -> None:
        resolved_binary = binary or shutil.which("q") or "/usr/local/bin/q"

        # Build args template: q chat -n [--trust-all-tools] [--profile X] {prompt}
        args_template: List[str] = ["chat", "-n"]
        if trust_all_tools:
            args_template.append("--trust-all-tools")
        if profile:
            args_template.extend(["--profile", profile])
        args_template.append("{prompt}")

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
        return "amazon_q"

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
        """Execute Amazon Q CLI with safety filtering.

        Blocks prompts containing AWS credential patterns (access keys,
        secret keys, session tokens, etc.) to prevent credential leakage.
        """
        # Safety filter -- never forward prompts with AWS credentials
        prompt_lower = prompt.lower()
        for kw in _UNSAFE_AWS_KEYWORDS:
            if kw in prompt_lower:
                logger.warning(
                    "amazon_q: prompt contains sensitive keyword '%s' -- blocked",
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
