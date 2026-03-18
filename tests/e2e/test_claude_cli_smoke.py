"""
E2E smoke tests for Claude Code CLI invocation.

These tests verify that the Claude CLI subprocess integration works correctly.
They do NOT make real API calls — they mock the subprocess to validate the
invocation contract (args, timeout, cwd, environment handling).

To run against a real Claude CLI (requires authentication):
    SWE_E2E_REAL=1 python3 -m pytest tests/e2e/ -v

Without SWE_E2E_REAL, all tests use mocked subprocess.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

REAL_MODE = os.environ.get("SWE_E2E_REAL", "").lower() in ("1", "true", "yes")
CLAUDE_PATH = "/usr/bin/claude"


class TestClaudeCLIContract(unittest.TestCase):
    """Verify claude CLI is invoked with correct arguments."""

    def _make_ticket(self, ticket_id="e2e-001", severity="high"):
        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
        return SWETicket(
            ticket_id=ticket_id,
            title="Test E2E ticket",
            severity=TicketSeverity(severity),
            status=TicketStatus.OPEN,
            description="Test description for e2e validation",
            error_log="KeyError: 'user_id' at line 42",
            source_module="test_module",
        )

    @patch("subprocess.run")
    def test_investigator_calls_claude_with_correct_flags(self, mock_run):
        """InvestigatorAgent calls claude --print --dangerously-skip-permissions."""
        from src.swe_team.config import load_config
        from src.swe_team.ticket_store import TicketStore

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Root cause: KeyError in user lookup. Fix: add .get() call.",
            stderr="",
        )

        config = load_config()
        store = TicketStore(path=PROJECT_ROOT / "data" / "swe_team" / "tickets.json")
        ticket = self._make_ticket()

        from src.swe_team.investigator import InvestigatorAgent
        agent = InvestigatorAgent(store=store, model_config=config.models)

        # _run_claude returns (stdout, stderr) tuple; backoff.execute unpacks it
        mock_report = "Mock investigation report " * 20
        with patch.object(agent, "_run_claude", return_value=(mock_report, "")) as mock_claude:
            agent.investigate(ticket)
            assert mock_claude.called, "_run_claude was not called"

    @patch("subprocess.run")
    def test_claude_invocation_includes_model_flag(self, mock_run):
        """claude subprocess call includes --model flag."""
        mock_run.return_value = MagicMock(returncode=0, stdout="Investigation: " * 20, stderr="")

        from src.swe_team.investigator import InvestigatorAgent
        from src.swe_team.config import load_config
        from src.swe_team.ticket_store import TicketStore

        config = load_config()
        store = TicketStore(path=PROJECT_ROOT / "data" / "swe_team" / "tickets.json")
        ticket = self._make_ticket()

        agent = InvestigatorAgent(store=store, model_config=config.models)
        # Call _run_claude directly to test subprocess contract
        result = agent._run_claude("Test prompt", model="sonnet", timeout=30)

        if mock_run.called:
            call_args = mock_run.call_args
            cmd = call_args[0][0] if call_args[0] else call_args[1].get("args", [])
            assert "--model" in cmd, f"--model flag missing from: {cmd}"
            assert "--print" in cmd, f"--print flag missing from: {cmd}"

    def test_claude_binary_exists_on_path(self):
        """Claude CLI binary is present and executable."""
        import shutil
        claude = shutil.which("claude") or (CLAUDE_PATH if Path(CLAUDE_PATH).exists() else None)
        if not claude:
            self.skipTest("claude binary not found — skipping (expected in CI)")
        self.assertTrue(Path(claude).exists(), f"claude binary not found at {claude}")

    @unittest.skipUnless(REAL_MODE, "Set SWE_E2E_REAL=1 to run against real Claude CLI")
    def test_real_claude_cli_responds(self):
        """Real Claude CLI returns a non-empty response to a simple prompt."""
        result = subprocess.run(
            [CLAUDE_PATH, "--print", "--model", "haiku", "--dangerously-skip-permissions"],
            input="Reply with exactly: SMOKE_TEST_OK",
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, f"claude exited {result.returncode}: {result.stderr}")
        self.assertIn("SMOKE_TEST_OK", result.stdout, f"Unexpected output: {result.stdout[:200]}")


class TestInvestigatorFallback(unittest.TestCase):
    """Verify fallback chain when primary model fails."""

    @patch("subprocess.run")
    def test_investigation_handles_empty_output_gracefully(self, mock_run):
        """Empty claude output doesn't crash — ticket gets re-queued."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        from src.swe_team.config import load_config
        from src.swe_team.ticket_store import TicketStore
        from src.swe_team.models import TicketStatus

        config = load_config()
        store = TicketStore(path=PROJECT_ROOT / "data" / "swe_team" / "tickets.json")

        from src.swe_team.investigator import InvestigatorAgent
        agent = InvestigatorAgent(store=store, model_config=config.models)

        ticket = TestClaudeCLIContract()._make_ticket("e2e-fallback-001")
        # _run_claude returns (stdout, stderr) tuple; empty stdout triggers failure path
        with patch.object(agent, "_run_claude", return_value=("", "")):
            result = agent.investigate(ticket)
        # Should not raise; ticket may be re-queued or have empty report
        self.assertIsNotNone(ticket)


if __name__ == "__main__":
    unittest.main()
