"""
Model boundary enforcement for SWE-Squad code generation.

SEC-68: kimi-k2.5 was used for unauthorized code generation via OpenClaw.
This module ensures ONLY Claude models are used for code generation tasks.
Non-Claude models (gemini, kimi, opencode, etc.) are restricted to
read-only tasks: investigation, review, search, dashboard.

Violation of model boundaries is logged as a CRITICAL security event.
"""
from __future__ import annotations

import logging
import re
from typing import FrozenSet

logger = logging.getLogger(__name__)

# Models authorized for CODE GENERATION (writing/editing code, creating PRs)
AUTHORIZED_CODE_MODELS: FrozenSet[str] = frozenset({
    "haiku",
    "sonnet",
    "opus",
    "claude-haiku",
    "claude-sonnet",
    "claude-opus",
    "claude-3-haiku",
    "claude-3-sonnet",
    "claude-3-opus",
    "claude-3.5-haiku",
    "claude-3.5-sonnet",
    "claude-4-haiku",
    "claude-4-sonnet",
    "claude-4-opus",
})

# Word-boundary pattern for detecting code-generation intent in task strings
_CODE_TASK_PATTERN = re.compile(
    r"\b(code|fix|develop|implement|write|generate|patch)\b",
    re.IGNORECASE,
)

# Models explicitly BLOCKED — known to have caused incidents
BLOCKED_MODELS: FrozenSet[str] = frozenset({
    "kimi",
    "kimi-k2",
    "kimi-k2.5",
    "kimi-k2.5:cloud",
    "moonshot",
})

# Tasks that require Claude (code generation, PR creation, merging)
CODE_GENERATION_TASKS: FrozenSet[str] = frozenset({
    "fix",
    "develop",
    "implement",
    "code_review",
    "pr_create",
    "merge",
    "refactor",
    "write_test",
})

# Tasks allowed for non-Claude models (read-only, no code changes)
READ_ONLY_TASKS: FrozenSet[str] = frozenset({
    "investigate",
    "review",
    "search",
    "dashboard",
    "websearch",
    "summarize",
    "triage",
})


def is_claude_model(model: str) -> bool:
    """Check if a model string refers to a Claude model.

    Returns True only if the model (case-insensitive) is in the exact allowlist.
    Prefix/substring matches are intentionally NOT accepted to prevent spoofing.
    """
    return model.strip().lower() in {m.lower() for m in AUTHORIZED_CODE_MODELS}


def validate_model_for_task(model: str, task: str) -> tuple[bool, str]:
    """Validate that the given model is authorized for the given task.

    Returns (allowed, reason). If not allowed, reason explains why.
    """
    model_lower = model.strip().lower()

    # Explicitly blocked models — NEVER allowed for anything
    for blocked in BLOCKED_MODELS:
        if blocked in model_lower:
            logger.critical(
                "SEC-68 BLOCKED MODEL: '%s' attempted for task '%s' — "
                "this model caused a security incident and is permanently banned",
                model, task,
            )
            return False, f"Model '{model}' is permanently blocked (SEC-68 incident)"

    # Code generation tasks REQUIRE Claude
    task_lower = task.strip().lower()
    is_code_task = (
        task_lower in CODE_GENERATION_TASKS
        or bool(_CODE_TASK_PATTERN.search(task_lower))
    )
    if is_code_task:
        if model_lower not in {m.lower() for m in AUTHORIZED_CODE_MODELS}:
            logger.critical(
                "SEC-68 MODEL BOUNDARY VIOLATION: non-Claude model '%s' "
                "attempted for code generation task '%s' — DENIED",
                model, task,
            )
            return False, (
                f"Model '{model}' is not authorized for code generation. "
                f"Only Claude models are permitted for task '{task}'"
            )

    # Read-only tasks — any model is fine (gemini for investigation, etc.)
    if task_lower in READ_ONLY_TASKS:
        return True, "ok"

    # Unknown task — default to Claude-only (fail-secure)
    if not is_claude_model(model):
        logger.warning(
            "SEC-68: Unknown task '%s' with non-Claude model '%s' — "
            "defaulting to DENY (fail-secure)",
            task, model,
        )
        return False, f"Unknown task '{task}' requires Claude model (fail-secure default)"

    return True, "ok"


def enforce_code_generation_boundary(model: str, task: str = "develop") -> str:
    """Enforce model boundary — returns the model if allowed, raises ValueError if not.

    Use this as a gate before any subprocess call that generates code.
    """
    allowed, reason = validate_model_for_task(model, task)
    if not allowed:
        raise ValueError(f"MODEL BOUNDARY VIOLATION: {reason}")
    return model
