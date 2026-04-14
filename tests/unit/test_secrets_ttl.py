"""Tests for time-limited (TTL) secrets in UserStore.

Covers:
- Creating secrets with a TTL
- Expired secrets excluded from list
- Purging expired secrets
- Secrets without TTL never expire
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from src.swe_team.webui.user_store import UserStore


@pytest.fixture()
def user_store(tmp_path):
    """Return a UserStore backed by a temporary SQLite database."""
    db = str(tmp_path / "test.db")
    return UserStore(db_path=db, encryption_key=b"test-key-32-bytes-long-padding!!")


class TestSecretsTTL:
    """Tests for the expires_at / TTL feature on account and project secrets."""

    def test_account_secret_with_ttl(self, user_store: UserStore):
        """A secret created with ttl_minutes should have expires_at set."""
        user_store.set_account_secret("acc-1", "SETUP_KEY", "val", ttl_minutes=60)
        entries = user_store.list_account_secret_names("acc-1")
        assert len(entries) == 1
        assert entries[0]["name"] == "SETUP_KEY"
        assert entries[0]["expires_at"] is not None
        # Verify the expiry is roughly 60 minutes in the future
        exp = datetime.strptime(entries[0]["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        now = datetime.now(timezone.utc)
        assert (exp - now).total_seconds() > 3500  # ~59 minutes
        assert (exp - now).total_seconds() < 3700  # ~61 minutes

    def test_project_secret_with_ttl(self, user_store: UserStore):
        """A project secret created with ttl_minutes should have expires_at set."""
        user_store.set_project_secret("proj-1", "OAUTH_SECRET", "val", ttl_minutes=1440)
        entries = user_store.list_project_secret_names("proj-1")
        assert len(entries) == 1
        assert entries[0]["name"] == "OAUTH_SECRET"
        assert entries[0]["expires_at"] is not None

    def test_secret_without_ttl_never_expires(self, user_store: UserStore):
        """A secret created without ttl_minutes should have expires_at=None."""
        user_store.set_account_secret("acc-1", "PERMANENT", "val")
        entries = user_store.list_account_secret_names("acc-1")
        assert len(entries) == 1
        assert entries[0]["name"] == "PERMANENT"
        assert entries[0]["expires_at"] is None

    def test_expired_account_secret_excluded_from_list(self, user_store: UserStore):
        """An expired secret should not appear in list_account_secret_names."""
        # Insert a secret with an already-past expiry directly
        conn = sqlite3.connect(str(user_store._db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        encrypted = user_store._enc.encrypt("secret_val")
        conn.execute(
            "INSERT INTO account_secrets (account_id, name, encrypted_value, created_at, updated_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("acc-1", "EXPIRED_KEY", encrypted, now_str, now_str, past),
        )
        # Also insert a non-expired secret
        conn.execute(
            "INSERT INTO account_secrets (account_id, name, encrypted_value, created_at, updated_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("acc-1", "VALID_KEY", encrypted, now_str, now_str, None),
        )
        conn.commit()
        conn.close()

        entries = user_store.list_account_secret_names("acc-1")
        names = [e["name"] for e in entries]
        assert "EXPIRED_KEY" not in names
        assert "VALID_KEY" in names

    def test_expired_project_secret_excluded_from_list(self, user_store: UserStore):
        """An expired project secret should not appear in list_project_secret_names."""
        conn = sqlite3.connect(str(user_store._db_path))
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        encrypted = user_store._enc.encrypt("secret_val")
        conn.execute(
            "INSERT INTO project_secrets (project_name, name, encrypted_value, created_at, updated_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("proj-1", "OLD_SECRET", encrypted, now_str, now_str, past),
        )
        conn.execute(
            "INSERT INTO project_secrets (project_name, name, encrypted_value, created_at, updated_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("proj-1", "GOOD_SECRET", encrypted, now_str, now_str, None),
        )
        conn.commit()
        conn.close()

        entries = user_store.list_project_secret_names("proj-1")
        names = [e["name"] for e in entries]
        assert "OLD_SECRET" not in names
        assert "GOOD_SECRET" in names

    def test_purge_expired_secrets(self, user_store: UserStore):
        """purge_expired_secrets should delete expired rows and return the count."""
        conn = sqlite3.connect(str(user_store._db_path))
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        encrypted = user_store._enc.encrypt("val")

        # 2 expired account secrets + 1 valid
        conn.execute(
            "INSERT INTO account_secrets (account_id, name, encrypted_value, created_at, updated_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("acc-1", "EXP1", encrypted, now_str, now_str, past),
        )
        conn.execute(
            "INSERT INTO account_secrets (account_id, name, encrypted_value, created_at, updated_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("acc-1", "EXP2", encrypted, now_str, now_str, past),
        )
        conn.execute(
            "INSERT INTO account_secrets (account_id, name, encrypted_value, created_at, updated_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("acc-1", "VALID", encrypted, now_str, now_str, future),
        )

        # 1 expired project secret
        conn.execute(
            "INSERT INTO project_secrets (project_name, name, encrypted_value, created_at, updated_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("proj-1", "PEXP", encrypted, now_str, now_str, past),
        )
        conn.commit()
        conn.close()

        deleted = user_store.purge_expired_secrets()
        assert deleted == 3  # 2 account + 1 project

        # Verify the valid one is still there
        entries = user_store.list_account_secret_names("acc-1")
        names = [e["name"] for e in entries]
        assert "VALID" in names
        assert "EXP1" not in names
        assert "EXP2" not in names

    def test_purge_with_no_expired_secrets(self, user_store: UserStore):
        """purge_expired_secrets returns 0 when nothing is expired."""
        user_store.set_account_secret("acc-1", "FOREVER", "val")
        deleted = user_store.purge_expired_secrets()
        assert deleted == 0

    def test_ttl_zero_or_negative_treated_as_no_expiry(self, user_store: UserStore):
        """ttl_minutes=0 or negative should not set an expiry."""
        user_store.set_account_secret("acc-1", "ZERO_TTL", "val", ttl_minutes=0)
        entries = user_store.list_account_secret_names("acc-1")
        assert entries[0]["expires_at"] is None

        user_store.set_account_secret("acc-1", "NEG_TTL", "val", ttl_minutes=-5)
        entries = user_store.list_account_secret_names("acc-1")
        neg = [e for e in entries if e["name"] == "NEG_TTL"]
        assert neg[0]["expires_at"] is None

    def test_upsert_updates_expiry(self, user_store: UserStore):
        """Re-setting a secret with a new TTL should update the expiry."""
        user_store.set_account_secret("acc-1", "KEY", "val1", ttl_minutes=60)
        entries = user_store.list_account_secret_names("acc-1")
        exp1 = entries[0]["expires_at"]

        # Update with no TTL - should clear expiry
        user_store.set_account_secret("acc-1", "KEY", "val2")
        entries = user_store.list_account_secret_names("acc-1")
        assert entries[0]["expires_at"] is None

        # Update with new TTL
        user_store.set_account_secret("acc-1", "KEY", "val3", ttl_minutes=120)
        entries = user_store.list_account_secret_names("acc-1")
        assert entries[0]["expires_at"] is not None

    def test_list_returns_dict_format(self, user_store: UserStore):
        """list_*_secret_names should return list of dicts with name and expires_at."""
        user_store.set_account_secret("acc-1", "A", "val")
        user_store.set_project_secret("proj-1", "B", "val", ttl_minutes=60)

        acct_entries = user_store.list_account_secret_names("acc-1")
        assert isinstance(acct_entries, list)
        assert isinstance(acct_entries[0], dict)
        assert "name" in acct_entries[0]
        assert "expires_at" in acct_entries[0]

        proj_entries = user_store.list_project_secret_names("proj-1")
        assert isinstance(proj_entries, list)
        assert isinstance(proj_entries[0], dict)
        assert "name" in proj_entries[0]
        assert "expires_at" in proj_entries[0]
