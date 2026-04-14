from __future__ import annotations

import pytest

from src.swe_team.integrations import get_connector, list_connectors


def test_builtin_connectors_include_existing_integrations() -> None:
    connector_types = {connector.manifest.connector_type for connector in list_connectors()}
    assert "telegram" in connector_types
    assert "github" in connector_types
    assert "supabase" in connector_types
    assert "engine:claude" in connector_types


def test_category_filter_returns_matching_connectors() -> None:
    notifications = list_connectors(category="notifications")
    notification_types = {connector.manifest.connector_type for connector in notifications}
    assert "telegram" in notification_types
    assert "github" not in notification_types


def test_static_connector_action_and_trigger_validation() -> None:
    connector = get_connector("github")
    creds = {"token": "token", "repo": "owner/repo"}

    action_result = connector.execute_action("create_issue", {"title": "t"}, creds)
    assert action_result["ok"] is True
    assert action_result["action"] == "create_issue"

    trigger_ref = connector.register_trigger("issues", "https://example.com/hook", creds)
    assert trigger_ref.startswith("github:issues:")

    with pytest.raises(ValueError, match="Action 'nonexistent_action'"):
        connector.execute_action("nonexistent_action", {}, creds)

    with pytest.raises(ValueError, match="Trigger 'nonexistent_trigger'"):
        connector.register_trigger("nonexistent_trigger", "https://example.com/hook", creds)


def test_unknown_connector_raises() -> None:
    with pytest.raises(ValueError, match="Unknown integration connector"):
        get_connector("not-real")
