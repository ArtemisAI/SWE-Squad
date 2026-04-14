"""
{EngineName} CLI coding engine -- CodingEngine implementation for {EngineName}.

Copy this file to implement a new engine connector:

    cp _template.py your_engine.py

Then:
    1. Replace all {EngineName} / {engine_name} / {binary} placeholders.
    2. Choose your base class:
       - Subclass GenericCLIEngine if the engine is a simple CLI tool.
       - Implement CodingEngine protocol directly if you need custom I/O.
    3. Delete whichever base class you are NOT using (both are shown below).
    4. Register in __init__.py (see registration section at bottom of file).
    5. Write tests in tests/unit/test_{engine_name}_engine.py.

Registered in swe_team.yaml under providers.coding_engine.provider: {engine_name}.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

logger = logging.getLogger(__name__)


# =============================================================================
# OPTION A: Subclass GenericCLIEngine (recommended for most CLI tools)
#
# Use this when:
#   - The engine is a CLI binary invoked via subprocess
#   - You need custom defaults, safety filters, or output parsing
#   - The basic run/health_check flow from GenericCLIEngine is sufficient
#
# Examples in this codebase: GeminiCLIEngine (gemini.py)
# =============================================================================


class TemplateSubclassEngine(GenericCLIEngine):
    """Engine connector for {EngineName} CLI.

    Extends GenericCLIEngine with {engine_name}-specific defaults and
    output parsing.

    Implements the :class:`CodingEngine` protocol.

    Parameters
    ----------
    default_model:
        Default model identifier passed via the model flag.
        Example values: "gpt-4o", "gemini-2.5-pro", "claude-sonnet-4-20250514"
    default_timeout:
        Default subprocess timeout in seconds. Tier A engines may need
        longer timeouts (600+). Tier C utilities can use shorter (60-120).
    binary:
        Path to the CLI binary. If None, resolves via shutil.which().
    model_flag:
        CLI flag for model selection. Most engines use "--model".
        Set to "" if the engine does not support model selection.
    env_vars:
        Extra environment variables passed to the subprocess.
        Useful for API keys, proxy URLs, etc.
    session_flag:
        CLI flag for session/conversation tracking. Set to None if
        the engine does not support sessions.
    """

    def __init__(
        self,
        *,
        default_model: str = "default-model-name",  # CHANGE: set your default model
        default_timeout: int = 300,
        binary: str | None = None,
        model_flag: str = "--model",
        env_vars: Dict[str, str] | None = None,
        session_flag: str | None = None,
        # --- Add engine-specific constructor params below ---
        # example_param: bool = False,
    ) -> None:
        # Resolve binary: explicit path > shutil.which > hardcoded fallback
        resolved_binary = binary or shutil.which("{binary}") or "/usr/bin/{binary}"

        # Build the args template.
        #
        # This defines how the prompt is passed to the CLI.
        # Common patterns:
        #   ["-p", "{prompt}"]              -- prompt as flag argument
        #   ["--message", "{prompt}"]       -- aider style
        #   ["{prompt}"]                    -- positional argument
        #   []                              -- prompt via stdin (set prompt_via="stdin")
        args_template: List[str] = ["-p", "{prompt}"]  # CHANGE: match your engine's CLI

        # Choose prompt delivery method:
        #   "args"  -- substitute {prompt} in args_template (default)
        #   "stdin" -- pipe prompt to stdin
        #   "file"  -- write to temp file, substitute path in args_template
        prompt_via = "args"  # CHANGE: if your engine reads from stdin, use "stdin"

        # Choose output format:
        #   "text" -- treat stdout as plain text (default)
        #   "json" -- parse stdout as JSON, extract "result" key
        output_format = "text"  # CHANGE: if your engine outputs JSON, use "json"

        super().__init__(
            binary=resolved_binary,
            default_model=default_model,
            default_timeout=default_timeout,
            args_template=args_template,
            model_flag=model_flag,
            prompt_via=prompt_via,
            output_format=output_format,
            session_flag=session_flag,
            env_vars=env_vars,
        )

        # Store engine-specific params
        # self._example_param = example_param

    # -- CodingEngine protocol override ----------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier.

        Must return a unique lowercase string matching the registered name
        in __init__.py. This is used in logs, metrics, and the UI.
        """
        return "{engine_name}"  # CHANGE: your engine's identifier

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
        """Execute {EngineName} CLI with the given prompt.

        Override this method to add:
        - Safety filters (block sensitive keywords before sending)
        - Prompt preprocessing (template injection, context limits)
        - Output post-processing (extract structured data)
        - Custom error handling

        If you do not need any of the above, delete this method entirely
        and let GenericCLIEngine.run() handle everything.
        """
        # --- OPTIONAL: Safety filter ---
        # Uncomment and adapt if the engine has data retention concerns
        # (like Gemini's Google data retention policy).
        #
        # _UNSAFE_KEYWORDS = ("password", "secret", "token", "api_key")
        # prompt_lower = prompt.lower()
        # for kw in _UNSAFE_KEYWORDS:
        #     if kw in prompt_lower:
        #         logger.warning("%s: blocked sensitive keyword '%s'", self.name, kw)
        #         return EngineResult(
        #             stdout="",
        #             stderr=f"Blocked: prompt contains sensitive keyword '{kw}'",
        #             returncode=-1,
        #             model=model or self._default_model,
        #             metadata={"error_type": "safety_block", "blocked_keyword": kw},
        #         )

        # --- OPTIONAL: Prompt preprocessing ---
        # processed_prompt = self._preprocess(prompt)

        # Delegate to GenericCLIEngine.run() for subprocess execution
        return super().run(
            prompt,
            model=model,
            timeout=timeout,
            cwd=cwd,
            env=env,
            session_id=session_id,
        )

    # --- OPTIONAL: Custom output parsing ---
    # Override _build_engine_result() if you need to extract structured data
    # (cost, tokens, session IDs) from the engine's output.
    #
    # def _build_engine_result(self, raw_stdout, stderr, returncode, model):
    #     result = super()._build_engine_result(raw_stdout, stderr, returncode, model)
    #     # Parse additional fields from stdout/stderr
    #     result.cost_usd = self._extract_cost(stderr)
    #     return result


# =============================================================================
# OPTION B: Implement CodingEngine protocol directly (for complex engines)
#
# Use this when:
#   - The engine uses HTTP API instead of CLI subprocess
#   - The engine has complex JSON output with telemetry
#   - The engine needs session management (resume, conversation tracking)
#   - GenericCLIEngine's subprocess model does not fit
#
# Examples in this codebase: ClaudeCodeEngine (claude.py)
# =============================================================================


class TemplateDirectEngine:
    """Engine connector for {EngineName}.

    Implements the :class:`CodingEngine` protocol directly for engines
    that do not fit the GenericCLIEngine subprocess model (e.g., HTTP
    API-based agents, engines with complex session management).

    Parameters
    ----------
    default_model:
        Default model identifier.
    default_timeout:
        Default timeout in seconds.
    binary:
        Path to CLI binary (if applicable). Set to None for API-only engines.
    api_url:
        Base URL for API-based engines. Set to None for CLI engines.
    api_key:
        API key for authenticated engines. Prefer env var injection.
    """

    def __init__(
        self,
        *,
        default_model: str = "default-model-name",
        default_timeout: int = 300,
        binary: str | None = None,
        api_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._default_model = default_model
        self._default_timeout = default_timeout
        self._binary = binary or shutil.which("{binary}") or "/usr/bin/{binary}"
        self._api_url = api_url
        self._api_key = api_key

    # -- CodingEngine protocol -------------------------------------------------

    @property
    def name(self) -> str:  # noqa: D401
        """Provider identifier."""
        return "{engine_name}"  # CHANGE: your engine's identifier

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
        """Execute a prompt through {EngineName} and return an EngineResult.

        For CLI-based engines:
            Build a command list, call subprocess.run(), parse output.

        For API-based engines:
            Build an HTTP request, call urllib.request, parse JSON response.

        Must handle these error cases:
            - subprocess.TimeoutExpired: re-raise (callers handle retry)
            - FileNotFoundError: return EngineResult(returncode=-1)
            - HTTP errors: return EngineResult(returncode=-1) with error in stderr
        """
        effective_model = model or self._default_model
        effective_timeout = timeout or self._default_timeout

        # --- IMPLEMENT YOUR ENGINE LOGIC HERE ---
        #
        # For subprocess-based engines:
        #   import subprocess
        #   cmd = self._build_cmd(effective_model, session_id)
        #   try:
        #       result = subprocess.run(
        #           cmd, input=prompt, text=True, capture_output=True,
        #           timeout=effective_timeout, cwd=cwd, env=env,
        #       )
        #       return self._parse_result(result, effective_model)
        #   except subprocess.TimeoutExpired:
        #       logger.warning("%s timed out after %ds", self.name, effective_timeout)
        #       raise
        #   except FileNotFoundError:
        #       return EngineResult(stdout="", stderr=f"Binary not found: {self._binary}",
        #                           returncode=-1, model=effective_model)
        #
        # For API-based engines:
        #   import urllib.request, json
        #   payload = json.dumps({"prompt": prompt, "model": effective_model}).encode()
        #   req = urllib.request.Request(self._api_url, data=payload,
        #           headers={"Authorization": f"Bearer {self._api_key}"})
        #   try:
        #       with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
        #           data = json.loads(resp.read())
        #           return EngineResult(stdout=data["result"], stderr="", returncode=0,
        #                               model=effective_model, cost_usd=data.get("cost"))
        #   except Exception as e:
        #       return EngineResult(stdout="", stderr=str(e), returncode=-1,
        #                           model=effective_model)

        raise NotImplementedError("Replace this with your engine logic")

    def health_check(self) -> bool:
        """Return True if the engine binary/API is reachable.

        For CLI engines: check if the binary exists.
        For API engines: send a lightweight health probe.
        """
        return self.is_available()

    # -- Convenience -----------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the engine is operational.

        For CLI engines: check binary on PATH.
        For API engines: check if the API URL is reachable.
        """
        if self._binary:
            return bool(shutil.which(self._binary) or Path(self._binary).exists())
        # For API-based engines, consider pinging the health endpoint
        return self._api_url is not None

    def model(self) -> str:
        """Return the default model name."""
        return self._default_model


# =============================================================================
# Registration snippet (add to __init__.py)
# =============================================================================
#
# def _{engine_name}_factory(config: Dict[str, Any]) -> CodingEngine:
#     """Build a {EngineName} from config dict."""
#     from src.swe_team.providers.coding_engine.{engine_name} import TemplateSubclassEngine
#     # OR: from src.swe_team.providers.coding_engine.{engine_name} import TemplateDirectEngine
#
#     return TemplateSubclassEngine(
#         binary=config.get("binary") or None,
#         default_model=config.get("default_model", "default-model-name"),
#         default_timeout=int(config.get("timeout_seconds", 300)),
#         env_vars=config.get("env_vars") or None,
#         session_flag=config.get("session_flag") or None,
#     )
#
# register_engine("{engine_name}", _{engine_name}_factory)
