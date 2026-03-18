"""
Gemini CLI adapter — fallback investigator and specialist for WebUI/dashboard tasks.

## When to use Gemini CLI (not Claude Code)

| Task type          | Use Gemini? | Reason |
|--------------------|-------------|--------|
| Log/error analysis | Yes         | 1M context, handles huge dumps |
| Library docs/CVE   | Yes         | Built-in web search |
| WebUI / dashboard  | YES (prefer) | Gemini excels at HTML/CSS/JS/charts |
| Playwright tests   | Yes         | Needs `npx playwright install chromium` |
| Proprietary code   | NO          | Data retention policy risk |
| Fix + git commit   | NO          | No repo write access |

## Data retention
Gemini CLI is subject to Google data retention policies.
The adapter blocks prompts containing: password, secret, token, api_key, credential.
Never send proprietary source code or PII through this adapter.

## Skills
Declared skills: investigate, review, dashboard, websearch
Skill routing: swe_team.yaml fallback_agents[gemini-cli].skills

Usage:
    adapter = GeminiCLIAdapter(skills=["investigate", "dashboard", "websearch"])
    if adapter.is_available() and adapter.has_skill("dashboard"):
        result = adapter.invoke(dashboard_prompt, timeout=180)
"""

from __future__ import annotations

import logging
import os
import subprocess
import shutil
from typing import List, Optional

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
        skills: Optional[List[str]] = None,
    ) -> None:
        self._command = command or os.environ.get("GEMINI_CLI_PATH", _DEFAULT_GEMINI_CMD)
        self._model = model
        self._max_prompt_chars = max_prompt_chars
        self._name = "gemini-cli"
        self._skills: List[str] = skills or ["investigate", "review", "dashboard", "websearch"]

    def has_skill(self, skill: str) -> bool:
        """Return True if this adapter declares the given skill."""
        return skill in self._skills

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
        max_retries = 3
        backoff = 15  # seconds

        for attempt in range(max_retries):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                stderr = (result.stderr or "")[:500]

                # Detect rate limiting (429 / quota / rate limit in stderr)
                if result.returncode != 0 and any(
                    kw in stderr.lower()
                    for kw in ("429", "rate limit", "quota", "resource exhausted", "too many requests")
                ):
                    wait = backoff * (2 ** attempt)
                    logger.warning(
                        "gemini-cli: rate limited (attempt %d/%d) — retrying in %ds: %s",
                        attempt + 1, max_retries, wait, stderr[:200],
                    )
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(wait)
                        continue
                    logger.warning("gemini-cli: rate limit exhausted after %d attempts", max_retries)
                    return None

                if result.returncode != 0:
                    logger.warning(
                        "gemini-cli: exited with rc=%d: %s",
                        result.returncode, stderr,
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
        return None
