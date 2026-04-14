"""Account store — multi-tenant account management via Supabase."""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)


class AccountStore:
    """Supabase PostgREST-backed account management.

    Provides multi-tenant account isolation: users belong to one or more
    accounts, and data (tickets, secrets) is scoped per account.

    Uses stdlib urllib only — no extra dependencies required.
    """

    def __init__(self, supabase_url: str = "", supabase_key: str = "") -> None:
        self._url = (supabase_url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self._key = supabase_key or os.environ.get(
            "SUPABASE_ANON_KEY",
            os.environ.get("SUPABASE_KEY", ""),
        )

    def _request(
        self,
        path: str,
        method: str = "GET",
        body: Optional[dict] = None,
    ):
        """Make a Supabase PostgREST request.

        Returns the parsed JSON response (list or dict).
        Raises urllib.error.HTTPError on non-2xx responses.
        """
        url = f"{self._url}/rest/v1/{path}"
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    # ------------------------------------------------------------------
    # Account CRUD
    # ------------------------------------------------------------------

    def create_account(
        self,
        name: str,
        slug: str,
        created_by: str,
        description: str = "",
    ) -> dict:
        """Create a new account and add the creator as owner.

        Parameters
        ----------
        name:
            Human-readable account name.
        slug:
            URL-friendly unique identifier (e.g. ``my-org``).
        created_by:
            GitHub login of the founding user.
        description:
            Optional free-text description.

        Returns
        -------
        dict
            The newly created account record.
        """
        account = self._request(
            "accounts",
            "POST",
            {
                "name": name,
                "slug": slug,
                "description": description,
                "created_by": created_by,
            },
        )
        if isinstance(account, list):
            account = account[0]
        # Add creator as owner member
        self._request(
            "account_members",
            "POST",
            {
                "account_id": account["id"],
                "github_login": created_by,
                "role": "owner",
            },
        )
        return account

    def get_account(self, account_id: str) -> Optional[dict]:
        """Get account by ID.

        Returns the account dict or None if not found.
        """
        result = self._request(f"accounts?id=eq.{account_id}")
        return result[0] if result else None

    def get_account_by_slug(self, slug: str) -> Optional[dict]:
        """Get account by slug.

        Returns the account dict or None if not found.
        """
        result = self._request(f"accounts?slug=eq.{slug}")
        return result[0] if result else None

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def get_user_accounts(self, github_login: str) -> List[dict]:
        """Get all accounts a user belongs to, with their role in each.

        Returns a list of dicts with account fields merged with ``role``.
        """
        try:
            members = self._request(
                f"account_members?github_login=eq.{github_login}&select=*,accounts(*)"
            )
            result = []
            for m in members:
                account_data = m.get("accounts") or {}
                entry = {
                    "id": m["account_id"],
                    "role": m["role"],
                    **account_data,
                }
                result.append(entry)
            return result
        except urllib.error.HTTPError as exc:
            if not self._is_schema_cache_relationship_error(exc):
                raise
            # Fallback for cases where PostgREST embedded relationship metadata
            # is stale or unavailable in schema cache.
            members = self._request(f"account_members?github_login=eq.{github_login}")
            account_ids = sorted({str(m["account_id"]) for m in members if m.get("account_id")})
            accounts_by_id = {}
            if account_ids:
                accounts = self._request(f"accounts?id=in.({','.join(account_ids)})")
                accounts_by_id = {
                    str(a["id"]): a for a in accounts
                    if isinstance(a, dict) and a.get("id") is not None
                }
            result = []
            for m in members:
                account_data = accounts_by_id.get(str(m["account_id"]), {})
                entry = {
                    "id": m["account_id"],
                    "role": m["role"],
                    **account_data,
                }
                result.append(entry)
            return result

    @staticmethod
    def _is_schema_cache_relationship_error(exc: urllib.error.HTTPError) -> bool:
        """Return True when HTTPError indicates stale/missing PostgREST relation metadata."""
        if exc.code != 400:
            return False
        try:
            payload = json.loads(exc.read().decode() or "{}")
        except Exception:
            return False
        code = str(payload.get("code", ""))
        message = str(payload.get("message", "")).lower()
        return code.startswith("PGRST") and "schema cache" in message

    def get_account_members(self, account_id: str) -> List[dict]:
        """List members of an account, ordered by join date."""
        return self._request(
            f"account_members?account_id=eq.{account_id}&order=joined_at"
        )

    def invite_member(
        self,
        account_id: str,
        github_login: str,
        role: str = "developer",
        invited_by: str = "",
    ) -> dict:
        """Add a member to an account.

        Parameters
        ----------
        account_id:
            UUID of the target account.
        github_login:
            GitHub login of the user to invite.
        role:
            One of ``owner``, ``admin``, ``developer``, ``viewer``.
        invited_by:
            GitHub login of the inviting user.

        Returns
        -------
        dict
            The newly created membership record.
        """
        result = self._request(
            "account_members",
            "POST",
            {
                "account_id": account_id,
                "github_login": github_login,
                "role": role,
                "invited_by": invited_by,
            },
        )
        if isinstance(result, list):
            return result[0] if result else {}
        return result

    def remove_member(self, account_id: str, github_login: str) -> None:
        """Remove a member from an account."""
        self._request(
            f"account_members?account_id=eq.{account_id}&github_login=eq.{github_login}",
            "DELETE",
        )

    def update_member_role(
        self,
        account_id: str,
        github_login: str,
        role: str,
    ) -> dict:
        """Update a member's role in an account.

        Parameters
        ----------
        account_id:
            UUID of the target account.
        github_login:
            GitHub login of the member whose role to change.
        role:
            New role — one of ``owner``, ``admin``, ``developer``, ``viewer``.

        Returns
        -------
        dict
            The updated membership record.
        """
        result = self._request(
            f"account_members?account_id=eq.{account_id}&github_login=eq.{github_login}",
            "PATCH",
            {"role": role},
        )
        if isinstance(result, list):
            return result[0] if result else {}
        return result
