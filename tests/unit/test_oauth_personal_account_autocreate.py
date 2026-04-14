"""Tests for OAuth first-login personal account auto-provisioning."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


def _make_handler():
    from scripts.ops.dashboard_server import DashboardHandler

    handler = mock.MagicMock(spec=DashboardHandler)
    handler.headers = {"Cookie": "swe_oauth_state=test-state"}
    return handler


class TestOAuthPersonalAccountAutocreate:
    def test_personal_account_slug_sanitizes_and_falls_back(self) -> None:
        from scripts.ops.dashboard_server import _personal_account_slug

        assert _personal_account_slug("Alice..--Example") == "alice-example"
        assert _personal_account_slug("!!!") == "personal-account"

    def test_callback_creates_personal_account_when_user_has_none(self) -> None:
        from scripts.ops import dashboard_server
        from scripts.ops.dashboard_server import DashboardHandler

        handler = _make_handler()

        oauth_provider = mock.Mock()
        oauth_provider.exchange_code.return_value = {
            "login": "Alice.Example",
            "email": "alice@example.com",
            "name": "Alice",
            "avatar_url": "https://example.com/alice.png",
        }
        oauth_provider.is_authorized.return_value = True
        oauth_provider.create_session_cookie.return_value = "session-cookie"

        user_store = mock.Mock()
        account_store = mock.Mock()
        account_store.get_user_accounts.return_value = []

        with mock.patch.object(dashboard_server, "_oauth_provider", oauth_provider), mock.patch.object(
            dashboard_server, "_get_user_store", return_value=user_store
        ), mock.patch.object(
            dashboard_server, "_get_account_store", return_value=account_store
        ):
            DashboardHandler._handle_auth_callback(
                handler,
                {"code": ["oauth-code"], "state": ["test-state"]},
            )

        account_store.create_account.assert_called_once_with(
            name="Alice.Example's Personal Account",
            slug="alice-example",
            created_by="Alice.Example",
            description="Auto-created personal account for Alice.Example",
        )

    def test_callback_skips_create_when_user_already_has_account(self) -> None:
        from scripts.ops import dashboard_server
        from scripts.ops.dashboard_server import DashboardHandler

        handler = _make_handler()

        oauth_provider = mock.Mock()
        oauth_provider.exchange_code.return_value = {"login": "alice"}
        oauth_provider.is_authorized.return_value = True
        oauth_provider.create_session_cookie.return_value = "session-cookie"

        user_store = mock.Mock()
        account_store = mock.Mock()
        account_store.get_user_accounts.return_value = [{"id": "acct-1"}]

        with mock.patch.object(dashboard_server, "_oauth_provider", oauth_provider), mock.patch.object(
            dashboard_server, "_get_user_store", return_value=user_store
        ), mock.patch.object(
            dashboard_server, "_get_account_store", return_value=account_store
        ):
            DashboardHandler._handle_auth_callback(
                handler,
                {"code": ["oauth-code"], "state": ["test-state"]},
            )

        account_store.create_account.assert_not_called()

    def test_callback_retries_slug_on_personal_account_collision(self) -> None:
        from scripts.ops import dashboard_server
        from scripts.ops.dashboard_server import DashboardHandler

        handler = _make_handler()

        oauth_provider = mock.Mock()
        oauth_provider.exchange_code.return_value = {"login": "alice"}
        oauth_provider.is_authorized.return_value = True
        oauth_provider.create_session_cookie.return_value = "session-cookie"

        user_store = mock.Mock()
        account_store = mock.Mock()
        account_store.get_user_accounts.return_value = []
        account_store.create_account.side_effect = [
            RuntimeError("duplicate key value violates unique constraint"),
            {"id": "acct-2"},
        ]

        with mock.patch.object(dashboard_server, "_oauth_provider", oauth_provider), mock.patch.object(
            dashboard_server, "_get_user_store", return_value=user_store
        ), mock.patch.object(
            dashboard_server, "_get_account_store", return_value=account_store
        ):
            DashboardHandler._handle_auth_callback(
                handler,
                {"code": ["oauth-code"], "state": ["test-state"]},
            )

        assert account_store.create_account.call_count == 2
        first_call = account_store.create_account.call_args_list[0]
        second_call = account_store.create_account.call_args_list[1]
        assert first_call.kwargs["slug"] == "alice"
        assert second_call.kwargs["slug"] == "alice-personal"

    def test_ensure_personal_account_is_idempotent(self) -> None:
        """Second call with existing account should not create a duplicate."""
        from scripts.ops.dashboard_server import _ensure_personal_account

        account_store = mock.Mock()
        account_store.get_user_accounts.return_value = [{"id": "existing-acct"}]

        with mock.patch(
            "scripts.ops.dashboard_server._get_account_store",
            return_value=account_store,
        ):
            _ensure_personal_account("alice")

        account_store.create_account.assert_not_called()

    def test_ensure_personal_account_graceful_when_store_unavailable(self) -> None:
        """If account store is None (DB unavailable), _ensure_personal_account returns silently."""
        from scripts.ops.dashboard_server import _ensure_personal_account

        with mock.patch(
            "scripts.ops.dashboard_server._get_account_store",
            return_value=None,
        ):
            # Should not raise
            _ensure_personal_account("alice")

    def test_list_accounts_returns_empty_when_store_unavailable(self) -> None:
        """GET /api/accounts should return [] (not 503) when AccountStore is None."""
        from scripts.ops import dashboard_server
        from scripts.ops.dashboard_server import DashboardHandler

        handler = mock.MagicMock(spec=DashboardHandler)

        with mock.patch.object(
            dashboard_server, "_get_account_store", return_value=None
        ):
            DashboardHandler._handle_list_accounts(handler, {"login": "alice"})

        handler._json_response.assert_called_once_with([])

    def test_list_accounts_triggers_on_demand_creation_when_empty(self) -> None:
        """GET /api/accounts should auto-create personal account if user has none."""
        from scripts.ops import dashboard_server
        from scripts.ops.dashboard_server import DashboardHandler

        handler = mock.MagicMock(spec=DashboardHandler)
        account_store = mock.Mock()
        # First call returns empty, second call (after auto-create) returns the new account.
        account_store.get_user_accounts.side_effect = [
            [],
            [{"id": "new-acct", "name": "alice's Personal Account"}],
        ]

        with mock.patch.object(
            dashboard_server, "_get_account_store", return_value=account_store
        ), mock.patch.object(
            dashboard_server, "_ensure_personal_account"
        ) as mock_ensure:
            DashboardHandler._handle_list_accounts(handler, {"login": "alice"})

        mock_ensure.assert_called_once_with("alice")
        handler._json_response.assert_called_once_with(
            [{"id": "new-acct", "name": "alice's Personal Account"}]
        )

    def test_list_accounts_handles_on_demand_creation_failure(self) -> None:
        """If on-demand creation fails, GET /api/accounts still returns empty list (not error)."""
        from scripts.ops import dashboard_server
        from scripts.ops.dashboard_server import DashboardHandler

        handler = mock.MagicMock(spec=DashboardHandler)
        account_store = mock.Mock()
        account_store.get_user_accounts.return_value = []

        with mock.patch.object(
            dashboard_server, "_get_account_store", return_value=account_store
        ), mock.patch.object(
            dashboard_server,
            "_ensure_personal_account",
            side_effect=RuntimeError("DB connection refused"),
        ):
            DashboardHandler._handle_list_accounts(handler, {"login": "alice"})

        handler._json_response.assert_called_once_with([])

    def test_list_accounts_skips_creation_when_accounts_exist(self) -> None:
        """GET /api/accounts should NOT try auto-create if user already has accounts."""
        from scripts.ops import dashboard_server
        from scripts.ops.dashboard_server import DashboardHandler

        handler = mock.MagicMock(spec=DashboardHandler)
        account_store = mock.Mock()
        account_store.get_user_accounts.return_value = [{"id": "acct-1"}]

        with mock.patch.object(
            dashboard_server, "_get_account_store", return_value=account_store
        ), mock.patch.object(
            dashboard_server, "_ensure_personal_account"
        ) as mock_ensure:
            DashboardHandler._handle_list_accounts(handler, {"login": "alice"})

        mock_ensure.assert_not_called()
        handler._json_response.assert_called_once_with([{"id": "acct-1"}])
