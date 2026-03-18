"""
OpenCode adapter for A2A integration.

Wraps the OpenCode CLI as an A2A-compatible agent.  Follows the pattern
established by the ``opencode-a2a-server`` project: the adapter can either
invoke the OpenCode CLI directly in headless mode or connect to a running
OpenCode A2A server instance.

Two operation modes:
  1. **CLI mode** (default): Invokes ``opencode`` directly via subprocess.
  2. **Server mode**: Connects to a running OpenCode A2A server and sends
     tasks via its JSON-RPC or REST endpoints.

Usage::

    # CLI mode
    adapter = OpenCodeCLIAdapter()
    result = adapter.invoke("Investigate this error: ...")

    # Server mode
    adapter = OpenCodeCLIAdapter(
        server_url="http://localhost:8080",
        mode="server",
    )
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import urllib.request
from typing import Any, Dict, List, Optional

from src.a2a.adapters.generic_cli_adapter import GenericCLIAdapter

logger = logging.getLogger(__name__)

_DEFAULT_OPENCODE_PATH = os.environ.get("OPENCODE_CLI_PATH", "") or shutil.which("opencode") or "/usr/local/bin/opencode"
_DEFAULT_TIMEOUT = 180

# Skills advertised by the OpenCode adapter
_OPENCODE_SKILLS: List[Dict[str, Any]] = [
    {
        "id": "investigate",
        "name": "Investigate Issue",
        "description": "Diagnose software issues using OpenCode's agent runtime",
        "tags": ["investigate", "diagnose", "analyze"],
    },
    {
        "id": "fix",
        "name": "Fix Issue",
        "description": "Implement code fixes using OpenCode's agentic capabilities",
        "tags": ["fix", "patch", "implement"],
    },
    {
        "id": "refactor",
        "name": "Refactor Code",
        "description": "Refactor and improve code quality",
        "tags": ["refactor", "improve", "clean"],
    },
]


class OpenCodeCLIAdapter(GenericCLIAdapter):
    """A2A adapter for the OpenCode CLI / server.

    In CLI mode, OpenCode is invoked directly with::

        opencode run -m "prompt" --model <model>

    In server mode, tasks are submitted to the OpenCode A2A server's
    ``/v1/message:send`` endpoint.

    Parameters
    ----------
    opencode_path:
        Path to the OpenCode CLI binary.
    default_model:
        Default model to use with OpenCode.
    timeout:
        Default timeout in seconds.
    server_url:
        URL of a running OpenCode A2A server (for server mode).
    mode:
        Operation mode: ``"cli"`` (default) or ``"server"``.
    priority:
        Priority for agent selection (lower = preferred).
    """

    def __init__(
        self,
        *,
        opencode_path: str = _DEFAULT_OPENCODE_PATH,
        default_model: str = "",
        timeout: int = _DEFAULT_TIMEOUT,
        server_url: Optional[str] = None,
        mode: str = "cli",
        priority: int = 60,
        cwd: Optional[str] = None,
    ) -> None:
        # OpenCode CLI: `opencode run -m "prompt"`
        args_template = ["run", "-m", "{prompt}"]
        if default_model:
            args_template.extend(["--model", "{model}"])

        super().__init__(
            name="opencode",
            command=opencode_path,
            args_template=args_template,
            default_model=default_model,
            skills=_OPENCODE_SKILLS,
            timeout=timeout,
            prompt_via_stdin=False,
            provider={"organization": "OpenCode"},
            version="1.0.0",
            priority=priority,
            cwd=cwd,
        )
        self._server_url = server_url
        self._mode = mode

    def invoke(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """Invoke OpenCode in the configured mode.

        In CLI mode, delegates to the generic CLI adapter.
        In server mode, sends a task to the A2A server endpoint.

        Raises ``RuntimeError`` if server mode is configured but no URL is set.
        """
        if self._mode == "server":
            if not self._server_url:
                raise RuntimeError("OpenCode server URL not configured")
            return self._invoke_server(prompt, model=model, timeout=timeout)
        return super().invoke(prompt, model=model, timeout=timeout)

    def is_available(self) -> bool:
        """Check availability based on mode."""
        if self._mode == "server" and self._server_url:
            return self._check_server_health()
        return super().is_available()

    def to_registry_card(self) -> Dict[str, Any]:
        """Return a registry card with mode information."""
        card = super().to_registry_card()
        card["mode"] = self._mode
        if self._server_url:
            card["server_url"] = self._server_url
        card["adapter_type"] = "opencode"
        return card

    # ------------------------------------------------------------------
    # Server mode
    # ------------------------------------------------------------------

    def _invoke_server(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """Send a task to the OpenCode A2A server.

        Uses the ``/v1/message:send`` endpoint following the A2A protocol.
        """
        if not self._server_url:
            raise RuntimeError("OpenCode server URL not configured")

        effective_timeout = timeout or self._timeout
        url = self._server_url.rstrip("/") + "/v1/message:send"

        payload = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": prompt}],
                },
            },
        }

        if model:
            payload["params"]["metadata"] = {  # type: ignore[index]
                "shared": {"model": {"id": model}}
            }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        logger.info("Sending task to OpenCode A2A server at %s", url)
        with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        # Extract response text from A2A result
        return self._extract_response_text(result)

    def _check_server_health(self) -> bool:
        """Check if the OpenCode A2A server is reachable."""
        if not self._server_url:
            return False
        try:
            url = self._server_url.rstrip("/") + "/.well-known/agent-card.json"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def _extract_response_text(result: Dict[str, Any]) -> str:
        """Extract text content from an A2A JSON-RPC response."""
        res = result.get("result", {})

        # Check for artifacts first
        for artifact in res.get("artifacts", []):
            for part in artifact.get("parts", []):
                if part.get("kind") == "text":
                    return part.get("text", "")

        # Fall back to message parts
        message = res.get("message", {})
        for part in message.get("parts", []):
            if part.get("kind") == "text":
                return part.get("text", "")

        return str(res)
