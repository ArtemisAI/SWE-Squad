"""Vercel deployment provider."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from src.swe_team.providers.deployment.base import DeploymentProvider

logger = logging.getLogger(__name__)


class VercelDeploymentProvider:
    """DeploymentProvider implementation using the Vercel REST API."""

    def __init__(self, *, token: str = "", team_id: str = "") -> None:
        self._token = token
        self._team_id = team_id

    @property
    def name(self) -> str:
        return "vercel"

    def create_deployment(
        self,
        *,
        project: str,
        branch: str,
        commit_sha: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        """Create a deployment and return its Vercel id (or fallback URL)."""
        if not self.health_check() or not project or not branch:
            return None

        payload: dict[str, Any] = {
            "name": project,
            "gitSource": {
                "type": "github",
                "ref": branch,
                "sha": commit_sha or None,
            },
        }
        if metadata:
            payload["meta"] = metadata

        data = self._request(
            "POST",
            self._build_url("/v13/deployments"),
            payload,
        )
        if not data:
            return None
        return data.get("id") or data.get("url")

    def get_deployment(self, deployment_id: str) -> Optional[dict[str, Any]]:
        if not self.health_check() or not deployment_id:
            return None
        return self._request(
            "GET",
            self._build_url(f"/v13/deployments/{urllib.parse.quote(deployment_id)}"),
        )

    def health_check(self) -> bool:
        return bool(self._token)

    def _build_url(self, path: str) -> str:
        if not self._team_id:
            return f"https://api.vercel.com{path}"
        query = urllib.parse.urlencode({"teamId": self._team_id})
        return f"https://api.vercel.com{path}?{query}"

    def _request(
        self,
        method: str,
        url: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
                raw = resp.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, ValueError):
            logger.warning("VercelDeploymentProvider request failed", exc_info=True)
            return None


assert isinstance(VercelDeploymentProvider(token="x"), DeploymentProvider)
