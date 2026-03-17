"""
A2A event dispatcher for SWE-Squad.

Supports two modes:
  - **Hub mode** (default): POSTs events to the centralized A2A hub at
    ``config.a2a_hub_url`` (default ``http://localhost:18790``).
  - **Standalone mode**: Falls back to local logging when the hub is
    unreachable or no hub URL is configured.

The dispatcher is designed for best-effort delivery: hub failures are logged
but never raise exceptions to the caller.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from src.a2a.events import PipelineEvent

logger = logging.getLogger(__name__)

# Module-level hub URL; set via ``configure()`` at startup.
_hub_url: Optional[str] = None

# Event endpoint path on the hub
_HUB_EVENT_PATH = "/v1/events"

# HTTP timeout for hub requests (seconds)
_HUB_TIMEOUT = 10


def configure(hub_url: Optional[str] = None) -> None:
    """Configure the dispatcher with the hub URL.

    Call this once at startup (e.g. from the runner) with the value from
    ``config.a2a_hub_url``.  When *hub_url* is ``None`` or empty, the
    dispatcher operates in standalone mode (log-only).

    Parameters
    ----------
    hub_url:
        Base URL of the centralized A2A hub (e.g. ``http://localhost:18790``).
    """
    global _hub_url
    _hub_url = hub_url.rstrip("/") if hub_url else None
    if _hub_url:
        logger.info("A2A dispatcher configured for hub: %s", _hub_url)
    else:
        logger.info("A2A dispatcher in standalone mode (no hub URL)")


def get_hub_url() -> Optional[str]:
    """Return the currently configured hub URL, or None if standalone."""
    return _hub_url


def _post_to_hub(payload: Dict[str, Any]) -> bool:
    """POST a JSON payload to the hub's event endpoint.

    Returns True on success, False on any failure.  Failures are logged
    but never raised.
    """
    if not _hub_url:
        return False

    url = _hub_url + _HUB_EVENT_PATH
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=_HUB_TIMEOUT) as resp:
            if resp.status < 300:
                logger.debug("Event dispatched to hub: %s (status=%d)", url, resp.status)
                return True
            logger.warning("Hub returned status %d for event POST", resp.status)
            return False
    except urllib.error.HTTPError as exc:
        logger.warning(
            "Hub HTTP error %d posting event to %s: %s",
            exc.code, url, exc.reason,
        )
        return False
    except (urllib.error.URLError, OSError) as exc:
        logger.debug(
            "Hub unreachable at %s — falling back to standalone: %s",
            url, exc,
        )
        return False
    except Exception:
        logger.warning("Unexpected error posting to hub %s", url, exc_info=True)
        return False


async def dispatch_event(
    event: PipelineEvent,
    agent: Optional[str] = None,
) -> bool:
    """Dispatch a pipeline event to the A2A hub.

    If the hub is configured and reachable, the event is POSTed as JSON.
    Otherwise falls back to local debug logging (standalone mode).

    Parameters
    ----------
    event:
        The pipeline event to dispatch.
    agent:
        Optional target agent name to include in the envelope.

    Returns
    -------
    bool
        True if the event was dispatched (hub accepted it or standalone
        log succeeded).
    """
    payload = {
        "event": event.event,
        "source_stage": event.source_stage,
        "payload": event.payload,
    }
    if agent:
        payload["target_agent"] = agent

    # Try hub dispatch first
    if _hub_url:
        sent = _post_to_hub(payload)
        if sent:
            return True
        # Hub unreachable — fall through to standalone logging
        logger.debug(
            "Hub dispatch failed; standalone fallback for event=%s agent=%s",
            event.event, agent,
        )

    # Standalone mode: log the event locally
    logger.debug("A2A standalone dispatch: %s (agent=%s)", event.event, agent)
    return True
