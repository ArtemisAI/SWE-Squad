"""Unit tests for SimpleRBACEngine (providers/rbac/simple.py).

Covers:
- Role-based permission grants and denials
- Unknown role defaults to deny-all
- check_permission returns (bool, str) matching RBACEngine protocol
- pr_merge is restricted to senior role only
- Integration with require_permission decorator
"""
from __future__ import annotations

import unittest

from src.swe_team.providers.rbac.simple import SimpleRBACEngine
from src.swe_team.rbac_middleware import PermissionDeniedError, require_permission


class TestSimpleRBACEngine(unittest.TestCase):
    """Test SimpleRBACEngine permission checks."""

    def test_developer_has_code_generation(self):
        engine = SimpleRBACEngine(team_role="developer")
        allowed, reason = engine.check_permission("swe_developer", "code_generation")
        self.assertTrue(allowed)
        self.assertIn("granted", reason)

    def test_developer_cannot_merge(self):
        engine = SimpleRBACEngine(team_role="developer")
        allowed, reason = engine.check_permission("swe_developer", "pr_merge")
        self.assertFalse(allowed)
        self.assertIn("pr_merge", reason)

    def test_investigator_has_investigation(self):
        engine = SimpleRBACEngine(team_role="investigator")
        allowed, reason = engine.check_permission("swe_investigator", "investigation")
        self.assertTrue(allowed)

    def test_investigator_cannot_generate_code(self):
        engine = SimpleRBACEngine(team_role="investigator")
        allowed, reason = engine.check_permission("swe_investigator", "code_generation")
        self.assertFalse(allowed)

    def test_reviewer_has_code_review(self):
        engine = SimpleRBACEngine(team_role="reviewer")
        allowed, reason = engine.check_permission("swe_reviewer", "code_review")
        self.assertTrue(allowed)

    def test_reviewer_cannot_generate_code(self):
        engine = SimpleRBACEngine(team_role="reviewer")
        allowed, reason = engine.check_permission("swe_reviewer", "code_generation")
        self.assertFalse(allowed)

    def test_senior_can_merge(self):
        engine = SimpleRBACEngine(team_role="senior")
        allowed, reason = engine.check_permission("swe_senior", "pr_merge")
        self.assertTrue(allowed)

    def test_full_cannot_merge(self):
        engine = SimpleRBACEngine(team_role="full")
        allowed, reason = engine.check_permission("any_agent", "pr_merge")
        self.assertFalse(allowed)

    def test_unknown_role_denies_all(self):
        engine = SimpleRBACEngine(team_role="nonexistent_role")
        allowed, reason = engine.check_permission("agent", "investigation")
        self.assertFalse(allowed)

    def test_role_property(self):
        engine = SimpleRBACEngine(team_role="developer")
        self.assertEqual(engine.role, "developer")

    def test_context_param_accepted(self):
        """check_permission accepts optional context dict (protocol compat)."""
        engine = SimpleRBACEngine(team_role="developer")
        allowed, reason = engine.check_permission(
            "swe_developer", "code_generation", context={"severity": "high"},
        )
        self.assertTrue(allowed)

    def test_orchestrator_has_orchestration(self):
        engine = SimpleRBACEngine(team_role="orchestrator")
        allowed, _ = engine.check_permission("swe_orchestrator", "orchestration")
        self.assertTrue(allowed)


class TestSimpleRBACWithDecorator(unittest.TestCase):
    """Integration test: SimpleRBACEngine wired into @require_permission."""

    def test_decorator_allows_with_engine(self):
        engine = SimpleRBACEngine(team_role="developer")

        class FakeAgent:
            _rbac_engine = engine
            _agent_name = "swe_developer"

            @require_permission("code_generation")
            def do_work(self):
                return "done"

        agent = FakeAgent()
        self.assertEqual(agent.do_work(), "done")

    def test_decorator_denies_with_engine(self):
        engine = SimpleRBACEngine(team_role="investigator")

        class FakeAgent:
            _rbac_engine = engine
            _agent_name = "swe_investigator"

            @require_permission("code_generation")
            def do_work(self):
                return "done"

        agent = FakeAgent()
        with self.assertRaises(PermissionDeniedError):
            agent.do_work()


if __name__ == "__main__":
    unittest.main()
