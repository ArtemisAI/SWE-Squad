"""
Agent registry for multi-agent A2A integration.

Tracks available coding agents on the network, their capabilities, and health.
Supports the A2A protocol's agent card discovery mechanism and provides
intelligent agent selection based on task type, severity, and availability.

Agents register via ``AgentCard``-like dicts and are discovered either through
local registration or by querying well-known A2A endpoints.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Default TTL for cached agent cards before re-discovery (seconds)
_DEFAULT_TTL_SECONDS = 300

# Well-known A2A discovery path per the spec
WELL_KNOWN_AGENT_CARD_PATH = "/.well-known/agent-card.json"


class AgentRegistry:
    """Registry of available coding agents on the network.

    Maintains a local cache of agent cards with TTL-based expiry.
    Each agent card is a dict following the A2A ``AgentCard`` schema with
    at minimum: ``name``, ``url``, ``skills`` (list of skill dicts with
    ``id`` and ``tags``), and ``status`` (``online``/``offline``).

    Usage::

        registry = AgentRegistry()
        registry.register({
            "name": "gemini-cli",
            "url": "local://gemini",
            "skills": [{"id": "investigate", "tags": ["investigate", "diagnose"]}],
            "status": "online",
        })
        agent = registry.select_agent(task_type="investigate", severity="high")
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        discovery_urls: Optional[List[str]] = None,
    ) -> None:
        self._agents: Dict[str, Dict[str, Any]] = {}
        self._registered_at: Dict[str, float] = {}
        self._ttl_seconds = ttl_seconds
        self._discovery_urls = discovery_urls or []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, agent_card: Dict[str, Any]) -> None:
        """Register or update an agent card in the registry.

        Parameters
        ----------
        agent_card:
            Dict with at least ``name`` (str).  Recommended fields:
            ``url``, ``skills`` (list of dicts), ``status`` (``online``/``offline``),
            ``provider``, ``version``.

        Raises
        ------
        ValueError
            If the agent card is missing the required ``name`` field.
        """
        name = agent_card.get("name")
        if not name:
            raise ValueError("Agent card must include a 'name' field")
        self._agents[name] = dict(agent_card)
        self._registered_at[name] = time.monotonic()
        logger.info("Registered agent: %s (skills=%s)", name, [
            s.get("id", "?") for s in agent_card.get("skills", [])
        ])

    def unregister(self, name: str) -> bool:
        """Remove an agent from the registry.

        Returns True if the agent was found and removed, False otherwise.
        """
        if name in self._agents:
            del self._agents[name]
            self._registered_at.pop(name, None)
            logger.info("Unregistered agent: %s", name)
            return True
        return False

    def discover(self) -> List[Dict[str, Any]]:
        """Query configured A2A discovery URLs for agent cards.

        This is a best-effort operation: network failures are logged but
        do not raise exceptions.  Successfully discovered agents are merged
        into the local registry.

        Returns
        -------
        list
            The list of newly discovered agent card dicts.
        """
        discovered: List[Dict[str, Any]] = []
        for base_url in self._discovery_urls:
            url = base_url.rstrip("/") + WELL_KNOWN_AGENT_CARD_PATH
            try:
                card = self._fetch_agent_card(url)
                if card and card.get("name"):
                    self.register(card)
                    discovered.append(card)
            except Exception:
                logger.warning("A2A discovery failed for %s", url, exc_info=True)
        return discovered

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the agent card for *name*, or ``None`` if not found."""
        return self._agents.get(name)

    def select_agent(
        self,
        task_type: str,
        severity: str = "medium",
        *,
        exclude: Optional[Sequence[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Select the best available agent for a task.

        Selection logic:
        1. Filter to online agents whose skills include *task_type*.
        2. Sort by priority (lower is better; defaults to 100).
        3. For ``critical``/``high`` severity, prefer agents with matching
           severity tags; otherwise fall back to the first match.
        4. Exclude agents listed in *exclude*.

        Parameters
        ----------
        task_type:
            The skill ID or tag to match (e.g. ``"investigate"``, ``"fix"``).
        severity:
            Ticket severity (``"critical"``, ``"high"``, ``"medium"``, ``"low"``).
        exclude:
            Agent names to skip (e.g. the primary agent that is rate-limited).

        Returns
        -------
        dict or None
            The selected agent card, or ``None`` if no suitable agent is found.
        """
        self._expire_stale()
        exclude_set = set(exclude or [])
        candidates: List[Dict[str, Any]] = []

        for name, card in self._agents.items():
            if name in exclude_set:
                continue
            if card.get("status", "online") != "online":
                continue
            if self._agent_has_skill(card, task_type):
                candidates.append(card)

        if not candidates:
            return None

        # Sort by priority (lower = higher priority)
        candidates.sort(key=lambda c: c.get("priority", 100))

        # For critical/high severity, prefer agents tagged for that tier
        if severity in ("critical", "high"):
            for candidate in candidates:
                tags = self._all_tags(candidate)
                if severity in tags or "heavy" in tags:
                    return candidate

        return candidates[0]

    def list_agents(
        self, *, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return all registered agent cards, optionally filtered by status.

        Parameters
        ----------
        status:
            If provided, only return agents with this status
            (e.g. ``"online"``).
        """
        self._expire_stale()
        agents = list(self._agents.values())
        if status:
            agents = [a for a in agents if a.get("status", "online") == status]
        return agents

    def set_status(self, name: str, status: str) -> bool:
        """Update the status of a registered agent.

        Returns True if the agent was found and updated.
        """
        if name in self._agents:
            self._agents[name]["status"] = status
            return True
        return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _expire_stale(self) -> None:
        """Remove agents whose registration has exceeded the TTL."""
        now = time.monotonic()
        expired = [
            name
            for name, ts in self._registered_at.items()
            if (now - ts) > self._ttl_seconds
        ]
        for name in expired:
            self._agents.pop(name, None)
            self._registered_at.pop(name, None)
            logger.debug("Expired stale agent card: %s", name)

    @staticmethod
    def _agent_has_skill(card: Dict[str, Any], task_type: str) -> bool:
        """Check if an agent card advertises a skill matching *task_type*."""
        for skill in card.get("skills", []):
            if skill.get("id") == task_type:
                return True
            if task_type in skill.get("tags", []):
                return True
        return False

    @staticmethod
    def _all_tags(card: Dict[str, Any]) -> set:
        """Collect all tags from an agent card's skills."""
        tags: set = set()
        for skill in card.get("skills", []):
            tags.update(skill.get("tags", []))
        return tags

    @staticmethod
    def _fetch_agent_card(url: str) -> Optional[Dict[str, Any]]:
        """Fetch an agent card from a remote URL (best-effort).

        Uses stdlib ``urllib`` to avoid external dependencies.
        """
        import json
        import urllib.request

        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            logger.debug("Failed to fetch agent card from %s", url, exc_info=True)
        return None
