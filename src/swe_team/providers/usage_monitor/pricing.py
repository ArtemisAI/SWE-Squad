"""Pricing configuration for Claude API token cost estimation.

All prices are **per 1 million tokens**.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# Default pricing (USD per 1M tokens)
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4": {
        "input": 15.0,
        "output": 75.0,
        "cache_write": 18.75,
        "cache_read": 1.50,
    },
    "claude-opus-4-6": {
        "input": 15.0,
        "output": 75.0,
        "cache_write": 18.75,
        "cache_read": 1.50,
    },
    "claude-sonnet-4": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-haiku-3-5": {
        "input": 0.80,
        "output": 4.0,
        "cache_write": 1.0,
        "cache_read": 0.08,
    },
    "gemini-2.5-pro": {
        "input": 1.25,
        "output": 10.0,
        "cache_write": 0.0,
        "cache_read": 0.0,
    },
    "gemini-2.5-flash": {
        "input": 0.15,
        "output": 0.60,
        "cache_write": 0.0,
        "cache_read": 0.0,
    },
    "gemini-3-flash": {
        "input": 0.10,
        "output": 0.40,
        "cache_write": 0.0,
        "cache_read": 0.0,
    },
}

# Regex: strip trailing date stamps, context-window suffixes, and normalize
_STRIP_SUFFIX = re.compile(r"[-_]\d{8}$|[-_]\d+[kmb]$|\[.*\]")


def _normalize_model(name: str) -> str:
    """Normalize a model identifier for fuzzy matching.

    Examples:
        ``claude-opus-4-20250514``  -> ``claude-opus-4``
        ``claude-opus-4-6[1m]``    -> ``claude-opus-4-6``
        ``claude-sonnet-4-6``      -> ``claude-sonnet-4``  (after fallback)
    """
    name = name.strip().lower()
    # Strip [context] suffixes like [1m]
    name = re.sub(r"\[.*?\]", "", name)
    # Strip trailing date stamps (8 digits)
    name = re.sub(r"[-_]\d{8}$", "", name)
    return name


def _fuzzy_match(model: str, pricing: dict[str, dict[str, float]]) -> str | None:
    """Try progressively shorter prefixes to find a pricing entry."""
    norm = _normalize_model(model)

    # Exact match after normalisation
    if norm in pricing:
        return norm

    # Try stripping last dash-segment repeatedly
    parts = norm.split("-")
    while len(parts) > 2:
        parts.pop()
        candidate = "-".join(parts)
        if candidate in pricing:
            return candidate

    return None


def get_price(
    model: str,
    token_type: str,
    pricing: dict[str, dict[str, float]] | None = None,
) -> float:
    """Return the price per 1M tokens for *model* and *token_type*.

    *token_type* is one of ``input``, ``output``, ``cache_write``, ``cache_read``.
    Returns ``0.0`` if the model or token type is unknown.
    """
    pricing = pricing or DEFAULT_PRICING
    key = _fuzzy_match(model, pricing)
    if key is None:
        return 0.0
    return pricing[key].get(token_type, 0.0)


def load_pricing(path: str | None = None) -> dict[str, dict[str, float]]:
    """Load pricing from a JSON or YAML file.  Falls back to defaults.

    Supports JSON natively.  YAML requires ``pyyaml`` (optional).
    """
    if path is None:
        # Check default location
        default = Path("config/pricing.yaml")
        if not default.exists():
            default = Path("config/pricing.json")
        if not default.exists():
            return dict(DEFAULT_PRICING)
        path = str(default)

    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_PRICING)

    text = p.read_text()
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]

            data = yaml.safe_load(text)
        except ImportError:
            return dict(DEFAULT_PRICING)
    else:
        data = json.loads(text)

    if isinstance(data, dict):
        return data
    return dict(DEFAULT_PRICING)


def save_pricing(pricing: dict[str, dict[str, float]], path: str) -> None:
    """Persist *pricing* to a JSON file at *path*."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(pricing, indent=2) + "\n")
