"""Unit tests for AccountStore account listing fallback behavior."""

from __future__ import annotations

import io
import urllib.error

from src.swe_team.webui.account_store import AccountStore


def _http_error(
    status: int = 400,
    payload: bytes = b'{"code":"PGRST200","message":"schema cache stale"}',
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://example.test/rest/v1/account_members",
        code=status,
        msg="Bad Request",
        hdrs=None,
        fp=io.BytesIO(payload),
    )


def test_get_user_accounts_uses_embedded_accounts() -> None:
    store = AccountStore("http://example.test", "key")
    calls: list[str] = []

    def fake_request(path: str, method: str = "GET", body: dict | None = None):  # noqa: ARG001
        calls.append(path)
        if path == "account_members?github_login=eq.alice&select=*,accounts(*)":
            return [
                {
                    "account_id": "acct-1",
                    "role": "owner",
                    "accounts": {"id": "acct-1", "name": "your-org", "slug": "your-org"},
                }
            ]
        raise AssertionError(f"Unexpected path: {path}")

    store._request = fake_request  # type: ignore[method-assign]

    result = store.get_user_accounts("alice")

    assert result == [
        {"id": "acct-1", "role": "owner", "name": "your-org", "slug": "your-org"}
    ]
    assert calls == ["account_members?github_login=eq.alice&select=*,accounts(*)"]


def test_get_user_accounts_falls_back_when_embedded_query_fails() -> None:
    store = AccountStore("http://example.test", "key")
    calls: list[str] = []

    def fake_request(path: str, method: str = "GET", body: dict | None = None):  # noqa: ARG001
        calls.append(path)
        if path == "account_members?github_login=eq.alice&select=*,accounts(*)":
            raise _http_error()
        if path == "account_members?github_login=eq.alice":
            return [{"account_id": "acct-1", "role": "owner"}]
        if path == "accounts?id=in.(acct-1)":
            return [{"id": "acct-1", "name": "your-org", "slug": "your-org"}]
        raise AssertionError(f"Unexpected path: {path}")

    store._request = fake_request  # type: ignore[method-assign]

    result = store.get_user_accounts("alice")

    assert result == [
        {"id": "acct-1", "role": "owner", "name": "your-org", "slug": "your-org"}
    ]
    assert calls == [
        "account_members?github_login=eq.alice&select=*,accounts(*)",
        "account_members?github_login=eq.alice",
        "accounts?id=in.(acct-1)",
    ]


def test_get_user_accounts_re_raises_non_schema_errors() -> None:
    store = AccountStore("http://example.test", "key")

    def fake_request(path: str, method: str = "GET", body: dict | None = None):  # noqa: ARG001
        if path == "account_members?github_login=eq.alice&select=*,accounts(*)":
            raise _http_error(
                payload=b'{"code":"PGRST301","message":"permission denied"}'
            )
        raise AssertionError(f"Unexpected path: {path}")

    store._request = fake_request  # type: ignore[method-assign]

    try:
        store.get_user_accounts("alice")
        raise AssertionError("Expected HTTPError to be re-raised")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
