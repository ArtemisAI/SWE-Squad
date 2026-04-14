"""Task queue provider registry.

Resolves queue name → TaskQueueProvider instance from config, so the
runner never hardcodes InMemoryTaskQueue directly.

Usage::

    queue = create_task_queue("memory", {})
    queue = create_task_queue("redis", {"url": "redis://localhost:6379"})  # future
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from src.swe_team.providers.schema import ProviderParameter
from src.swe_team.providers.task_queue.base import TaskQueueProvider

logger = logging.getLogger(__name__)

# Registry of provider name → factory callable.
# Each factory receives (config: dict) and returns a TaskQueueProvider.
_REGISTRY: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
_PARAMETER_SCHEMAS: Dict[str, list[ProviderParameter]] = {}


def register_task_queue(
    name: str,
    factory: Callable[[Dict[str, Any]], Any],
    *,
    parameters: Optional[list[ProviderParameter]] = None,
) -> None:
    """Register a task queue factory by name."""
    _REGISTRY[name] = factory
    _PARAMETER_SCHEMAS[name] = list(parameters or [])


def _memory_factory(config: Dict[str, Any]) -> TaskQueueProvider:
    """Build an InMemoryTaskQueue (ignores config — no external resources needed)."""
    from src.swe_team.providers.task_queue.memory import InMemoryTaskQueue

    return InMemoryTaskQueue()


# Register built-in providers.
register_task_queue("memory", _memory_factory, parameters=[])


def create_task_queue(
    provider_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> TaskQueueProvider:
    """Resolve a task queue by provider name.

    Args:
        provider_name: Queue backend name (e.g. 'memory', 'redis').
                       Must be registered in the task queue registry.
        config: Provider-specific config dict (from swe_team.yaml
                ``providers.task_queue``).

    Returns:
        A configured TaskQueueProvider instance.

    Raises:
        ValueError: If the provider name is not registered.
    """
    config = config or {}
    factory = _REGISTRY.get(provider_name)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown task queue provider '{provider_name}'. "
            f"Available: {available}"
        )
    logger.info("Creating task queue: %s", provider_name)
    return factory(config)


def list_task_queues() -> List[str]:
    """Return sorted list of registered task queue provider names."""
    return sorted(_REGISTRY.keys())


def get_task_queue_parameters(provider_name: str) -> list[ProviderParameter]:
    """Return provider parameter schema for dynamic config forms."""
    if provider_name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown task queue provider '{provider_name}'. "
            f"Available: {available}"
        )
    return list(_PARAMETER_SCHEMAS.get(provider_name, []))


def list_task_queue_parameters() -> Dict[str, list[ProviderParameter]]:
    """Return parameter schemas for all registered task queue providers."""
    return {
        name: list(_PARAMETER_SCHEMAS.get(name, []))
        for name in sorted(_REGISTRY.keys())
    }
