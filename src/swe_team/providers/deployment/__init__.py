"""Deployment provider registry."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.swe_team.providers.deployment.base import DeploymentProvider

logger = logging.getLogger(__name__)

_REGISTRY: Dict[str, Any] = {}


def register_deployment_provider(name: str, factory: Any) -> None:
    _REGISTRY[name] = factory


def _vercel_factory(config: Dict[str, Any]) -> DeploymentProvider:
    from src.swe_team.providers.deployment.vercel_provider import VercelDeploymentProvider

    return VercelDeploymentProvider(
        token=config.get("token", ""),
        team_id=config.get("team_id", ""),
    )


register_deployment_provider("vercel", _vercel_factory)


def create_deployment_provider(
    provider_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> DeploymentProvider:
    config = config or {}
    factory = _REGISTRY.get(provider_name)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown deployment provider '{provider_name}'. Available: {available}"
        )
    logger.info("Resolving deployment provider: %s", provider_name)
    return factory(config)


def list_deployment_providers() -> list[str]:
    return sorted(_REGISTRY.keys())
