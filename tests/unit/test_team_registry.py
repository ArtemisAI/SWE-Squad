from __future__ import annotations

from src.swe_team.config import TeamConfig
from src.swe_team.models import TicketStatus
from src.swe_team.team_registry import TeamRegistry


class _FakeStore:
    def __init__(self, investigating: int = 0, in_development: int = 0):
        self._counts = {
            TicketStatus.INVESTIGATING: investigating,
            TicketStatus.IN_DEVELOPMENT: in_development,
        }

    def list_by_status(self, status: TicketStatus, limit: int = 500) -> list:
        return [object()] * min(self._counts.get(status, 0), limit)


class TestTeamRegistry:
    def test_get_team_returns_team_config(self):
        reg = TeamRegistry(teams={"alpha": TeamConfig(name="alpha", role="developer")})
        team = reg.get_team("alpha")
        assert team.name == "alpha"
        assert team.role == "developer"

    def test_get_team_raises_helpful_error_for_unknown_team(self):
        reg = TeamRegistry(teams={"alpha": TeamConfig(name="alpha")})
        try:
            reg.get_team("missing")
            raise AssertionError("Expected KeyError for unknown team")
        except KeyError as exc:
            assert "Available teams: alpha" in str(exc)

    def test_get_team_load_uses_store_counts(self):
        stores = {"alpha": _FakeStore(investigating=1, in_development=2)}
        reg = TeamRegistry(
            teams={"alpha": TeamConfig(name="alpha", max_concurrent=3)},
            store_factory=lambda team_id: stores[team_id],
        )
        assert reg.get_team_load("alpha") == 3

    def test_get_available_teams_filters_by_capacity(self):
        teams = {
            "alpha": TeamConfig(name="alpha", role="developer", max_concurrent=2),
            "beta": TeamConfig(name="beta", role="developer", max_concurrent=2),
        }
        stores = {
            "alpha": _FakeStore(investigating=1, in_development=0),  # available
            "beta": _FakeStore(investigating=2, in_development=0),   # full
        }
        reg = TeamRegistry(teams=teams, store_factory=lambda team_id: stores[team_id])
        available = reg.get_available_teams(role_filter="developer")
        assert [t.name for t in available] == ["alpha"]

    def test_get_available_teams_honors_role_filter(self):
        teams = {
            "alpha": TeamConfig(name="alpha", role="developer", max_concurrent=3),
            "beta": TeamConfig(name="beta", role="investigator", max_concurrent=3),
            "gamma": TeamConfig(name="gamma", role="full", max_concurrent=3),
        }
        stores = {
            "alpha": _FakeStore(),
            "beta": _FakeStore(),
            "gamma": _FakeStore(),
        }
        reg = TeamRegistry(teams=teams, store_factory=lambda team_id: stores[team_id])
        available = reg.get_available_teams(role_filter="developer")
        assert [t.name for t in available] == ["alpha", "gamma"]
