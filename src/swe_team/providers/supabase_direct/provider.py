"""Supabase direct provider using PostgREST endpoints."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from src.swe_team.providers.supabase_direct.base import SupabaseDirectProvider

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class SupabaseRESTDirectProvider:
    """SupabaseDirectProvider implementation backed by REST + RPC endpoints."""

    def __init__(self, *, url: str = "", key: str = "", schema: str = "public") -> None:
        self._url = url.rstrip("/")
        self._key = key
        self._schema = schema

    @property
    def name(self) -> str:
        return "supabase-direct"

    def select(
        self,
        table: str,
        *,
        filters: Optional[dict[str, str]] = None,
        limit: Optional[int] = None,
    ) -> Optional[list[dict[str, Any]]]:
        if not self.health_check() or not table:
            return None

        params: dict[str, str] = {"select": "*"}
        for key, value in (filters or {}).items():
            params[key] = f"eq.{value}"
        if limit is not None:
            params["limit"] = str(limit)

        query = urllib.parse.urlencode(params)
        table_path = urllib.parse.quote(table)
        result = self._request("GET", f"{self._url}/rest/v1/{table_path}?{query}")
        if isinstance(result, list):
            return result
        return None

    def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
    ) -> Optional[list[dict[str, Any]]]:
        if not self.health_check() or not table or not rows:
            return None
        table_path = urllib.parse.quote(table)
        result = self._request(
            "POST",
            f"{self._url}/rest/v1/{table_path}",
            rows,
            extra_headers={"Prefer": "return=representation"},
        )
        if isinstance(result, list):
            return result
        return None

    def rpc(
        self,
        function_name: str,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        if not self.health_check() or not function_name:
            return None
        function_path = urllib.parse.quote(function_name)
        return self._request(
            "POST",
            f"{self._url}/rest/v1/rpc/{function_path}",
            params or {},
        )

    def health_check(self) -> bool:
        return bool(self._url and self._key)

    def _request(
        self,
        method: str,
        url: str,
        payload: Optional[Any] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> Any:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept-Profile": self._schema,
            "Content-Profile": self._schema,
        }
        headers.update(extra_headers or {})
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    try:
                        content_length_int = int(content_length.strip())
                    except ValueError:
                        content_length_int = -1
                    if content_length_int > _MAX_RESPONSE_BYTES:
                        logger.warning(
                            "SupabaseRESTDirectProvider response too large: %s bytes",
                            content_length,
                        )
                        return None
                raw_bytes = resp.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw_bytes) > _MAX_RESPONSE_BYTES:
                logger.warning("SupabaseRESTDirectProvider response exceeded size limit")
                return None
            raw = raw_bytes.decode("utf-8")
            if not raw:
                return None
            return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, ValueError):
            logger.warning("SupabaseRESTDirectProvider request failed", exc_info=True)
            return None


assert isinstance(SupabaseRESTDirectProvider(url="u", key="k"), SupabaseDirectProvider)
