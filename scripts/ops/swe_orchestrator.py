#!/usr/bin/env python3
"""SWE-Squad Fleet Orchestrator — higher-level intelligence that manages the
entire agent fleet.

Detects systemic patterns (idle agents with pending work, stuck tickets,
throughput drops, failure cascades), takes corrective action where safe, and
creates GitHub issues for problems that require human intervention.

Usage::

    python3 scripts/ops/swe_orchestrator.py                # normal run
    python3 scripts/ops/swe_orchestrator.py --dry-run      # no mutations
    python3 scripts/ops/swe_orchestrator.py --verbose       # debug logging
    python3 scripts/ops/swe_orchestrator.py --daemon        # loop every N min
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Ensure src package is importable when run as a script
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.swe_team.config import TeamConfig
from src.swe_team.dependency_graph import DependencyGraph
from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.team_registry import TeamRegistry
from src.swe_team.workload_distributor import WorkloadDistributor

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("swe_orchestrator")

_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "swe_team"
_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "swe_team.yaml"


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class VMConfig:
    """A managed VM running SWE-Squad agents."""
    name: str
    ip: str
    team_id: str = ""
    ssh_alias: str = ""  # SSH config alias (e.g. "swe-squad-2"); used instead of ip when set


@dataclass
class OrchestratorConfig:
    """Runtime configuration for the orchestrator."""
    enabled: bool = True
    cycle_interval_minutes: int = 240
    auto_fix: bool = True
    dry_run: bool = False
    verbose: bool = False

    # Thresholds
    idle_with_work_minutes: int = 30
    stuck_ticket_hours: int = 6
    throughput_drop_percent: int = 50
    max_circuit_breaker_resets_per_day: int = 3

    # Managed VMs
    vms: List[VMConfig] = field(default_factory=list)

    # Supabase
    supabase_url: str = ""
    supabase_key: str = ""

    # A2A integration
    # NOTE: SWE-Squad is sandboxed. It does NOT register with the LinkedAi hub or any
    # external agentic system. The a2a_hub_url below is used only to EMIT outbound events
    # (fire-and-forget logging). SWE agents accept work ONLY via GitHub issues.
    a2a_hub_url: str = ""        # Outbound event sink only — never inbound task intake
    a2a_server_url: str = ""

    # Repos scanned for unassigned issues (populated from swe_team.yaml repos:)
    repos: List[str] = field(default_factory=list)
    teams: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_yaml_section(cls, data: Dict[str, Any]) -> "OrchestratorConfig":
        """Build config from the ``orchestrator:`` section of swe_team.yaml."""
        thresholds = data.get("thresholds", {})
        vms_raw = data.get("vms", [])
        vms = [VMConfig(**v) for v in vms_raw]
        return cls(
            enabled=data.get("enabled", True),
            cycle_interval_minutes=data.get("cycle_interval_minutes", 240),
            auto_fix=data.get("auto_fix", True),
            idle_with_work_minutes=thresholds.get("idle_with_work_minutes", 30),
            stuck_ticket_hours=thresholds.get("stuck_ticket_hours", 6),
            throughput_drop_percent=thresholds.get("throughput_drop_percent", 50),
            max_circuit_breaker_resets_per_day=thresholds.get(
                "max_circuit_breaker_resets_per_day", 3
            ),
            vms=vms,
        )


@dataclass
class Finding:
    """A detected systemic pattern or point failure."""
    category: str       # idle_with_work, stuck_tickets, throughput_drop, etc.
    severity: str       # info, warning, critical
    title: str
    description: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    recommended_action: str = ""
    auto_fixable: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Supabase Client (stdlib-only)
# ---------------------------------------------------------------------------

class SupabaseClient:
    """Minimal Supabase PostgREST client using only urllib."""

    def __init__(self, url: str, key: str) -> None:
        self.base_url = url.rstrip("/")
        self.key = key

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Optional[bytes] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        if extra_headers:
            headers.update(extra_headers)
        url = f"{self.base_url}/rest/v1/{path}"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            logger.warning("Supabase request failed: %s %s → %s", method, path, exc)
            return None

    def query_tickets(self, filters: str = "") -> Optional[List[Dict[str, Any]]]:
        """Query tickets with optional PostgREST filter string."""
        path = f"swe_tickets?{filters}" if filters else "swe_tickets"
        return self._request(path)

    def list_all(self, limit: int = 500) -> Optional[List[Dict[str, Any]]]:
        """Return all tickets, newest first."""
        path = f"swe_tickets?order=created_at.desc&limit={limit}"
        return self._request(path)

    def patch_ticket(self, ticket_id: str, updates: Dict[str, Any]) -> bool:
        path = f"swe_tickets?ticket_id=eq.{ticket_id}"
        result = self._request(
            path,
            method="PATCH",
            body=json.dumps(updates).encode(),
        )
        return result is not None

    def rpc(self, fn_name: str, params: Optional[Dict[str, Any]] = None) -> Any:
        body = json.dumps(params or {}).encode()
        return self._request(f"rpc/{fn_name}", method="POST", body=body)


# ---------------------------------------------------------------------------
# Pipeline Intelligence
# ---------------------------------------------------------------------------

class PipelineIntelligence:
    """Detects patterns that indicate systemic issues, not just point failures."""

    def __init__(self, config: OrchestratorConfig, db: Optional[SupabaseClient]) -> None:
        self._cfg = config
        self._db = db

    # -- helpers --

    def _get_tickets_by_status(self, status: str) -> List[Dict[str, Any]]:
        if not self._db:
            return []
        result = self._db.query_tickets(f"status=eq.{status}&order=created_at.desc")
        return result or []

    def _get_tickets_since(self, hours: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self._db:
            return []
        cutoff = datetime.now(timezone.utc).isoformat()
        filt = f"created_at=gte.{cutoff}"
        if status:
            filt += f"&status=eq.{status}"
        result = self._db.query_tickets(filt)
        return result or []

    def _count_by_status(self) -> Dict[str, int]:
        """Return ticket counts grouped by status."""
        if not self._db:
            return {}
        # Use a simple query — group counts client-side
        all_tickets = self._db.query_tickets("select=status") or []
        counts: Dict[str, int] = {}
        for t in all_tickets:
            s = t.get("status", "unknown")
            counts[s] = counts.get(s, 0) + 1
        return counts

    # -- detectors --

    def detect_idle_with_work(self) -> List[Finding]:
        """Agents idle but assigned issues exist -> scanner/filter/routing problem."""
        open_tickets = self._get_tickets_by_status("open")
        triaged = self._get_tickets_by_status("triaged")
        investigating = self._get_tickets_by_status("investigating")
        in_dev = self._get_tickets_by_status("in_development")

        available_work = len(open_tickets) + len(triaged)
        active_work = len(investigating) + len(in_dev)

        if available_work > 0 and active_work == 0:
            return [Finding(
                category="idle_with_work",
                severity="warning",
                title=f"Agents idle with {available_work} tickets waiting",
                description=(
                    f"{available_work} tickets in open/triaged status but no active "
                    f"investigations or development. Possible scanner, filter, or "
                    f"routing problem."
                ),
                evidence={
                    "open_count": len(open_tickets),
                    "triaged_count": len(triaged),
                    "investigating_count": len(investigating),
                    "in_development_count": len(in_dev),
                    "idle_vms": [vm.name for vm in self._cfg.vms],
                },
                recommended_action="Trigger runner cycle on idle VMs to pick up waiting work.",
                auto_fixable=True,
            )]
        return []

    def detect_stuck_tickets(self) -> List[Finding]:
        """Tickets in same status for >N hours -> status transition bug or exhausted attempts."""
        findings: List[Finding] = []
        for status in ("investigating", "in_development"):
            tickets = self._get_tickets_by_status(status)
            stuck = []
            now = datetime.now(timezone.utc)
            for t in tickets:
                updated = t.get("updated_at") or t.get("created_at", "")
                if not updated:
                    continue
                try:
                    ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    age_hours = (now - ts).total_seconds() / 3600
                    if age_hours > self._cfg.stuck_ticket_hours:
                        stuck.append({
                            "ticket_id": t.get("ticket_id"),
                            "title": t.get("title", "")[:80],
                            "hours_stuck": round(age_hours, 1),
                            "status": status,
                        })
                except (ValueError, TypeError):
                    continue

            if stuck:
                findings.append(Finding(
                    category="stuck_tickets",
                    severity="warning" if len(stuck) < 5 else "critical",
                    title=f"{len(stuck)} tickets stuck in {status} for >{self._cfg.stuck_ticket_hours}h",
                    description=(
                        f"Tickets have been in '{status}' status beyond the "
                        f"{self._cfg.stuck_ticket_hours}h threshold. This may "
                        f"indicate a status-transition bug, exhausted attempts, "
                        f"or a hung agent process."
                    ),
                    evidence={"stuck_tickets": stuck[:20]},
                    recommended_action=(
                        "Reset stuck tickets to investigation_complete or open for retry."
                    ),
                    auto_fixable=True,
                ))
        return findings

    def detect_throughput_drop(self, resolved_last_24h: int = -1, resolved_prev_24h: int = -1) -> List[Finding]:
        """Resolved rate dropped >N% vs prior period -> something changed."""
        if resolved_last_24h < 0 or resolved_prev_24h < 0:
            # Cannot compute without data
            return []

        if resolved_prev_24h == 0:
            return []

        drop_pct = ((resolved_prev_24h - resolved_last_24h) / resolved_prev_24h) * 100
        if drop_pct >= self._cfg.throughput_drop_percent:
            return [Finding(
                category="throughput_drop",
                severity="warning" if drop_pct < 80 else "critical",
                title=f"Throughput dropped {drop_pct:.0f}% vs prior 24h",
                description=(
                    f"Resolved {resolved_last_24h} tickets in last 24h vs "
                    f"{resolved_prev_24h} in prior 24h ({drop_pct:.0f}% drop)."
                ),
                evidence={
                    "resolved_last_24h": resolved_last_24h,
                    "resolved_prev_24h": resolved_prev_24h,
                    "drop_percent": round(drop_pct, 1),
                },
                recommended_action="Check agent logs for rate limits, infra failures, or config changes.",
                auto_fixable=False,
            )]
        return []

    def detect_failure_cascade(self) -> List[Finding]:
        """Multiple failures in short window -> rate limit or infra issue."""
        failed = self._get_tickets_by_status("failed")
        if len(failed) < 3:
            return []

        # Check if multiple failures happened within 1 hour of each other
        timestamps = []
        for t in failed[:20]:
            ua = t.get("updated_at") or t.get("created_at", "")
            try:
                timestamps.append(datetime.fromisoformat(ua.replace("Z", "+00:00")))
            except (ValueError, TypeError):
                continue

        if len(timestamps) < 3:
            return []

        timestamps.sort(reverse=True)
        # Check if 3+ failures within a 2-hour window
        recent = timestamps[:10]
        if len(recent) >= 3:
            span = (recent[0] - recent[2]).total_seconds() / 3600
            if span <= 2.0:
                return [Finding(
                    category="failure_cascade",
                    severity="critical",
                    title=f"{len(recent)} failures within {span:.1f}h — possible cascade",
                    description=(
                        "Multiple ticket failures in a short window suggest a "
                        "systemic issue (rate limits, infra outage, bad deploy) "
                        "rather than individual code bugs."
                    ),
                    evidence={
                        "failed_count": len(failed),
                        "window_hours": round(span, 2),
                        "sample_ids": [t.get("ticket_id") for t in failed[:5]],
                    },
                    recommended_action=(
                        "Check agent logs for rate-limit errors (429), SSH failures, "
                        "or Supabase outages. Consider resetting circuit breaker."
                    ),
                    auto_fixable=False,
                )]
        return []

    def detect_work_starvation(self) -> List[Finding]:
        """All repos nearly empty -> need to create/assign more issues."""
        counts = self._count_by_status()
        total_active = sum(
            counts.get(s, 0)
            for s in ("open", "triaged", "investigating", "in_development")
        )
        if total_active == 0:
            return [Finding(
                category="work_starvation",
                severity="info",
                title="No active work — pipeline is starved",
                description="Zero tickets in any active status. Agents are completely idle.",
                evidence={"status_counts": counts},
                recommended_action="Reset exhausted feature tickets and trigger runner cycles.",
                auto_fixable=True,
            )]
        return []

    def detect_config_mismatch(self, severity_filter: str = "medium") -> List[Finding]:
        """Severity filter blocking available work."""
        if not self._db:
            return []

        severity_order = ["low", "medium", "high", "critical"]
        try:
            filter_idx = severity_order.index(severity_filter)
        except ValueError:
            return []

        blocked_severities = severity_order[:filter_idx]
        if not blocked_severities:
            return []

        # Count tickets at blocked severities that are in open/triaged
        blocked_count = 0
        for sev in blocked_severities:
            tickets = self._db.query_tickets(
                f"severity=eq.{sev}&status=in.(open,triaged)"
            ) or []
            blocked_count += len(tickets)

        active = self._get_tickets_by_status("investigating")
        active += self._get_tickets_by_status("in_development")

        if blocked_count > 5 and len(active) == 0:
            return [Finding(
                category="config_mismatch",
                severity="warning",
                title=f"Severity filter '{severity_filter}' blocking {blocked_count} tickets",
                description=(
                    f"{blocked_count} tickets at severity below '{severity_filter}' "
                    f"are waiting but agents are idle. Consider lowering the filter."
                ),
                evidence={
                    "severity_filter": severity_filter,
                    "blocked_count": blocked_count,
                    "blocked_severities": blocked_severities,
                },
                recommended_action=f"Lower severity_filter from '{severity_filter}' to 'low'.",
                auto_fixable=True,
            )]
        return []

    def detect_unassigned_issues(self) -> Optional[Finding]:
        """Scan all configured repos for open issues not assigned to any bot.

        For each repo in ``config.repos``, runs ``gh issue list`` and filters
        for issues with no assignees.  Returns a single Finding listing all
        unassigned issues, or ``None`` if everything is assigned.
        """
        if not self._cfg.repos:
            return None

        # Bot accounts recognised by this orchestrator
        _BOT_ACCOUNTS = set(os.environ.get("SWE_BOT_ACCOUNTS", "").split(",")) if os.environ.get("SWE_BOT_ACCOUNTS") else {"bot-alpha", "bot-beta"}

        unassigned: List[Dict[str, Any]] = []
        for repo in self._cfg.repos:
            try:
                result = subprocess.run(
                    [
                        "gh", "issue", "list",
                        "--repo", repo,
                        "--state", "open",
                        "--json", "number,title,assignees,labels",
                        "--limit", "100",
                    ],
                    capture_output=True, text=True, timeout=20,
                )
                if result.returncode != 0:
                    logger.debug("gh issue list failed for %s: %s", repo, result.stderr[:200])
                    continue
                issues = json.loads(result.stdout.strip() or "[]")
                for issue in issues:
                    assignees = issue.get("assignees") or []
                    assignee_logins = {
                        a.get("login", "").lower() for a in assignees
                    }
                    # Unassigned = no assignees at all, OR none of the assignees
                    # is a known bot account
                    if not assignees or not (assignee_logins & _BOT_ACCOUNTS):
                        unassigned.append({
                            "repo": repo,
                            "number": issue.get("number"),
                            "title": (issue.get("title") or "")[:100],
                            "labels": [
                                lbl.get("name", "") for lbl in (issue.get("labels") or [])
                            ],
                            "assignees": list(assignee_logins),
                        })
            except Exception as exc:
                logger.debug("detect_unassigned_issues error for %s: %s", repo, exc)

        if not unassigned:
            return None

        return Finding(
            category="unassigned_issues",
            severity="warning",
            title=f"{len(unassigned)} open issue(s) have no bot assignee",
            description=(
                f"Found {len(unassigned)} open GitHub issue(s) across configured repos "
                f"with no SWE-Squad bot assigned. Idle agents cannot pick up work until "
                f"issues are assigned."
            ),
            evidence={"unassigned_issues": unassigned},
            recommended_action="Assign idle bot accounts to these issues.",
            auto_fixable=True,
        )

    def detect_idle_agents(self) -> Optional[Finding]:
        """Find bot teams with zero active workable tickets that could take on work.

        Queries Supabase for active ticket counts per ``team_id`` (statuses:
        open, triaged, investigating, in_development).  A team is considered
        idle when it has zero such tickets.  Returns a Finding listing idle
        teams, or ``None`` if all teams are busy.
        """
        if not self._db:
            return None

        _ACTIVE_STATUSES = ("open", "triaged", "investigating", "in_development")
        _SKIP_LABELS = ("umbrella", "hitl")

        # Count active workable tickets per team
        team_counts: Dict[str, int] = {vm.team_id: 0 for vm in self._cfg.vms if vm.team_id}

        for status in _ACTIVE_STATUSES:
            tickets = self._get_tickets_by_status(status)
            for t in tickets:
                # Exclude umbrella / hitl tickets
                labels = t.get("labels") or []
                if isinstance(labels, str):
                    try:
                        labels = json.loads(labels)
                    except (ValueError, TypeError):
                        labels = []
                label_names = [str(lbl).lower() for lbl in labels]
                if any(skip in label_names for skip in _SKIP_LABELS):
                    continue
                team = t.get("team_id", "")
                if team in team_counts:
                    team_counts[team] += 1

        idle_teams = [tid for tid, cnt in team_counts.items() if cnt == 0]
        if not idle_teams:
            return None

        return Finding(
            category="idle_agents",
            severity="info",
            title=f"{len(idle_teams)} team(s) idle with no active tickets",
            description=(
                f"Team(s) {idle_teams} have zero active workable tickets. "
                f"They are available to take on new issues from the backlog."
            ),
            evidence={
                "idle_teams": idle_teams,
                "team_ticket_counts": team_counts,
            },
            recommended_action="Assign unassigned GitHub issues to these idle teams.",
            auto_fixable=True,
        )

    def detect_stale_github_issues(self) -> List[Finding]:
        """Find resolved/closed Supabase tickets whose GitHub issues are still open.

        For each resolved or closed ticket that has ``metadata.github_issue`` and
        ``metadata.repo``, check whether the GitHub issue is still open via
        ``gh issue view``.  Returns a finding listing all stale issues so that
        :meth:`OrchestratorActions.close_stale_github_issues` can close them.
        """
        if not self._db:
            return []

        stale: List[Dict[str, Any]] = []
        for status in ("resolved", "closed"):
            tickets = self._get_tickets_by_status(status)
            for t in tickets:
                # metadata is stored as a JSON column; handle both dict and None
                meta = t.get("metadata") or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (ValueError, TypeError):
                        meta = {}

                issue_num = meta.get("github_issue")
                repo = meta.get("repo", "")
                if not issue_num or not repo:
                    continue

                # Check current state via gh CLI
                try:
                    result = subprocess.run(
                        [
                            "gh", "issue", "view", str(issue_num),
                            "--repo", repo,
                            "--json", "state,number",
                        ],
                        capture_output=True, text=True, timeout=15,
                    )
                    if result.returncode != 0:
                        continue
                    data = json.loads(result.stdout.strip() or "{}")
                    if data.get("state", "").lower() == "open":
                        stale.append({
                            "ticket_id": t.get("ticket_id"),
                            "ticket_status": status,
                            "github_issue": issue_num,
                            "repo": repo,
                        })
                except Exception as exc:
                    logger.debug("stale-issue check failed for #%s in %s: %s", issue_num, repo, exc)

        if not stale:
            return []

        return [Finding(
            category="stale_github_issues",
            severity="warning",
            title=f"{len(stale)} resolved ticket(s) have stale open GitHub issues",
            description=(
                f"{len(stale)} ticket(s) are resolved/closed in Supabase but their "
                f"corresponding GitHub issues remain open, making the backlog appear larger "
                f"than it is."
            ),
            evidence={"stale_issues": stale},
            recommended_action="Close the stale GitHub issues to reflect resolved status.",
            auto_fixable=True,
        )]

    def detect_github_supabase_desync(self) -> List[Finding]:
        """Detect GitHub issues that are open but have closed/failed Supabase tickets."""
        if not self._db:
            return []

        # Get open GH issues across all configured repos
        open_gh: set[int] = set()
        for repo in self._cfg.repos:
            try:
                result = subprocess.run(
                    ["gh", "issue", "list", "--state", "open", "--json", "number",
                     "--jq", ".[].number", "--repo", repo],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    continue
                for n in result.stdout.strip().split('\n'):
                    if n.strip().isdigit():
                        open_gh.add(int(n))
            except Exception as exc:
                logger.debug("detect_github_supabase_desync: gh list failed for %s: %s", repo, exc)

        if not open_gh:
            return []

        # Check Supabase for tickets whose GH issue is open but ticket is closed/failed
        all_tickets = self._db.list_all(limit=500) or []
        desynced: List[Dict[str, Any]] = []
        for t in all_tickets:
            meta = t.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (ValueError, TypeError):
                    meta = {}
            gh_num = meta.get("github_issue")
            status = t.get("status", "")
            if gh_num and int(gh_num) in open_gh and status in ("closed", "resolved", "failed"):
                desynced.append({
                    "ticket_id": t.get("ticket_id"),
                    "github_issue": gh_num,
                    "status": status,
                })

        if desynced:
            return [Finding(
                category="github_supabase_desync",
                severity="warning",
                title=f"{len(desynced)} GitHub issues open but Supabase tickets closed/failed",
                description="Open GitHub issues have no active Supabase ticket. Work is stalled.",
                evidence={"desynced": desynced[:20]},
                recommended_action="Reopen Supabase tickets for active GitHub issues.",
                auto_fixable=True,
            )]
        return []

    def run_all_detections(self, severity_filter: str = "medium") -> List[Finding]:
        """Run all detection functions and return combined findings."""
        findings: List[Finding] = []

        # Detectors that return List[Finding]
        list_detectors = [
            self.detect_idle_with_work,
            self.detect_stuck_tickets,
            lambda: self.detect_throughput_drop(),
            self.detect_failure_cascade,
            self.detect_work_starvation,
            lambda: self.detect_config_mismatch(severity_filter),
            self.detect_stale_github_issues,
            self.detect_github_supabase_desync,
        ]
        for detect_fn in list_detectors:
            try:
                findings.extend(detect_fn())
            except Exception as exc:
                logger.error("Detection failed: %s", exc)

        # Detectors that return Optional[Finding]
        optional_detectors = [
            self.detect_unassigned_issues,
            self.detect_idle_agents,
        ]
        for detect_fn in optional_detectors:
            try:
                result = detect_fn()
                if result is not None:
                    findings.append(result)
            except Exception as exc:
                logger.error("Detection failed: %s", exc)

        return findings


# ---------------------------------------------------------------------------
# Corrective Actions
# ---------------------------------------------------------------------------

class OrchestratorActions:
    """Takes corrective action based on findings."""

    def __init__(
        self,
        config: OrchestratorConfig,
        db: Optional[SupabaseClient],
        dry_run: bool = False,
    ) -> None:
        self._cfg = config
        self._db = db
        self._dry_run = dry_run
        self._actions_taken: List[Dict[str, Any]] = []

    @property
    def actions_taken(self) -> List[Dict[str, Any]]:
        return list(self._actions_taken)

    def _log_action(self, action: str, detail: str, success: bool) -> None:
        entry = {
            "action": action,
            "detail": detail,
            "success": success,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dry_run": self._dry_run,
        }
        self._actions_taken.append(entry)
        level = logging.INFO if success else logging.WARNING
        prefix = "[DRY-RUN] " if self._dry_run else ""
        logger.log(level, "%s%s: %s (success=%s)", prefix, action, detail, success)

    def _ssh_command(self, vm: VMConfig, cmd: str, timeout: int = 30) -> Tuple[bool, str]:
        """Run a command on a VM via SSH.

        Uses the scoped SSH config (config/ssh_workers.conf) if available,
        falling back to direct agent@ip connection.
        """
        if self._dry_run:
            self._log_action("ssh", f"{vm.name}: {cmd}", True)
            return True, "(dry-run)"
        ssh_config = _PROJECT_ROOT / "config" / "ssh_workers.conf"
        try:
            ssh_cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no"]
            if ssh_config.exists():
                ssh_cmd.extend(["-F", str(ssh_config)])
            # Use SSH config alias if available (handles identity keys),
            # otherwise fall back to agent@ip
            ssh_target = vm.ssh_alias if vm.ssh_alias else f"agent@{vm.ip}"
            ssh_cmd.extend([ssh_target, cmd])
            result = subprocess.run(
                ssh_cmd, capture_output=True, text=True, timeout=timeout,
            )
            return result.returncode == 0, result.stdout + result.stderr
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, str(exc)

    def _gh_command(self, args: List[str], timeout: int = 30) -> Tuple[bool, str]:
        """Run a gh CLI command."""
        if self._dry_run:
            self._log_action("gh", " ".join(args), True)
            return True, "(dry-run)"
        try:
            result = subprocess.run(
                ["gh"] + args,
                capture_output=True, text=True, timeout=timeout,
            )
            return result.returncode == 0, result.stdout + result.stderr
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            return False, str(exc)

    def fix_stuck_tickets(self, finding: Finding) -> int:
        """Reset stuck tickets: investigating -> open, in_development -> investigation_complete."""
        if not self._db:
            return 0
        stuck = finding.evidence.get("stuck_tickets", [])
        fixed = 0
        for item in stuck:
            tid = item.get("ticket_id")
            status = item.get("status")
            if not tid:
                continue
            new_status = "open" if status == "investigating" else "investigation_complete"
            if self._dry_run:
                self._log_action("fix_stuck", f"{tid}: {status} -> {new_status}", True)
                fixed += 1
                continue
            ok = self._db.patch_ticket(tid, {"status": new_status})
            self._log_action("fix_stuck", f"{tid}: {status} -> {new_status}", ok)
            if ok:
                fixed += 1
        return fixed

    def reset_circuit_breaker(self, vm: VMConfig) -> bool:
        """Reset the circuit breaker on a VM by removing the state file."""
        cmd = "rm -f data/swe_team/circuit_breaker.json"
        ok, output = self._ssh_command(vm, cmd)
        self._log_action("reset_circuit_breaker", f"{vm.name}: {output[:200]}", ok)
        return ok

    def remove_git_locks(self, vm: VMConfig) -> int:
        """Find and remove stale .git/index.lock files on a VM."""
        cmd = "find ~/Projects -name 'index.lock' -path '*/.git/*' -mmin +10 -delete -print 2>/dev/null"
        ok, output = self._ssh_command(vm, cmd, timeout=15)
        removed = len(output.strip().splitlines()) if ok and output.strip() else 0
        self._log_action("remove_git_locks", f"{vm.name}: removed {removed}", ok)
        return removed

    def deploy_latest(self) -> bool:
        """Rsync latest code to all VMs using propagate.sh."""
        script = Path(__file__).resolve().parent / "propagate.sh"
        if not script.exists():
            self._log_action("deploy_latest", "propagate.sh not found", False)
            return False
        if self._dry_run:
            self._log_action("deploy_latest", "would run propagate.sh", True)
            return True
        try:
            result = subprocess.run(
                [str(script)], capture_output=True, text=True, timeout=60,
            )
            ok = result.returncode == 0
            self._log_action("deploy_latest", result.stdout[:300], ok)
            return ok
        except (subprocess.TimeoutExpired, OSError) as exc:
            self._log_action("deploy_latest", str(exc), False)
            return False

    def create_github_issue(self, finding: Finding, repo: str = "") -> Optional[str]:
        """Create a GitHub issue for a finding, deduped by title prefix."""
        if not repo:
            repo = os.environ.get("SWE_GITHUB_REPO", "")
        if not repo:
            self._log_action("create_issue", "no repo configured", False)
            return None

        prefix = f"[Orchestrator] {finding.category}"

        # Dedup: check for existing open issue with same prefix
        ok, existing = self._gh_command(
            ["issue", "list", "--repo", repo, "--state", "open",
             "--search", prefix, "--json", "number,title", "--limit", "5"]
        )
        if ok and existing.strip():
            try:
                issues = json.loads(existing)
                for issue in issues:
                    if issue.get("title", "").startswith(prefix):
                        logger.info("Skipping duplicate issue: %s", issue.get("title"))
                        return None
            except (json.JSONDecodeError, TypeError):
                pass

        body = (
            f"## {finding.title}\n\n"
            f"{finding.description}\n\n"
            f"**Severity:** {finding.severity}\n"
            f"**Category:** {finding.category}\n"
            f"**Recommended Action:** {finding.recommended_action}\n\n"
            f"### Evidence\n```json\n{json.dumps(finding.evidence, indent=2)}\n```\n\n"
            f"*Auto-generated by SWE-Squad Orchestrator at {finding.timestamp}*"
        )

        ok, output = self._gh_command([
            "issue", "create", "--repo", repo,
            "--title", f"{prefix}: {finding.title}",
            "--body", body,
            "--label", "orchestrator",
        ])
        if ok:
            # Extract issue URL from output
            url = output.strip().splitlines()[-1] if output.strip() else None
            self._log_action("create_issue", f"created: {url}", True)
            return url
        self._log_action("create_issue", output[:200], False)
        return None

    def close_stale_github_issues(self, finding: Finding) -> int:
        """DISABLED — agents must NEVER close GitHub issues.

        GitHub issues are the source of truth. Internal Supabase tickets may
        be closed/stale for many reasons (exhausted attempts, false positives,
        agent failures) but the GitHub issue represents the REAL work item
        that a human created. Only agents with the ``close_issues`` RBAC
        permission may close GitHub issues after verifying the work is done.
        This permission must be enforced via the RBAC engine, not hardcoded.

        Previously this function auto-closed GitHub issues when internal
        tickets were resolved, causing critical issues (#627, #617-#622,
        #625-#626, #636) to be wrongly closed. See incident #591.
        """
        logger.warning(
            "close_stale_github_issues: DISABLED — agents must not close GitHub issues. "
            "Found %d stale issues that would have been closed.",
            len(finding.evidence.get("stale_issues", [])),
        )
        return 0
        # --- DISABLED CODE BELOW ---
        stale = finding.evidence.get("stale_issues", [])
        closed = 0
        for item in stale:
            issue_num = item.get("github_issue")
            repo = item.get("repo", "")
            ticket_id = item.get("ticket_id", "unknown")
            ticket_status = item.get("ticket_status", "resolved")
            if not issue_num or not repo:
                continue

            comment = (
                f"Closed by SWE-Squad Orchestrator — the corresponding internal ticket "
                f"`{ticket_id}` was already {ticket_status}. Closing to keep the backlog accurate."
            )

            if self._dry_run:
                self._log_action(
                    "close_stale_issue",
                    f"#{issue_num} in {repo} (ticket {ticket_id})",
                    True,
                )
                closed += 1
                continue

            # Post comment then close
            try:
                comment_ok, _ = self._gh_command(
                    ["issue", "comment", str(issue_num), "--repo", repo, "--body", comment],
                    timeout=15,
                )
                if not comment_ok:
                    logger.warning("close_stale_github_issues: comment failed for #%d in %s", issue_num, repo)
            except Exception as exc:
                logger.warning("close_stale_github_issues: comment error for #%d: %s", issue_num, exc)

            ok, output = self._gh_command(
                ["issue", "close", str(issue_num), "--repo", repo],
                timeout=15,
            )
            self._log_action(
                "close_stale_issue",
                f"#{issue_num} in {repo} (ticket {ticket_id}): {output[:100]}",
                ok,
            )
            if ok:
                closed += 1

        return closed

    # -- New auto-remediation methods --

    def _trigger_vm_cycle(self, vm: VMConfig) -> bool:
        """SSH to a VM and trigger a runner cycle.

        Uses a fire-and-forget pattern: the remote command backgrounds
        itself and SSH exits immediately. We treat any non-timeout exit
        as success (the background process may not have started yet, but
        SSH itself succeeded).
        """
        cmd = (
            "cd ~/Projects/SWE-Squad && flock -n /tmp/swe_runner.lock "
            "-c 'SWE_TEAM_ENABLED=true python3 scripts/ops/swe_team_runner.py "
            "--max-cycles 1 -v >> logs/cron.log 2>&1' & disown; echo triggered"
        )
        ok, output = self._ssh_command(vm, cmd, timeout=60)
        self._log_action(
            "trigger_vm",
            f"Triggered cycle on {vm.name} ({vm.ip}): {output.strip()[:100]}",
            ok,
        )
        return ok

    def fix_exhausted_tickets(self) -> int:
        """Reset exhausted feature tickets for retry."""
        if not self._db:
            return 0
        count = 0
        for status in ("failed", "investigation_complete"):
            tickets = self._db.query_tickets(f"status=eq.{status}&order=updated_at.desc&limit=50") or []
            for t in tickets:
                meta = t.get("metadata") or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (ValueError, TypeError):
                        meta = {}
                attempts = meta.get("dev_attempts", 0)
                if attempts >= 3 and self._is_feature_ticket(t):
                    tid = t.get("ticket_id")
                    if not tid:
                        continue
                    meta["dev_attempts"] = 0
                    meta["attempt_reset_by"] = "orchestrator"
                    meta["attempt_reset_at"] = datetime.now(timezone.utc).isoformat()
                    new_status = "investigation_complete" if status == "failed" else status
                    if self._dry_run:
                        self._log_action("reset_exhausted", f"{tid}: reset attempts", True)
                        count += 1
                        continue
                    ok = self._db.patch_ticket(tid, {
                        "status": new_status,
                        "metadata": json.dumps(meta),
                    })
                    self._log_action("reset_exhausted", f"{tid}: reset attempts", ok)
                    if ok:
                        count += 1
        return count

    def _is_feature_ticket(self, ticket: Dict[str, Any]) -> bool:
        """Check if a ticket is a feature/enhancement (not a bug)."""
        raw_labels = ticket.get("labels") or []
        if isinstance(raw_labels, str):
            try:
                raw_labels = json.loads(raw_labels)
            except (ValueError, TypeError):
                raw_labels = []
        labels = {str(lbl).lower() for lbl in raw_labels}
        if labels & {"enhancement", "feature", "webui", "frontend"}:
            return True
        title = ticket.get("title") or ""
        return "[WebUI]" in title or "[feature]" in title.lower()

    def fix_work_starvation(self, finding: Finding) -> int:
        """Fix starved pipeline: reset exhausted tickets, trigger idle VMs."""
        count = 0
        # Reset exhausted feature tickets
        count += self.fix_exhausted_tickets()
        # Trigger runner on all VMs
        for vm in self._cfg.vms:
            if self._trigger_vm_cycle(vm):
                count += 1
        return count

    def fix_idle_with_work(self, finding: Finding) -> int:
        """Fix idle agents: trigger runner cycle on the idle VM(s)."""
        count = 0
        idle_vms = finding.evidence.get("idle_vms", [])
        for vm_name in idle_vms:
            vm = next((v for v in self._cfg.vms if v.name == vm_name), None)
            if vm and self._trigger_vm_cycle(vm):
                count += 1
        if not idle_vms:
            # No specific VM identified, trigger all
            for vm in self._cfg.vms:
                if self._trigger_vm_cycle(vm):
                    count += 1
        return count

    # Map team_id → GitHub bot account login
    _TEAM_BOT_MAP: Dict[str, str] = {
        "team-alpha": "bot-alpha",
        "bot-alpha": "bot-alpha",
        "bot-beta": "bot-beta",
        "team-beta": "bot-beta",
    }

    # Hard cap to avoid flooding repos in a single cycle
    _MAX_ASSIGNMENTS_PER_CYCLE: int = 10

    def assign_issues_to_idle_agents(self, finding: Finding) -> int:
        """Assign unassigned GitHub issues to idle bot agents via the gh CLI.

        Uses ``unassigned_issues`` from the finding evidence together with the
        ``idle_agents`` finding (if available via ``finding.evidence``).  When
        both datasets are present they are joined; otherwise only issue
        assignment by round-robin across all known bots is performed.

        Assignment rules:

        * Only assign to bots listed in ``_TEAM_BOT_MAP``.
        * Pick the bot whose team has the fewest active tickets
          (carried in ``team_ticket_counts`` inside the evidence, or falls back
          to round-robin if that data is absent).
        * Cap total assignments at ``_MAX_ASSIGNMENTS_PER_CYCLE`` (default 10).
        * Never modify issue content — only ``gh issue edit --add-assignee``.

        Returns the number of assignments successfully made.
        """
        unassigned = finding.evidence.get("unassigned_issues", [])
        if not unassigned:
            return 0

        # Build an ordered list of (bot_login, active_ticket_count) sorted by
        # fewest tickets first so we load-balance automatically.
        team_counts: Dict[str, int] = finding.evidence.get("team_ticket_counts", {})
        bot_load: Dict[str, int] = {}
        for team_id, bot_login in self._TEAM_BOT_MAP.items():
            if bot_login not in bot_load:
                # Use ticket count for any matching team_id key; default 0.
                count = team_counts.get(team_id, 0)
                bot_load[bot_login] = count

        if not bot_load:
            self._log_action("assign_issues", "no bot accounts configured", False)
            return 0

        assigned_count = 0
        # Track per-bot assignment counts within this cycle for load-balancing
        cycle_load: Dict[str, int] = {bot: 0 for bot in bot_load}

        for issue in unassigned:
            if assigned_count >= self._MAX_ASSIGNMENTS_PER_CYCLE:
                logger.info(
                    "assign_issues_to_idle_agents: reached cap of %d assignments",
                    self._MAX_ASSIGNMENTS_PER_CYCLE,
                )
                break

            repo = issue.get("repo", "")
            number = issue.get("number")
            if not repo or number is None:
                continue

            # Pick bot with fewest total load (base + cycle)
            chosen_bot = min(bot_load, key=lambda b: bot_load[b] + cycle_load.get(b, 0))

            detail = f"#{number} in {repo} → {chosen_bot}"
            if self._dry_run:
                self._log_action("assign_issue", detail, True)
                assigned_count += 1
                cycle_load[chosen_bot] = cycle_load.get(chosen_bot, 0) + 1
                continue

            ok, output = self._gh_command(
                [
                    "issue", "edit", str(number),
                    "--repo", repo,
                    "--add-assignee", chosen_bot,
                ],
                timeout=20,
            )
            self._log_action("assign_issue", f"{detail}: {output[:100]}", ok)
            if ok:
                assigned_count += 1
                cycle_load[chosen_bot] = cycle_load.get(chosen_bot, 0) + 1
                # Increment the base load so subsequent picks account for it
                bot_load[chosen_bot] = bot_load[chosen_bot] + 1

        return assigned_count

    def fix_github_supabase_desync(self, finding: Finding) -> int:
        """Reopen Supabase tickets for open GitHub issues."""
        if not self._db:
            return 0
        desynced = finding.evidence.get("desynced", [])
        count = 0
        for item in desynced:
            tid = item.get("ticket_id")
            status = item.get("status", "")
            if not tid or status not in ("closed", "resolved", "failed"):
                continue
            updates = {
                "status": "open",
                "metadata": json.dumps({
                    "dev_attempts": 0,
                    "investigation_attempts": 0,
                    "attempt_reset": "orchestrator auto-sync",
                }),
            }
            if self._dry_run:
                self._log_action("fix_desync", f"{tid}: {status} -> open", True)
                count += 1
                continue
            ok = self._db.patch_ticket(tid, updates)
            self._log_action("fix_desync", f"{tid}: {status} -> open", ok)
            if ok:
                count += 1
        return count

    def execute(self, finding: Finding) -> bool:
        """Execute the appropriate corrective action for a finding."""
        if finding.category == "stuck_tickets":
            count = self.fix_stuck_tickets(finding)
            return count > 0
        if finding.category == "config_mismatch":
            # Config changes require SSH to VMs — log recommendation only
            self._log_action(
                "config_recommendation",
                finding.recommended_action,
                True,
            )
            return True
        if finding.category == "stale_github_issues":
            count = self.close_stale_github_issues(finding)
            return count > 0
        if finding.category in ("unassigned_issues", "idle_agents"):
            count = self.assign_issues_to_idle_agents(finding)
            return count > 0
        if finding.category == "work_starvation":
            count = self.fix_work_starvation(finding)
            logger.info("Auto-fix: work_starvation — %d actions taken", count)
            return count > 0
        if finding.category == "idle_with_work":
            count = self.fix_idle_with_work(finding)
            logger.info("Auto-fix: idle_with_work — %d VMs triggered", count)
            return count > 0
        if finding.category == "github_supabase_desync":
            count = self.fix_github_supabase_desync(finding)
            logger.info("Auto-fix: github_supabase_desync — %d tickets reopened", count)
            return count > 0
        return False


# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------

def generate_report(findings: List[Finding], actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate a structured orchestrator report."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "finding_count": len(findings),
        "findings_by_severity": {
            sev: len([f for f in findings if f.severity == sev])
            for sev in ("critical", "warning", "info")
        },
        "findings": [asdict(f) for f in findings],
        "actions_taken": actions,
        "summary": _build_summary(findings),
    }


def _build_summary(findings: List[Finding]) -> str:
    if not findings:
        return "All clear — no systemic issues detected."
    lines = [f"Detected {len(findings)} issue(s):"]
    for f in findings:
        lines.append(f"  [{f.severity.upper()}] {f.title}")
    return "\n".join(lines)


def save_report(report: Dict[str, Any], path: Optional[Path] = None) -> Path:
    """Save the report to JSON."""
    if path is None:
        path = _DATA_DIR / "orchestrator_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    logger.info("Report saved to %s", path)
    return path


# ---------------------------------------------------------------------------
# Main Orchestration Cycle
# ---------------------------------------------------------------------------

def load_orchestrator_config() -> OrchestratorConfig:
    """Load orchestrator config from swe_team.yaml + env vars."""
    config = OrchestratorConfig()
    try:
        import yaml  # noqa: F811
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH) as f:
                raw = yaml.safe_load(f) or {}
            orch_section = raw.get("orchestrator", {})
            if orch_section:
                config = OrchestratorConfig.from_yaml_section(orch_section)

            # Populate repos from the top-level ``repos:`` list so that the
            # orchestrator can scan for unassigned issues without duplicating
            # the list in the ``orchestrator:`` sub-section.
            repos_raw = raw.get("repos", [])
            config.repos = [
                r["name"] for r in repos_raw if isinstance(r, dict) and r.get("name")
            ]
            config.teams = raw.get("teams", {}) if isinstance(raw.get("teams", {}), dict) else {}
    except ImportError:
        logger.warning("PyYAML not available; using default config")
    except Exception as exc:
        logger.warning("Failed to load config: %s", exc)

    config.supabase_url = os.environ.get("SUPABASE_URL", "")
    config.supabase_key = os.environ.get("SUPABASE_ANON_KEY", "")

    # A2A URLs — read from yaml first, then env var overrides
    try:
        import yaml as _yaml  # noqa: F811
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH) as _f:
                _raw = _yaml.safe_load(_f) or {}
            config.a2a_hub_url = _raw.get("a2a_hub_url", "")
            config.a2a_server_url = _raw.get("a2a_server_url", "")
    except Exception:
        pass
    config.a2a_hub_url = os.environ.get("A2A_HUB_URL", config.a2a_hub_url)
    config.a2a_server_url = os.environ.get("A2A_SERVER_URL", config.a2a_server_url)

    return config


def _issue_to_ticket(issue: Dict[str, Any]) -> SWETicket:
    repo = str(issue.get("repo") or "")
    number = issue.get("number")
    labels = [str(lbl).lower() for lbl in (issue.get("labels") or [])]
    severity = TicketSeverity.MEDIUM
    if "critical" in labels:
        severity = TicketSeverity.CRITICAL
    elif "high" in labels:
        severity = TicketSeverity.HIGH
    elif "low" in labels:
        severity = TicketSeverity.LOW
    return SWETicket(
        ticket_id=f"gh-{repo}-{number}",
        title=str(issue.get("title", "")),
        description=str(issue.get("title", "")),
        severity=severity,
        labels=labels,
        metadata={
            "github_issue": number,
            "repo": repo,
            "required_role": "investigator" if "investigation" in labels else "developer",
        },
    )


def _apply_distributor_assignments(
    config: OrchestratorConfig,
    db: Optional[SupabaseClient],
    actions: OrchestratorActions,
    finding: Optional[Finding],
    dry_run: bool,
) -> None:
    if finding is None:
        return
    if not config.teams:
        return
    issues = finding.evidence.get("unassigned_issues", [])
    if not issues:
        return
    tickets = [_issue_to_ticket(issue) for issue in issues]
    tickets = [t for t in tickets if t.metadata.get("repo") and t.metadata.get("github_issue") is not None]
    if not tickets:
        return
    teams = {
        tid: TeamConfig.from_dict(tid, tdata) if isinstance(tdata, dict) else tdata
        for tid, tdata in config.teams.items()
    }

    class _EmptyStore:
        """Zero-load stub so TeamRegistry doesn't need a real Supabase connection."""
        def list_by_status(self, status: Any, limit: int = 500) -> list:
            return []

    registry = TeamRegistry(teams=teams, store_factory=lambda _tid: _EmptyStore())
    raw_tickets = db.query_tickets("") if db is not None else []
    all_tickets = [
        SWETicket.from_dict(ticket)
        for ticket in (raw_tickets or [])
    ] if raw_tickets else []
    distributor = WorkloadDistributor(registry, DependencyGraph(all_tickets))
    decisions = distributor.distribute(tickets)
    for decision in decisions:
        # Defensive: handle both AssignmentDecision dataclass and dict
        if isinstance(decision, dict):
            ticket_id = decision.get("ticket_id", "")
            team_id = decision.get("team_id", "")
            reason = decision.get("reason", "")
            logger.warning("Decision was a dict, not AssignmentDecision: %s", decision)
        else:
            ticket_id = decision.ticket_id
            team_id = decision.team_id
            reason = decision.reason
        issue = next((i for i in issues if f"gh-{i.get('repo', '')}-{i.get('number')}" == ticket_id), None)
        team_cfg = config.teams.get(team_id, {})
        assignee = team_cfg.get("github_account", "")
        if not issue or not assignee:
            continue
        detail = f"#{issue.get('number')} in {issue.get('repo')} → {assignee}; {reason}"
        if dry_run:
            actions._log_action("workload_assignment", detail, True)
            continue
        ok, output = actions._gh_command(
            [
                "issue",
                "edit",
                str(issue.get("number")),
                "--repo",
                str(issue.get("repo")),
                "--add-assignee",
                str(assignee),
            ],
            timeout=20,
        )
        actions._log_action("workload_assignment", f"{detail}: {output[:100]}", ok)
    finding.evidence["workload_distributor_applied"] = True
    finding.evidence["unassigned_issues"] = []


def run_orchestrator(
    *,
    dry_run: bool = False,
    verbose: bool = False,
    config: Optional[OrchestratorConfig] = None,
) -> List[Finding]:
    """Execute a single orchestration cycle."""
    if config is None:
        config = load_orchestrator_config()
    config.dry_run = dry_run
    config.verbose = verbose

    db: Optional[SupabaseClient] = None
    if config.supabase_url and config.supabase_key:
        db = SupabaseClient(config.supabase_url, config.supabase_key)

    intel = PipelineIntelligence(config, db)
    actions = OrchestratorActions(config, db, dry_run=dry_run)

    # Run detections
    findings = intel.run_all_detections()

    # Enrich unassigned_issues finding with idle-agent ticket counts so that
    # assign_issues_to_idle_agents can load-balance across bots correctly.
    _unassigned = next((f for f in findings if f.category == "unassigned_issues"), None)
    _idle = next((f for f in findings if f.category == "idle_agents"), None)
    if _unassigned is not None and _idle is not None:
        _unassigned.evidence.setdefault(
            "team_ticket_counts", _idle.evidence.get("team_ticket_counts", {})
        )
    _apply_distributor_assignments(config, db, actions, _unassigned, dry_run)

    # Auto-fix where safe
    if config.auto_fix:
        for f in findings:
            if f.auto_fixable:
                try:
                    actions.execute(f)
                except Exception as exc:
                    logger.error("Auto-fix failed for %s: %s", f.category, exc)

    # Create issues for non-fixable warnings/criticals
    for f in findings:
        if f.severity in ("warning", "critical") and not f.auto_fixable:
            try:
                actions.create_github_issue(f)
            except Exception as exc:
                logger.error("Issue creation failed: %s", exc)

    # Generate and save report
    report = generate_report(findings, actions.actions_taken)
    save_report(report)

    # Log summary
    summary = report["summary"]
    if findings:
        logger.warning("Orchestrator findings:\n%s", summary)
    else:
        logger.info(summary)

    return findings


# ---------------------------------------------------------------------------
# A2A Hub Registration
# ---------------------------------------------------------------------------

def _emit_startup_event(config: OrchestratorConfig) -> None:
    """Emit a best-effort startup event to the outbound event sink (a2a_hub_url).

    ISOLATION POLICY: SWE-Squad is sandboxed. This is a one-way outbound log
    event only. SWE-Squad does NOT register with the LinkedAi hub, does NOT
    accept inbound tasks from any external agentic system, and must NOT be
    reachable by LinkedAi or other agents. Work intake is via GitHub issues only.
    """
    hub_url = config.a2a_hub_url
    if not hub_url:
        return

    event_payload = {
        "jsonrpc": "2.0",
        "method": "agent.startup",
        "params": {
            "agent": "swe-squad",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "id": None,
    }
    events_url = hub_url.rstrip("/") + "/v1/events"
    body = json.dumps(event_payload).encode()
    req = urllib.request.Request(
        events_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            logger.debug("Startup event emitted to %s (HTTP %s)", events_url, resp.status)
    except Exception as exc:
        logger.debug("Startup event to %s failed (non-fatal): %s", events_url, exc)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SWE-Squad Fleet Orchestrator — detect and fix systemic issues"
    )
    parser.add_argument("--dry-run", action="store_true", help="No mutations")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--daemon", action="store_true", help="Loop continuously")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(_LOG_DIR / "orchestrator.log", mode="a"),
        ] if _LOG_DIR.exists() else [logging.StreamHandler()],
    )

    if args.daemon:
        config = load_orchestrator_config()
        interval = config.cycle_interval_minutes * 60
        logger.info("Starting orchestrator daemon (interval=%dm)", config.cycle_interval_minutes)
        _emit_startup_event(config)
        while True:
            try:
                run_orchestrator(dry_run=args.dry_run, verbose=args.verbose, config=config)
            except Exception as exc:
                logger.error("Orchestrator cycle failed: %s", exc)
            time.sleep(interval)
    else:
        findings = run_orchestrator(dry_run=args.dry_run, verbose=args.verbose)
        sys.exit(1 if any(f.severity == "critical" for f in findings) else 0)


if __name__ == "__main__":
    main()
