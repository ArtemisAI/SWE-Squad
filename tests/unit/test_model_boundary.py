"""
Unit tests for model boundary enforcement (SEC-68).

Validates that:
- Only Claude models can do code generation
- kimi-k2.5 and other blocked models are always rejected
- Read-only tasks (investigate, review) allow non-Claude models
- Unknown tasks default to Claude-only (fail-secure)
- Spoofed names (haiku-openai, sonnet-local) are rejected
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.swe_team.model_boundary import (
    AUTHORIZED_CODE_MODELS,
    BLOCKED_MODELS,
    enforce_code_generation_boundary,
    is_claude_model,
    validate_model_for_task,
)


# ---------------------------------------------------------------------------
# is_claude_model — exact allowlist checks
# ---------------------------------------------------------------------------

def test_haiku_is_claude():
    assert is_claude_model("haiku")


def test_sonnet_is_claude():
    assert is_claude_model("sonnet")


def test_opus_is_claude():
    assert is_claude_model("opus")


def test_claude_3_sonnet_is_claude():
    assert is_claude_model("claude-3-sonnet")


def test_claude_prefix_is_claude():
    assert is_claude_model("claude-4-opus")


def test_kimi_is_not_claude():
    assert not is_claude_model("kimi-k2.5")


def test_gemini_is_not_claude():
    assert not is_claude_model("gemini-2.5-flash")


def test_gpt_is_not_claude():
    assert not is_claude_model("gpt-4o")


def test_empty_string():
    assert not is_claude_model("")


def test_case_insensitive():
    assert is_claude_model("Claude-3-Sonnet")
    assert is_claude_model("HAIKU")


# Spoofed names must NOT pass — they are not in AUTHORIZED_CODE_MODELS
def test_haiku_openai_is_not_claude():
    assert not is_claude_model("haiku-openai")


def test_sonnet_local_is_not_claude():
    assert not is_claude_model("sonnet-local")


def test_claude_fake_is_not_claude():
    assert not is_claude_model("claude-fake")


def test_all_authorized_models_pass():
    """Every entry in AUTHORIZED_CODE_MODELS must pass is_claude_model."""
    for m in AUTHORIZED_CODE_MODELS:
        assert is_claude_model(m), f"Expected {m!r} to be recognized as a Claude model"


# ---------------------------------------------------------------------------
# validate_model_for_task
# ---------------------------------------------------------------------------

def test_claude_for_develop_allowed():
    allowed, _ = validate_model_for_task("sonnet", "develop")
    assert allowed


def test_claude_for_fix_allowed():
    allowed, _ = validate_model_for_task("haiku", "fix")
    assert allowed


def test_gemini_for_develop_blocked():
    allowed, reason = validate_model_for_task("gemini-flash", "develop")
    assert not allowed
    assert "not authorized" in reason.lower()


def test_kimi_for_develop_blocked():
    allowed, reason = validate_model_for_task("kimi-k2.5", "develop")
    assert not allowed
    assert "blocked" in reason.lower() or "permanently" in reason.lower()


def test_kimi_for_investigate_also_blocked():
    """kimi is permanently blocked for ALL tasks."""
    allowed, _ = validate_model_for_task("kimi-k2.5:cloud", "investigate")
    assert not allowed


def test_gemini_for_investigate_allowed():
    allowed, _ = validate_model_for_task("gemini-flash", "investigate")
    assert allowed


def test_gemini_for_review_allowed():
    allowed, _ = validate_model_for_task("gemini-flash", "review")
    assert allowed


def test_gemini_for_search_allowed():
    allowed, _ = validate_model_for_task("gemini-flash", "search")
    assert allowed


def test_unknown_task_claude_allowed():
    allowed, _ = validate_model_for_task("sonnet", "some_new_task")
    assert allowed


def test_unknown_task_gemini_blocked():
    allowed, _ = validate_model_for_task("gemini-flash", "some_new_task")
    assert not allowed


def test_code_word_boundary_triggers_gate():
    """Task string 'write code' must trigger the gate for non-Claude models."""
    allowed, reason = validate_model_for_task("gemini-flash", "write code")
    assert not allowed
    assert "not authorized" in reason.lower()


def test_prefix_not_misclassified_as_code_gen():
    """'prefix' does not contain a whole code-gen keyword — should hit fail-secure deny."""
    allowed, reason = validate_model_for_task("gemini-flash", "prefix")
    # Falls through to unknown-task fail-secure, NOT a code-gen boundary violation
    assert not allowed
    assert "not authorized" not in reason.lower()


def test_decode_not_misclassified_as_code_gen():
    """'decode' does not contain a whole code-gen keyword — should hit fail-secure deny."""
    allowed, reason = validate_model_for_task("gemini-flash", "decode")
    assert not allowed
    assert "not authorized" not in reason.lower()


# ---------------------------------------------------------------------------
# enforce_code_generation_boundary
# ---------------------------------------------------------------------------

def test_claude_passes():
    result = enforce_code_generation_boundary("sonnet", task="develop")
    assert result == "sonnet"


def test_kimi_raises():
    with pytest.raises(ValueError, match="MODEL BOUNDARY VIOLATION"):
        enforce_code_generation_boundary("kimi-k2.5", task="develop")


def test_gemini_for_code_raises():
    with pytest.raises(ValueError, match="MODEL BOUNDARY VIOLATION"):
        enforce_code_generation_boundary("gemini-flash", task="fix")


def test_blocked_model_always_raises():
    for model in BLOCKED_MODELS:
        with pytest.raises(ValueError, match="MODEL BOUNDARY VIOLATION"):
            enforce_code_generation_boundary(model, task="investigate")


# ---------------------------------------------------------------------------
# Blocked model variants
# ---------------------------------------------------------------------------

def test_kimi_variants_blocked():
    for variant in ["kimi", "kimi-k2", "kimi-k2.5", "kimi-k2.5:cloud", "moonshot"]:
        allowed, _ = validate_model_for_task(variant, "develop")
        assert not allowed, f"{variant} should be blocked"
