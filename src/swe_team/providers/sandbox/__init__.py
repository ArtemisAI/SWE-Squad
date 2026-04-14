"""Sandbox provider registry.

Resolves provider name → SandboxProvider instance from config, so core
agents never hardcode a specific sandbox backend directly.

Usage::

    sandbox = create_sandbox_provider("local", config_dict)
    sandbox = create_sandbox_provider("docker", config_dict)
    sandbox = create_sandbox_provider("proxmox", config_dict)
    sandbox = create_sandbox_provider("aws", config_dict)
    sandbox = create_sandbox_provider("gcp", config_dict)
    sandbox = create_sandbox_provider("azure", config_dict)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.swe_team.providers.sandbox.base import SandboxProvider
from src.swe_team.providers.schema import ProviderParameter

logger = logging.getLogger(__name__)

# Registry of provider name → factory callable.
# Each factory receives (config: dict) and returns a SandboxProvider.
_REGISTRY: Dict[str, Any] = {}
_PARAMETER_SCHEMAS: Dict[str, list[ProviderParameter]] = {}


def register_sandbox_provider(
    name: str,
    factory: Any,
    *,
    parameters: Optional[list[ProviderParameter]] = None,
) -> None:
    """Register a sandbox provider factory by name."""
    _REGISTRY[name] = factory
    _PARAMETER_SCHEMAS[name] = list(parameters or [])


def _local_factory(config: Dict[str, Any]) -> SandboxProvider:
    """Build a LocalSandbox from config dict."""
    from src.swe_team.providers.sandbox.local import from_config

    return from_config(config)


def _docker_factory(config: Dict[str, Any]) -> SandboxProvider:
    """Build a DockerSandbox from config dict."""
    from src.swe_team.providers.sandbox.docker import from_config

    return from_config(config)


def _proxmox_factory(config: Dict[str, Any]) -> SandboxProvider:
    """Build a ProxmoxSandbox from config dict."""
    from src.swe_team.providers.sandbox.proxmox import from_config

    return from_config(config)


def _cloud_factory(platform: str, config: Dict[str, Any]) -> SandboxProvider:
    """Build a cloud VM sandbox provider from config dict."""
    from src.swe_team.providers.sandbox.cloud import from_config

    return from_config(platform, config)


def _make_cloud_factory(platform: str) -> Any:
    return lambda config: _cloud_factory(platform, config)


# Register built-in providers
register_sandbox_provider("local", _local_factory, parameters=[])
register_sandbox_provider(
    "docker",
    _docker_factory,
    parameters=[
        {
            "name": "docker_binary",
            "type": "string",
            "required": False,
            "default": "docker",
            "description": "Path to docker CLI binary",
        },
        {
            "name": "default_image",
            "type": "string",
            "required": False,
            "default": "python:3.11-slim",
            "description": "Default Docker image for sandboxes",
        },
    ],
)
register_sandbox_provider(
    "proxmox",
    _proxmox_factory,
    parameters=[
        {
            "name": "gateway_url",
            "type": "string",
            "required": True,
            "description": "Proxmox gateway URL",
        },
        {
            "name": "api_key",
            "type": "secret",
            "required": True,
            "description": "Proxmox API key",
        },
    ],
)
for _platform in ("aws", "gcp", "azure"):
    register_sandbox_provider(_platform, _make_cloud_factory(_platform))


def create_sandbox_provider(
    provider_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> SandboxProvider:
    """Resolve a sandbox provider by name.

    Args:
        provider_name: Provider name (e.g. 'local', 'docker', 'proxmox').
                       Must be registered in the provider registry.
        config: Provider-specific config dict (from swe_team.yaml
                ``providers.sandbox``).

    Returns:
        A configured SandboxProvider instance.

    Raises:
        ValueError: If the provider name is not registered.
    """
    config = config or {}
    factory = _REGISTRY.get(provider_name)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown sandbox provider '{provider_name}'. "
            f"Available: {available}"
        )
    logger.info("Resolving sandbox provider: %s", provider_name)
    return factory(config)


def list_sandbox_providers() -> list[str]:
    """Return sorted list of registered sandbox provider names."""
    return sorted(_REGISTRY.keys())


def get_sandbox_provider_parameters(provider_name: str) -> list[ProviderParameter]:
    """Return provider parameter schema for dynamic config forms."""
    if provider_name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown sandbox provider '{provider_name}'. "
            f"Available: {available}"
        )
    return list(_PARAMETER_SCHEMAS.get(provider_name, []))


def list_sandbox_provider_parameters() -> Dict[str, list[ProviderParameter]]:
    """Return parameter schemas for all registered sandbox providers."""
    return {
        name: list(_PARAMETER_SCHEMAS.get(name, []))
        for name in sorted(_REGISTRY.keys())
    }
