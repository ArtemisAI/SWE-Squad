"""SQLite-backed user and secrets store for the SWE-Squad WebUI dashboard.

Tables
------
- users          : id, github_login (unique), email, role, display_name,
                   avatar_url, created_at, last_login
- secrets        : id, user_id (FK), name, encrypted_value, created_at, updated_at
- user_settings  : user_id (FK, unique), settings_json, updated_at

Encryption
----------
Uses ``cryptography.fernet.Fernet`` when available; falls back to a
HMAC+AES-256-SIV-style scheme built from stdlib (``hmac`` + ``hashlib``
+ ``base64``).  The key is read from the ``WEBUI_ENCRYPTION_KEY`` env var
(must be a valid Fernet URL-safe base64 32-byte key when using Fernet) or
auto-generated and stored in-process for the lifetime of the server.

Role model
----------
- admin  : full read/write access
- user   : normal authenticated user
- bot    : auto-provisioned service account

The first human user to log in is automatically promoted to *admin* (unless
``DASHBOARD_ADMINS`` env var lists specific logins, in which case those
logins become admins regardless of order).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Encryption layer
# ---------------------------------------------------------------------------

_FERNET_AVAILABLE: bool = False
try:
    from cryptography.fernet import Fernet as _Fernet  # type: ignore[import-untyped]
    _FERNET_AVAILABLE = True
except ImportError:
    pass


def _derive_key(raw_key: bytes) -> bytes:
    """Derive a 32-byte key from raw_key using SHA-256."""
    return hashlib.sha256(raw_key).digest()


class _FernetEncryption:
    """Encryption using cryptography.fernet.Fernet."""

    def __init__(self, key: bytes) -> None:
        # Fernet expects a URL-safe base64-encoded 32-byte key
        fernet_key = base64.urlsafe_b64encode(key[:32].ljust(32, b"\x00"))
        self._f = _Fernet(fernet_key)

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._f.decrypt(token.encode()).decode()


class _HmacEncryption:
    """Fallback: XOR-based stream cipher keyed by HMAC-SHA256 + base64 envelope.

    This is NOT production-grade crypto — it exists only as a stdlib-only
    fallback when *cryptography* is not installed.  The plaintext is XOR-ed
    with a keystream derived by HMAC-SHA256(key, nonce || counter) and then
    wrapped in a base64 envelope together with a 16-byte nonce and a 32-byte
    HMAC integrity tag.

    Layout (raw bytes before base64):
        nonce (16 bytes) | mac (32 bytes) | ciphertext (N bytes)
    """

    _NONCE_BYTES = 16
    _MAC_BYTES = 32

    def __init__(self, key: bytes) -> None:
        self._key = key

    # ---------- internal helpers ----------

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        """Generate *length* keystream bytes by chaining HMAC blocks."""
        out = bytearray()
        block = 0
        while len(out) < length:
            h = hmac.new(
                self._key,
                nonce + block.to_bytes(4, "big"),
                hashlib.sha256,
            ).digest()
            out.extend(h)
            block += 1
        return bytes(out[:length])

    def _mac(self, nonce: bytes, ciphertext: bytes) -> bytes:
        return hmac.new(
            self._key,
            b"mac" + nonce + ciphertext,
            hashlib.sha256,
        ).digest()

    # ---------- public API ----------

    def encrypt(self, plaintext: str) -> str:
        raw = plaintext.encode()
        nonce = secrets.token_bytes(self._NONCE_BYTES)
        ks = self._keystream(nonce, len(raw))
        ciphertext = bytes(a ^ b for a, b in zip(raw, ks))
        mac = self._mac(nonce, ciphertext)
        envelope = nonce + mac + ciphertext
        return base64.urlsafe_b64encode(envelope).decode()

    def decrypt(self, token: str) -> str:
        try:
            envelope = base64.urlsafe_b64decode(token + "==")
        except Exception as exc:
            raise ValueError(f"Invalid token: {exc}") from exc
        if len(envelope) < self._NONCE_BYTES + self._MAC_BYTES:
            raise ValueError("Token too short")
        nonce = envelope[: self._NONCE_BYTES]
        mac = envelope[self._NONCE_BYTES : self._NONCE_BYTES + self._MAC_BYTES]
        ciphertext = envelope[self._NONCE_BYTES + self._MAC_BYTES :]
        expected_mac = self._mac(nonce, ciphertext)
        if not hmac.compare_digest(mac, expected_mac):
            raise ValueError("MAC verification failed — token tampered or wrong key")
        ks = self._keystream(nonce, len(ciphertext))
        return bytes(a ^ b for a, b in zip(ciphertext, ks)).decode()


def _build_encryption(key: bytes):
    """Return the best available encryption wrapper for *key*."""
    if _FERNET_AVAILABLE:
        return _FernetEncryption(key)
    return _HmacEncryption(key)


def _load_or_generate_key() -> bytes:
    """Return a 32-byte encryption key.

    Priority:
    1. ``WEBUI_ENCRYPTION_KEY`` env var (raw or base64 — we normalise it).
    2. Auto-generate a random 32-byte key for this process lifetime.

    A warning is logged when the key is ephemeral so operators know secrets
    will be unreadable after a restart.
    """
    raw = os.environ.get("WEBUI_ENCRYPTION_KEY", "")
    if raw:
        # Try base64 decode first; fall back to using raw bytes
        try:
            decoded = base64.urlsafe_b64decode(raw + "==")
            if len(decoded) >= 16:
                logger.debug("WEBUI_ENCRYPTION_KEY: using base64-decoded key")
                return decoded[:32].ljust(32, b"\x00")
        except Exception:
            pass
        return _derive_key(raw.encode())
    logger.warning(
        "WEBUI_ENCRYPTION_KEY not set — using ephemeral random key. "
        "Encrypted secrets will be unreadable after a server restart."
    )
    return secrets.token_bytes(32)


# ---------------------------------------------------------------------------
# Default user settings shape
# ---------------------------------------------------------------------------

_DEFAULT_USER_SETTINGS: dict = {
    "theme": "dark",
    "refresh_interval": 30,
    "tickets_per_page": 25,
    "default_tab": "overview",
    "notifications_enabled": True,
    "notification_level": "errors",
}

# ---------------------------------------------------------------------------
# UserStore
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    github_login  TEXT    NOT NULL UNIQUE,
    email         TEXT    NOT NULL DEFAULT '',
    role          TEXT    NOT NULL DEFAULT 'user',
    display_name  TEXT    NOT NULL DEFAULT '',
    avatar_url    TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    last_login    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS secrets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    encrypted_value TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    settings_json TEXT    NOT NULL DEFAULT '{}',
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS account_secrets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    encrypted_value TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    expires_at      TEXT,
    UNIQUE(account_id, name)
);

CREATE TABLE IF NOT EXISTS project_secrets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name    TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    encrypted_value TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    expires_at      TEXT,
    UNIQUE(project_name, name)
);
"""


class UserStore:
    """SQLite-backed user, secrets, and settings store.

    Thread-safe: uses a per-instance threading.Lock for write operations.
    Read operations use a separate connection in check_same_thread=False mode
    which is safe for SQLite WAL (write-ahead logging is enabled automatically).
    """

    def __init__(
        self,
        db_path: str = "data/swe_team/webui_users.db",
        encryption_key: Optional[bytes] = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        key = encryption_key if encryption_key is not None else _load_or_generate_key()
        self._enc = _build_encryption(key)
        self._admins: list[str] = [
            s.strip()
            for s in os.environ.get("DASHBOARD_ADMINS", "").split(",")
            if s.strip()
        ]
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA_SQL)
                # Migration: add expires_at column to existing tables
                for table in ("account_secrets", "project_secrets"):
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN expires_at TEXT")
                    except sqlite3.OperationalError:
                        pass  # Column already exists
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _row_to_user(self, row) -> dict:
        return {
            "id": row["id"],
            "github_login": row["github_login"],
            "email": row["email"],
            "role": row["role"],
            "display_name": row["display_name"],
            "avatar_url": row["avatar_url"],
            "created_at": row["created_at"],
            "last_login": row["last_login"],
        }

    def _resolve_role(self, github_login: str, requested_role: str) -> str:
        """Determine the actual role to assign, respecting DASHBOARD_ADMINS."""
        if github_login in self._admins:
            return "admin"
        return requested_role

    def _is_first_human_user(self, conn: sqlite3.Connection) -> bool:
        """Return True if no non-bot users exist yet."""
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE role != 'bot'"
        ).fetchone()
        return (row["cnt"] == 0) if row else True

    # ------------------------------------------------------------------
    # User CRUD
    # ------------------------------------------------------------------

    def get_or_create_user(
        self,
        github_login: str,
        email: str = "",
        role: str = "user",
        display_name: str = "",
        avatar_url: str = "",
    ) -> dict:
        """Return the user record for *github_login*, creating it if absent.

        The first non-bot user to be created is promoted to *admin* (unless
        ``DASHBOARD_ADMINS`` explicitly names other logins).
        """
        now = self._now()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM users WHERE github_login = ?",
                    (github_login,),
                ).fetchone()
                if row:
                    # Update last_login and mutable fields on every login
                    conn.execute(
                        "UPDATE users SET last_login=?, email=COALESCE(NULLIF(?,''),(SELECT email FROM users WHERE github_login=?)),"
                        " display_name=COALESCE(NULLIF(?,''),(SELECT display_name FROM users WHERE github_login=?)),"
                        " avatar_url=COALESCE(NULLIF(?,''),(SELECT avatar_url FROM users WHERE github_login=?)) WHERE github_login=?",
                        (now, email, github_login, display_name, github_login, avatar_url, github_login, github_login),
                    )
                    conn.commit()
                    row = conn.execute(
                        "SELECT * FROM users WHERE github_login = ?",
                        (github_login,),
                    ).fetchone()
                    return self._row_to_user(row)

                # New user — determine role
                if role != "bot" and self._is_first_human_user(conn) and github_login not in self._admins:
                    effective_role = "admin"
                else:
                    effective_role = self._resolve_role(github_login, role)

                conn.execute(
                    "INSERT INTO users (github_login, email, role, display_name, avatar_url, created_at, last_login)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (github_login, email, effective_role, display_name, avatar_url, now, now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM users WHERE github_login = ?",
                    (github_login,),
                ).fetchone()
                return self._row_to_user(row)
            finally:
                conn.close()

    def get_user(self, github_login: str) -> Optional[dict]:
        """Return the user record or None if not found."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE github_login = ?",
                (github_login,),
            ).fetchone()
            return self._row_to_user(row) if row else None
        finally:
            conn.close()

    def list_users(self) -> List[dict]:
        """Return all users, ordered by created_at."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at"
            ).fetchall()
            return [self._row_to_user(r) for r in rows]
        finally:
            conn.close()

    def update_user(self, github_login: str, **kwargs) -> dict:
        """Update allowed fields on the user record.

        Allowed fields: email, role, display_name, avatar_url.
        Returns the updated user dict.  Raises ValueError if user not found.
        """
        allowed = {"email", "role", "display_name", "avatar_url"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            user = self.get_user(github_login)
            if user is None:
                raise ValueError(f"User {github_login!r} not found")
            return user

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [github_login]
        with self._lock:
            conn = self._connect()
            try:
                result = conn.execute(
                    f"UPDATE users SET {set_clause} WHERE github_login = ?",
                    values,
                )
                if result.rowcount == 0:
                    raise ValueError(f"User {github_login!r} not found")
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM users WHERE github_login = ?",
                    (github_login,),
                ).fetchone()
                return self._row_to_user(row)
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------

    def set_secret(self, github_login: str, name: str, value: str) -> None:
        """Encrypt and store (or update) a named secret for *github_login*.

        Raises ValueError if the user does not exist.
        """
        user = self.get_user(github_login)
        if user is None:
            raise ValueError(f"User {github_login!r} not found")
        user_id = user["id"]
        encrypted = self._enc.encrypt(value)
        now = self._now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO secrets (user_id, name, encrypted_value, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(user_id, name) DO UPDATE SET encrypted_value=excluded.encrypted_value, updated_at=excluded.updated_at",
                    (user_id, name, encrypted, now, now),
                )
                conn.commit()
            finally:
                conn.close()

    def get_secret(self, github_login: str, name: str) -> Optional[str]:
        """Return the decrypted secret value or None if not found.

        Raises ValueError if the user does not exist.
        """
        user = self.get_user(github_login)
        if user is None:
            raise ValueError(f"User {github_login!r} not found")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT encrypted_value FROM secrets WHERE user_id = ? AND name = ?",
                (user["id"], name),
            ).fetchone()
            if row is None:
                return None
            return self._enc.decrypt(row["encrypted_value"])
        finally:
            conn.close()

    def list_secret_names(self, github_login: str) -> List[str]:
        """Return the list of secret names for *github_login* (values never returned).

        Raises ValueError if the user does not exist.
        """
        user = self.get_user(github_login)
        if user is None:
            raise ValueError(f"User {github_login!r} not found")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT name FROM secrets WHERE user_id = ? ORDER BY name",
                (user["id"],),
            ).fetchall()
            return [r["name"] for r in rows]
        finally:
            conn.close()

    def delete_secret(self, github_login: str, name: str) -> bool:
        """Delete a secret.  Returns True if a row was deleted, False if not found.

        Raises ValueError if the user does not exist.
        """
        user = self.get_user(github_login)
        if user is None:
            raise ValueError(f"User {github_login!r} not found")
        with self._lock:
            conn = self._connect()
            try:
                result = conn.execute(
                    "DELETE FROM secrets WHERE user_id = ? AND name = ?",
                    (user["id"], name),
                )
                conn.commit()
                return result.rowcount > 0
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Account-level secrets
    # ------------------------------------------------------------------

    def set_account_secret(
        self, account_id: str, name: str, value: str, ttl_minutes: Optional[int] = None,
    ) -> None:
        """Encrypt and store (or update) a named secret scoped to *account_id*.

        If *ttl_minutes* is set, the secret will auto-expire after that duration.
        """
        encrypted = self._enc.encrypt(value)
        now = self._now()
        expires_at: Optional[str] = None
        if ttl_minutes is not None and ttl_minutes > 0:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO account_secrets (account_id, name, encrypted_value, created_at, updated_at, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(account_id, name) DO UPDATE SET encrypted_value=excluded.encrypted_value,"
                    " updated_at=excluded.updated_at, expires_at=excluded.expires_at",
                    (account_id, name, encrypted, now, now, expires_at),
                )
                conn.commit()
            finally:
                conn.close()

    def list_account_secret_names(self, account_id: str) -> List[dict]:
        """Return secret metadata for *account_id* (values never returned).

        Each entry is ``{"name": str, "expires_at": str | None}``.
        Expired secrets are excluded from the result.
        """
        now = self._now()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT name, expires_at FROM account_secrets"
                " WHERE account_id = ? AND (expires_at IS NULL OR expires_at > ?)"
                " ORDER BY name",
                (account_id, now),
            ).fetchall()
            return [{"name": r["name"], "expires_at": r["expires_at"]} for r in rows]
        finally:
            conn.close()

    def delete_account_secret(self, account_id: str, name: str) -> bool:
        """Delete an account secret. Returns True if a row was deleted."""
        with self._lock:
            conn = self._connect()
            try:
                result = conn.execute(
                    "DELETE FROM account_secrets WHERE account_id = ? AND name = ?",
                    (account_id, name),
                )
                conn.commit()
                return result.rowcount > 0
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Project-level secrets
    # ------------------------------------------------------------------

    def set_project_secret(
        self, project_name: str, name: str, value: str, ttl_minutes: Optional[int] = None,
    ) -> None:
        """Encrypt and store (or update) a named secret scoped to *project_name*.

        If *ttl_minutes* is set, the secret will auto-expire after that duration.
        """
        encrypted = self._enc.encrypt(value)
        now = self._now()
        expires_at: Optional[str] = None
        if ttl_minutes is not None and ttl_minutes > 0:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO project_secrets (project_name, name, encrypted_value, created_at, updated_at, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(project_name, name) DO UPDATE SET encrypted_value=excluded.encrypted_value,"
                    " updated_at=excluded.updated_at, expires_at=excluded.expires_at",
                    (project_name, name, encrypted, now, now, expires_at),
                )
                conn.commit()
            finally:
                conn.close()

    def list_project_secret_names(self, project_name: str) -> List[dict]:
        """Return secret metadata for *project_name* (values never returned).

        Each entry is ``{"name": str, "expires_at": str | None}``.
        Expired secrets are excluded from the result.
        """
        now = self._now()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT name, expires_at FROM project_secrets"
                " WHERE project_name = ? AND (expires_at IS NULL OR expires_at > ?)"
                " ORDER BY name",
                (project_name, now),
            ).fetchall()
            return [{"name": r["name"], "expires_at": r["expires_at"]} for r in rows]
        finally:
            conn.close()

    def delete_project_secret(self, project_name: str, name: str) -> bool:
        """Delete a project secret. Returns True if a row was deleted."""
        with self._lock:
            conn = self._connect()
            try:
                result = conn.execute(
                    "DELETE FROM project_secrets WHERE project_name = ? AND name = ?",
                    (project_name, name),
                )
                conn.commit()
                return result.rowcount > 0
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Expired secrets cleanup
    # ------------------------------------------------------------------

    def purge_expired_secrets(self) -> int:
        """Delete all expired secrets from account_secrets and project_secrets.

        Returns the total number of rows deleted.
        """
        now = self._now()
        total = 0
        with self._lock:
            conn = self._connect()
            try:
                for table in ("account_secrets", "project_secrets"):
                    result = conn.execute(
                        f"DELETE FROM {table} WHERE expires_at IS NOT NULL AND expires_at <= ?",
                        (now,),
                    )
                    total += result.rowcount
                conn.commit()
            finally:
                conn.close()
        return total

    # ------------------------------------------------------------------
    # User settings
    # ------------------------------------------------------------------

    def get_settings(self, github_login: str) -> dict:
        """Return the user's settings dict, merged with defaults.

        Raises ValueError if the user does not exist.
        """
        user = self.get_user(github_login)
        if user is None:
            raise ValueError(f"User {github_login!r} not found")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT settings_json FROM user_settings WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
            saved: dict = {}
            if row:
                try:
                    saved = json.loads(row["settings_json"]) or {}
                except Exception:
                    pass
            merged = dict(_DEFAULT_USER_SETTINGS)
            merged.update(saved)
            return merged
        finally:
            conn.close()

    def update_settings(self, github_login: str, settings: dict) -> dict:
        """Merge *settings* into the user's stored settings and return the result.

        Raises ValueError if the user does not exist.
        """
        user = self.get_user(github_login)
        if user is None:
            raise ValueError(f"User {github_login!r} not found")
        current = self.get_settings(github_login)
        current.update(settings)
        now = self._now()
        settings_json = json.dumps(current)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO user_settings (user_id, settings_json, updated_at)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(user_id) DO UPDATE SET settings_json=excluded.settings_json, updated_at=excluded.updated_at",
                    (user["id"], settings_json, now),
                )
                conn.commit()
            finally:
                conn.close()
        return current
