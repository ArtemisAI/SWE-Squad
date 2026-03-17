"""
Gemini CLI fallback adapter for InvestigatorAgent.

When Claude Code CLI is rate-limited or unavailable, the investigator
falls back to Gemini CLI for investigation tasks.

Gemini CLI advantages:
  - 1M token context window (large codebases, long logs)
  - Web search capability (external library docs, CVEs)
  - Separate quota from Claude — keeps wheels turning during 429s

Data retention caution: Do NOT pass proprietary source code or PII.
Use only for log analysis, library research, and public-facing patterns.
Gemini CLI is subject to Google data retention policies.

Usage:
    adapter = GeminiCLIAdapter()
    if adapter.is_available():
        result = adapter.invoke(prompt, timeout=120)
"""

from __future__ import annotations

import logging
import os
import subprocess
import shutil
from typing import Optional

logger = logging.getLogger(__name__)

# Safe tasks for Gemini delegation (no proprietary code)
_SAFE_TASK_KEYWORDS = [
    "library", "traceback", "import", "version", "api",
    "timeout", "rate limit", "network", "http", "error",
    "exception", "log", "investigate", "diagnose",
]

# Never delegate if prompt contains these (proprietary/sensitive)
_UNSAFE_KEYWORDS = [
    "password", "secret", "token", "api_key", "credential",
    "private_key", "access_key",
]

_DEFAULT_GEMINI_CMD = "/usr/bin/gemini"
_DEFAULT_MODEL = "gemini-2.5-flash-thinking"


class GeminiCLIAdapter:
    """Wraps the Gemini CLI as a drop-in fallback for InvestigatorAgent.

    Implements the duck-typed _FallbackAgent interface:
      - is_available() -> bool
      - invoke(prompt, timeout) -> str
    """

    def __init__(
        self,
        command: Optional[str] = None,
        model: str = _DEFAULT_MODEL,
        max_prompt_chars: int = 50_000,  # stay well within context, keep cost low
    ) -> None:
        self._command = command or os.environ.get("GEMINI_CLI_PATH", _DEFAULT_GEMINI_CMD)
        self._model = model
        self._max_prompt_chars = max_prompt_chars
        self._name = "gemini-cli"

    def is_available(self) -> bool:
        """Return True if gemini CLI is installed and reachable."""
        cmd = shutil.which(self._command) or (
            self._command if os.path.isfile(self._command) else None
        )
        if not cmd:
            logger.debug("gemini-cli: command not found at %s", self._command)
            return False
        return True

    def invoke(self, prompt: str, timeout: int = 120) -> Optional[str]:
        """Run the prompt through Gemini CLI and return the response.

        Truncates prompt to max_prompt_chars to avoid quota burn on huge inputs.
        Returns None on failure.
        """
        if not self.is_available():
            return None

        # Safety check — never forward prompts with credentials
        prompt_lower = prompt.lower()
        for kw in _UNSAFE_KEYWORDS:
            if kw in prompt_lower:
                logger.warning(
                    "gemini-cli: prompt contains sensitive keyword '%s' — skipping delegation",
                    kw,
                )
                return None

        # Truncate if needed
        if len(prompt) > self._max_prompt_chars:
            logger.info(
                "gemini-cli: truncating prompt from %d → %d chars",
                len(prompt), self._max_prompt_chars,
            )
            prompt = prompt[:self._max_prompt_chars] + "\n\n[... truncated for context limit ...]"

        cmd = [self._command, "-p", prompt]

        logger.info(
            "gemini-cli: delegating to Gemini CLI (model=%s, prompt_chars=%d)",
            self._model, len(prompt),
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                logger.warning(
                    "gemini-cli: exited with rc=%d: %s",
                    result.returncode,
                    (result.stderr or "")[:500],
                )
                return None
            output = (result.stdout or "").strip()
            if not output:
                logger.warning("gemini-cli: returned empty output")
                return None
            logger.info("gemini-cli: success (%d chars returned)", len(output))
            return output
        except subprocess.TimeoutExpired:
            logger.warning("gemini-cli: timed out after %ds", timeout)
            return None
        except Exception as exc:
            logger.warning("gemini-cli: unexpected error: %s", exc)
            return None
