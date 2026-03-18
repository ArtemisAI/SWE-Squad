"""Tests for Role-Based Access Control (agent_rbac.py)."""
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from src.swe_team.agent_rbac import (
    AgentRole,
    PermissionDeniedError,
    RBACEngine,
)


def _write_roles(tmp_dir: str, content: str) -> Path:
    p = Path(tmp_dir) / "roles.yaml"
    p.write_text(textwrap.dedent(content))
    return p


FULL_ROLES_YAML = """\
roles:
  claude-code:
    description: "Primary coding and orchestration agent"
    enabled: true
    permissions:
      - code_generation
      - code_review
      - pr_create
      - pr_merge
      - investigation
      - orchestration
      - commit
      - branch_create
      - triage
      - summarization
      - search
      - dashboard
    models: [haiku, sonnet, opus]
    merge_policy:
      cooldown_minutes: 30
      require_human_approval: true
      self_merge: false

  gemini-cli:
    description: "Investigation and summarization agent (read-only)"
    enabled: true
    permissions:
      - investigation
      - summarization
      - triage
      - search
      - dashboard
      - websearch
    deny:
      - code_generation
      - pr_create
      - pr_merge
      - commit
      - branch_create
      - code_review
    models: [gemini-2.5-flash-thinking, gemini-2.5-pro]

  openclaw:
    description: "Communication relay"
    enabled: true
    permissions:
      - messaging
      - notification
      - user_interaction
      - ticket_intake
    deny:
      - code_generation
      - code_review
      - pr_create
      - pr_merge
      - commit
      - investigation
      - orchestration
    models: []

  opencode:
    description: "Auxiliary agent (disabled)"
    enabled: false
    permissions:
      - investigation
      - code_review
    deny:
      - pr_merge
      - orchestration
    models: [deepseek-coder]

overrides:
  - rule: "LOW/MEDIUM bugs can be investigated by gemini-cli"
    condition:
      severity: [low, medium]
      task: investigation
    allow_agents: [gemini-cli]
  - rule: "Only claude-code generates code"
    condition:
      task: [code_generation, commit, pr_create, pr_merge]
    allow_agents: [claude-code]
    enforce: strict
"""


class TestAgentRBAC(unittest.TestCase):
    """Unit tests for the RBAC engine."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._roles_path = _write_roles(self._tmp, FULL_ROLES_YAML)
        self.engine = RBACEngine(roles_path=self._roles_path)

    # --- claude-code permissions ---

    def test_claude_code_code_generation_allowed(self):
        allowed, reason = self.engine.check_permission("claude-code", "code_generation")
        self.assertTrue(allowed)
        self.assertEqual(reason, "granted")

    def test_claude_code_pr_create_allowed(self):
        allowed, _ = self.engine.check_permission("claude-code", "pr_create")
        self.assertTrue(allowed)

    def test_claude_code_pr_merge_allowed(self):
        allowed, _ = self.engine.check_permission("claude-code", "pr_merge")
        self.assertTrue(allowed)

    def test_claude_code_commit_allowed(self):
        allowed, _ = self.engine.check_permission("claude-code", "commit")
        self.assertTrue(allowed)

    # --- gemini-cli explicit denies ---

    def test_gemini_cli_denied_code_generation(self):
        allowed, reason = self.engine.check_permission("gemini-cli", "code_generation")
        self.assertFalse(allowed)
        self.assertIn("explicitly denied", reason)

    def test_gemini_cli_denied_pr_create(self):
        allowed, _ = self.engine.check_permission("gemini-cli", "pr_create")
        self.assertFalse(allowed)

    def test_gemini_cli_denied_commit(self):
        allowed, _ = self.engine.check_permission("gemini-cli", "commit")
        self.assertFalse(allowed)

    def test_gemini_cli_investigation_allowed(self):
        allowed, _ = self.engine.check_permission("gemini-cli", "investigation")
        self.assertTrue(allowed)

    # --- openclaw denies ---

    def test_openclaw_denied_code_generation(self):
        allowed, reason = self.engine.check_permission("openclaw", "code_generation")
        self.assertFalse(allowed)
        self.assertIn("explicitly denied", reason)

    def test_openclaw_denied_investigation(self):
        allowed, _ = self.engine.check_permission("openclaw", "investigation")
        self.assertFalse(allowed)

    def test_openclaw_messaging_allowed(self):
        allowed, _ = self.engine.check_permission("openclaw", "messaging")
        self.assertTrue(allowed)

    # --- opencode disabled ---

    def test_opencode_disabled_denied_everything(self):
        allowed, reason = self.engine.check_permission("opencode", "investigation")
        self.assertFalse(allowed)
        self.assertIn("disabled", reason)

    def test_opencode_disabled_denied_code_review(self):
        allowed, reason = self.engine.check_permission("opencode", "code_review")
        self.assertFalse(allowed)
        self.assertIn("disabled", reason)

    # --- unknown agent deny-by-default ---

    def test_unknown_agent_denied(self):
        allowed, reason = self.engine.check_permission("rogue-bot", "code_generation")
        self.assertFalse(allowed)
        self.assertIn("Unknown agent", reason)

    # --- override: gemini-cli investigation for low severity ---

    def test_override_gemini_cli_investigation_low_severity(self):
        """gemini-cli already has investigation in base permissions, but
        the override also covers it — should be granted either way."""
        allowed, _ = self.engine.check_permission(
            "gemini-cli", "investigation", {"severity": "low"}
        )
        self.assertTrue(allowed)

    # --- override strict: only claude-code for code_generation ---

    def test_override_strict_blocks_non_claude_code_generation(self):
        """gemini-cli has code_generation explicitly denied, so it stays denied."""
        allowed, _ = self.engine.check_permission("gemini-cli", "code_generation")
        self.assertFalse(allowed)

    # --- enforce raises PermissionDeniedError ---

    def test_enforce_raises_on_denied(self):
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.engine.enforce("openclaw", "code_generation")
        self.assertIn("RBAC", str(ctx.exception))

    def test_enforce_passes_on_allowed(self):
        # Should not raise
        self.engine.enforce("claude-code", "code_generation")

    # --- missing roles file => deny-by-default ---

    def test_missing_roles_file_deny_by_default(self):
        engine = RBACEngine(roles_path=Path("/nonexistent/roles.yaml"))
        allowed, reason = engine.check_permission("claude-code", "code_generation")
        self.assertFalse(allowed)
        self.assertIn("Unknown agent", reason)

    # --- reload picks up changes ---

    def test_reload_picks_up_changes(self):
        # Initially opencode is disabled
        allowed, _ = self.engine.check_permission("opencode", "investigation")
        self.assertFalse(allowed)

        # Update the file to enable opencode
        data = yaml.safe_load(self._roles_path.read_text())
        data["roles"]["opencode"]["enabled"] = True
        self._roles_path.write_text(yaml.dump(data))

        self.engine.reload()

        allowed, reason = self.engine.check_permission("opencode", "investigation")
        self.assertTrue(allowed)
        self.assertEqual(reason, "granted")

    # --- list_roles and get_role ---

    def test_list_roles_returns_all(self):
        roles = self.engine.list_roles()
        self.assertEqual(set(roles.keys()), {"claude-code", "gemini-cli", "openclaw", "opencode"})

    def test_get_role_returns_none_for_unknown(self):
        self.assertIsNone(self.engine.get_role("nonexistent"))

    # --- AgentRole unit tests ---

    def test_agent_role_has_permission_deny_wins(self):
        role = AgentRole("test", {
            "enabled": True,
            "permissions": ["code_generation"],
            "deny": ["code_generation"],
        })
        self.assertFalse(role.has_permission("code_generation"))

    def test_agent_role_disabled_denies_all(self):
        role = AgentRole("test", {
            "enabled": False,
            "permissions": ["code_generation"],
        })
        self.assertFalse(role.has_permission("code_generation"))

    # --- deny-by-default for unlisted permission ---

    def test_unlisted_permission_denied(self):
        """A permission not in the list is denied even for claude-code."""
        allowed, reason = self.engine.check_permission("claude-code", "delete_repo")
        self.assertFalse(allowed)
        self.assertIn("does not have permission", reason)


if __name__ == "__main__":
    unittest.main()
