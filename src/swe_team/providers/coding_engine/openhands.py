"""OpenHands coding engine -- CodingEngine implementation for OpenHands autonomous agent.

OpenHands is an API-based agent platform.  This connector sends prompts via
HTTP to the OpenHands API (no CLI binary required).

Registered in swe_team.yaml under providers.coding_engine.provider: openhands.
"""
from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult

logger = logging.getLogger(__name__)


class OpenHandsEngine:
    """OpenHands API engine -- autonomous agent via REST API.

    Implements the :class:`CodingEngine` protocol directly (not CLI-based).
    Sends prompts to the OpenHands API at ``{api_url}/api/submit`` and
    returns the result as an :class:`EngineResult`.

    Parameters
    ----------
    api_url:
        Base URL for the OpenHands API (e.g. ``http://localhost:3000``).
    api_key:
        API key for authentication.  Sent as ``Authorization: Bearer``.
    default_model:
        Default model identifier (passed in the request body).
    default_timeout:
        Default HTTP request timeout in seconds.
    """

    def __init__(
        self,
        *,
        api_url: str = "http://localhost:3000",
        api_key: str = "",
        default_model: str = "",
        default_timeout: int = 300,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._default_model = default_model
        self._default_timeout = default_timeout

    # -- CodingEngine protocol -------------------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "openhands"

    def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout: int | None = None,
        cwd: Optional[str] = None,
        env: dict | None = None,
        session_id: str | None = None,
    ) -> EngineResult:
        """Submit a prompt to the OpenHands API and return the result."""
        effective_model = model or self._default_model
        effective_timeout = timeout or self._default_timeout

        url = f"{self._api_url}/api/submit"
        payload: Dict[str, Any] = {
            "prompt": prompt,
        }
        if effective_model:
            payload["model"] = effective_model
        if cwd:
            payload["cwd"] = cwd
        if session_id:
            payload["session_id"] = session_id

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                stdout = body.get("result", body.get("output", ""))
                if not isinstance(stdout, str):
                    stdout = json.dumps(stdout)
                return EngineResult(
                    stdout=stdout,
                    stderr="",
                    returncode=0,
                    model=effective_model,
                    cost_usd=body.get("cost"),
                    session_id=body.get("session_id"),
                    metadata=body.get("metadata", {}),
                )
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            logger.error("OpenHands API error %d: %s", e.code, error_body)
            return EngineResult(
                stdout="",
                stderr=f"HTTP {e.code}: {error_body}",
                returncode=-1,
                model=effective_model,
            )
        except urllib.error.URLError as e:
            logger.error("OpenHands API unreachable: %s", e.reason)
            return EngineResult(
                stdout="",
                stderr=f"Connection error: {e.reason}",
                returncode=-1,
                model=effective_model,
            )
        except Exception as e:
            logger.error("OpenHands API unexpected error: %s", e)
            return EngineResult(
                stdout="",
                stderr=str(e),
                returncode=-1,
                model=effective_model,
            )

    def health_check(self) -> bool:
        """Return True if the OpenHands API health endpoint is reachable."""
        url = f"{self._api_url}/health"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception:
            return False

    # -- Convenience -----------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the API URL is configured."""
        return bool(self._api_url)

    def model(self) -> str:
        """Return the default model name."""
        return self._default_model
