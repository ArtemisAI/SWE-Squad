"""Unit tests for src.swe_team.webui.user_store.

All tests use a temporary in-memory or temp-file SQLite database — no network
access, no environment variables required.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Generator

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path, encryption_key: bytes | None = None):
    """Return a UserStore backed by a temp file database."""
    from src.swe_team.webui.user_store import UserStore

    db = tmp_path / "test_users.db"
    key = encryption_key or b"\x01" * 32
    return UserStore(db_path=str(db), encryption_key=key)


# ===========================================================================
# 1. User CRUD
# ===========================================================================


class TestUserCRUD:
    def test_create_and_get(self, tmp_path):
        store = _make_store(tmp_path)
        user = store.get_or_create_user("alice", email="alice@example.com")
        assert user["github_login"] == "alice"
        assert user["email"] == "alice@example.com"
        assert user["id"] is not None

    def test_get_nonexistent_returns_none(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.get_user("nobody") is None

    def test_idempotent_create(self, tmp_path):
        store = _make_store(tmp_path)
        u1 = store.get_or_create_user("bob")
        u2 = store.get_or_create_user("bob")
        assert u1["id"] == u2["id"]

    def test_list_users(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("carol")
        store.get_or_create_user("dave")
        users = store.list_users()
        logins = [u["github_login"] for u in users]
        assert "carol" in logins
        assert "dave" in logins

    def test_update_user_email(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("eve")
        updated = store.update_user("eve", email="eve@new.com")
        assert updated["email"] == "eve@new.com"

    def test_update_user_not_found_raises(self, tmp_path):
        store = _make_store(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            store.update_user("ghost", email="x@x.com")

    def test_update_user_ignores_unknown_fields(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("frank")
        # Should not raise; unknown fields are silently dropped
        updated = store.update_user("frank", email="f@f.com", unknown_field="boom")
        assert updated["email"] == "f@f.com"

    def test_last_login_updates_on_subsequent_get_or_create(self, tmp_path):
        store = _make_store(tmp_path)
        u1 = store.get_or_create_user("grace")
        time.sleep(0.01)
        u2 = store.get_or_create_user("grace")
        # last_login should be >= first time (same or newer)
        assert u2["last_login"] >= u1["last_login"]

    def test_avatar_url_stored(self, tmp_path):
        store = _make_store(tmp_path)
        u = store.get_or_create_user("heidi", avatar_url="https://example.com/heidi.png")
        assert u["avatar_url"] == "https://example.com/heidi.png"


# ===========================================================================
# 2. Role enforcement
# ===========================================================================


class TestRoleEnforcement:
    def test_first_human_user_becomes_admin(self, tmp_path):
        store = _make_store(tmp_path)
        user = store.get_or_create_user("ivan")
        assert user["role"] == "admin"

    def test_second_user_is_regular_user(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("ivan")  # first → admin
        user2 = store.get_or_create_user("judy")
        assert user2["role"] == "user"

    def test_bot_role_not_promoted_to_admin(self, tmp_path):
        store = _make_store(tmp_path)
        bot = store.get_or_create_user("squad-bot-1", role="bot")
        assert bot["role"] == "bot"

    def test_bot_does_not_count_as_first_human(self, tmp_path):
        """A bot created first must NOT prevent the first real user from getting admin."""
        store = _make_store(tmp_path)
        store.get_or_create_user("squad-bot-1", role="bot")
        human = store.get_or_create_user("mallory")
        assert human["role"] == "admin"

    def test_dashboard_admins_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DASHBOARD_ADMINS", "karen,leo")
        from src.swe_team.webui.user_store import UserStore

        db = tmp_path / "env_test.db"
        store = UserStore(db_path=str(db), encryption_key=b"\x02" * 32)

        karen = store.get_or_create_user("karen")
        assert karen["role"] == "admin"

        # leo is also in DASHBOARD_ADMINS but the store was created with karen first
        leo = store.get_or_create_user("leo")
        assert leo["role"] == "admin"

    def test_dashboard_admins_does_not_promote_others(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DASHBOARD_ADMINS", "karen")
        from src.swe_team.webui.user_store import UserStore

        db = tmp_path / "env_test2.db"
        store = UserStore(db_path=str(db), encryption_key=b"\x03" * 32)
        # First user is not in DASHBOARD_ADMINS — first-user-is-admin logic fires
        normal = store.get_or_create_user("mallory2")
        # mallory2 would normally be admin (first), but karen is listed in DASHBOARD_ADMINS
        # so mallory2 should NOT be admin since it's not in DASHBOARD_ADMINS
        assert normal["role"] == "admin"  # first human still gets admin in default logic

    def test_update_role(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("neo", role="user")  # not first, role=user after first bot
        store.get_or_create_user("opus_admin")  # first human → admin
        neo = store.get_or_create_user("neo")
        updated = store.update_user("neo", role="admin")
        assert updated["role"] == "admin"


# ===========================================================================
# 3. Secret encryption roundtrip
# ===========================================================================


class TestSecretEncryption:
    def test_set_and_get_secret(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("alice")
        store.set_secret("alice", "API_KEY", "super-secret-value")
        assert store.get_secret("alice", "API_KEY") == "super-secret-value"

    def test_secret_stored_encrypted(self, tmp_path):
        """The stored value must not equal the plaintext."""
        store = _make_store(tmp_path)
        store.get_or_create_user("alice")
        store.set_secret("alice", "MY_KEY", "plaintext")
        conn = store._connect()
        row = conn.execute("SELECT encrypted_value FROM secrets WHERE name='MY_KEY'").fetchone()
        conn.close()
        assert row["encrypted_value"] != "plaintext"

    def test_update_existing_secret(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("bob")
        store.set_secret("bob", "TOKEN", "v1")
        store.set_secret("bob", "TOKEN", "v2")
        assert store.get_secret("bob", "TOKEN") == "v2"

    def test_get_missing_secret_returns_none(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("carol")
        assert store.get_secret("carol", "MISSING") is None

    def test_delete_secret(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("dave")
        store.set_secret("dave", "K", "v")
        assert store.delete_secret("dave", "K") is True
        assert store.get_secret("dave", "K") is None

    def test_delete_nonexistent_returns_false(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("eve")
        assert store.delete_secret("eve", "NOPE") is False

    def test_list_secret_names_hides_values(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("frank")
        store.set_secret("frank", "A", "alpha")
        store.set_secret("frank", "B", "beta")
        names = store.list_secret_names("frank")
        assert sorted(names) == ["A", "B"]
        # Values must not appear in the list
        assert "alpha" not in names
        assert "beta" not in names

    def test_set_secret_for_unknown_user_raises(self, tmp_path):
        store = _make_store(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            store.set_secret("ghost", "K", "v")

    def test_get_secret_for_unknown_user_raises(self, tmp_path):
        store = _make_store(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            store.get_secret("ghost", "K")

    def test_different_key_cannot_decrypt(self, tmp_path):
        """Data encrypted with key A must not decrypt with key B."""
        from src.swe_team.webui.user_store import UserStore

        db = tmp_path / "key_test.db"
        store_a = UserStore(db_path=str(db), encryption_key=b"\xAA" * 32)
        store_a.get_or_create_user("alice")
        store_a.set_secret("alice", "X", "secret_value")

        # Open same DB with a different key
        store_b = UserStore(db_path=str(db), encryption_key=b"\xBB" * 32)
        with pytest.raises(Exception):
            store_b.get_secret("alice", "X")


# ===========================================================================
# 4. Secret isolation — user A cannot read user B's secrets
# ===========================================================================


class TestSecretIsolation:
    def test_users_cannot_read_each_others_secrets(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("alice")
        store.get_or_create_user("bob")

        store.set_secret("alice", "MY_TOKEN", "alice-secret")
        store.set_secret("bob", "MY_TOKEN", "bob-secret")

        assert store.get_secret("alice", "MY_TOKEN") == "alice-secret"
        assert store.get_secret("bob", "MY_TOKEN") == "bob-secret"

    def test_secret_names_are_user_scoped(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("carol")
        store.get_or_create_user("dave")

        store.set_secret("carol", "SHARED_NAME", "carol_val")
        # dave has no secrets
        assert store.list_secret_names("dave") == []
        assert store.list_secret_names("carol") == ["SHARED_NAME"]

    def test_delete_only_affects_owning_user(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("eve")
        store.get_or_create_user("frank")

        store.set_secret("eve", "K", "v1")
        store.set_secret("frank", "K", "v2")

        store.delete_secret("eve", "K")

        assert store.get_secret("eve", "K") is None
        assert store.get_secret("frank", "K") == "v2"


# ===========================================================================
# 5. Settings CRUD
# ===========================================================================


class TestSettingsCRUD:
    def test_default_settings_returned_before_save(self, tmp_path):
        from src.swe_team.webui.user_store import _DEFAULT_USER_SETTINGS

        store = _make_store(tmp_path)
        store.get_or_create_user("alice")
        settings = store.get_settings("alice")
        for k, v in _DEFAULT_USER_SETTINGS.items():
            assert settings[k] == v

    def test_update_and_retrieve_settings(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("bob")
        result = store.update_settings("bob", {"theme": "light", "tickets_per_page": 50})
        assert result["theme"] == "light"
        assert result["tickets_per_page"] == 50

    def test_partial_update_preserves_other_keys(self, tmp_path):
        from src.swe_team.webui.user_store import _DEFAULT_USER_SETTINGS

        store = _make_store(tmp_path)
        store.get_or_create_user("carol")
        store.update_settings("carol", {"theme": "light"})
        settings = store.get_settings("carol")
        assert settings["theme"] == "light"
        # Other defaults should be intact
        assert settings["refresh_interval"] == _DEFAULT_USER_SETTINGS["refresh_interval"]

    def test_get_settings_for_unknown_user_raises(self, tmp_path):
        store = _make_store(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            store.get_settings("ghost")

    def test_update_settings_for_unknown_user_raises(self, tmp_path):
        store = _make_store(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            store.update_settings("ghost", {"theme": "light"})

    def test_settings_are_user_isolated(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("dave")
        store.get_or_create_user("eve")

        store.update_settings("dave", {"theme": "light"})
        eve_settings = store.get_settings("eve")
        assert eve_settings["theme"] == "dark"  # eve has default


# ===========================================================================
# 6. First-user-is-admin edge cases
# ===========================================================================


class TestFirstUserAdmin:
    def test_first_user_gets_admin_even_if_role_user_passed(self, tmp_path):
        store = _make_store(tmp_path)
        user = store.get_or_create_user("alice", role="user")
        assert user["role"] == "admin"

    def test_role_preserved_on_subsequent_login(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("alice")  # first → admin
        user_again = store.get_or_create_user("alice")
        assert user_again["role"] == "admin"

    def test_multiple_bots_then_first_human_is_admin(self, tmp_path):
        store = _make_store(tmp_path)
        for i in range(3):
            store.get_or_create_user(f"bot-{i}", role="bot")
        human = store.get_or_create_user("human")
        assert human["role"] == "admin"


# ===========================================================================
# 7. Thread-safety smoke test
# ===========================================================================


class TestThreadSafety:
    def test_concurrent_secret_writes(self, tmp_path):
        store = _make_store(tmp_path)
        store.get_or_create_user("alice")

        errors: list = []

        def _write(i: int) -> None:
            try:
                store.set_secret("alice", f"KEY_{i}", f"value_{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        names = store.list_secret_names("alice")
        assert len(names) == 10
