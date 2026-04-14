"""Dry-run pipeline simulation — full lifecycle without external services."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Set
from unittest.mock import MagicMock, patch

import pytest

import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.guardrails import GuardrailsCoordinator
from src.swe_team.providers.task_queue.memory import InMemoryTaskQueue
from src.swe_team.preflight import PreflightCheck


class FakeDryRunStore:
    """Minimal store for dry-run simulation."""
    def __init__(self):
        self._tickets: List[SWETicket] = []
        self._fps: Set[str] = set()

    @property
    def known_fingerprints(self) -> Set[str]:
        return self._fps

    def add(self, ticket: SWETicket) -> None:
        fp = ticket.metadata.get("fingerprint", "")
        self._fps.add(fp)
        self._tickets.append(ticket)

    def list_all(self) -> List[SWETicket]:
        return self._tickets


class TestPipelineDryRun:
    """Full pipeline dry-run simulation."""

    def test_config_loads_all_repos(self):
        """Verify swe_team.yaml has all configured repos."""
        import yaml
        config_path = PROJECT_ROOT / "config" / "swe_team.yaml"
        with open(config_path) as f:
            config = yaml.safe_load(f)

        repo_names = [r["name"] for r in config["repos"]]
        # Sandbox repos
        assert "your-org/SWE-Sandbox" in repo_names
        assert "your-org/SWE-Sandbox-HealthTrack" in repo_names
        assert "your-org/SWE-Sandbox-ShopStream" in repo_names
        assert "your-org/SWE-Sandbox-GreenGrid" in repo_names
        assert "your-org/SWE-Sandbox-EduPath" in repo_names
        # Production repos
        assert "your-org/example-site" in repo_names
        assert len(repo_names) >= 6

    def test_repo_router_resolves_all_sandbox_repos(self):
        """RepoRouter maps each repo name to a local path."""
        from src.swe_team.repo_router import RepoRouter
        import yaml

        config_path = PROJECT_ROOT / "config" / "swe_team.yaml"
        with open(config_path) as f:
            config = yaml.safe_load(f)

        router = RepoRouter(config["repos"])
        repos_map = router.build_repos_map()

        assert len(repos_map) >= 6
        for name in ["your-org/SWE-Sandbox", "your-org/SWE-Sandbox-HealthTrack",
                      "your-org/SWE-Sandbox-ShopStream", "your-org/SWE-Sandbox-GreenGrid",
                      "your-org/example-site",
                      "your-org/SWE-Sandbox-EduPath"]:
            assert name in repos_map
            assert isinstance(repos_map[name], Path)

    def test_repo_router_fails_closed_on_unknown(self):
        """Unknown repo raises ValueError."""
        from src.swe_team.repo_router import RepoRouter
        import yaml

        config_path = PROJECT_ROOT / "config" / "swe_team.yaml"
        with open(config_path) as f:
            config = yaml.safe_load(f)

        router = RepoRouter(config["repos"])
        ticket = MagicMock()
        ticket.metadata = {"repo": "your-org/production-repo"}

        with pytest.raises(ValueError, match="not in the configured sandbox list"):
            router.resolve(ticket)

    @patch("subprocess.run")
    def test_multi_repo_fetch_and_dedup(self, mock_run):
        """Fetch from multiple repos, verify dedup across cycles."""
        from scripts.ops.swe_team_runner import fetch_github_tickets

        issues_ht = [{"number": 1, "title": "[CRITICAL] API 500", "body": "test", "labels": [{"name": "critical"}]}]
        issues_ss = [{"number": 1, "title": "[HIGH] Cart bug", "body": "test", "labels": [{"name": "bug"}]}]

        def side_effect(cmd, **kw):
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            repo = ""
            for i, a in enumerate(cmd):
                if a == "--repo" and i + 1 < len(cmd):
                    repo = cmd[i + 1]
            if "HealthTrack" in repo:
                r.stdout = json.dumps(issues_ht)
            elif "ShopStream" in repo:
                r.stdout = json.dumps(issues_ss)
            else:
                r.stdout = "[]"
            return r

        mock_run.side_effect = side_effect
        store = FakeDryRunStore()
        repos = ["your-org/SWE-Sandbox-HealthTrack", "your-org/SWE-Sandbox-ShopStream",
                  "your-org/SWE-Sandbox-GreenGrid", "your-org/SWE-Sandbox-EduPath"]

        # Cycle 1
        t1 = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(t1) == 2
        for t in t1:
            store.add(t)

        # Cycle 2 — dedup
        t2 = fetch_github_tickets(store, github_account="swe-squad-alpha", repos=repos)
        assert len(t2) == 0, "Second cycle should return 0 — dedup"

    def test_guardrails_allow_investigation(self):
        """Guardrails allow investigation even when stability gate would block deploy."""
        coord = GuardrailsCoordinator()

        gate = MagicMock()
        report = MagicMock()
        report.verdict = "BLOCK"
        report.reason = "5 critical bugs"
        report.open_critical = 5
        gate.evaluate.return_value = report
        coord.set_stability_gate(gate)

        # Investigation should pass (stability gate only blocks deploy/creative)
        d = coord.can_proceed(task_type="investigate")
        assert d.allowed is True

        # Deploy should be blocked
        d = coord.can_proceed(task_type="deploy")
        assert d.allowed is False

    def test_queue_priority_ordering(self):
        """Tickets enqueued with correct priority (CRITICAL < HIGH < MEDIUM)."""
        queue = InMemoryTaskQueue()

        # Enqueue in wrong order
        queue.enqueue(task_type="investigate", ticket_id="med-1", priority=50, payload={"sev": "MEDIUM"})
        queue.enqueue(task_type="investigate", ticket_id="crit-1", priority=10, payload={"sev": "CRITICAL"})
        queue.enqueue(task_type="investigate", ticket_id="high-1", priority=30, payload={"sev": "HIGH"})

        # Claim should return in priority order
        t1 = queue.claim(task_type="investigate", worker_id="w1")
        assert t1 is not None
        assert t1.ticket_id == "crit-1"

        t2 = queue.claim(task_type="investigate", worker_id="w2")
        assert t2 is not None
        assert t2.ticket_id == "high-1"

        t3 = queue.claim(task_type="investigate", worker_id="w3")
        assert t3 is not None
        assert t3.ticket_id == "med-1"

    def test_full_lifecycle_simulation(self):
        """Simulate complete ticket lifecycle: create -> triage -> guard -> queue -> dedup."""
        store = FakeDryRunStore()
        coord = GuardrailsCoordinator()
        queue = InMemoryTaskQueue()

        # Create ticket
        ticket = SWETicket(
            title="[GH-1] Test bug",
            description="A test bug",
            severity=TicketSeverity.HIGH,
            metadata={"github_issue": 1, "fingerprint": "gh-issue-test/repo-1", "repo": "test/repo"},
        )
        store.add(ticket)

        # Check guardrails
        decision = coord.can_proceed(task_type="investigate", ticket_severity="HIGH")
        assert decision.allowed is True

        # Enqueue
        task = queue.enqueue(
            task_type="investigate",
            ticket_id=ticket.metadata["fingerprint"],
            priority=30,
            payload={"title": ticket.title, "severity": "HIGH"},
        )
        assert task is not None
        assert queue.queue_depth() == 1

        # Claim and complete
        claimed = queue.claim(task_type="investigate", worker_id="agent-1")
        assert claimed is not None
        assert claimed.ticket_id == ticket.metadata["fingerprint"]
        queue.complete(claimed.task_id, result={"status": "resolved"})
        assert queue.queue_depth() == 0

        # Dedup — same fingerprint should be known
        assert ticket.metadata["fingerprint"] in store.known_fingerprints

    def test_preflight_sandbox_paths_wired_from_config(self, tmp_path):
        """PreflightCheck receives sandbox_paths extracted from config.repos local_path values."""
        sandbox_a = tmp_path / "SWE-Sandbox"
        sandbox_b = tmp_path / "SWE-Sandbox-HealthTrack"
        sandbox_a.mkdir()
        sandbox_b.mkdir()

        repos = [
            {"name": "your-org/SWE-Sandbox", "local_path": str(sandbox_a)},
            {"name": "your-org/SWE-Sandbox-HealthTrack", "local_path": str(sandbox_b)},
            {"name": "your-org/no-local-path"},  # no local_path — must be skipped
        ]

        sandbox_paths = [
            Path(r["local_path"]) for r in repos if r.get("local_path")
        ]

        # Repo root inside sandbox_a — should pass
        check_inside = PreflightCheck(
            expected_repo_root=sandbox_a,
            required_env_vars=[],
            sandbox_paths=sandbox_paths,
        )
        with MagicMock() as _mock:
            import subprocess
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout=str(sandbox_a.resolve()) + "\n", stderr=""
                )
                failures = check_inside.check_sandbox_boundary()
        assert failures == [], f"Expected no sandbox failures, got: {failures}"

        # Repo root outside all sandboxes — should fail
        outside = tmp_path / "outside-repo"
        outside.mkdir()
        check_outside = PreflightCheck(
            expected_repo_root=outside,
            required_env_vars=[],
            sandbox_paths=sandbox_paths,
        )
        failures = check_outside.check_sandbox_boundary()
        assert len(failures) == 1
        assert "outside all configured sandbox paths" in failures[0]

    def test_preflight_sandbox_paths_empty_when_no_local_path(self):
        """When repos have no local_path, sandbox_paths is empty and boundary check is skipped."""
        repos = [
            {"name": "your-org/SWE-Sandbox"},  # missing local_path
        ]
        sandbox_paths = [
            Path(r["local_path"]) for r in repos if r.get("local_path")
        ]
        assert sandbox_paths == []

        check = PreflightCheck(
            expected_repo_root=Path("/some/path"),
            required_env_vars=[],
            sandbox_paths=sandbox_paths,
        )
        # Empty sandbox_paths → boundary check is a no-op
        failures = check.check_sandbox_boundary()
        assert failures == []
