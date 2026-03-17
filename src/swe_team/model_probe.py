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
# Each candidate is PROBED with a real API call before being selected.
# Order: fastest/cheapest first, degrading gracefully to heavier models.
# gemini-3-flash is known to return empty completions on this proxy — keep it
# as a last-resort in case it's fixed, but never first.
_EMBEDDING_FALLBACKS = [
    "bge-m3",            # primary — fast, 1024-dim
    "mxbai-embed-large", # fallback
    "nomic-embed-text",  # fallback
    "qwen3-embedding",   # fallback
]
_EXTRACTION_FALLBACKS = [
    "gemini-2.5-flash-thinking",  # proven working
    "kimi-k2.5:cloud",            # 1M context, good for long logs
    "qwen3-coder:30b",            # code-aware, reliable
    "qwen3:8b",                   # lightweight fallback
    "deepseek-r1:14b",            # strong reasoning fallback
    "gemini-3-flash",             # last resort — known to return empty sometimes
]
_T1_FALLBACKS = [                 # cheap/fast tasks via BASE_LLM proxy
    "gemini-2.5-flash-thinking",
    "kimi-k2.5:cloud",
    "qwen3:8b",
    "qwen3:4b",
    "gemini-3-flash",             # last resort
]
_T2_FALLBACKS = [                 # standard tasks via BASE_LLM proxy
    "gemini-2.5-flash-thinking",
    "kimi-k2.5:cloud",
    "qwen3-coder:30b",
    "deepseek-r1:14b",
    "gemini-3-flash",
]
_T3_FALLBACKS = [                 # heavy/critical tasks via BASE_LLM proxy
    "gemini-2.5-pro",
    "kimi-k2.5:cloud",
    "gemini-3-pro-high",
    "claude-opus-4-6",
    "deepseek-v3.1:671b-cloud",
    "gemini-2.5-flash-thinking",
]


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


def probe_embedding_model(
    model: str,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> bool:
    """Test that *model* actually responds to an embed request.

    Returns True on success, False on any error.  Uses a tiny probe string
    to keep cost near zero.
    """
    url = api_url or os.getenv("BASE_LLM_API_URL")
    key = api_key or os.getenv("BASE_LLM_API_KEY", "")
    if not url:
        return False
    try:
        from openai import OpenAI
        client = OpenAI(base_url=url, api_key=key, timeout=10)
        resp = client.embeddings.create(model=model, input="probe")
        ok = bool(resp.data and resp.data[0].embedding)
        if not ok:
            logger.warning("model_probe: embed probe for '%s' returned empty result", model)
        return ok
    except Exception as exc:
        logger.warning("model_probe: embed probe for '%s' failed: %s", model, exc)
        return False


def probe_chat_model(
    model: str,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> bool:
    """Test that *model* actually responds to a minimal chat completion.

    Returns True on success, False on any error.
    """
    url = api_url or os.getenv("BASE_LLM_API_URL")
    key = api_key or os.getenv("BASE_LLM_API_KEY", "")
    if not url:
        return False
    try:
        from openai import OpenAI
        client = OpenAI(base_url=url, api_key=key, timeout=15)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=4,
        )
        ok = bool(resp.choices and resp.choices[0].message.content)
        if not ok:
            logger.warning("model_probe: chat probe for '%s' returned empty result", model)
        return ok
    except Exception as exc:
        logger.warning("model_probe: chat probe for '%s' failed: %s", model, exc)
        return False


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

        For each model, first checks the proxy's model list, then sends an
        actual probe request to confirm the model responds before committing
        to it.  Falls back through the fallback list until one passes both
        checks.

        Returns a dict of {env_var: final_value} for any vars that were
        changed so callers can log/alert.
        """
        available = self.available
        if not available:
            return {}

        patches: dict[str, str] = {}

        checks = [
            ("EMBEDDING_MODEL", os.getenv("EMBEDDING_MODEL", "bge-m3"), _EMBEDDING_FALLBACKS, "embedding", "embed"),
            ("EXTRACTION_MODEL", os.getenv("EXTRACTION_MODEL", "gemini-3-flash"), _EXTRACTION_FALLBACKS, "extraction", "chat"),
        ]

        for env_var, configured, fallbacks, task, probe_type in checks:
            # Build candidate list: configured first, then fallbacks
            candidates = [configured] + [f for f in fallbacks if f != configured]
            chosen = None
            for candidate in candidates:
                if candidate not in available:
                    logger.debug("model_probe: '%s' not in proxy model list — skipping", candidate)
                    continue
                # Actually probe the model
                if probe_type == "embed":
                    ok = probe_embedding_model(candidate, self._api_url, self._api_key)
                else:
                    ok = probe_chat_model(candidate, self._api_url, self._api_key)
                if ok:
                    chosen = candidate
                    break
                logger.warning(
                    "model_probe: '%s' listed but probe failed for %s — trying next fallback",
                    candidate, task,
                )

            if chosen is None:
                logger.error(
                    "model_probe: ALL candidates failed probe for %s; keeping '%s'",
                    task, configured,
                )
                chosen = configured  # keep original — let caller fail loudly

            if chosen != configured:
                logger.warning(
                    "model_probe: using '%s' instead of '%s' for %s",
                    chosen, configured, task,
                )
            os.environ[env_var] = chosen
            if chosen != configured:
                patches[env_var] = chosen

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
