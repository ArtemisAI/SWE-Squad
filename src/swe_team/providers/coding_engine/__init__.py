"""Coding engine provider registry.

Resolves engine name → CodingEngine instance from config, so the runner
never hardcodes ``ClaudeCodeEngine`` directly.

Usage::

    engine = resolve_engine("claude", config_dict)
    engine = resolve_engine("gemini", config_dict)  # future
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.swe_team.providers.coding_engine.base import CodingEngine
from src.swe_team.providers.schema import ProviderParameter

logger = logging.getLogger(__name__)

# Registry of provider name → factory callable.
# Each factory receives (config: dict) and returns a CodingEngine.
_REGISTRY: Dict[str, Any] = {}
_PARAMETER_SCHEMAS: Dict[str, list[ProviderParameter]] = {}

_DEFAULT_ENGINE_PARAMETERS: list[ProviderParameter] = [
    {
        "name": "default_model",
        "type": "string",
        "required": False,
        "description": "Default model used by this coding engine",
    },
    {
        "name": "timeout_seconds",
        "type": "number",
        "required": False,
        "default": 300,
        "description": "Execution timeout in seconds",
    },
]


def register_engine(
    name: str,
    factory: Any,
    *,
    parameters: Optional[list[ProviderParameter]] = None,
) -> None:
    """Register a coding engine factory by name."""
    _REGISTRY[name] = factory
    _PARAMETER_SCHEMAS[name] = list(parameters or _DEFAULT_ENGINE_PARAMETERS)


def _claude_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a ClaudeCodeEngine from config dict."""
    from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine

    return ClaudeCodeEngine(
        default_model=config.get("default_model", "sonnet"),
        default_timeout=int(config.get("timeout_seconds", 300)),
        binary=config.get("claude_path") or None,
        allowed_tools=config.get("allowed_tools") or None,
        dangerously_skip_permissions=config.get("skip_permissions", True),
    )


def _generic_cli_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a GenericCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.generic_cli import GenericCLIEngine

    return GenericCLIEngine(
        binary=config.get("binary", "/usr/bin/false"),
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        args_template=config.get("args_template") or None,
        model_flag=config.get("model_flag", "--model"),
        prompt_via=config.get("prompt_via", "args"),
        output_format=config.get("output_format", "text"),
        session_flag=config.get("session_flag") or None,
        env_vars=config.get("env_vars") or None,
    )


def _copilot_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a CopilotCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.copilot import CopilotCLIEngine

    return CopilotCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 600)),
        env_vars=config.get("env_vars") or None,
        allow_all=config.get("allow_all", True),
        no_ask_user=config.get("no_ask_user", True),
        silent=config.get("silent", True),
        no_auto_update=config.get("no_auto_update", True),
        no_custom_instructions=config.get("no_custom_instructions", False),
        autopilot=config.get("autopilot", True),
        max_autopilot_continues=config.get("max_autopilot_continues"),
        effort=config.get("effort"),
    )


def _aider_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build an AiderEngine from config dict."""
    from src.swe_team.providers.coding_engine.aider import AiderEngine

    return AiderEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", "sonnet"),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
        auto_commits=config.get("auto_commits", False),
        no_git=config.get("no_git", True),
        yes_always=config.get("yes_always", True),
        edit_format=config.get("edit_format") or None,
    )


def _opencode_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build an OpenCodeEngine from config dict."""
    from src.swe_team.providers.coding_engine.opencode import OpenCodeEngine

    return OpenCodeEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
        auto_approve=config.get("auto_approve", True),
        output_json=config.get("output_json", False),
    )


def _gemini_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a GeminiCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.gemini import GeminiCLIEngine

    return GeminiCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", "gemini-2.5-pro"),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
        session_flag=config.get("session_flag") or None,
        sandbox_mode=config.get("sandbox_mode", False),
    )


def _codex_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a CodexCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.codex import CodexCLIEngine

    return CodexCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", "o4-mini"),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
        approval_mode=config.get("approval_mode", "full-auto"),
        sandbox=config.get("sandbox") or None,
        quiet=config.get("quiet", True),
    )


def _amazon_q_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build an AmazonQCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.amazon_q import AmazonQCLIEngine

    return AmazonQCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
        trust_all_tools=config.get("trust_all_tools", True),
        profile=config.get("profile") or None,
    )


def _bolt_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a BoltCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.bolt import BoltCLIEngine

    return BoltCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        model_flag=config.get("model_flag", ""),
        args_template=config.get("args_template") or None,
        env_vars=config.get("env_vars") or None,
    )


def _windsurf_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a WindsurfCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.windsurf import WindsurfCLIEngine

    return WindsurfCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        model_flag=config.get("model_flag", ""),
        args_template=config.get("args_template") or None,
        env_vars=config.get("env_vars") or None,
        cascade_mode=config.get("cascade_mode", False),
    )


def _sweep_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a SweepCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.sweep import SweepCLIEngine

    return SweepCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        model_flag=config.get("model_flag", ""),
        args_template=config.get("args_template") or None,
        env_vars=config.get("env_vars") or None,
        repo=config.get("repo") or None,
    )


def _codegpt_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a CodeGPTCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.codegpt import CodeGPTCLIEngine

    return CodeGPTCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        model_flag=config.get("model_flag", "--model"),
        args_template=config.get("args_template") or None,
        env_vars=config.get("env_vars") or None,
        provider=config.get("provider") or None,
    )


def _sgpt_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a SgpTCLEngine from config dict."""
    from src.swe_team.providers.coding_engine.sgpt import SgpTCLEngine

    return SgpTCLEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        model_flag=config.get("model_flag", "--model"),
        args_template=config.get("args_template") or None,
        env_vars=config.get("env_vars") or None,
    )


def _pipeline_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a PipelineEngine from config dict."""
    from src.swe_team.providers.coding_engine.pipeline_engine import PipelineEngine

    return PipelineEngine(
        workflow_name=config.get("workflow_name", "default"),
        default_model=config.get("default_model", "sonnet"),
        default_timeout=int(config.get("timeout_seconds", 300)),
        stages=config.get("stages", []),
        stop_on_first_failure=config.get("stop_on_first_failure", True),
        continue_on_skip=config.get("continue_on_skip", True),
    )


def _roo_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a RooCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.roo import RooCLIEngine

    return RooCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
    )


def _cline_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a ClineCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.cline import ClineCLIEngine

    return ClineCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        auto_approve=bool(config.get("auto_approve", True)),
        no_edit=bool(config.get("no_edit", False)),
        output_format=config.get("output_format", "json"),
        env_vars=config.get("env_vars") or None,
    )


def _cursor_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a CursorCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.cursor import CursorCLIEngine

    return CursorCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
    )


def _junie_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a JunieCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.junie import JunieCLIEngine

    return JunieCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
    )


def _goose_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a GooseCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.goose import GooseCLIEngine

    return GooseCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
        recipe=config.get("recipe") or None,
    )


def _kilo_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a KiloCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.kilo import KiloCLIEngine

    return KiloCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
    )


# Register built-in engines
register_engine(
    "claude",
    _claude_factory,
    parameters=[
        *_DEFAULT_ENGINE_PARAMETERS,
        {
            "name": "claude_path",
            "type": "string",
            "required": False,
            "description": "Path to claude CLI binary",
        },
        {
            "name": "allowed_tools",
            "type": "array",
            "required": False,
            "description": "Allowed tool names",
        },
        {
            "name": "skip_permissions",
            "type": "boolean",
            "required": False,
            "default": True,
            "description": "Whether to bypass CLI permission prompts",
        },
    ],
)
register_engine("generic_cli", _generic_cli_factory)
register_engine("aider", _aider_factory)
register_engine("codex", _codex_factory)
register_engine("copilot", _copilot_factory)
register_engine("gemini", _gemini_factory)
register_engine("opencode", _opencode_factory)
register_engine("amazon_q", _amazon_q_factory)
register_engine("bolt", _bolt_factory)
register_engine("windsurf", _windsurf_factory)
register_engine("sweep", _sweep_factory)
register_engine("codegpt", _codegpt_factory)
register_engine("sgpt", _sgpt_factory)
register_engine("pipeline", _pipeline_factory)
register_engine("roo", _roo_factory)
register_engine("cline", _cline_factory)
register_engine("cursor", _cursor_factory)
register_engine("junie", _junie_factory)
register_engine("goose", _goose_factory)
register_engine("kilo", _kilo_factory)


def _antigravity_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build an AntigravityCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.antigravity import AntigravityCLIEngine

    return AntigravityCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", "gemini-2.5-pro"),
        default_timeout=int(config.get("timeout_seconds", 300)),
        model_flag=config.get("model_flag", "--model"),
        env_vars=config.get("env_vars") or None,
    )


def _jules_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a JulesCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.jules import JulesCLIEngine

    return JulesCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
    )


def _gorilla_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a GorillaCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.gorilla import GorillaCLIEngine

    return GorillaCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
    )


def _openhands_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build an OpenHandsEngine from config dict."""
    from src.swe_team.providers.coding_engine.openhands import OpenHandsEngine

    return OpenHandsEngine(
        api_url=config.get("api_url", "http://localhost:3000"),
        api_key=config.get("api_key", ""),
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
    )


def _devin_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a DevinEngine from config dict."""
    from src.swe_team.providers.coding_engine.devin import DevinEngine

    return DevinEngine(
        api_url=config.get("api_url", "https://api.devin.ai"),
        api_key=config.get("api_key", ""),
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
    )


def _pi_agents_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build a PiAgentsCLIEngine from config dict."""
    from src.swe_team.providers.coding_engine.pi_agents import PiAgentsCLIEngine

    return PiAgentsCLIEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", ""),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
    )


def _openclaw_factory(config: Dict[str, Any]) -> CodingEngine:
    """Build an OpenClawEngine from config dict."""
    from src.swe_team.providers.coding_engine.openclaw import OpenClawEngine

    return OpenClawEngine(
        binary=config.get("binary") or None,
        default_model=config.get("default_model", "kimi-k2.5"),
        default_timeout=int(config.get("timeout_seconds", 300)),
        env_vars=config.get("env_vars") or None,
        session_flag=config.get("session_flag", "--session"),
        gateway_url=config.get("gateway_url") or None,
    )


register_engine("openclaw", _openclaw_factory)
register_engine("antigravity", _antigravity_factory)
register_engine("jules", _jules_factory)
register_engine("gorilla", _gorilla_factory)
register_engine("openhands", _openhands_factory)
register_engine("devin", _devin_factory)
register_engine("pi_agents", _pi_agents_factory)


def resolve_engine(
    provider_name: str,
    config: Optional[Dict[str, Any]] = None,
) -> CodingEngine:
    """Resolve a coding engine by provider name.

    Args:
        provider_name: Engine name (e.g. 'claude', 'gemini', 'opencode').
                       Must be registered in the engine registry.
        config: Provider-specific config dict (from swe_team.yaml
                ``providers.coding_engine``).

    Returns:
        A configured CodingEngine instance.

    Raises:
        ValueError: If the provider name is not registered.
    """
    config = config or {}
    factory = _REGISTRY.get(provider_name)
    if factory is None:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown coding engine provider '{provider_name}'. "
            f"Available: {available}"
        )
    logger.info("Resolving coding engine: %s", provider_name)
    return factory(config)


def list_engines() -> list[str]:
    """Return sorted list of registered engine provider names."""
    return sorted(_REGISTRY.keys())


def get_engine_parameters(provider_name: str) -> list[ProviderParameter]:
    """Return provider parameter schema for dynamic config forms."""
    if provider_name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown coding engine provider '{provider_name}'. "
            f"Available: {available}"
        )
    return list(_PARAMETER_SCHEMAS.get(provider_name, []))


def list_engine_parameters() -> Dict[str, list[ProviderParameter]]:
    """Return parameter schemas for all registered coding engines."""
    return {
        name: list(_PARAMETER_SCHEMAS.get(name, []))
        for name in sorted(_REGISTRY.keys())
    }


# ---------------------------------------------------------------------------
# Tier-aware engine resolution
# ---------------------------------------------------------------------------


def resolve_engine_by_tier(
    tier_name: str,
    tiers_config: Dict[str, Any],
) -> Optional[str]:
    """Resolve engine provider name from tier configuration.

    Args:
        tier_name: Tier name (e.g., "premium", "standard", "economy").
        tiers_config: Engine tiers configuration from swe_team.yaml.

    Returns:
        Engine provider name (e.g., "claude", "claudez"), or None if tier not found.
    """
    tier_config = tiers_config.get(tier_name)
    if not tier_config:
        logger.warning("Engine tier %s not found in config", tier_name)
        return None
    return tier_config.get("provider")


def resolve_engine_with_fallback(
    primary_tier: str,
    tiers_config: Dict[str, Any],
    engine_config: Optional[Dict[str, Any]] = None,
) -> tuple[str, CodingEngine]:
    """Resolve an engine with automatic fallback through tier chain.

    Tries the primary tier, then follows fallback chain until an available
    engine is found.

    Args:
        primary_tier: Primary tier name to start with.
        tiers_config: Engine tiers configuration.
        engine_config: Provider-specific config dict for engine factory.

    Returns:
        Tuple of (tier_name, CodingEngine instance).

    Raises:
        ValueError: If no available engine is found in the fallback chain.
    """
    engine_config = engine_config or {}

    # Follow fallback chain
    current_tier = primary_tier
    visited = set()

    while current_tier and current_tier not in visited:
        visited.add(current_tier)
        provider_name = resolve_engine_by_tier(current_tier, tiers_config)

        if provider_name and provider_name in _REGISTRY:
            try:
                engine = resolve_engine(provider_name, engine_config)
                logger.info(
                    "Resolved engine %s for tier %s",
                    provider_name,
                    current_tier,
                )
                return current_tier, engine
            except Exception as e:
                logger.warning(
                    "Failed to create engine %s for tier %s: %s",
                    provider_name,
                    current_tier,
                    e,
                )

        # Move to fallback tier
        tier_config = tiers_config.get(current_tier, {})
        current_tier = tier_config.get("fallback")

    raise ValueError(
        f"No available engine found for tier chain starting at {primary_tier}. "
        f"Tried: {', '.join(visited)}"
    )
