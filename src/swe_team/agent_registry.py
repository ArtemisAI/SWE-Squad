"""
Agent registry for multi-agent A2A integration.

Tracks available coding agents on the network, their capabilities, and health.
Supports the A2A protocol's agent card discovery mechanism and provides
intelligent agent selection based on task type, severity, and availability.

Two operating modes:
  - **Standalone mode** (default): agents register locally and discovery
    queries well-known A2A endpoints directly.
  - **Hub mode**: when ``hub_url`` is provided, the registry registers
    agents with the centralized A2A hub and discovers other agents through
    it.  Local registration still works as a cache/fallback.

Agents register via ``AgentCard``-like dicts and are discovered either through
local registration, the centralized hub, or by querying well-known A2A
endpoints.

The registry can use an :class:`~src.a2a.client.A2AClient` for network-based
discovery and health checking when available.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

if TYPE_CHECKING:
    from src.a2a.adapters.base import AgentAdapter
    from src.a2a.client import A2AClient

logger = logging.getLogger(__name__)

# Default TTL for cached agent cards before re-discovery (seconds)
_DEFAULT_TTL_SECONDS = 300

# Well-known A2A discovery path per the spec
WELL_KNOWN_AGENT_CARD_PATH = "/.well-known/agent-card.json"

# Hub API paths
_HUB_REGISTER_PATH = "/v1/agents/register"
_HUB_AGENTS_PATH = "/v1/agents"
_HUB_TIMEOUT = 10


class AgentRegistry:
    """Registry of available coding agents on the network.

    Maintains a local cache of agent cards with TTL-based expiry.
    Each agent card is a dict following the A2A ``AgentCard`` schema with
    at minimum: ``name``, ``url``, ``skills`` (list of skill dicts with
    ``id`` and ``tags``), and ``status`` (``online``/``offline``).

    When *hub_url* is provided, the registry operates in **hub mode**:
    registrations are mirrored to the centralized hub, and ``discover()``
    queries the hub in addition to any configured discovery URLs.

    Usage::

        # Standalone mode
        registry = AgentRegistry()

        # Hub mode
        registry = AgentRegistry(hub_url="http://localhost:18790")

        registry.register({
            "name": "gemini-cli",
            "url": "local://gemini",
            "skills": [{"id": "investigate", "tags": ["investigate", "diagnose"]}],
            "status": "online",
        })
        agent = registry.select_agent(task_type="investigate", severity="high")

    Parameters
    ----------
    ttl_seconds:
        Time-to-live for cached agent cards before they expire.
    discovery_urls:
        List of agent base URLs to query during ``discover()``.
    a2a_client:
        An ``A2AClient`` instance for network-based discovery and health checks.
    hub_url:
        Base URL of the centralized A2A hub.  When set, enables hub mode.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        discovery_urls: Optional[List[str]] = None,
        a2a_client: Optional["A2AClient"] = None,
        hub_url: Optional[str] = None,
    ) -> None:
        self._agents: Dict[str, Dict[str, Any]] = {}
        self._registered_at: Dict[str, float] = {}
        self._ttl_seconds = ttl_seconds
        self._discovery_urls = discovery_urls or []
        self._a2a_client = a2a_client
        self._local_adapters: Dict[str, "AgentAdapter"] = {}
        self._hub_url = hub_url.rstrip("/") if hub_url else None
        if self._hub_url:
            logger.info("AgentRegistry operating in hub mode: %s", self._hub_url)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def hub_url(self) -> Optional[str]:
        """Return the hub URL, or None if operating in standalone mode."""
        return self._hub_url

    @property
    def is_hub_mode(self) -> bool:
        """True if the registry is configured to use a centralized hub."""
        return self._hub_url is not None

    def register(self, agent_card: Dict[str, Any]) -> None:
        """Register or update an agent card in the registry.

        In hub mode, the registration is also mirrored to the centralized
        hub (best-effort; failures are logged but do not block local
        registration).

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

        # Mirror to hub (best-effort)
        if self._hub_url:
            self._register_with_hub(agent_card)

    def register_local(self, adapter: "AgentAdapter") -> None:
        """Register a locally-running agent adapter.

        The adapter's agent card is extracted and registered, and the adapter
        reference is stored so it can be used for local invocations without
        going through HTTP.

        Parameters
        ----------
        adapter:
            An ``AgentAdapter`` instance (e.g. ``GeminiCLIAdapter``).
        """
        card = adapter.agent_card()
        card_dict: Dict[str, Any] = {
            "name": card.name,
            "url": card.url,
            "version": card.version,
            "provider": card.provider,
            "skills": [
                {"id": s.id, "name": s.name, "description": s.description, "tags": s.tags}
                for s in card.skills
            ],
            "status": "online",
        }
        # Include priority if the adapter exposes it
        if hasattr(adapter, "_priority"):
            card_dict["priority"] = adapter._priority
        # Include availability check if available
        if hasattr(adapter, "is_available") and callable(adapter.is_available):
            try:
                card_dict["status"] = "online" if adapter.is_available() else "offline"
            except Exception:
                card_dict["status"] = "offline"

        self.register(card_dict)
        self._local_adapters[card.name] = adapter
        logger.info("Registered local adapter: %s", card.name)

    def get_local_adapter(self, name: str) -> Optional["AgentAdapter"]:
        """Return the local adapter for *name*, or ``None`` if not registered locally."""
        return self._local_adapters.get(name)

    def unregister(self, name: str) -> bool:
        """Remove an agent from the registry.

        Returns True if the agent was found and removed, False otherwise.
        """
        if name in self._agents:
            del self._agents[name]
            self._registered_at.pop(name, None)
            self._local_adapters.pop(name, None)
            logger.info("Unregistered agent: %s", name)
            return True
        return False

    def discover(self) -> List[Dict[str, Any]]:
        """Discover agents through the hub and/or configured discovery URLs.

        In hub mode, the hub's ``/v1/agents`` endpoint is queried first.
        Then individual discovery URLs are queried as in standalone mode.

        Uses :class:`A2AClient` when available, falling back to direct
        ``urllib`` requests.  This is a best-effort operation: network
        failures are logged but do not raise exceptions.

        Returns
        -------
        list
            The list of newly discovered agent card dicts.
        """
        discovered: List[Dict[str, Any]] = []

        # Hub discovery (if configured)
        if self._hub_url:
            hub_agents = self._discover_from_hub()
            for card in hub_agents:
                if card.get("name"):
                    # Register locally without re-posting to hub
                    name = card["name"]
                    self._agents[name] = dict(card)
                    self._registered_at[name] = time.monotonic()
                    discovered.append(card)
            if hub_agents:
                logger.info(
                    "Discovered %d agents from hub at %s",
                    len(hub_agents), self._hub_url,
                )

        # Standalone discovery (query individual agent URLs)
        for base_url in self._discovery_urls:
            try:
                if self._a2a_client is not None:
                    card = self._a2a_client.discover(base_url)
                else:
                    url = base_url.rstrip("/") + WELL_KNOWN_AGENT_CARD_PATH
                    card = self._fetch_agent_card(url)
                if card and card.get("name"):
                    # Preserve the discovery URL so we know where this agent lives
                    card.setdefault("url", base_url)
                    card.setdefault("status", "online")
                    self.register(card)
                    discovered.append(card)
            except Exception:
                logger.warning("A2A discovery failed for %s", base_url, exc_info=True)
        return discovered

    def check_health(self, name: str) -> bool:
        """Check if a registered agent is healthy.

        Uses the A2A client for remote agents (those with ``http://`` URLs)
        and the adapter's ``is_available()`` for local agents.

        Returns True if the agent is reachable and healthy.
        """
        card = self._agents.get(name)
        if card is None:
            return False

        # Check local adapter first
        adapter = self._local_adapters.get(name)
        if adapter is not None:
            if hasattr(adapter, "is_available") and callable(adapter.is_available):
                try:
                    healthy = adapter.is_available()
                except Exception:
                    healthy = False
                self.set_status(name, "online" if healthy else "offline")
                return healthy
            return True

        # Remote agent — use A2A client
        url = card.get("url", "")
        if url.startswith("http") and self._a2a_client is not None:
            try:
                healthy = self._a2a_client.health_check(url)
            except Exception:
                healthy = False
            self.set_status(name, "online" if healthy else "offline")
            return healthy

        return card.get("status", "online") == "online"

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

    # ------------------------------------------------------------------
    # Hub integration
    # ------------------------------------------------------------------

    def _register_with_hub(self, agent_card: Dict[str, Any]) -> bool:
        """POST an agent card to the hub's registration endpoint.

        Best-effort: returns True on success, False on any failure.
        """
        if not self._hub_url:
            return False
        url = self._hub_url + _HUB_REGISTER_PATH
        data = json.dumps(agent_card).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_HUB_TIMEOUT) as resp:
                if resp.status < 300:
                    logger.debug(
                        "Agent %s registered with hub at %s",
                        agent_card.get("name"), self._hub_url,
                    )
                    return True
                logger.warning(
                    "Hub returned status %d for agent registration",
                    resp.status,
                )
                return False
        except urllib.error.HTTPError as exc:
            logger.warning(
                "Hub HTTP error %d registering agent %s: %s",
                exc.code, agent_card.get("name"), exc.reason,
            )
            return False
        except (urllib.error.URLError, OSError) as exc:
            logger.debug(
                "Hub unreachable at %s for registration: %s",
                self._hub_url, exc,
            )
            return False
        except Exception:
            logger.warning(
                "Unexpected error registering with hub",
                exc_info=True,
            )
            return False

    def _discover_from_hub(self) -> List[Dict[str, Any]]:
        """Query the hub's ``/v1/agents`` endpoint for all registered agents.

        Returns an empty list if the hub is unreachable.
        """
        if not self._hub_url:
            return []
        url = self._hub_url + _HUB_AGENTS_PATH
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=_HUB_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("agents", [])
        except (urllib.error.URLError, OSError) as exc:
            logger.debug(
                "Hub discovery failed at %s: %s", url, exc,
            )
        except Exception:
            logger.debug(
                "Hub discovery failed unexpectedly at %s",
                url, exc_info=True,
            )
        return []
