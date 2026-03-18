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

## Rate limit handling
Gemini has per-model rate limits. On 429/quota errors:
1. Exponential backoff: 60s → 120s → 240s → 480s (4 retries)
2. Model failover: gemini-3-pro-high → gemini-2.5-pro → gemini-2.5-flash
3. Cooldown: after exhausting all retries, 15-min cooldown before next invocation

## Skills
Declared skills: investigate, review, dashboard, websearch
Skill routing: swe_team.yaml fallback_agents[gemini-cli].skills

Usage:
    adapter = GeminiCLIAdapter(skills=["investigate", "dashboard", "websearch"])
    if adapter.is_available() and adapter.has_skill("dashboard"):
        result = adapter.invoke(dashboard_prompt, timeout=180)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import shutil
import tempfile
import time
from pathlib import Path
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
_DEFAULT_MODEL = "gemini-3-pro-high"

# Rate limit constants
_RATE_LIMIT_KEYWORDS = ("429", "rate limit", "quota", "resource exhausted", "too many requests")
_MAX_RETRIES = 4
_INITIAL_BACKOFF_SECONDS = 60       # 60s → 120s → 240s → 480s
_COOLDOWN_SECONDS = 900             # 15 min cooldown after all retries exhausted
_COOLDOWN_FILE = Path(os.environ.get("SWE_DATA_DIR", tempfile.gettempdir())) / "gemini_cli_cooldown.lock"

# Model failover chain — try progressively cheaper/less-limited models
_DEFAULT_MODELS = ["gemini-2.5-flash-thinking", "gemini-2.5-pro", "gemini-2.5-flash"]
_MODEL_FAILOVER_CHAIN = json.loads(os.environ.get("GEMINI_MODELS", "null")) or _DEFAULT_MODELS


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
        models: Optional[List[str]] = None,
    ) -> None:
        self._command = command or os.environ.get("GEMINI_CLI_PATH", _DEFAULT_GEMINI_CMD)
        self._model = model
        self._models = json.loads(os.environ.get("GEMINI_MODELS", "null")) or models or _DEFAULT_MODELS
        self._max_prompt_chars = max_prompt_chars
        self._name = "gemini-cli"
        self._skills: List[str] = skills or ["investigate", "review", "dashboard", "websearch"]

    def has_skill(self, skill: str) -> bool:
        """Return True if this adapter declares the given skill."""
        return skill in self._skills

    def is_available(self) -> bool:
        """Return True if gemini CLI is installed, reachable, and not in cooldown."""
        cmd = shutil.which(self._command) or (
            self._command if os.path.isfile(self._command) else None
        )
        if not cmd:
            logger.debug("gemini-cli: command not found at %s", self._command)
            return False

        # Check cooldown
        if _COOLDOWN_FILE.exists():
            age = time.time() - _COOLDOWN_FILE.stat().st_mtime
            if age < _COOLDOWN_SECONDS:
                remaining = int(_COOLDOWN_SECONDS - age)
                logger.info(
                    "gemini-cli: in cooldown (%ds remaining) — skipping",
                    remaining,
                )
                return False
            # Cooldown expired — remove lock
            _COOLDOWN_FILE.unlink(missing_ok=True)

        return True

    def invoke(self, prompt: str, timeout: int = 180) -> Optional[str]:
        """Run the prompt through Gemini CLI with rate limit handling.

        On rate limit (429/quota):
        1. Exponential backoff: 60s, 120s, 240s, 480s
        2. Model failover: try cheaper models in the chain
        3. Cooldown: 15-min lock after exhausting all options

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

        # Build model failover chain starting from configured model
        models_to_try = self._build_failover_chain()

        for model in models_to_try:
            result = self._invoke_with_retries(prompt, model, timeout)
            if result is not None:
                return result
            # Model exhausted retries — try next model
            logger.warning(
                "gemini-cli: model %s exhausted — trying next in failover chain",
                model,
            )

        # All models exhausted — enter cooldown
        logger.warning(
            "gemini-cli: all models and retries exhausted — entering %ds cooldown",
            _COOLDOWN_SECONDS,
        )
        _COOLDOWN_FILE.touch()
        return None

    def _build_failover_chain(self) -> List[str]:
        """Build ordered list of models to try, starting from the configured model."""
        chain = []
        # Start with configured model
        chain.append(self._model)
        # Add remaining failover models not already in chain
        for model in self._models:
            if model not in chain:
                chain.append(model)
        return chain

    def _invoke_with_retries(
        self, prompt: str, model: str, timeout: int
    ) -> Optional[str]:
        """Try a single model with exponential backoff on rate limits."""
        cmd = [self._command, "-p", prompt, "--model", model]

        for attempt in range(_MAX_RETRIES):
            logger.info(
                "gemini-cli: invoking (model=%s, attempt=%d/%d, prompt_chars=%d)",
                model, attempt + 1, _MAX_RETRIES, len(prompt),
            )
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                stderr = (result.stderr or "")[:500]

                # Detect rate limiting
                if result.returncode != 0 and any(
                    kw in stderr.lower() for kw in _RATE_LIMIT_KEYWORDS
                ):
                    wait = _INITIAL_BACKOFF_SECONDS * (2 ** attempt)
                    logger.warning(
                        "gemini-cli: rate limited on %s (attempt %d/%d) — "
                        "backing off %ds: %s",
                        model, attempt + 1, _MAX_RETRIES, wait, stderr[:200],
                    )
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(wait)
                        continue
                    # Last retry exhausted for this model
                    return None

                # Non-rate-limit failure
                if result.returncode != 0:
                    logger.warning(
                        "gemini-cli: %s exited with rc=%d: %s",
                        model, result.returncode, stderr,
                    )
                    return None

                # Success
                output = (result.stdout or "").strip()
                if not output:
                    logger.warning("gemini-cli: %s returned empty output", model)
                    return None

                logger.info(
                    "gemini-cli: success (model=%s, %d chars returned)",
                    model, len(output),
                )
                return output

            except subprocess.TimeoutExpired:
                logger.warning(
                    "gemini-cli: %s timed out after %ds", model, timeout
                )
                return None
            except Exception as exc:
                logger.warning(
                    "gemini-cli: %s unexpected error: %s", model, exc
                )
                return None

        return None
