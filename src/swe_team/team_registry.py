"""Team registry utilities for config-driven fleet allocation."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol

from src.swe_team.config import SWETeamConfig, TeamConfig
from src.swe_team.models import TicketStatus
from src.swe_team.supabase_store import SupabaseTicketStore


class _TicketStoreLike(Protocol):
    def list_by_status(self, status: TicketStatus, limit: int = 500) -> List[Any]: ...


class TeamRegistry:
    """Query team configuration and capacity/load."""

    def __init__(
        self,
        teams: Optional[Dict[str, TeamConfig]] = None,
        store_factory: Optional[Callable[[str], _TicketStoreLike]] = None,
    ) -> None:
        self._teams: Dict[str, TeamConfig] = teams or {}
        self._store_factory = store_factory or self._default_store_factory
        self._stores: Dict[str, _TicketStoreLike] = {}

    @classmethod
    def from_config(
        cls,
        config: SWETeamConfig,
        store_factory: Optional[Callable[[str], _TicketStoreLike]] = None,
    ) -> "TeamRegistry":
        return cls(teams=config.teams, store_factory=store_factory)

    def get_team(self, team_id: str) -> TeamConfig:
        team = self._teams.get(team_id)
        if team is None:
            available = ", ".join(sorted(self._teams)) or "<none>"
            raise KeyError(f"Team {team_id!r} not found in registry. Available teams: {available}")
        return team

    def get_team_load(self, team_id: str) -> int:
        _team = self.get_team(team_id)
        store = self._get_store(team_id)
        # Team active load is derived from in-flight states only.
        # list_by_status materializes tickets, but team sizes are intentionally
        # small (max_concurrent is typically a single-digit value).
        investigating = len(store.list_by_status(TicketStatus.INVESTIGATING))
        in_development = len(store.list_by_status(TicketStatus.IN_DEVELOPMENT))
        return investigating + in_development

    def get_available_teams(self, role_filter: Optional[str] = None) -> List[TeamConfig]:
        available: List[TeamConfig] = []
        for team in self._teams.values():
            if role_filter and team.role not in {role_filter, "full"}:
                continue
            if self.get_team_load(team.name) < team.max_concurrent:
                available.append(team)
        return available

    def _get_store(self, team_id: str) -> _TicketStoreLike:
        if team_id not in self._stores:
            self._stores[team_id] = self._store_factory(team_id)
        return self._stores[team_id]

    @staticmethod
    def _default_store_factory(team_id: str) -> _TicketStoreLike:
        return SupabaseTicketStore(team_id=team_id)
