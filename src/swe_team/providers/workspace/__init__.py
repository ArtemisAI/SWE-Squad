"""Workspace provider registry.

Resolves provider name → WorkspaceProvider instance from config, so core
agents never hardcode ``GitWorktreeProvider`` directly.

Usage::

    workspace = create_workspace_provider("git-worktree", config_dict)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.swe_team.providers.schema import ProviderParameter
from src.swe_team.providers.workspace.base import WorkspaceProvider
from src.swe_team.providers.workspace.git_worktree import GitWorktreeProvider

logger = logging.getLogger(__name__)

# Registry of provider name → factory callable.
# Each factory receives (config: dict) and returns a WorkspaceProvider.
_REGISTRY: Dict[str, Any] = {}
_PARAMETER_SCHEMAS: Dict[str, list[ProviderParameter]] = {}


def register_workspace_provider(
    name: str,
    factory: Any,
    *,
    parameters: Optional[list[ProviderParameter]] = None,
) -> None:
    """Register a workspace provider factory by name."""
    _REGISTRY[name] = factory
    _PARAMETER_SCHEMAS[name] = list(parameters or [])


def _git_worktree_factory(config: Dict[str, Any]) -> WorkspaceProvider:
    """Build a GitWorktreeProvider from config dict."""
    return GitWorktreeProvider(config=config)


# Register built-in providers
register_workspace_provider(
    "git-worktree",
    _git_worktree_factory,
    parameters=[
        {
            "name": "base_dir",
            "type": "string",
            "required": False,
            "description": "Directory where worktrees are created",
        },
        {
            "name": "keep_days",
            "type": "number",
            "required": False,
            "default": 7,
            "description": "How long to keep inactive worktrees",
        },
    ],
)


def create_workspace_provider(
    provider_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> WorkspaceProvider:
    """Resolve a workspace provider by name.

    Args:
        provider_name: Provider name (e.g. 'git-worktree').
                       Must be registered in the provider registry.
        config: Provider-specific config dict (from swe_team.yaml
                ``providers.workspace``).

    Returns:
        A configured WorkspaceProvider instance.

    Raises:
        ValueError: If the provider name is not registered.
    """
    config = config or {}
    factory = _REGISTRY.get(provider_name)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown workspace provider '{provider_name}'. "
            f"Available: {available}"
        )
    logger.info("Resolving workspace provider: %s", provider_name)
    return factory(config)


def list_workspace_providers() -> list[str]:
    """Return sorted list of registered workspace provider names."""
    return sorted(_REGISTRY.keys())


def get_workspace_provider_parameters(provider_name: str) -> list[ProviderParameter]:
    """Return provider parameter schema for dynamic config forms."""
    if provider_name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown workspace provider '{provider_name}'. "
            f"Available: {available}"
        )
    return list(_PARAMETER_SCHEMAS.get(provider_name, []))


def list_workspace_provider_parameters() -> Dict[str, list[ProviderParameter]]:
    """Return parameter schemas for all registered workspace providers."""
    return {
        name: list(_PARAMETER_SCHEMAS.get(name, []))
        for name in sorted(_REGISTRY.keys())
    }


__all__ = [
    "GitWorktreeProvider",
    "create_workspace_provider",
    "list_workspace_providers",
    "list_workspace_provider_parameters",
    "get_workspace_provider_parameters",
    "register_workspace_provider",
]
