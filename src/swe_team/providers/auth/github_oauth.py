"""GitHub OAuth provider for SWE-Squad dashboard authentication.

Uses only stdlib: urllib, hmac, hashlib, base64, json, http.cookies.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_USER_URL = "https://api.github.com/user"
_GITHUB_ORGS_URL = "https://api.github.com/user/orgs"


class GitHubOAuthProvider:
    """GitHub OAuth 2.0 provider with HMAC-signed session cookies.

    Cookie format:
        base64url(json({"login": ..., "orgs": [...], "exp": unix_ts}))
        + "."
        + hex(hmac_sha256(cookie_secret, payload_part))
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        allowed_orgs: list[str],
        cookie_secret: str,
        session_expiry_hours: int = 24,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._allowed_orgs = [o.strip() for o in allowed_orgs if o.strip()]
        self._cookie_secret = cookie_secret.encode() if isinstance(cookie_secret, str) else cookie_secret
        self._session_expiry_seconds = session_expiry_hours * 3600

    # ------------------------------------------------------------------
    # Public OAuth flow helpers
    # ------------------------------------------------------------------

    def get_authorize_url(self, state: str) -> str:
        """Return the GitHub OAuth authorize URL with the given state token."""
        params = urllib.parse.urlencode({
            "client_id": self._client_id,
            "scope": "read:user read:org repo",
            "state": state,
        })
        return f"{_GITHUB_AUTHORIZE_URL}?{params}"

    def exchange_code(self, code: str) -> dict:
        """Exchange an OAuth code for a token, then fetch user info and orgs.

        Returns a dict with keys: login, name, email, avatar_url, orgs (list).
        Raises RuntimeError on failure.
        """
        token = self._fetch_access_token(code)
        user_info = self._fetch_user(token)
        orgs = self._fetch_orgs(token)
        user_info["orgs"] = orgs
        user_info["access_token"] = token
        return user_info

    # ------------------------------------------------------------------
    # Session cookie helpers
    # ------------------------------------------------------------------

    def create_session_cookie(self, user_info: dict) -> str:
        """Create an HMAC-signed session cookie string.

        Format: ``<b64_payload>.<hex_signature>``
        """
        payload_data = {
            "login": user_info.get("login", ""),
            "name": user_info.get("name", ""),
            "orgs": user_info.get("orgs", []),
            "exp": int(time.time()) + self._session_expiry_seconds,
        }
        payload_json = json.dumps(payload_data, separators=(",", ":"), sort_keys=True)
        payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode()
        sig = self._sign(payload_b64)
        return f"{payload_b64}.{sig}"

    def validate_session(self, cookie_value: str) -> Optional[dict]:
        """Validate a signed session cookie.

        Returns the user dict (login, name, orgs) on success, or None if the
        cookie is missing, tampered with, or expired.
        """
        if not cookie_value or "." not in cookie_value:
            return None
        parts = cookie_value.rsplit(".", 1)
        if len(parts) != 2:
            return None
        payload_b64, sig = parts

        # Constant-time signature comparison to prevent timing attacks
        expected_sig = self._sign(payload_b64)
        if not hmac.compare_digest(sig, expected_sig):
            return None

        try:
            payload_json = base64.urlsafe_b64decode(payload_b64 + "==").decode()
            data = json.loads(payload_json)
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        # Check expiry
        exp = data.get("exp", 0)
        if time.time() > exp:
            return None

        return {
            "login": data.get("login", ""),
            "name": data.get("name", ""),
            "orgs": data.get("orgs", []),
        }

    def is_authorized(self, user_info: dict) -> bool:
        """Return True if the user is allowed to access the dashboard.

        Authorization passes if ANY of these conditions are met:
        1. ``allowed_orgs`` is empty — all authenticated users are allowed
        2. User's login matches an entry in ``allowed_orgs`` (covers org owners
           and personal accounts that don't show up in /user/orgs)
        3. User belongs to at least one of the allowed orgs

        This ensures the org owner, org members, and explicitly-allowed
        individual accounts can all log in.
        """
        if not self._allowed_orgs:
            return True
        allowed = set(self._allowed_orgs)
        # Check if the user's own login is in the allowed list
        if user_info.get("login", "") in allowed:
            return True
        # Check org memberships
        user_orgs = set(user_info.get("orgs", []))
        return bool(user_orgs & allowed)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _sign(self, payload: str) -> str:
        """Return the hex HMAC-SHA256 signature of *payload*."""
        return hmac.new(
            self._cookie_secret,
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _fetch_access_token(self, code: str) -> str:
        """Exchange the OAuth code for a GitHub access token."""
        params = urllib.parse.urlencode({
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
        }).encode()
        req = urllib.request.Request(
            _GITHUB_TOKEN_URL,
            data=params,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode())
        except Exception as exc:
            raise RuntimeError(f"GitHub token exchange failed: {exc}") from exc

        if "error" in body:
            raise RuntimeError(f"GitHub OAuth error: {body.get('error_description', body['error'])}")

        token = body.get("access_token", "")
        if not token:
            raise RuntimeError("GitHub OAuth: no access_token in response")
        return token

    def _fetch_user(self, token: str) -> dict:
        """Fetch the authenticated user's profile from GitHub."""
        req = urllib.request.Request(
            _GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "SWE-Squad-Dashboard/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            raise RuntimeError(f"GitHub user fetch failed: {exc}") from exc
        return {
            "login": data.get("login", ""),
            "name": data.get("name") or data.get("login", ""),
            "email": data.get("email", ""),
            "avatar_url": data.get("avatar_url", ""),
        }

    def _fetch_orgs(self, token: str) -> list[str]:
        """Return a list of org logins the user belongs to."""
        req = urllib.request.Request(
            _GITHUB_ORGS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "SWE-Squad-Dashboard/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            # GitHub /user/orgs can fail if the org has not granted OAuth app access.
            # Log a warning so it is visible in the server log, then fall through.
            # is_authorized() will still return True when allowed_orgs is empty.
            logger.warning("GitHub /user/orgs fetch failed (org may have restricted OAuth access): %s", exc)
            return []
        if not isinstance(data, list):
            logger.warning("GitHub /user/orgs returned unexpected type: %r", type(data))
            return []
        return [org.get("login", "") for org in data if org.get("login")]
