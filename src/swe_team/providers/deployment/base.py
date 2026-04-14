"""Deployment provider interface — pluggable deployment backends."""
from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class DeploymentProvider(Protocol):
    @property
    def name(self) -> str: ...

    def create_deployment(
        self,
        *,
        project: str,
        branch: str,
        commit_sha: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        """Create a deployment and return deployment id/url on success."""
        ...

    def get_deployment(self, deployment_id: str) -> Optional[dict[str, Any]]:
        """Fetch deployment details by id."""
        ...

    def health_check(self) -> bool: ...
