"""
Unit tests for model boundary enforcement (SEC-68).

Validates that:
- Only Claude models can do code generation
- kimi-k2.5 and other blocked models are always rejected
- Read-only tasks (investigate, review) allow non-Claude models
- Unknown tasks default to Claude-only (fail-secure)
"""
from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.swe_team.model_boundary import (
    is_claude_model,
    validate_model_for_task,
    enforce_code_generation_boundary,
    AUTHORIZED_CODE_MODELS,
    BLOCKED_MODELS,
)


class TestIsClaudeModel(unittest.TestCase):
    """Test Claude model identification."""

    def test_haiku_is_claude(self):
        assert is_claude_model("haiku")

    def test_sonnet_is_claude(self):
        assert is_claude_model("sonnet")

    def test_opus_is_claude(self):
        assert is_claude_model("opus")

    def test_claude_3_sonnet_is_claude(self):
        assert is_claude_model("claude-3-sonnet")

    def test_claude_prefix_is_claude(self):
        assert is_claude_model("claude-4-opus")

    def test_kimi_is_not_claude(self):
        assert not is_claude_model("kimi-k2.5")

    def test_gemini_is_not_claude(self):
        assert not is_claude_model("gemini-2.5-flash")

    def test_gpt_is_not_claude(self):
        assert not is_claude_model("gpt-4o")

    def test_empty_string(self):
        assert not is_claude_model("")

    def test_case_insensitive(self):
        assert is_claude_model("Claude-3-Sonnet")
        assert is_claude_model("HAIKU")


class TestValidateModelForTask(unittest.TestCase):
    """Test model-task validation matrix."""

    # Code generation tasks — Claude only
    def test_claude_for_develop_allowed(self):
        allowed, _ = validate_model_for_task("sonnet", "develop")
        assert allowed

    def test_claude_for_fix_allowed(self):
        allowed, _ = validate_model_for_task("haiku", "fix")
        assert allowed

    def test_gemini_for_develop_blocked(self):
        allowed, reason = validate_model_for_task("gemini-flash", "develop")
        assert not allowed
        assert "not authorized" in reason.lower()

    def test_kimi_for_develop_blocked(self):
        allowed, reason = validate_model_for_task("kimi-k2.5", "develop")
        assert not allowed
        assert "blocked" in reason.lower() or "permanently" in reason.lower()

    def test_kimi_for_investigate_also_blocked(self):
        """kimi is permanently blocked for ALL tasks."""
        allowed, reason = validate_model_for_task("kimi-k2.5:cloud", "investigate")
        assert not allowed

    # Read-only tasks — any non-blocked model OK
    def test_gemini_for_investigate_allowed(self):
        allowed, _ = validate_model_for_task("gemini-flash", "investigate")
        assert allowed

    def test_gemini_for_review_allowed(self):
        allowed, _ = validate_model_for_task("gemini-flash", "review")
        assert allowed

    def test_gemini_for_search_allowed(self):
        allowed, _ = validate_model_for_task("gemini-flash", "search")
        assert allowed

    # Unknown tasks — fail-secure (Claude only)
    def test_unknown_task_claude_allowed(self):
        allowed, _ = validate_model_for_task("sonnet", "some_new_task")
        assert allowed

    def test_unknown_task_gemini_blocked(self):
        allowed, _ = validate_model_for_task("gemini-flash", "some_new_task")
        assert not allowed


class TestEnforceCodeGenerationBoundary(unittest.TestCase):
    """Test the enforcement gate function."""

    def test_claude_passes(self):
        result = enforce_code_generation_boundary("sonnet", task="develop")
        assert result == "sonnet"

    def test_kimi_raises(self):
        with self.assertRaises(ValueError) as ctx:
            enforce_code_generation_boundary("kimi-k2.5", task="develop")
        assert "MODEL BOUNDARY VIOLATION" in str(ctx.exception)

    def test_gemini_for_code_raises(self):
        with self.assertRaises(ValueError) as ctx:
            enforce_code_generation_boundary("gemini-flash", task="fix")
        assert "MODEL BOUNDARY VIOLATION" in str(ctx.exception)

    def test_blocked_model_always_raises(self):
        for model in BLOCKED_MODELS:
            with self.assertRaises(ValueError, msg=f"{model} should be blocked"):
                enforce_code_generation_boundary(model, task="investigate")


class TestBlockedModels(unittest.TestCase):
    """Ensure all known-bad models are in the blocklist."""

    def test_kimi_variants_blocked(self):
        for variant in ["kimi", "kimi-k2", "kimi-k2.5", "kimi-k2.5:cloud", "moonshot"]:
            allowed, _ = validate_model_for_task(variant, "develop")
            assert not allowed, f"{variant} should be blocked"


if __name__ == "__main__":
    unittest.main()
