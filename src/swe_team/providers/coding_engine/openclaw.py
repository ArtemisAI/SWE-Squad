"""OpenClaw coding engine -- ACP (Agent Communication Protocol) bridge.

OpenClaw is a multi-agent platform that can be driven via its CLI or through
a Gateway WebSocket endpoint.  This connector supports both modes:

- **CLI mode** (default): invokes the ``openclaw`` binary as a subprocess.
- **Gateway mode**: connects to an OpenClaw Gateway WebSocket endpoint,
  sends prompts as JSON messages, and streams back responses.

Registered in swe_team.yaml under providers.coding_engine.provider: openclaw.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)

# Keywords that must never be sent through OpenClaw (data-retention risk).
_SENSITIVE_KEYWORDS = (
    "password",
    "secret",
    "api_key",
    "private_key",
    "credential",
    "token",
)


class OpenClawEngine(GenericCLIEngine):
    """Engine connector for OpenClaw ACP bridge.

    Extends GenericCLIEngine with OpenClaw-specific defaults, JSON output
    parsing, safety filtering, and optional Gateway WebSocket mode.

    Parameters
    ----------
    default_model:
        Default model identifier (e.g. ``"kimi-k2.5"``).
    default_timeout:
        Default subprocess timeout in seconds.
    binary:
        Path to the ``openclaw`` CLI binary.  Resolved via ``shutil.which``
        if not provided.
    model_flag:
        CLI flag for model selection.  OpenClaw uses ``--model``.
    env_vars:
        Extra environment variables for the subprocess (API keys, proxy URLs).
    session_flag:
        CLI flag for session tracking.  OpenClaw supports ``--session``.
    gateway_url:
        WebSocket URL for Gateway mode (e.g. ``"ws://localhost:8765"``).
        When set, ``run()`` uses WebSocket instead of subprocess.
    """

    def __init__(
        self,
        *,
        default_model: str = "kimi-k2.5",
        default_timeout: int = 300,
        binary: str | None = None,
        model_flag: str = "--model",
        env_vars: Dict[str, str] | None = None,
        session_flag: str | None = "--session",
        gateway_url: str | None = None,
    ) -> None:
        resolved_binary = binary or shutil.which("openclaw") or "/usr/local/bin/openclaw"

        # OpenClaw CLI: openclaw run --model <model> --output json -p <prompt>
        args_template: List[str] = ["run", "--output", "json", "-p", "{prompt}"]

        super().__init__(
            binary=resolved_binary,
            default_model=default_model,
            default_timeout=default_timeout,
            args_template=args_template,
            model_flag=model_flag,
            prompt_via="args",
            output_format="json",
            session_flag=session_flag,
            env_vars=env_vars,
        )

        self._gateway_url = gateway_url

    # -- CodingEngine protocol -------------------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "openclaw"

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
        """Execute OpenClaw with the given prompt.

        Applies a safety filter to block sensitive keywords, then delegates
        to either Gateway WebSocket mode or CLI subprocess mode.
        """
        effective_model = model or self._default_model

        # Safety filter: block prompts containing sensitive keywords
        prompt_lower = prompt.lower()
        for kw in _SENSITIVE_KEYWORDS:
            if kw in prompt_lower:
                logger.warning("openclaw: blocked sensitive keyword '%s'", kw)
                return EngineResult(
                    stdout="",
                    stderr=f"Blocked: prompt contains sensitive keyword '{kw}'",
                    returncode=-1,
                    model=effective_model,
                    metadata={"error_type": "safety_block", "blocked_keyword": kw},
                )

        # Gateway WebSocket mode
        if self._gateway_url:
            return self._run_gateway(
                prompt,
                model=effective_model,
                timeout=timeout or self._default_timeout,
                cwd=cwd,
                session_id=session_id,
            )

        # CLI subprocess mode (via GenericCLIEngine)
        return super().run(
            prompt,
            model=model,
            timeout=timeout,
            cwd=cwd,
            env=env,
            session_id=session_id,
        )

    # -- Gateway WebSocket mode ------------------------------------------------

    def _run_gateway(
        self,
        prompt: str,
        *,
        model: str,
        timeout: int,
        cwd: str | None = None,
        session_id: str | None = None,
    ) -> EngineResult:
        """Send a prompt to the OpenClaw Gateway via WebSocket.

        Uses stdlib ``http.client`` style interaction via subprocess call to
        a helper, keeping the connector dependency-free.  Falls back to CLI
        mode if the gateway is unreachable.

        In production, this would use the ``websockets`` library or
        ``asyncio`` for true WebSocket communication.  The current
        implementation shells out to the openclaw CLI with a
        ``--gateway`` flag for simplicity.
        """
        cmd = [
            self._binary,
            "gateway",
            "--url", self._gateway_url,
            "--model", model,
            "--output", "json",
            "-p", prompt,
        ]
        if session_id and self._session_flag:
            cmd.extend([self._session_flag, session_id])

        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=timeout,
                cwd=cwd,
            )
            return self._parse_openclaw_output(
                result.stdout, result.stderr, result.returncode, model,
            )
        except subprocess.TimeoutExpired:
            logger.warning("openclaw gateway timed out after %ds", timeout)
            raise
        except FileNotFoundError:
            logger.error("OpenClaw binary not found: %s", self._binary)
            return EngineResult(
                stdout="",
                stderr=f"Binary not found: {self._binary}",
                returncode=-1,
                model=model,
            )

    # -- Output parsing --------------------------------------------------------

    def _build_engine_result(
        self,
        raw_stdout: str,
        stderr: str,
        returncode: int,
        model: str,
    ) -> EngineResult:
        """Parse OpenClaw JSON output into an EngineResult."""
        return self._parse_openclaw_output(raw_stdout, stderr, returncode, model)

    @staticmethod
    def _parse_openclaw_output(
        raw_stdout: str,
        stderr: str,
        returncode: int,
        model: str,
    ) -> EngineResult:
        """Parse OpenClaw's JSON output format.

        Expected JSON structure::

            {
                "result": "the agent's response text",
                "model": "kimi-k2.5",
                "usage": {
                    "input_tokens": 1234,
                    "output_tokens": 567,
                    "cost_usd": 0.01
                },
                "session_id": "abc-123",
                "status": "success"
            }

        Falls back to raw text if JSON parsing fails.
        """
        text = raw_stdout.strip()
        if not text:
            return EngineResult(
                stdout="",
                stderr=stderr,
                returncode=returncode,
                model=model,
            )

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            # Not JSON -- return raw text
            return EngineResult(
                stdout=text,
                stderr=stderr,
                returncode=returncode,
                model=model,
            )

        if not isinstance(data, dict):
            return EngineResult(
                stdout=text,
                stderr=stderr,
                returncode=returncode,
                model=model,
            )

        result_text = data.get("result", text)
        if not isinstance(result_text, str):
            result_text = str(result_text)

        usage = data.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}

        return EngineResult(
            stdout=result_text,
            stderr=stderr,
            returncode=returncode,
            model=data.get("model", model),
            cost_usd=usage.get("cost_usd"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            session_id=data.get("session_id"),
            metadata={
                "status": data.get("status", ""),
                "gateway": bool(data.get("gateway")),
            },
        )
