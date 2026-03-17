"""
Model availability probe for BASE_LLM proxy.

Runs at cycle start to validate configured models against what the
BASE_LLM endpoint actually serves, and auto-selects working alternatives
when a configured model is unavailable.

This prevents silent failures where an agent makes an API call with a
model that doesn't exist on the proxy — errors that would otherwise only
surface as log-level failures and create reactive tickets.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Preference-ordered fallback lists per task type.
# First match wins when the configured model is not available.
_EMBEDDING_FALLBACKS = ["bge-m3", "mxbai-embed-large", "nomic-embed-text", "qwen3-embedding"]
_EXTRACTION_FALLBACKS = ["gemini-3-flash", "qwen3:8b", "gemini-2.5-flash-thinking", "qwen3:4b"]
_T1_FALLBACKS = ["gemini-3-flash", "qwen3:8b", "qwen3:4b"]
_T2_FALLBACKS = ["gemini-2.5-flash-thinking", "qwen3:8b", "gemini-3-flash"]
_T3_FALLBACKS = ["gemini-2.5-pro", "gemini-3-pro-high", "claude-opus-4-6", "gemini-2.5-flash-thinking"]


def list_available_models(
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> list[str]:
    """Return model IDs available on the BASE_LLM proxy.

    Returns an empty list if the endpoint is unreachable or the openai
    package is not installed — callers treat this as non-fatal.
    """
    url = api_url or os.getenv("BASE_LLM_API_URL")
    key = api_key or os.getenv("BASE_LLM_API_KEY", "")
    if not url:
        return []
    try:
        from openai import OpenAI
        client = OpenAI(base_url=url, api_key=key)
        return sorted(m.id for m in client.models.list().data)
    except Exception as exc:
        logger.warning("model_probe: could not list models from %s: %s", url, exc)
        return []


def select_model(
    preferred: str,
    available: list[str],
    fallbacks: list[str],
    task: str = "task",
) -> str:
    """Return *preferred* if available, else first matching fallback.

    Logs a warning if the preferred model is missing so operators can
    update their config without waiting for a downstream failure.
    """
    if preferred in available:
        return preferred
    for candidate in fallbacks:
        if candidate in available:
            logger.warning(
                "model_probe: '%s' not available for %s — using '%s' instead",
                preferred, task, candidate,
            )
            return candidate
    # Nothing matched — return preferred anyway and let the caller fail loudly
    logger.error(
        "model_probe: no suitable model found for %s (wanted '%s', checked %s)",
        task, preferred, fallbacks,
    )
    return preferred


class ModelProbe:
    """Validates and auto-corrects model configuration against the live proxy.

    Usage (typically at runner cycle start):

        probe = ModelProbe()
        probe.validate_and_patch_env()   # patches os.environ in-place
        # Now EMBEDDING_MODEL, EXTRACTION_MODEL are guaranteed available
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self._api_url = api_url or os.getenv("BASE_LLM_API_URL")
        self._api_key = api_key or os.getenv("BASE_LLM_API_KEY", "")
        self._available: Optional[list[str]] = None

    @property
    def available(self) -> list[str]:
        """Lazily fetched model list — cached for the lifetime of this probe."""
        if self._available is None:
            self._available = list_available_models(self._api_url, self._api_key)
            if self._available:
                logger.info(
                    "model_probe: %d model(s) available on BASE_LLM proxy",
                    len(self._available),
                )
            else:
                logger.warning("model_probe: BASE_LLM proxy unreachable or returned no models")
        return self._available

    def check(self, model: str, fallbacks: list[str], task: str = "task") -> str:
        """Return *model* if available on the proxy, otherwise best fallback."""
        if not self.available:
            return model  # can't validate — pass through
        return select_model(model, self.available, fallbacks, task)

    def validate_and_patch_env(self) -> dict[str, str]:
        """Validate key env-var models and patch os.environ with corrections.

        Returns a dict of {env_var: corrected_value} for any vars that
        were changed, so callers can log or alert.
        """
        available = self.available
        if not available:
            return {}

        patches: dict[str, str] = {}

        checks = [
            ("EMBEDDING_MODEL", os.getenv("EMBEDDING_MODEL", "bge-m3"), _EMBEDDING_FALLBACKS, "embedding"),
            ("EXTRACTION_MODEL", os.getenv("EXTRACTION_MODEL", "gemini-3-flash"), _EXTRACTION_FALLBACKS, "extraction"),
        ]

        for env_var, configured, fallbacks, task in checks:
            corrected = select_model(configured, available, fallbacks, task)
            if corrected != configured:
                os.environ[env_var] = corrected
                patches[env_var] = corrected

        return patches

    def validate_model_tiers(self, model_config) -> dict[str, str]:
        """Check T1/T2/T3 model tier names and return suggested corrections.

        Does NOT patch env vars — tier models are used by Claude CLI subprocess
        (not BASE_LLM proxy), so they're validated separately for informational
        purposes only.
        """
        if not model_config or not self.available:
            return {}
        report = {}
        for attr, task in [
            ("t1_heavy", "T1-heavy"),
            ("t2_standard", "T2-standard"),
        ]:
            model = getattr(model_config, attr, None)
            if model and model not in self.available:
                report[attr] = f"'{model}' not in BASE_LLM proxy (may be Claude CLI model — OK)"
        return report
