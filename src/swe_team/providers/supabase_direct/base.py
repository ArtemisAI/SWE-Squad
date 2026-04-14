"""Supabase direct provider interface."""
from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class SupabaseDirectProvider(Protocol):
    @property
    def name(self) -> str: ...

    def select(
        self,
        table: str,
        *,
        filters: Optional[dict[str, str]] = None,
        limit: Optional[int] = None,
    ) -> Optional[list[dict[str, Any]]]:
        ...

    def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
    ) -> Optional[list[dict[str, Any]]]:
        ...

    def rpc(
        self,
        function_name: str,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        ...

    def health_check(self) -> bool: ...
