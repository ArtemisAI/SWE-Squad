"""Repo map provider registry.

Resolves provider name → RepoMapProvider instance from config, so core
agents never hardcode ``CtagsRepoMapProvider`` directly.

Usage::

    repomap = create_repomap_provider("ctags", config_dict)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.swe_team.providers.repomap.base import RepoMapProvider
from src.swe_team.providers.schema import ProviderParameter

logger = logging.getLogger(__name__)

# Registry of provider name → factory callable.
# Each factory receives (config: dict) and returns a RepoMapProvider.
_REGISTRY: Dict[str, Any] = {}
_PARAMETER_SCHEMAS: Dict[str, list[ProviderParameter]] = {}


def register_repomap_provider(
    name: str,
    factory: Any,
    *,
    parameters: Optional[list[ProviderParameter]] = None,
) -> None:
    """Register a repo map provider factory by name."""
    _REGISTRY[name] = factory
    _PARAMETER_SCHEMAS[name] = list(parameters or [])


def _ctags_factory(config: Dict[str, Any]) -> RepoMapProvider:
    """Build a CtagsRepoMapProvider from config dict."""
    from src.swe_team.providers.repomap.ctags_provider import CtagsRepoMapProvider

    return CtagsRepoMapProvider(config=config)


# Register built-in providers
register_repomap_provider(
    "ctags",
    _ctags_factory,
    parameters=[
        {
            "name": "ctags_binary",
            "type": "string",
            "required": False,
            "default": "ctags",
            "description": "Path to universal-ctags binary",
        },
    ],
)


def create_repomap_provider(
    provider_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> RepoMapProvider:
    """Resolve a repo map provider by name.

    Args:
        provider_name: Provider name (e.g. 'ctags').
                       Must be registered in the provider registry.
        config: Provider-specific config dict (from swe_team.yaml
                ``providers.repomap``).

    Returns:
        A configured RepoMapProvider instance.

    Raises:
        ValueError: If the provider name is not registered.
    """
    config = config or {}
    factory = _REGISTRY.get(provider_name)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown repo map provider '{provider_name}'. "
            f"Available: {available}"
        )
    logger.info("Resolving repo map provider: %s", provider_name)
    return factory(config)


def list_repomap_providers() -> list[str]:
    """Return sorted list of registered repo map provider names."""
    return sorted(_REGISTRY.keys())


def get_repomap_provider_parameters(provider_name: str) -> list[ProviderParameter]:
    """Return provider parameter schema for dynamic config forms."""
    if provider_name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown repo map provider '{provider_name}'. "
            f"Available: {available}"
        )
    return list(_PARAMETER_SCHEMAS.get(provider_name, []))


def list_repomap_provider_parameters() -> Dict[str, list[ProviderParameter]]:
    """Return parameter schemas for all registered repo map providers."""
    return {
        name: list(_PARAMETER_SCHEMAS.get(name, []))
        for name in sorted(_REGISTRY.keys())
    }
