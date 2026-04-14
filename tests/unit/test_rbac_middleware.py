"""Unit tests for rbac_middleware.py.

Covers:
- require_permission decorator blocks unauthorized access
- require_permission decorator allows authorized access
- require_permission skips check when no RBAC engine present (backward compat)
- require_sandbox decorator blocks paths outside sandbox
- require_sandbox decorator allows paths inside sandbox
- RBACContext audit logging (enter/exit entries)
- fail_action modes: raise, return_none, log_only
"""

from __future__ import annotations

import textwrap
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.swe_team.rbac_middleware import (
    RBACContext,
    SandboxViolationError,
    PermissionDeniedError,
    require_permission,
    require_sandbox,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rbac_engine(*, allowed: bool = True, reason: str = "granted") -> MagicMock:
    """Build a mock RBACEngine that returns a fixed (allowed, reason) pair."""
    engine = MagicMock()
    engine.check_permission.return_value = (allowed, reason)
    return engine


class _AgentWithRBAC:
    """Minimal agent stub that has _rbac_engine and _agent_name."""

    def __init__(self, rbac_engine, agent_name: str = "test_agent"):
        self._rbac_engine = rbac_engine
        self._agent_name = agent_name
        self.called = False

    @require_permission("code_generation")
    def do_work(self, value: int = 0) -> int:
        self.called = True
        return value + 1

    @require_permission("code_generation", fail_action="return_none")
    def do_work_return_none(self) -> str:
        self.called = True
        return "done"

    @require_permission("code_generation", fail_action="log_only")
    def do_work_log_only(self) -> str:
        self.called = True
        return "done"


class _AgentNoRBAC:
    """Agent stub with no RBAC attributes (backward compat scenario)."""

    def __init__(self):
        self.called = False

    @require_permission("code_generation")
    def do_work(self) -> str:
        self.called = True
        return "done"


class _AgentPartialRBAC:
    """Agent with _rbac_engine but no _agent_name."""

    def __init__(self, rbac_engine):
        self._rbac_engine = rbac_engine
        self.called = False

    @require_permission("code_generation")
    def do_work(self) -> str:
        self.called = True
        return "done"


class _AgentWithSandbox:
    """Agent stub that has _sandbox_paths and optionally _repo_root."""

    def __init__(self, sandbox_paths, repo_root=None):
        self._sandbox_paths = sandbox_paths
        if repo_root is not None:
            self._repo_root = repo_root
        self.called = False

    @require_sandbox
    def do_work(self) -> str:
        self.called = True
        return "done"


# ---------------------------------------------------------------------------
# Tests: require_permission — authorization
# ---------------------------------------------------------------------------

class TestRequirePermissionAuthorized(unittest.TestCase):
    """Decorator allows execution when permission is granted."""

    def test_executes_method_on_allow(self):
        engine = _make_rbac_engine(allowed=True)
        agent = _AgentWithRBAC(engine)
        result = agent.do_work(5)
        self.assertTrue(agent.called)
        self.assertEqual(result, 6)

    def test_calls_rbac_with_correct_task(self):
        engine = _make_rbac_engine(allowed=True)
        agent = _AgentWithRBAC(engine, agent_name="my_agent")
        agent.do_work()
        engine.check_permission.assert_called_once_with("my_agent", "code_generation")

    def test_returns_method_return_value(self):
        engine = _make_rbac_engine(allowed=True)
        agent = _AgentWithRBAC(engine)
        self.assertEqual(agent.do_work(10), 11)


# ---------------------------------------------------------------------------
# Tests: require_permission — denial
# ---------------------------------------------------------------------------

class TestRequirePermissionDenied(unittest.TestCase):
    """Decorator blocks execution and raises PermissionDeniedError by default."""

    def test_raises_on_deny_default(self):
        engine = _make_rbac_engine(allowed=False, reason="not in permissions list")
        agent = _AgentWithRBAC(engine)
        with self.assertRaises(PermissionDeniedError) as ctx:
            agent.do_work()
        self.assertIn("code_generation", str(ctx.exception))
        self.assertFalse(agent.called)

    def test_return_none_on_deny(self):
        engine = _make_rbac_engine(allowed=False, reason="denied")
        agent = _AgentWithRBAC(engine)
        result = agent.do_work_return_none()
        self.assertIsNone(result)
        self.assertFalse(agent.called)

    def test_log_only_executes_on_deny(self):
        engine = _make_rbac_engine(allowed=False, reason="denied")
        agent = _AgentWithRBAC(engine)
        # log_only: method still executes even when denied
        result = agent.do_work_log_only()
        self.assertEqual(result, "done")
        self.assertTrue(agent.called)

    def test_error_message_includes_agent_and_task(self):
        engine = _make_rbac_engine(allowed=False, reason="no permission")
        agent = _AgentWithRBAC(engine, agent_name="swe_developer")
        with self.assertRaises(PermissionDeniedError) as ctx:
            agent.do_work()
        msg = str(ctx.exception)
        self.assertIn("swe_developer", msg)
        self.assertIn("code_generation", msg)


# ---------------------------------------------------------------------------
# Tests: require_permission — backward compatibility (no RBAC engine)
# ---------------------------------------------------------------------------

class TestRequirePermissionBackwardCompat(unittest.TestCase):
    """Decorator skips check gracefully when RBAC attrs are absent."""

    def test_no_rbac_engine_skips_check(self):
        agent = _AgentNoRBAC()
        result = agent.do_work()
        self.assertTrue(agent.called)
        self.assertEqual(result, "done")

    def test_engine_but_no_agent_name_skips_check(self):
        engine = _make_rbac_engine(allowed=False)  # would deny if called
        agent = _AgentPartialRBAC(engine)
        result = agent.do_work()
        # No agent_name → skip check entirely, method runs
        self.assertTrue(agent.called)
        self.assertEqual(result, "done")
        engine.check_permission.assert_not_called()

    def test_no_rbac_attrs_does_not_raise(self):
        """Plain object with no RBAC attributes — must not raise."""
        agent = _AgentNoRBAC()
        try:
            agent.do_work()
        except PermissionDeniedError:
            self.fail("PermissionDeniedError raised unexpectedly without RBAC engine")


# ---------------------------------------------------------------------------
# Tests: require_permission — invalid fail_action
# ---------------------------------------------------------------------------

class TestRequirePermissionInvalidFailAction(unittest.TestCase):
    def test_invalid_fail_action_raises_value_error(self):
        with self.assertRaises(ValueError):
            require_permission("task", fail_action="invalid_action")


# ---------------------------------------------------------------------------
# Tests: require_sandbox
# ---------------------------------------------------------------------------

class TestRequireSandbox(unittest.TestCase):
    """Decorator blocks execution when cwd is outside sandbox paths."""

    def test_allows_path_inside_sandbox(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sandbox = Path(tmpdir)
            repo_root = sandbox / "projects" / "myrepo"
            repo_root.mkdir(parents=True)
            agent = _AgentWithSandbox([sandbox], repo_root=repo_root)
            result = agent.do_work()
            self.assertTrue(agent.called)
            self.assertEqual(result, "done")

    def test_blocks_path_outside_sandbox(self):
        with tempfile.TemporaryDirectory() as sandbox_dir:
            with tempfile.TemporaryDirectory() as outside_dir:
                sandbox = Path(sandbox_dir)
                outside = Path(outside_dir)
                agent = _AgentWithSandbox([sandbox], repo_root=outside)
                with self.assertRaises(SandboxViolationError) as ctx:
                    agent.do_work()
                self.assertFalse(agent.called)
                self.assertIn(str(outside.resolve()), str(ctx.exception))

    def test_no_sandbox_paths_skips_check(self):
        """Empty sandbox list → check skipped, method executes."""
        agent = _AgentWithSandbox([], repo_root=Path("/any/path"))
        result = agent.do_work()
        self.assertTrue(agent.called)
        self.assertEqual(result, "done")

    def test_no_sandbox_attr_skips_check(self):
        """No _sandbox_paths attribute → check skipped."""

        class _NoBoundary:
            called = False

            @require_sandbox
            def do_work(self):
                self.called = True
                return "done"

        agent = _NoBoundary()
        result = agent.do_work()
        self.assertTrue(agent.called)
        self.assertEqual(result, "done")

    def test_allows_sandbox_is_exact_repo_root(self):
        """sandbox_path == repo_root is valid (root of sandbox)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sandbox = Path(tmpdir)
            agent = _AgentWithSandbox([sandbox], repo_root=sandbox)
            result = agent.do_work()
            self.assertTrue(agent.called)

    def test_multiple_sandboxes_matches_second(self):
        with tempfile.TemporaryDirectory() as dir1:
            with tempfile.TemporaryDirectory() as dir2:
                sandbox1 = Path(dir1)
                sandbox2 = Path(dir2)
                # repo_root is inside sandbox2 only
                repo_root = Path(dir2) / "sub"
                repo_root.mkdir()
                agent = _AgentWithSandbox([sandbox1, sandbox2], repo_root=repo_root)
                result = agent.do_work()
                self.assertTrue(agent.called)
                self.assertEqual(result, "done")

    def test_violation_error_message_contains_path(self):
        with tempfile.TemporaryDirectory() as sandbox_dir:
            with tempfile.TemporaryDirectory() as outside_dir:
                agent = _AgentWithSandbox(
                    [Path(sandbox_dir)],
                    repo_root=Path(outside_dir),
                )
                with self.assertRaises(SandboxViolationError) as ctx:
                    agent.do_work()
                self.assertIn("sandbox", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# Tests: RBACContext
# ---------------------------------------------------------------------------

class TestRBACContext(unittest.TestCase):
    """Context manager performs permission check and records audit trail."""

    def test_allowed_enters_without_exception(self):
        engine = _make_rbac_engine(allowed=True)
        ctx = RBACContext(engine, "swe_developer", "code_generation")
        with ctx:
            pass
        self.assertTrue(ctx.allowed)

    def test_denied_raises_on_enter(self):
        engine = _make_rbac_engine(allowed=False, reason="denied")
        ctx = RBACContext(engine, "swe_developer", "code_generation")
        with self.assertRaises(PermissionDeniedError):
            with ctx:
                pass

    def test_audit_trail_has_permission_check_entry(self):
        engine = _make_rbac_engine(allowed=True)
        ctx = RBACContext(engine, "swe_developer", "code_generation")
        with ctx:
            pass
        entries = [e for e in ctx.audit_trail if e["event"] == "permission_check"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["agent"], "swe_developer")
        self.assertEqual(entry["task"], "code_generation")
        self.assertTrue(entry["allowed"])

    def test_audit_trail_has_operation_end_entry(self):
        engine = _make_rbac_engine(allowed=True)
        ctx = RBACContext(engine, "swe_developer", "code_generation")
        with ctx:
            pass
        entries = [e for e in ctx.audit_trail if e["event"] == "operation_end"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["outcome"], "completed")

    def test_audit_trail_records_failure_on_exception(self):
        engine = _make_rbac_engine(allowed=True)
        ctx = RBACContext(engine, "swe_developer", "code_generation")
        with self.assertRaises(RuntimeError):
            with ctx:
                raise RuntimeError("something went wrong")
        end_entries = [e for e in ctx.audit_trail if e["event"] == "operation_end"]
        self.assertEqual(len(end_entries), 1)
        self.assertIn("RuntimeError", end_entries[0]["outcome"])

    def test_audit_trail_timestamps_are_numeric(self):
        engine = _make_rbac_engine(allowed=True)
        ctx = RBACContext(engine, "swe_developer", "code_generation")
        with ctx:
            pass
        for entry in ctx.audit_trail:
            self.assertIn("ts", entry)
            self.assertIsInstance(entry["ts"], float)

    def test_context_passed_to_engine(self):
        engine = _make_rbac_engine(allowed=True)
        context = {"severity": "CRITICAL", "ticket_id": "T-001"}
        ctx = RBACContext(engine, "swe_developer", "code_generation", context=context)
        with ctx:
            pass
        engine.check_permission.assert_called_once_with(
            "swe_developer", "code_generation", context
        )

    def test_denied_audit_trail_still_has_permission_check(self):
        engine = _make_rbac_engine(allowed=False, reason="no perm")
        ctx = RBACContext(engine, "bad_agent", "pr_merge")
        try:
            with ctx:
                pass
        except PermissionDeniedError:
            pass
        entries = [e for e in ctx.audit_trail if e["event"] == "permission_check"]
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["allowed"])

    def test_context_does_not_suppress_exceptions(self):
        """__exit__ must return False — exceptions must propagate."""
        engine = _make_rbac_engine(allowed=True)
        ctx = RBACContext(engine, "swe_developer", "code_generation")
        with self.assertRaises(ValueError):
            with ctx:
                raise ValueError("user error")

    def test_allowed_is_false_before_enter(self):
        engine = _make_rbac_engine(allowed=True)
        ctx = RBACContext(engine, "swe_developer", "code_generation")
        self.assertFalse(ctx.allowed)


# ---------------------------------------------------------------------------
# Tests: decorator preserves function metadata
# ---------------------------------------------------------------------------

class TestDecoratorMetadata(unittest.TestCase):
    """functools.wraps must preserve __name__ and __doc__."""

    def test_require_permission_preserves_name(self):
        engine = _make_rbac_engine(allowed=True)
        agent = _AgentWithRBAC(engine)
        self.assertEqual(agent.do_work.__name__, "do_work")

    def test_require_sandbox_preserves_name(self):
        agent = _AgentWithSandbox([], repo_root=Path("/tmp"))
        self.assertEqual(agent.do_work.__name__, "do_work")


if __name__ == "__main__":
    unittest.main()
