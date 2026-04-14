"""Shared schema types for self-describing provider configuration."""
from __future__ import annotations

from typing import Any, Literal, TypedDict


class ProviderParameter(TypedDict, total=False):
    """Single provider config field descriptor for dynamic forms."""

    name: str
    type: Literal["string", "number", "boolean", "secret", "array", "object"]
    required: bool
    description: str
    default: Any
    options: list[str]
