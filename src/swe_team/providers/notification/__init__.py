"""Notification provider registry.

Resolves provider name → NotificationProvider instance from config, so the
notifier never hardcodes ``TelegramNotificationProvider`` directly.

Usage::

    provider = create_notification_provider("telegram", config_dict)
    provider = create_notification_provider("slack", config_dict)  # future
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.swe_team.providers.notification.base import NotificationProvider
from src.swe_team.providers.schema import ProviderParameter

logger = logging.getLogger(__name__)

# Registry of provider name → factory callable.
# Each factory receives (config: dict) and returns a NotificationProvider.
_REGISTRY: Dict[str, Any] = {}
_PARAMETER_SCHEMAS: Dict[str, list[ProviderParameter]] = {}


def register_notification_provider(
    name: str,
    factory: Any,
    *,
    parameters: Optional[list[ProviderParameter]] = None,
) -> None:
    """Register a notification provider factory by name."""
    _REGISTRY[name] = factory
    _PARAMETER_SCHEMAS[name] = list(parameters or [])


def _telegram_factory(config: Dict[str, Any]) -> NotificationProvider:
    """Build a TelegramNotificationProvider from config dict."""
    from src.swe_team.providers.notification.telegram_provider import (
        TelegramNotificationProvider,
    )

    return TelegramNotificationProvider(
        token=config.get("token", ""),
        chat_id=config.get("chat_id", ""),
    )


def _slack_factory(config: Dict[str, Any]) -> NotificationProvider:
    """Build a SlackNotificationProvider from config dict."""
    from src.swe_team.providers.notification.slack_provider import (
        SlackNotificationProvider,
    )

    return SlackNotificationProvider(
        token=config.get("token", ""),
        channel=config.get("channel", ""),
    )


# Register built-in providers
register_notification_provider(
    "telegram",
    _telegram_factory,
    parameters=[
        {
            "name": "token",
            "type": "secret",
            "required": True,
            "description": "Telegram bot token",
        },
        {
            "name": "chat_id",
            "type": "string",
            "required": True,
            "description": "Telegram chat ID for alerts",
        },
    ],
)
register_notification_provider("slack", _slack_factory)


def create_notification_provider(
    provider_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> NotificationProvider:
    """Resolve a notification provider by name.

    Args:
        provider_name: Provider name (e.g. 'telegram', 'slack').
                       Must be registered in the provider registry.
        config: Provider-specific config dict (from swe_team.yaml
                ``providers.notification``).

    Returns:
        A configured NotificationProvider instance.

    Raises:
        ValueError: If the provider name is not registered.
    """
    config = config or {}
    factory = _REGISTRY.get(provider_name)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown notification provider '{provider_name}'. "
            f"Available: {available}"
        )
    logger.info("Resolving notification provider: %s", provider_name)
    return factory(config)


def list_notification_providers() -> list[str]:
    """Return sorted list of registered notification provider names."""
    return sorted(_REGISTRY.keys())


def get_notification_provider_parameters(provider_name: str) -> list[ProviderParameter]:
    """Return provider parameter schema for dynamic config forms."""
    if provider_name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown notification provider '{provider_name}'. "
            f"Available: {available}"
        )
    return list(_PARAMETER_SCHEMAS.get(provider_name, []))


def list_notification_provider_parameters() -> Dict[str, list[ProviderParameter]]:
    """Return parameter schemas for all registered notification providers."""
    return {
        name: list(_PARAMETER_SCHEMAS.get(name, []))
        for name in sorted(_REGISTRY.keys())
    }
