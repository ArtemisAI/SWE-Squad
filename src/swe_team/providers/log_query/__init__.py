"""Log query providers — pluggable log querying and search."""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.swe_team.providers.log_query.base import LogEntry, LogQueryProvider
from src.swe_team.providers.schema import ProviderParameter

_PARAMETER_SCHEMAS: Dict[str, list[ProviderParameter]] = {
    "local": [
        {
            "name": "log_directories",
            "type": "array",
            "required": True,
            "description": "Directories to scan for log files",
        },
        {
            "name": "file_pattern",
            "type": "string",
            "required": False,
            "default": "*.log",
            "description": "Glob pattern for log files",
        },
    ],
    "loki": [
        {
            "name": "url",
            "type": "string",
            "required": True,
            "description": "Loki base URL",
        },
        {
            "name": "query_timeout_seconds",
            "type": "number",
            "required": False,
            "default": 10,
            "description": "Loki query timeout in seconds",
        },
    ],
}


def create_log_query_provider(config: Optional[Dict[str, Any]] = None) -> Optional[LogQueryProvider]:
    """Factory: create a LogQueryProvider from config.

    Config is expected to come from swe_team.yaml under providers.log_query.
    Returns None if no provider is configured (plugin-based: system works without one).

    Supported provider values:
        "local" — LocalFileProvider (default if log_directories are set)

    Example config::

        providers:
          log_query:
            provider: local
            log_directories:
              - logs/
              - logs/remote/
            remote_collection: false
            file_pattern: "*.log"
    """
    if config is None:
        return None

    provider_name = config.get("provider", "local")

    if provider_name == "local":
        from src.swe_team.providers.log_query.local import LocalFileProvider

        return LocalFileProvider(config)

    if provider_name == "loki":
        from src.swe_team.providers.log_query.loki import LokiProvider

        return LokiProvider(config)

    raise ValueError(f"Unknown log_query provider: {provider_name!r}")


def list_log_query_providers() -> list[str]:
    """Return supported log query providers."""
    return sorted(_PARAMETER_SCHEMAS.keys())


def get_log_query_provider_parameters(provider_name: str) -> list[ProviderParameter]:
    """Return provider parameter schema for dynamic config forms."""
    if provider_name not in _PARAMETER_SCHEMAS:
        available = ", ".join(sorted(_PARAMETER_SCHEMAS.keys())) or "(none)"
        raise ValueError(
            f"Unknown log_query provider '{provider_name}'. "
            f"Available: {available}"
        )
    return list(_PARAMETER_SCHEMAS.get(provider_name, []))


def list_log_query_provider_parameters() -> Dict[str, list[ProviderParameter]]:
    """Return parameter schemas for all log query providers."""
    return {
        name: list(_PARAMETER_SCHEMAS.get(name, []))
        for name in sorted(_PARAMETER_SCHEMAS.keys())
    }


__all__ = [
    "LogEntry",
    "LogQueryProvider",
    "create_log_query_provider",
    "list_log_query_providers",
    "get_log_query_provider_parameters",
    "list_log_query_provider_parameters",
]
