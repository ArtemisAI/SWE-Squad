"""
Generic CLI adapter for A2A-compatible coding agents.

Wraps any CLI-based coding agent (Claude, Gemini, OpenCode, Codex, etc.)
as an A2A-compatible agent.  New agents can be added by providing the
command pattern and argument template — no subclassing required.

Follows the A2A protocol patterns:
  - Agent card with skills and capabilities
  - Task lifecycle (submitted -> working -> completed/failed)
  - Configurable timeout and model selection
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any, Dict, List, Optional

from src.a2a.adapters.base import AgentAdapter
from src.a2a.models import (
    AgentCard,
    AgentSkill,
    Artifact,
    DataPart,
    Message,
    Task,
    TaskState,
    TaskStatus,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120


class GenericCLIAdapter(AgentAdapter):
    """Wraps any coding agent CLI as an A2A-compatible agent.

    The adapter shells out to the configured command, passes the prompt
    via stdin (or as an argument), and captures stdout as the response.

    Parameters
    ----------
    name:
        Human-readable agent name (used in agent card).
    command:
        Path to the CLI binary (e.g. ``"/usr/bin/gemini"``).
    args_template:
        List of argument strings.  The placeholder ``{prompt}`` is
        replaced with the actual prompt text; ``{model}`` is replaced
        with the model name.  Example::

            ["-p", "{prompt}", "--model", "{model}"]

    default_model:
        Model name to use when none is specified.
    skills:
        List of ``AgentSkill``-compatible dicts for the agent card.
    timeout:
        Default subprocess timeout in seconds.
    prompt_via_stdin:
        If True (default), send the prompt via stdin instead of
        substituting ``{prompt}`` in args_template.
    env:
        Extra environment variables to pass to the subprocess.
    cwd:
        Working directory for the subprocess.
    provider:
        Provider metadata for the agent card (e.g. ``{"organization": "Google"}``).
    version:
        Version string for the agent card.
    priority:
        Priority level for agent selection (lower = higher priority).
    """

    def __init__(
        self,
        *,
        name: str,
        command: str,
        args_template: Optional[List[str]] = None,
        default_model: str = "",
        skills: Optional[List[Dict[str, Any]]] = None,
        timeout: int = _DEFAULT_TIMEOUT,
        prompt_via_stdin: bool = True,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        provider: Optional[Dict[str, str]] = None,
        version: str = "0.1.0",
        priority: int = 100,
    ) -> None:
        self._name = name
        self._command = command
        self._args_template = args_template or []
        self._default_model = default_model
        self._skills = skills or []
        self._timeout = timeout
        self._prompt_via_stdin = prompt_via_stdin
        self._env = env
        self._cwd = cwd
        self._provider = provider or {}
        self._version = version
        self._priority = priority

    # ------------------------------------------------------------------
    # AgentAdapter interface
    # ------------------------------------------------------------------

    def agent_card(self) -> AgentCard:
        """Return the A2A agent card for this CLI agent."""
        agent_skills = [
            AgentSkill(
                id=s.get("id", ""),
                name=s.get("name", ""),
                description=s.get("description", ""),
                tags=s.get("tags", []),
            )
            for s in self._skills
        ]
        return AgentCard(
            name=self._name,
            description=f"CLI-wrapped coding agent: {self._name}",
            url=f"local://{self._name}",
            version=self._version,
            skills=agent_skills,
            provider=self._provider,
        )

    async def handle_message(
        self, message: Message, session_id: Optional[str] = None
    ) -> Task:
        """Handle an A2A message by invoking the CLI agent."""
        task = Task(session_id=session_id)
        task.history.append(message)
        task.status = TaskStatus(state=TaskState.WORKING)

        prompt = self._extract_prompt(message)
        if not prompt:
            task.status = TaskStatus(
                state=TaskState.FAILED,
                message="No prompt text found in message",
            )
            return task

        try:
            stdout = self.invoke(prompt)
            task.status = TaskStatus(state=TaskState.COMPLETED)
            task.artifacts.append(Artifact(parts=[DataPart(data={
                "agent": self._name,
                "response": stdout,
            })]))
        except Exception as exc:
            task.status = TaskStatus(
                state=TaskState.FAILED,
                message=f"{self._name} CLI failed: {exc}",
            )
            logger.exception("%s adapter failed", self._name)

        return task

    # ------------------------------------------------------------------
    # Public invocation API
    # ------------------------------------------------------------------

    def invoke(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """Invoke the CLI agent with a prompt and return the output.

        Parameters
        ----------
        prompt:
            The prompt text to send to the agent.
        model:
            Override the default model.  Substituted into ``{model}``
            in the args template.
        timeout:
            Override the default timeout in seconds.

        Returns
        -------
        str
            The agent's stdout output (stripped).

        Raises
        ------
        RuntimeError
            If the CLI exits with a non-zero return code.
        subprocess.TimeoutExpired
            If the process exceeds the timeout.
        FileNotFoundError
            If the CLI binary is not found.
        """
        effective_model = model or self._default_model
        effective_timeout = timeout or self._timeout

        cmd = self._build_command(prompt, effective_model)
        stdin_data = prompt if self._prompt_via_stdin else None

        logger.info(
            "Invoking %s (model=%s, timeout=%ds)",
            self._name, effective_model or "default", effective_timeout,
        )
        start = time.monotonic()

        import os
        env = os.environ.copy() if self._env else None
        if self._env and env is not None:
            env.update(self._env)

        result = subprocess.run(
            cmd,
            input=stdin_data,
            text=True,
            capture_output=True,
            timeout=effective_timeout,
            cwd=self._cwd,
            env=env,
        )

        duration = time.monotonic() - start
        logger.info(
            "%s completed in %.1fs (rc=%d)",
            self._name, duration, result.returncode,
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or "CLI exited with non-zero status"
            raise RuntimeError(f"{self._name}: {error_msg}")

        return result.stdout.strip()

    def is_available(self) -> bool:
        """Check if the CLI binary is accessible.

        Returns True if the binary exists and is executable.
        """
        import os
        return os.path.isfile(self._command) and os.access(self._command, os.X_OK)

    # ------------------------------------------------------------------
    # Registration helper
    # ------------------------------------------------------------------

    def to_registry_card(self) -> Dict[str, Any]:
        """Return a dict suitable for ``AgentRegistry.register()``."""
        card = self.agent_card()
        return {
            "name": card.name,
            "url": card.url,
            "version": card.version,
            "provider": card.provider,
            "skills": [
                {"id": s.id, "name": s.name, "description": s.description, "tags": s.tags}
                for s in card.skills
            ],
            "status": "online" if self.is_available() else "offline",
            "priority": self._priority,
            "adapter_type": "generic_cli",
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_command(self, prompt: str, model: str) -> List[str]:
        """Build the full subprocess command list."""
        cmd = [self._command]
        for arg in self._args_template:
            rendered = arg.replace("{prompt}", prompt).replace("{model}", model)
            cmd.append(rendered)
        return cmd

    @staticmethod
    def _extract_prompt(message: Message) -> str:
        """Extract prompt text from an A2A message."""
        for part in message.parts:
            if isinstance(part, DataPart) and isinstance(part.data, dict):
                prompt = part.data.get("prompt") or part.data.get("text") or part.data.get("message")
                if prompt:
                    return str(prompt)
            if isinstance(part, str):
                return part
        return ""
