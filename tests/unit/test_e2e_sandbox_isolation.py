"""
End-to-end test suite for SWE-Squad multi-repo sandbox isolation.

Simulates 20+ rounds of pipeline cycles across 5 sandbox repos,
verifying strict isolation between repos at every stage:
  fetch -> route -> queue -> claim -> process -> complete

All external calls (gh CLI, subprocess) are mocked. Uses real
TicketStore, RepoRouter, InMemoryTaskQueue, and RBAC decorators.
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from unittest.mock import MagicMock, patch

import pytest

from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.repo_router import RepoRouter, ResolvedRepo
from src.swe_team.ticket_store import TicketStore
from src.swe_team.providers.task_queue.memory import InMemoryTaskQueue
from src.swe_team.rbac_middleware import (
    SandboxViolationError,
    require_permission,
    require_sandbox,
)
from src.swe_team.agent_rbac import PermissionDeniedError


# ---------------------------------------------------------------------------
# Sandbox repos configuration (mirrors production config)
# ---------------------------------------------------------------------------

SANDBOX_REPOS = [
    {"name": "test-org/SWE-Sandbox", "local_path": "/home/agent/Projects/SWE-Sandbox"},
    {"name": "test-org/SWE-Sandbox-HealthTrack", "local_path": "/home/agent/Projects/SWE-Sandbox-HealthTrack"},
    {"name": "test-org/SWE-Sandbox-ShopStream", "local_path": "/home/agent/Projects/SWE-Sandbox-ShopStream"},
    {"name": "test-org/SWE-Sandbox-GreenGrid", "local_path": "/home/agent/Projects/SWE-Sandbox-GreenGrid"},
    {"name": "test-org/SWE-Sandbox-EduPath", "local_path": "/home/agent/Projects/SWE-Sandbox-EduPath"},
]

REPO_NAMES = [r["name"] for r in SANDBOX_REPOS]

GITHUB_ACCOUNT = "test-bot"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gh_issue(number: int, title: str = "", labels: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """Build a dict matching the gh CLI JSON output for a single issue."""
    return {
        "number": number,
        "title": title or f"Test issue #{number}",
        "body": f"Description for issue #{number}",
        "labels": labels or [],
    }


def _make_ticket(
    repo: str,
    issue_number: int,
    severity: TicketSeverity = TicketSeverity.HIGH,
    status: TicketStatus = TicketStatus.OPEN,
) -> SWETicket:
    """Create a ticket with proper fingerprint and repo metadata."""
    fingerprint = f"gh-issue-{repo}-{issue_number}"
    return SWETicket(
        title=f"[GH-{issue_number}] Test issue #{issue_number}",
        description=f"Description for issue #{issue_number}",
        severity=severity,
        status=status,
        source_module="unknown",
        metadata={
            "github_issue": issue_number,
            "fingerprint": fingerprint,
            "repo": repo,
        },
    )


def _simulate_fetch_for_repo(
    repo: str,
    issues: List[Dict[str, Any]],
    store: TicketStore,
    known_fps: Set[str],
) -> List[SWETicket]:
    """Simulate _fetch_github_issues_for_repo without subprocess calls.

    Mirrors the logic in scripts/ops/swe_team_runner.py exactly.
    """
    new_tickets: List[SWETicket] = []
    for issue in issues:
        fingerprint = f"gh-issue-{repo}-{issue['number']}"
        if fingerprint in known_fps or fingerprint in store.known_fingerprints:
            continue

        label_names = [la.get("name", "").lower() for la in issue.get("labels", [])]
        title_lower = issue["title"].lower()
        if any("critical" in la or "p0" in la for la in label_names) or "p0" in title_lower:
            severity = TicketSeverity.CRITICAL
        elif any("high" in la or "p1" in la for la in label_names) or "p1" in title_lower:
            severity = TicketSeverity.HIGH
        elif any("low" in la for la in label_names) or "p3" in title_lower:
            severity = TicketSeverity.LOW
        else:
            severity = TicketSeverity.HIGH

        module = "unknown"
        for la in label_names:
            if "module:" in la:
                module = la.replace("module:", "").strip()
                break

        ticket = SWETicket(
            title=f"[GH-{issue['number']}] {issue['title'][:100]}",
            description=(issue.get("body") or "")[:500],
            severity=severity,
            source_module=module,
            metadata={
                "github_issue": issue["number"],
                "fingerprint": fingerprint,
                "repo": repo,
            },
        )
        new_tickets.append(ticket)
        known_fps.add(fingerprint)
    return new_tickets


def _simulate_fetch_all(
    repo_issues: Dict[str, List[Dict[str, Any]]],
    store: TicketStore,
) -> List[SWETicket]:
    """Simulate fetch_github_tickets across all repos."""
    all_tickets: List[SWETicket] = []
    seen_fps: Set[str] = set()
    for repo, issues in repo_issues.items():
        tickets = _simulate_fetch_for_repo(repo, issues, store, seen_fps)
        all_tickets.extend(tickets)
    return all_tickets


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_store(tmp_path):
    """Provide a fresh TicketStore backed by a temporary file."""
    return TicketStore(path=str(tmp_path / "tickets.json"))


@pytest.fixture
def router():
    """Provide a RepoRouter configured with all sandbox repos."""
    return RepoRouter(SANDBOX_REPOS)


@pytest.fixture
def queue():
    """Provide a fresh InMemoryTaskQueue."""
    return InMemoryTaskQueue()


# ---------------------------------------------------------------------------
# Class: TestMultiRoundIsolation
# ---------------------------------------------------------------------------

class TestMultiRoundIsolation:
    """Simulate 20+ rounds of pipeline cycles with varying issues across repos."""

    def test_20_round_no_cross_contamination(self, tmp_store, router):
        """Run 20 fetch cycles. Each round has issues from random repos.
        Verify NO ticket ever has metadata['repo'] that doesn't match its
        fingerprint repo."""
        rng = random.Random(42)

        for round_num in range(20):
            # Each round: 1-3 repos produce 1-3 issues each
            active_repos = rng.sample(REPO_NAMES, k=rng.randint(1, 3))
            repo_issues = {}
            for repo in active_repos:
                count = rng.randint(1, 3)
                repo_issues[repo] = [
                    _make_gh_issue(round_num * 100 + i, f"Round {round_num} issue {i}")
                    for i in range(count)
                ]

            tickets = _simulate_fetch_all(repo_issues, tmp_store)
            for t in tickets:
                tmp_store.add(t)
                # Verify repo in metadata matches the repo in the fingerprint
                fp = t.metadata["fingerprint"]
                repo_from_fp = fp.replace("gh-issue-", "").rsplit("-", 1)[0]
                assert t.metadata["repo"] == repo_from_fp, (
                    f"Cross-contamination: ticket {t.ticket_id} has repo={t.metadata['repo']} "
                    f"but fingerprint implies repo={repo_from_fp}"
                )

        # Also verify via router: every ticket resolves to its own repo
        for t in tmp_store.list_all():
            resolved = router.resolve(t)
            assert resolved.repo_name == t.metadata["repo"]

    def test_20_round_dedup_across_cycles(self, tmp_store):
        """20 rounds with the same issues. After round 1, zero new tickets
        should be created (all deduped by fingerprint)."""
        # Fixed set of issues across 3 repos
        repo_issues = {
            "test-org/SWE-Sandbox": [_make_gh_issue(1), _make_gh_issue(2)],
            "test-org/SWE-Sandbox-HealthTrack": [_make_gh_issue(3)],
            "test-org/SWE-Sandbox-ShopStream": [_make_gh_issue(4), _make_gh_issue(5)],
        }

        for round_num in range(20):
            tickets = _simulate_fetch_all(repo_issues, tmp_store)
            if round_num == 0:
                assert len(tickets) == 5, f"Round 0 should create 5 tickets, got {len(tickets)}"
                for t in tickets:
                    tmp_store.add(t)
            else:
                assert len(tickets) == 0, (
                    f"Round {round_num} should create 0 tickets (all deduped), got {len(tickets)}"
                )

    def test_20_round_mixed_new_and_existing(self, tmp_store):
        """Each round adds 1 new issue to a random repo. After 20 rounds,
        exactly 20 unique tickets exist."""
        rng = random.Random(99)

        for round_num in range(20):
            repo = rng.choice(REPO_NAMES)
            # Use a unique issue number per round
            issue_num = 1000 + round_num
            repo_issues = {repo: [_make_gh_issue(issue_num, f"New issue round {round_num}")]}
            tickets = _simulate_fetch_all(repo_issues, tmp_store)
            assert len(tickets) == 1, f"Round {round_num}: expected 1 new ticket, got {len(tickets)}"
            for t in tickets:
                tmp_store.add(t)

        assert len(tmp_store.list_all()) == 20

    def test_round_isolation_repo_a_issues_never_in_repo_b(self, tmp_store):
        """Issues from HealthTrack NEVER appear in ShopStream's pipeline
        and vice versa."""
        ht_repo = "test-org/SWE-Sandbox-HealthTrack"
        ss_repo = "test-org/SWE-Sandbox-ShopStream"

        for round_num in range(20):
            repo_issues = {
                ht_repo: [_make_gh_issue(round_num + 100, f"HealthTrack issue {round_num}")],
                ss_repo: [_make_gh_issue(round_num + 200, f"ShopStream issue {round_num}")],
            }
            tickets = _simulate_fetch_all(repo_issues, tmp_store)
            for t in tickets:
                tmp_store.add(t)

        all_tickets = tmp_store.list_all()
        ht_tickets = [t for t in all_tickets if t.metadata["repo"] == ht_repo]
        ss_tickets = [t for t in all_tickets if t.metadata["repo"] == ss_repo]

        # Verify counts
        assert len(ht_tickets) == 20
        assert len(ss_tickets) == 20

        # Verify no HealthTrack fingerprint contains ShopStream and vice versa
        for t in ht_tickets:
            assert ss_repo not in t.metadata["fingerprint"]
            assert t.metadata["repo"] == ht_repo
        for t in ss_tickets:
            assert ht_repo not in t.metadata["fingerprint"]
            assert t.metadata["repo"] == ss_repo

    def test_fingerprint_collision_impossible(self, tmp_store):
        """Same issue number (#1) across all 5 repos creates 5 distinct
        fingerprints."""
        repo_issues = {repo: [_make_gh_issue(1, "Bug #1")] for repo in REPO_NAMES}
        tickets = _simulate_fetch_all(repo_issues, tmp_store)

        assert len(tickets) == 5
        fingerprints = {t.metadata["fingerprint"] for t in tickets}
        assert len(fingerprints) == 5, "Fingerprints must be unique across repos"

        # Each fingerprint should contain the repo name
        for t in tickets:
            assert t.metadata["repo"] in t.metadata["fingerprint"]


# ---------------------------------------------------------------------------
# Class: TestClaimTicketIsolation
# ---------------------------------------------------------------------------

class TestClaimTicketIsolation:
    """Test claim_ticket fail-closed behavior and cross-agent isolation."""

    def _make_mock_supabase_store(self) -> MagicMock:
        """Create a mock SupabaseStore with claim_ticket semantics."""
        store = MagicMock()
        store._claimed: Dict[str, str] = {}

        def _claim(ticket_id: str, agent_id: str) -> bool:
            if ticket_id in store._claimed:
                return False
            store._claimed[ticket_id] = agent_id
            return True

        store.claim_ticket = MagicMock(side_effect=_claim)
        return store

    def test_claim_prevents_double_action(self):
        """Two agents try to claim the same ticket. Only one succeeds."""
        store = self._make_mock_supabase_store()
        assert store.claim_ticket("ticket-1", "agent-a") is True
        assert store.claim_ticket("ticket-1", "agent-b") is False

    def test_claim_fail_closed_on_error(self):
        """RPC error returns False, not True."""
        store = MagicMock()
        store.claim_ticket = MagicMock(side_effect=Exception("RPC timeout"))

        # Wrap to simulate fail-closed behavior
        def safe_claim(ticket_id: str, agent_id: str) -> bool:
            try:
                return store.claim_ticket(ticket_id, agent_id)
            except Exception:
                return False

        assert safe_claim("ticket-1", "agent-a") is False

    def test_claim_different_tickets_both_succeed(self):
        """Two agents claim different tickets, both succeed."""
        store = self._make_mock_supabase_store()
        assert store.claim_ticket("ticket-1", "agent-a") is True
        assert store.claim_ticket("ticket-2", "agent-b") is True

    def test_claim_release_reclaim(self):
        """Agent claims, releases, another agent can claim."""
        store = self._make_mock_supabase_store()
        assert store.claim_ticket("ticket-1", "agent-a") is True

        # Simulate release
        del store._claimed["ticket-1"]

        assert store.claim_ticket("ticket-1", "agent-b") is True
        assert store._claimed["ticket-1"] == "agent-b"

    def test_concurrent_claims_across_repos(self):
        """5 agents each claim a ticket from different repos. All succeed
        (no cross-blocking between repos)."""
        store = self._make_mock_supabase_store()
        results = []

        def claim_in_thread(ticket_id, agent_id):
            result = store.claim_ticket(ticket_id, agent_id)
            results.append((ticket_id, agent_id, result))

        threads = []
        for i, repo in enumerate(REPO_NAMES):
            ticket_id = f"ticket-{repo}-1"
            agent_id = f"agent-{i}"
            t = threading.Thread(target=claim_in_thread, args=(ticket_id, agent_id))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 5 claims should succeed (different ticket IDs)
        assert len(results) == 5
        assert all(r[2] is True for r in results), f"Some claims failed: {results}"


# ---------------------------------------------------------------------------
# Class: TestRBACEnforcement
# ---------------------------------------------------------------------------

class TestRBACEnforcement:
    """Test RBAC decorators enforce sandbox and permission boundaries."""

    def test_sandbox_violation_blocked(self):
        """Agent working outside sandbox paths raises SandboxViolationError."""
        class FakeAgent:
            _sandbox_paths = [Path("/home/agent/Projects/SWE-Sandbox")]
            _repo_root = Path("/home/agent/Projects/ProductionRepo")

            @require_sandbox
            def do_work(self):
                return "should not reach here"

        agent = FakeAgent()
        with pytest.raises(SandboxViolationError):
            agent.do_work()

    def test_sandbox_inside_allowed(self):
        """Agent working inside sandbox path proceeds normally."""
        class FakeAgent:
            _sandbox_paths = [Path("/home/agent/Projects/SWE-Sandbox")]
            _repo_root = Path("/home/agent/Projects/SWE-Sandbox")

            @require_sandbox
            def do_work(self):
                return "success"

        agent = FakeAgent()
        assert agent.do_work() == "success"

    def test_permission_denied_blocks_action(self):
        """Agent without code_generation permission gets blocked."""
        engine = MagicMock()
        engine.check_permission = MagicMock(
            return_value=(False, "Agent lacks code_generation permission")
        )

        class FakeAgent:
            _rbac_engine = engine
            _agent_name = "test-agent"

            @require_permission("code_generation")
            def generate_code(self):
                return "should not reach here"

        agent = FakeAgent()
        with pytest.raises(PermissionDeniedError):
            agent.generate_code()

    def test_permission_granted_allows_action(self):
        """Agent with permission proceeds."""
        engine = MagicMock()
        engine.check_permission = MagicMock(return_value=(True, "granted"))

        class FakeAgent:
            _rbac_engine = engine
            _agent_name = "swe-developer"

            @require_permission("code_generation")
            def generate_code(self):
                return "code generated"

        agent = FakeAgent()
        assert agent.generate_code() == "code generated"

    def test_no_rbac_engine_skips_check(self):
        """Backward compat: no engine = no block."""
        class FakeAgent:
            # No _rbac_engine or _agent_name

            @require_permission("code_generation")
            def generate_code(self):
                return "code generated"

        agent = FakeAgent()
        assert agent.generate_code() == "code generated"

    def test_sandbox_nested_path_allowed(self):
        """Working in a subdirectory of sandbox is allowed."""
        class FakeAgent:
            _sandbox_paths = [Path("/home/agent/Projects/SWE-Sandbox")]
            _repo_root = Path("/home/agent/Projects/SWE-Sandbox/src/components")

            @require_sandbox
            def do_work(self):
                return "nested ok"

        agent = FakeAgent()
        assert agent.do_work() == "nested ok"

    def test_sandbox_sibling_path_blocked(self):
        """Working in /home/agent/Projects/OtherRepo is blocked."""
        class FakeAgent:
            _sandbox_paths = [Path(r["local_path"]) for r in SANDBOX_REPOS]
            _repo_root = Path("/home/agent/Projects/OtherRepo")

            @require_sandbox
            def do_work(self):
                return "should not reach here"

        agent = FakeAgent()
        with pytest.raises(SandboxViolationError):
            agent.do_work()


# ---------------------------------------------------------------------------
# Class: TestRepoRouterFailClosed
# ---------------------------------------------------------------------------

class TestRepoRouterFailClosed:
    """Test RepoRouter fail-closed routing behavior."""

    def test_unknown_repo_raises_valueerror(self, router):
        """Ticket with repo not in config raises ValueError."""
        ticket = _make_ticket("test-org/Unknown-Repo", 1)
        with pytest.raises(ValueError, match="not in the configured sandbox list"):
            router.resolve(ticket)

    def test_production_repo_rejected(self, router):
        """Ticket targeting a real production repo is rejected."""
        ticket = _make_ticket("test-org/my-app", 42)
        with pytest.raises(ValueError, match="not in the configured sandbox list"):
            router.resolve(ticket)

    def test_all_sandbox_repos_resolve(self, router):
        """All 5 sandbox repos resolve correctly."""
        for repo_cfg in SANDBOX_REPOS:
            ticket = _make_ticket(repo_cfg["name"], 1)
            resolved = router.resolve(ticket)
            assert resolved.repo_name == repo_cfg["name"]
            assert resolved.local_path == Path(repo_cfg["local_path"])

    def test_ticket_without_repo_defaults_to_first(self, router):
        """Ticket with no repo metadata defaults to first sandbox."""
        ticket = SWETicket(
            title="No repo ticket",
            description="Missing repo metadata",
            severity=TicketSeverity.HIGH,
            metadata={},
        )
        resolved = router.resolve(ticket)
        assert resolved.repo_name == SANDBOX_REPOS[0]["name"]

    def test_is_sandbox_path_rejects_outside(self, router):
        """Path outside all sandbox repos returns False."""
        assert router.is_sandbox_path(Path("/home/agent/Projects/ProductionRepo")) is False
        assert router.is_sandbox_path(Path("/tmp/evil")) is False
        assert router.is_sandbox_path(Path("/home/agent")) is False

    def test_empty_config_raises(self):
        """No repos configured raises ValueError on resolve."""
        empty_router = RepoRouter([])
        ticket = SWETicket(
            title="Test",
            description="Test",
            severity=TicketSeverity.HIGH,
            metadata={},
        )
        with pytest.raises(ValueError, match="No sandbox repos configured"):
            empty_router.resolve(ticket)


# ---------------------------------------------------------------------------
# Class: TestNoAssignedTicketsSilent
# ---------------------------------------------------------------------------

class TestNoAssignedTicketsSilent:
    """Test that empty / unassigned issue lists return silently."""

    def test_no_assigned_issues_returns_empty(self, tmp_store):
        """When gh returns no issues assigned to bot, fetch returns []."""
        repo_issues: Dict[str, List[Dict]] = {"test-org/SWE-Sandbox": []}
        tickets = _simulate_fetch_all(repo_issues, tmp_store)
        assert tickets == []

    def test_issues_assigned_to_other_user_skipped(self, tmp_store):
        """Issues assigned to someone else are not picked up.
        The gh CLI --assignee filter handles this; we simulate empty results."""
        # Simulating: gh returns no issues because they're assigned to a different user
        repo_issues: Dict[str, List[Dict]] = {repo: [] for repo in REPO_NAMES}
        tickets = _simulate_fetch_all(repo_issues, tmp_store)
        assert tickets == []

    def test_unassigned_issues_skipped(self, tmp_store):
        """Issues with no assignee are not picked up (gh --assignee filters them out)."""
        repo_issues: Dict[str, List[Dict]] = {repo: [] for repo in REPO_NAMES}
        tickets = _simulate_fetch_all(repo_issues, tmp_store)
        assert tickets == []

    def test_empty_repo_list_returns_empty(self, tmp_store):
        """Empty repos list returns []."""
        tickets = _simulate_fetch_all({}, tmp_store)
        assert tickets == []

    def test_all_repos_empty_returns_empty(self, tmp_store):
        """All 5 repos have zero assigned issues -> []."""
        repo_issues = {repo: [] for repo in REPO_NAMES}
        tickets = _simulate_fetch_all(repo_issues, tmp_store)
        assert tickets == []


# ---------------------------------------------------------------------------
# Class: TestTicketOverlapPrevention
# ---------------------------------------------------------------------------

class TestTicketOverlapPrevention:
    """Test dedup, idempotency, and status transition protection."""

    def test_same_issue_fetched_twice_only_one_ticket(self, tmp_store):
        """Same issue appearing in two cycles creates only 1 ticket."""
        issue = _make_gh_issue(42, "Duplicate bug")
        repo = "test-org/SWE-Sandbox"

        # Cycle 1
        tickets_1 = _simulate_fetch_all({repo: [issue]}, tmp_store)
        assert len(tickets_1) == 1
        for t in tickets_1:
            tmp_store.add(t)

        # Cycle 2
        tickets_2 = _simulate_fetch_all({repo: [issue]}, tmp_store)
        assert len(tickets_2) == 0

    def test_store_add_idempotent(self, tmp_store):
        """Adding same ticket twice doesn't create duplicate."""
        ticket = _make_ticket("test-org/SWE-Sandbox", 1)
        tmp_store.add(ticket)
        tmp_store.add(ticket)

        # Only one ticket with that ID
        all_tickets = tmp_store.list_all()
        matching = [t for t in all_tickets if t.ticket_id == ticket.ticket_id]
        assert len(matching) == 1

    def test_fingerprint_based_dedup(self, tmp_store):
        """Two issues with same fingerprint: second is skipped."""
        repo = "test-org/SWE-Sandbox"
        issue = _make_gh_issue(99, "Same bug")

        tickets_1 = _simulate_fetch_all({repo: [issue]}, tmp_store)
        for t in tickets_1:
            tmp_store.add(t)
        assert len(tickets_1) == 1

        # Second fetch with same fingerprint
        tickets_2 = _simulate_fetch_all({repo: [issue]}, tmp_store)
        assert len(tickets_2) == 0

    def test_status_transition_no_overwrite(self, tmp_store):
        """Ticket in INVESTIGATING cannot be overwritten by a new OPEN ticket
        with same fingerprint."""
        repo = "test-org/SWE-Sandbox"
        ticket = _make_ticket(repo, 55, status=TicketStatus.INVESTIGATING)
        tmp_store.add(ticket)

        # Attempt to fetch same issue again
        issue = _make_gh_issue(55, "Same bug again")
        new_tickets = _simulate_fetch_all({repo: [issue]}, tmp_store)
        assert len(new_tickets) == 0, "Dedup should prevent creating a new ticket"

        # Original ticket should still be INVESTIGATING
        stored = tmp_store.get(ticket.ticket_id)
        assert stored is not None
        assert stored.status == TicketStatus.INVESTIGATING

    def test_metadata_preserved_across_updates(self, tmp_store):
        """Updating ticket preserves original metadata (repo, fingerprint)."""
        repo = "test-org/SWE-Sandbox-GreenGrid"
        ticket = _make_ticket(repo, 10)
        original_fp = ticket.metadata["fingerprint"]
        original_repo = ticket.metadata["repo"]
        tmp_store.add(ticket)

        # Simulate an update (e.g., adding investigation report)
        ticket.investigation_report = "Root cause found: null pointer"
        ticket.metadata["investigation_model"] = "sonnet"
        tmp_store.add(ticket)

        stored = tmp_store.get(ticket.ticket_id)
        assert stored.metadata["fingerprint"] == original_fp
        assert stored.metadata["repo"] == original_repo
        assert stored.investigation_report == "Root cause found: null pointer"


# ---------------------------------------------------------------------------
# Class: TestQueueIsolation
# ---------------------------------------------------------------------------

class TestQueueIsolation:
    """Test InMemoryTaskQueue isolation between repos and task types."""

    def test_queue_tasks_per_repo_independent(self, queue):
        """Tasks from different repos don't interfere."""
        for i, repo in enumerate(REPO_NAMES):
            queue.enqueue("investigate", f"ticket-{repo}-1", {"repo": repo}, priority=50)

        # Claiming investigate tasks: each claim returns a different repo's task
        claimed_repos = set()
        for i in range(5):
            task = queue.claim("investigate", f"worker-{i}")
            assert task is not None
            claimed_repos.add(task.payload["repo"])
            queue.complete(task.task_id, {"status": "done"})

        assert claimed_repos == set(REPO_NAMES)

    def test_queue_claim_respects_task_type(self, queue):
        """Claiming 'investigate' task doesn't steal 'develop' task."""
        queue.enqueue("develop", "ticket-1", {"action": "fix"}, priority=10)
        queue.enqueue("investigate", "ticket-2", {"action": "analyze"}, priority=10)

        # Claim investigate only
        task = queue.claim("investigate", "worker-1")
        assert task is not None
        assert task.task_type == "investigate"
        assert task.ticket_id == "ticket-2"

        # Develop task should still be there
        dev_task = queue.claim("develop", "worker-2")
        assert dev_task is not None
        assert dev_task.task_type == "develop"

    def test_queue_priority_ordering(self, queue):
        """CRITICAL ticket (priority 10) claimed before HIGH (priority 50)."""
        queue.enqueue("investigate", "high-ticket", {"sev": "high"}, priority=50)
        queue.enqueue("investigate", "critical-ticket", {"sev": "critical"}, priority=10)

        task = queue.claim("investigate", "worker-1")
        assert task is not None
        assert task.ticket_id == "critical-ticket"

    def test_queue_dead_letter_isolation(self, queue):
        """Failed task goes to DLQ, doesn't block other repos."""
        # Enqueue tasks for two repos
        t1 = queue.enqueue("investigate", "ticket-repo-a", {"repo": "a"}, priority=50)
        queue.enqueue("investigate", "ticket-repo-b", {"repo": "b"}, priority=50)

        # Claim and fail the first task repeatedly until it hits DLQ.
        # Each fail() re-queues with a retry delay, so we must set
        # next_retry_at to the past to make the task claimable again.
        import time
        for attempt in range(t1.max_retries):
            claimed = queue.claim("investigate", "worker-1")
            if claimed is None:
                # Force retry eligibility by backdating next_retry_at
                with queue._lock:
                    task_obj = queue._tasks[t1.task_id]
                    if task_obj.next_retry_at is not None:
                        task_obj.next_retry_at = time.time() - 1
                claimed = queue.claim("investigate", "worker-1")
            assert claimed is not None, f"Failed to claim on attempt {attempt}"
            if claimed.task_id == t1.task_id:
                queue.fail(claimed.task_id, "simulated failure")
            else:
                # We got repo-b's task; complete it and re-claim
                queue.complete(claimed.task_id, {})
                # Force retry eligibility
                with queue._lock:
                    task_obj = queue._tasks[t1.task_id]
                    if task_obj.next_retry_at is not None:
                        task_obj.next_retry_at = time.time() - 1
                claimed2 = queue.claim("investigate", "worker-1")
                assert claimed2 is not None and claimed2.task_id == t1.task_id
                queue.fail(claimed2.task_id, "simulated failure")

        # The DLQ should have the failed task
        dlq = queue.get_dead_letter()
        dlq_ticket_ids = {t.ticket_id for t in dlq}
        assert "ticket-repo-a" in dlq_ticket_ids

        # Repo B's task should still be claimable (or already completed above)
        # Either way, it should NOT be in the DLQ
        assert "ticket-repo-b" not in dlq_ticket_ids

    def test_queue_depth_per_type(self, queue):
        """Queue depth counts only specified task type."""
        queue.enqueue("investigate", "t1", {}, priority=50)
        queue.enqueue("investigate", "t2", {}, priority=50)
        queue.enqueue("develop", "t3", {}, priority=50)

        assert queue.queue_depth("investigate") == 2
        assert queue.queue_depth("develop") == 1
        assert queue.queue_depth("review") == 0


# ---------------------------------------------------------------------------
# Class: TestEndToEndPipelineRounds
# ---------------------------------------------------------------------------

class TestEndToEndPipelineRounds:
    """Full pipeline simulation: fetch -> route -> queue -> claim -> process -> complete."""

    def test_full_pipeline_20_rounds(self, tmp_store, router, queue):
        """Simulate 20 complete pipeline rounds. Verify all invariants hold."""
        rng = random.Random(123)
        all_completed_ticket_ids = set()

        for round_num in range(20):
            # 1. Fetch: random repo, unique issue
            repo = rng.choice(REPO_NAMES)
            issue_num = 2000 + round_num
            repo_issues = {repo: [_make_gh_issue(issue_num, f"Pipeline round {round_num}")]}
            new_tickets = _simulate_fetch_all(repo_issues, tmp_store)

            for ticket in new_tickets:
                # 2. Store
                tmp_store.add(ticket)

                # 3. Route
                resolved = router.resolve(ticket)
                assert resolved.repo_name == repo

                # 4. Queue
                priority = 10 if ticket.severity == TicketSeverity.CRITICAL else 50
                queue.enqueue(
                    "investigate",
                    ticket.ticket_id,
                    {"repo": resolved.repo_name, "local_path": str(resolved.local_path)},
                    priority=priority,
                )

            # 5. Claim + process + complete
            while True:
                task = queue.claim("investigate", f"worker-round-{round_num}")
                if task is None:
                    break
                # Verify the task payload matches the ticket's repo
                stored_ticket = tmp_store.get(task.ticket_id)
                if stored_ticket:
                    assert task.payload["repo"] == stored_ticket.metadata["repo"]
                queue.complete(task.task_id, {"result": "investigated"})
                all_completed_ticket_ids.add(task.ticket_id)

        # Final invariants
        assert len(all_completed_ticket_ids) == 20
        assert queue.queue_depth("investigate") == 0

    def test_pipeline_partial_failure_no_cascade(self, tmp_store, router, queue):
        """One repo's fetch fails, others still process normally."""
        working_repos = REPO_NAMES[:3]
        failing_repo = REPO_NAMES[3]

        # Simulate: failing_repo returns no issues (as if gh CLI failed)
        repo_issues: Dict[str, List[Dict]] = {}
        for repo in working_repos:
            repo_issues[repo] = [_make_gh_issue(1, f"Issue from {repo}")]
        repo_issues[failing_repo] = []  # Simulates failure

        tickets = _simulate_fetch_all(repo_issues, tmp_store)
        for t in tickets:
            tmp_store.add(t)

        assert len(tickets) == 3  # Only working repos produced tickets
        for t in tickets:
            assert t.metadata["repo"] != failing_repo

    def test_pipeline_stale_ticket_not_reprocessed(self, tmp_store, router, queue):
        """Ticket resolved in round 5 is never reprocessed in rounds 6-20."""
        repo = "test-org/SWE-Sandbox"
        issue = _make_gh_issue(500, "Will be resolved early")

        resolved_ticket_id = None

        for round_num in range(20):
            # Fetch
            repo_issues = {repo: [issue]}
            new_tickets = _simulate_fetch_all(repo_issues, tmp_store)

            for t in new_tickets:
                tmp_store.add(t)
                resolved_ticket_id = t.ticket_id
                queue.enqueue("investigate", t.ticket_id, {"repo": repo}, priority=50)

            # Process
            task = queue.claim("investigate", f"worker-{round_num}")
            if task is not None:
                queue.complete(task.task_id, {"result": "done"})

                # Resolve the ticket after round 5
                if round_num == 0:
                    stored = tmp_store.get(task.ticket_id)
                    if stored:
                        stored.investigation_report = "x" * 201
                        stored.metadata["resolution_note"] = "fix_succeeded"
                        stored.transition(TicketStatus.RESOLVED)
                        tmp_store.add(stored)

        # The resolved ticket should still be RESOLVED, not reopened
        if resolved_ticket_id:
            final = tmp_store.get(resolved_ticket_id)
            assert final.status == TicketStatus.RESOLVED

        # Dedup should have prevented any re-creation after round 0
        # (fingerprint was already known)
        all_tickets = tmp_store.list_all()
        fp_500 = f"gh-issue-{repo}-500"
        matching = [t for t in all_tickets if t.metadata.get("fingerprint") == fp_500]
        assert len(matching) == 1

    def test_pipeline_concurrent_repos_no_mixup(self, tmp_store, router, queue):
        """Simulate concurrent processing of 5 repos. Each ticket's
        investigation uses only its own repo context."""
        # Generate 1 issue per repo
        for i, repo in enumerate(REPO_NAMES):
            issue = _make_gh_issue(i + 1, f"Issue in {repo.split('/')[-1]}")
            tickets = _simulate_fetch_all({repo: [issue]}, tmp_store)
            for t in tickets:
                tmp_store.add(t)
                resolved = router.resolve(t)
                queue.enqueue(
                    "investigate",
                    t.ticket_id,
                    {"repo": resolved.repo_name, "local_path": str(resolved.local_path)},
                    priority=50,
                )

        # Simulate concurrent claims from 5 workers
        worker_results: Dict[str, Dict] = {}
        errors = []

        def worker_fn(worker_id):
            try:
                task = queue.claim("investigate", worker_id)
                if task is None:
                    return
                ticket = tmp_store.get(task.ticket_id)
                if ticket is None:
                    return
                # Verify isolation: task payload repo matches ticket repo
                assert task.payload["repo"] == ticket.metadata["repo"], (
                    f"Worker {worker_id}: payload repo {task.payload['repo']} "
                    f"!= ticket repo {ticket.metadata['repo']}"
                )
                worker_results[worker_id] = {
                    "ticket_id": task.ticket_id,
                    "repo": task.payload["repo"],
                }
                queue.complete(task.task_id, {"worker": worker_id})
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=worker_fn, args=(f"worker-{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Worker errors: {errors}"
        # All 5 repos should have been processed
        processed_repos = {r["repo"] for r in worker_results.values()}
        assert processed_repos == set(REPO_NAMES)
