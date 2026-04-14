"""Integrations Hub connector protocol and built-in connector registry."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from src.swe_team.providers.coding_engine import list_engines


@dataclass(frozen=True)
class CredentialField:
    """Credential field for dynamic integration auth forms."""

    key: str
    label: str
    field_type: str
    required: bool = True
    secret: bool = True
    description: str = ""


@dataclass(frozen=True)
class ConnectorManifest:
    """Self-describing connector metadata."""

    connector_type: str
    name: str
    category: str
    description: str
    icon: str
    auth_type: str
    actions: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    credential_schema: list[CredentialField] = field(default_factory=list)


@runtime_checkable
class IntegrationConnector(Protocol):
    @property
    def manifest(self) -> ConnectorManifest: ...

    @property
    def credential_schema(self) -> list[CredentialField]: ...

    def test_connection(self, credentials: dict[str, Any]) -> bool: ...

    def execute_action(
        self, action: str, params: dict[str, Any], credentials: dict[str, Any]
    ) -> dict[str, Any]: ...

    def register_trigger(
        self, trigger: str, webhook_url: str, credentials: dict[str, Any]
    ) -> str: ...


class StaticIntegrationConnector:
    """Manifest-driven connector used for integrations catalog and dry-run wiring."""

    def __init__(self, manifest: ConnectorManifest):
        self._manifest = manifest

    @property
    def manifest(self) -> ConnectorManifest:
        return self._manifest

    @property
    def credential_schema(self) -> list[CredentialField]:
        return list(self._manifest.credential_schema)

    def test_connection(self, credentials: dict[str, Any]) -> bool:
        for field in self._manifest.credential_schema:
            if field.required and not credentials.get(field.key):
                return False
        return True

    def execute_action(
        self, action: str, params: dict[str, Any], credentials: dict[str, Any]
    ) -> dict[str, Any]:
        if action not in self._manifest.actions:
            raise ValueError(
                f"Action '{action}' not supported by connector '{self._manifest.connector_type}'"
            )
        return {
            "ok": self.test_connection(credentials),
            "connector_type": self._manifest.connector_type,
            "action": action,
            "params": params,
        }

    def register_trigger(
        self, trigger: str, webhook_url: str, credentials: dict[str, Any]
    ) -> str:
        if trigger not in self._manifest.triggers:
            raise ValueError(
                f"Trigger '{trigger}' not supported by connector '{self._manifest.connector_type}'"
            )
        if not webhook_url:
            raise ValueError("webhook_url is required")
        if not self.test_connection(credentials):
            raise ValueError("invalid credentials")
        return f"{self._manifest.connector_type}:{trigger}:{webhook_url}"


class IntegrationRegistry:
    """Registry of connector_type -> IntegrationConnector."""

    def __init__(self) -> None:
        self._connectors: dict[str, IntegrationConnector] = {}

    def register(self, connector: IntegrationConnector) -> None:
        self._connectors[connector.manifest.connector_type] = connector

    def get(self, connector_type: str) -> IntegrationConnector:
        connector = self._connectors.get(connector_type)
        if connector is None:
            available = ", ".join(sorted(self._connectors.keys())) or "(none)"
            raise ValueError(
                f"Unknown integration connector '{connector_type}'. Available: {available}"
            )
        return connector

    def list(self, *, category: str | None = None) -> list[IntegrationConnector]:
        connectors = self._connectors.values()
        if category is not None:
            connectors = [c for c in connectors if c.manifest.category == category]
        return sorted(connectors, key=lambda c: c.manifest.name.lower())


_REGISTRY = IntegrationRegistry()
_BUILTINS_REGISTERED = False


def register_connector(connector: IntegrationConnector) -> None:
    """Register an integration connector instance."""
    _REGISTRY.register(connector)


def get_connector(connector_type: str) -> IntegrationConnector:
    """Return a registered connector by type."""
    return _REGISTRY.get(connector_type)


def list_connectors(*, category: str | None = None) -> list[IntegrationConnector]:
    """List registered connectors, optionally filtered by category."""
    return _REGISTRY.list(category=category)


def _builtin_connectors() -> list[StaticIntegrationConnector]:
    return [
        StaticIntegrationConnector(
            ConnectorManifest(
                connector_type="telegram",
                name="Telegram",
                category="notifications",
                description="Send alerts, summaries, and HITL escalation messages.",
                icon="message-circle",
                auth_type="api_key",
                actions=["send_message", "send_alert"],
                triggers=[],
                credential_schema=[
                    CredentialField("token", "Bot Token", "password", required=True),
                    CredentialField("chat_id", "Chat ID", "text", required=True, secret=False),
                ],
            )
        ),
        StaticIntegrationConnector(
            ConnectorManifest(
                connector_type="github",
                name="GitHub",
                category="source_control",
                description="Create and comment on issues, pull requests, and workflow hooks.",
                icon="github",
                auth_type="api_key",
                actions=["create_issue", "comment_issue", "create_pull_request"],
                triggers=["issues", "pull_request", "push", "workflow_run"],
                credential_schema=[
                    CredentialField("token", "Personal Access Token", "password", required=True),
                    CredentialField("repo", "Repository (owner/name)", "text", required=True, secret=False),
                ],
            )
        ),
        StaticIntegrationConnector(
            ConnectorManifest(
                connector_type="supabase",
                name="Supabase",
                category="storage",
                description="Persist tickets, embeddings, audit logs, and analytics state.",
                icon="database",
                auth_type="api_key",
                actions=["upsert_ticket", "query_tickets", "store_embedding"],
                triggers=[],
                credential_schema=[
                    CredentialField("url", "Supabase URL", "url", required=True, secret=False),
                    CredentialField("anon_key", "Anon/Service Key", "password", required=True),
                ],
            )
        ),
        *[
            StaticIntegrationConnector(
                ConnectorManifest(
                    connector_type=f"engine:{engine}",
                    name=f"{engine.replace('_', ' ').title()} Engine",
                    category="ai_llm",
                    description=f"Route coding tasks through the '{engine}' coding engine connector.",
                    icon="cpu",
                    auth_type="config",
                    actions=["run_prompt"],
                    triggers=[],
                    config_schema={"type": "object", "additionalProperties": True},
                )
            )
            for engine in list_engines()
        ],
    ]


def _register_builtins_once() -> None:
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return
    for connector in _builtin_connectors():
        register_connector(connector)
    _BUILTINS_REGISTERED = True


_register_builtins_once()
