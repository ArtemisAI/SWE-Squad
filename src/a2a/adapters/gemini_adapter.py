"""
Gemini CLI adapter for A2A integration.

Wraps Google's Gemini CLI (installed at /usr/bin/gemini) as an A2A-compatible
agent.  Gemini is registered with skills for investigation and lower-tier
fix tasks, providing a fallback when Claude Code is rate-limited.

Headless mode uses the ``-p`` flag (equivalent to Claude's ``--print``).

Usage::

    adapter = GeminiCLIAdapter()
    result = adapter.invoke("Investigate this error: ...")

    # Or with model override:
    result = adapter.invoke("Fix this bug", model="gemini-2.5-pro")
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Dict, List, Optional

from src.a2a.adapters.generic_cli_adapter import GenericCLIAdapter

logger = logging.getLogger(__name__)

_DEFAULT_GEMINI_PATH = os.environ.get("GEMINI_CLI_PATH", "") or shutil.which("gemini") or "/usr/bin/gemini"
_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
_DEFAULT_TIMEOUT = 120

# Skills this adapter advertises in the A2A registry
_GEMINI_SKILLS: List[Dict[str, Any]] = [
    {
        "id": "investigate",
        "name": "Investigate Issue",
        "description": "Diagnose and investigate software issues using Gemini",
        "tags": ["investigate", "diagnose", "analyze", "standard"],
    },
    {
        "id": "fix",
        "name": "Fix Issue",
        "description": "Attempt code fixes for lower-tier bugs using Gemini",
        "tags": ["fix", "patch", "standard"],
    },
    {
        "id": "review",
        "name": "Code Review",
        "description": "Review code changes and provide feedback",
        "tags": ["review", "feedback", "standard"],
    },
]


class GeminiCLIAdapter(GenericCLIAdapter):
    """A2A adapter for Google Gemini CLI.

    Gemini CLI uses ``-p`` for headless prompt mode and ``--model`` for
    model selection.  This adapter inherits from ``GenericCLIAdapter``
    and pre-configures the command pattern for Gemini.

    Parameters
    ----------
    gemini_path:
        Path to the Gemini CLI binary.
    default_model:
        Default Gemini model to use.
    timeout:
        Default timeout in seconds for CLI invocations.
    sandbox:
        If True, add ``--sandbox`` flag for restricted execution.
    extra_args:
        Additional CLI arguments to pass to Gemini.
    priority:
        Priority for agent selection (lower = preferred).
    """

    def __init__(
        self,
        *,
        gemini_path: str = _DEFAULT_GEMINI_PATH,
        default_model: str = _DEFAULT_GEMINI_MODEL,
        timeout: int = _DEFAULT_TIMEOUT,
        sandbox: bool = False,
        extra_args: Optional[List[str]] = None,
        priority: int = 50,
        cwd: Optional[str] = None,
    ) -> None:
        args_template = ["-p", "{prompt}", "--model", "{model}"]

        if sandbox:
            args_template.append("--sandbox")

        if extra_args:
            args_template.extend(extra_args)

        super().__init__(
            name="gemini-cli",
            command=gemini_path,
            args_template=args_template,
            default_model=default_model,
            skills=_GEMINI_SKILLS,
            timeout=timeout,
            prompt_via_stdin=False,  # Gemini uses -p "prompt" as argument
            provider={"organization": "Google"},
            version="1.0.0",
            priority=priority,
            cwd=cwd,
        )

    def invoke(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """Invoke Gemini CLI with the given prompt.

        Overrides the base to log Gemini-specific context.
        """
        effective_model = model or self._default_model
        logger.info(
            "Gemini CLI invocation (model=%s, timeout=%s)",
            effective_model,
            timeout or self._timeout,
        )
        return super().invoke(prompt, model=model, timeout=timeout)
