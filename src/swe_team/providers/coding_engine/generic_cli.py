"""Generic CLI coding engine -- universal adapter for any CLI coding tool.

Wraps any CLI binary (gemini, opencode, roo, cline, aider, etc.) as a
CodingEngine.  Configured entirely via constructor parameters -- no
tool-specific code needed.

Registered in swe_team.yaml under providers.coding_engine.provider: generic_cli.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult

logger = logging.getLogger(__name__)


class GenericCLIEngine:
    """Universal CLI coding engine adapter.

    Wraps any CLI tool (gemini, opencode, roo, cline, etc.) as a CodingEngine.
    Configured entirely via constructor params -- no tool-specific code needed.

    Parameters
    ----------
    binary:
        Path to the CLI binary (resolved via ``shutil.which`` if not absolute).
    default_model:
        Default model name passed via *model_flag*.
    default_timeout:
        Default subprocess timeout in seconds.
    args_template:
        Argument list template.  The placeholder ``{prompt}`` is replaced with
        the actual prompt text.  Example: ``["-p", "{prompt}"]``.
    model_flag:
        CLI flag used to specify the model (e.g. ``"--model"``).
    prompt_via:
        How to deliver the prompt to the subprocess:

        * ``"args"`` -- replace ``{prompt}`` in *args_template* (default).
        * ``"stdin"`` -- pipe the prompt through stdin.
        * ``"file"`` -- write the prompt to a temporary file and replace
          ``{prompt}`` in *args_template* with the file path.
    output_format:
        How to interpret stdout:

        * ``"text"`` -- treat raw stdout as the result (default).
        * ``"json"`` -- parse stdout as JSON and extract ``"result"`` key.
    session_flag:
        CLI flag for session/conversation support (e.g. ``"--session"``).
        When set, ``run()`` will append ``<session_flag> <session_id>`` to
        the command if a *session_id* is provided.
    env_vars:
        Extra environment variables merged into the subprocess environment.
    """

    def __init__(
        self,
        *,
        binary: str,
        default_model: str = "",
        default_timeout: int = 300,
        args_template: List[str] | None = None,
        model_flag: str = "--model",
        prompt_via: str = "args",
        output_format: str = "text",
        session_flag: str | None = None,
        env_vars: Dict[str, str] | None = None,
    ) -> None:
        if prompt_via not in ("args", "stdin", "file"):
            raise ValueError(
                f"prompt_via must be 'args', 'stdin', or 'file'; got {prompt_via!r}"
            )
        if output_format not in ("text", "json"):
            raise ValueError(
                f"output_format must be 'text' or 'json'; got {output_format!r}"
            )

        self._binary = binary
        self._default_model = default_model
        self._default_timeout = default_timeout
        self._args_template: List[str] = args_template or []
        self._model_flag = model_flag
        self._prompt_via = prompt_via
        self._output_format = output_format
        self._session_flag = session_flag
        self._env_vars: Dict[str, str] = env_vars or {}

    # -- CodingEngine protocol -------------------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier derived from the binary name."""
        return Path(self._binary).stem

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
        """Execute the CLI tool with *prompt* and return an EngineResult."""
        effective_model = model or self._default_model
        effective_timeout = timeout or self._default_timeout

        cmd = self._build_cmd(prompt, effective_model, session_id=session_id)
        stdin_input: str | None = None
        tmp_file: str | None = None

        if self._prompt_via == "stdin":
            stdin_input = prompt
        elif self._prompt_via == "file":
            # Write prompt to a temp file; the path replaces {prompt} in cmd
            fd, tmp_file = tempfile.mkstemp(suffix=".txt", prefix="swe_prompt_")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(prompt)
            except Exception:
                os.close(fd)
                raise
            cmd = [arg.replace("{prompt}", tmp_file) for arg in cmd]

        # Merge environment
        run_env: dict | None = None
        if self._env_vars or env:
            run_env = dict(os.environ)
            run_env.update(self._env_vars)
            if env:
                run_env.update(env)

        try:
            result = subprocess.run(
                cmd,
                input=stdin_input,
                text=True,
                capture_output=True,
                timeout=effective_timeout,
                cwd=cwd,
                env=run_env,
            )
            return self._build_engine_result(
                result.stdout, result.stderr, result.returncode, effective_model,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "%s timed out after %ds", self.name, effective_timeout,
            )
            raise
        except FileNotFoundError:
            logger.error("CLI binary not found: %s", self._binary)
            return EngineResult(
                stdout="",
                stderr=f"Binary not found: {self._binary}",
                returncode=-1,
                model=effective_model,
            )
        finally:
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.unlink(tmp_file)
                except OSError:
                    pass

    def health_check(self) -> bool:
        """Return True if the CLI binary is found."""
        return self.is_available()

    # -- Command builder -------------------------------------------------------

    def _build_cmd(
        self,
        prompt: str,
        model: str,
        *,
        session_id: str | None = None,
    ) -> List[str]:
        """Build the subprocess argument list."""
        cmd: List[str] = [self._binary]

        # Model flag
        if model and self._model_flag:
            cmd.extend([self._model_flag, model])

        # Session flag
        if session_id and self._session_flag:
            cmd.extend([self._session_flag, session_id])

        # Args template -- substitute {prompt} for "args" mode
        for arg in self._args_template:
            if self._prompt_via == "args":
                cmd.append(arg.replace("{prompt}", prompt))
            else:
                # For stdin/file modes, {prompt} in template is a placeholder
                # that gets resolved later (file) or is unused (stdin)
                cmd.append(arg)

        return cmd

    # -- Output parsing --------------------------------------------------------

    def _build_engine_result(
        self,
        raw_stdout: str,
        stderr: str,
        returncode: int,
        model: str,
    ) -> EngineResult:
        """Build an EngineResult from subprocess output."""
        text_result = raw_stdout.strip()

        if self._output_format == "json":
            text_result = self._parse_json_result(raw_stdout)

        return EngineResult(
            stdout=text_result,
            stderr=stderr,
            returncode=returncode,
            model=model,
        )

    @staticmethod
    def _parse_json_result(raw_stdout: str) -> str:
        """Extract the 'result' field from JSON stdout, or return raw text."""
        text = raw_stdout.strip()
        if not text:
            return ""
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
        if isinstance(data, dict):
            result = data.get("result", text)
            return str(result) if not isinstance(result, str) else result
        return text

    # -- Convenience -----------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the CLI binary exists on PATH or at the configured path."""
        return bool(shutil.which(self._binary) or Path(self._binary).exists())

    def model(self) -> str:
        """Return the default model name."""
        return self._default_model
