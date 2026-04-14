"""Supabase direct provider registry."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.swe_team.providers.supabase_direct.base import SupabaseDirectProvider

logger = logging.getLogger(__name__)

_REGISTRY: Dict[str, Any] = {}


def register_supabase_direct_provider(name: str, factory: Any) -> None:
    _REGISTRY[name] = factory


def _rest_factory(config: Dict[str, Any]) -> SupabaseDirectProvider:
    from src.swe_team.providers.supabase_direct.provider import (
        SupabaseRESTDirectProvider,
    )

    return SupabaseRESTDirectProvider(
        url=config.get("url", ""),
        key=config.get("key", ""),
        schema=config.get("schema", "public"),
    )


register_supabase_direct_provider("rest", _rest_factory)


def create_supabase_direct_provider(
    provider_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> SupabaseDirectProvider:
    config = config or {}
    factory = _REGISTRY.get(provider_name)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown supabase direct provider '{provider_name}'. Available: {available}"
        )
    logger.info("Resolving supabase direct provider: %s", provider_name)
    return factory(config)


def list_supabase_direct_providers() -> list[str]:
    return sorted(_REGISTRY.keys())
