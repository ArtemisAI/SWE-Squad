"""GitHub Issue Scanner — autonomous backlog pickup.

Scans open GitHub issues labeled for SWE-Squad processing and creates
internal SWE tickets for any that don't already have one.  Complements
the existing ``fetch_github_tickets()`` helper (which only picks up issues
*assigned* to the team's GitHub account) by adding **label-based discovery**
so human-filed issues are picked up even before explicit assignment.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from src.swe_team.models import SWETicket, TicketSeverity

logger = logging.getLogger(__name__)

# Labels that mark issues for SWE-Squad processing (explicit opt-in only)
_PICKUP_LABELS: Set[str] = {"swe-squad", "automated"}
# Labels that allow pickup ONLY when the issue is assigned to the bot account
_ASSIGNED_ONLY_LABELS: Set[str] = {"bug", "critical", "high", "medium"}
# Labels that mean "do NOT auto-pickup"
_SKIP_LABELS: Set[str] = {"needs-human-review", "wontfix", "duplicate", "question"}

_SEVERITY_MAP: Dict[str, TicketSeverity] = {
    "critical": TicketSeverity.CRITICAL,
    "high": TicketSeverity.HIGH,
    "medium": TicketSeverity.MEDIUM,
    "low": TicketSeverity.LOW,
    "bug": TicketSeverity.HIGH,  # default for unlabeled bugs
}

# Priority order: first match wins when an issue has multiple severity labels
_SEVERITY_PRIORITY: List[str] = ["critical", "high", "bug", "medium", "low"]
_DEPENDS_ON_MARKER = re.compile(r"depends-on\s*:\s*([^\n\r]+)", re.IGNORECASE)
_ISSUE_NUMBER = re.compile(r"#(\d+)")


@dataclass
class GitHubScannerConfig:
    """Configuration for the GitHub issue scanner."""
    repo: str = ""  # owner/repo
    pickup_labels: Set[str] = field(default_factory=lambda: set(_PICKUP_LABELS))
    assigned_only_labels: Set[str] = field(default_factory=lambda: set(_ASSIGNED_ONLY_LABELS))
    skip_labels: Set[str] = field(default_factory=lambda: set(_SKIP_LABELS))
    max_issues_per_scan: int = 10
    enabled: bool = True
    github_account: str = ""  # Bot account name for assignee checks


class GitHubIssueScanner:
    """Scans GitHub issues and creates SWE tickets for untracked ones."""

    def __init__(
        self,
        config: GitHubScannerConfig,
        known_fingerprints: Optional[Set[str]] = None,
        known_issue_numbers: Optional[Set[int]] = None,
    ) -> None:
        self._config = config
        self._repo: str = config.repo or ""
        self._github_account: str = config.github_account or ""
        self._data_dir = Path("data/swe_team")
        self._scanner_seen_file = self._data_dir / "scanner_seen.json"

        # Load persisted dedup state
        persisted_state = self._load_persisted_state()
        self._known_fps: Set[str] = (
            set(known_fingerprints)
            if known_fingerprints is not None
            else persisted_state.get("fingerprints", set())
        )
        # _known_issue_numbers is now repo-scoped: keys are "repo#num" strings.
        # The legacy constructor arg (Set[int]) is still accepted for backward
        # compat but treated as repo-scoped for the current repo.
        if known_issue_numbers is not None:
            self._known_repo_issues: Set[str] = {
                f"{self._repo}#{n}" for n in known_issue_numbers
            }
        else:
            self._known_repo_issues = persisted_state.get("repo_issues", set())

    def _load_persisted_state(self) -> Dict[str, Set]:
        """Load persisted scanner dedup state from disk."""
        if not self._scanner_seen_file.exists():
            return {"fingerprints": set(), "repo_issues": set()}

        try:
            with open(self._scanner_seen_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # repo_issues: repo-scoped keys like "owner/repo#42" (new format)
                repo_issues: Set[str] = set(data.get("repo_issues", []))
                # Migrate legacy bare issue_numbers (not repo-scoped) by ignoring
                # them — they caused cross-repo false dedup and must be dropped.
                # fingerprints are also repo-scoped now ("gh-issue-owner/repo-42").
                return {
                    "fingerprints": set(data.get("fingerprints", [])),
                    "repo_issues": repo_issues,
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load scanner dedup state: %s", exc)
            return {"fingerprints": set(), "repo_issues": set()}

    def _save_persisted_state(self) -> None:
        """Save current scanner dedup state to disk."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "fingerprints": list(self._known_fps),
                "repo_issues": list(self._known_repo_issues),
            }
            with open(self._scanner_seen_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to save scanner dedup state: %s", exc)

    def scan(self) -> List[SWETicket]:
        """Fetch open GitHub issues and return new SWE tickets for untracked ones."""
        if not self._config.enabled or not self._repo:
            return []

        issues = self._fetch_open_issues()
        new_tickets: List[SWETicket] = []

        for issue in issues:
            issue_num = issue.get("number")
            if issue_num is None:
                continue
            # Fingerprint and dedup keys are repo-scoped to avoid false positives
            # when multiple repos have issues with the same number.
            fingerprint = f"gh-issue-{self._repo}-{issue_num}"
            repo_issue_key = f"{self._repo}#{issue_num}"
            if fingerprint in self._known_fps:
                continue
            if repo_issue_key in self._known_repo_issues:
                continue

            labels = {l.get("name", "").lower() for l in issue.get("labels", [])}

            # Skip issues with exclusion labels
            if labels & self._config.skip_labels:
                logger.debug("Skipping issue #%d — has skip label", issue_num)
                continue

            # ── Merged PR check ─────────────────────────────────────────
            # Skip issues that already have a merged PR fixing them to avoid
            # duplicate work (issue #376).
            if self._has_merged_pr_for_issue(issue_num):
                logger.debug("Skipping issue #%d — has merged PR fixing it", issue_num)
                # Mark as seen to avoid re-scanning in future cycles
                self._known_fps.add(fingerprint)
                self._known_repo_issues.add(repo_issue_key)
                continue

            # ── Assignee gate (MANDATORY) ──────────────────────────────
            # An issue is eligible ONLY if it is assigned to this team's
            # github_account.  Labels are informational metadata, never
            # pickup triggers.  This is the core isolation mechanism that
            # allows multiple squads (alpha, beta, …) to coexist on the
            # same repos without conflicts.
            assignee_logins = {
                a.get("login", "").lower()
                for a in issue.get("assignees", [])
            }
            if not self._github_account:
                logger.debug(
                    "Skipping issue #%d — no github_account configured for assignee check",
                    issue_num,
                )
                continue
            if self._github_account.lower() not in assignee_logins:
                logger.debug(
                    "Skipping issue #%d — not assigned to %s (assignees: %s)",
                    issue_num, self._github_account, assignee_logins or "none",
                )
                continue

            ticket = self._issue_to_ticket(issue, labels)
            if ticket:
                new_tickets.append(ticket)
                # Update in-memory dedup state immediately so a second scan()
                # call within the same session does not re-return the same issue.
                # Disk persistence is deferred: the caller must invoke mark_stored()
                # after successfully adding tickets to the store, so that a crash
                # between scan() and store.add() does not permanently drop issues.
                self._known_fps.add(fingerprint)
                self._known_repo_issues.add(repo_issue_key)

            if len(new_tickets) >= self._config.max_issues_per_scan:
                break

        if new_tickets:
            logger.info("GitHub scanner found %d new issues to process", len(new_tickets))

        return new_tickets

    def mark_stored(self, tickets: List[SWETicket]) -> None:
        """Mark *tickets* as stored so they are not re-scanned on the next cycle.

        Must be called by the runner **after** successfully adding tickets to the
        ticket store.  This decouples dedup persistence from ticket creation,
        preventing silent drops if the process restarts before store.add() runs.
        """
        changed = False
        for ticket in tickets:
            issue_num = ticket.metadata.get("github_issue")
            if issue_num is None:
                continue
            repo = ticket.metadata.get("github_repo") or ticket.metadata.get("repo") or self._repo
            fingerprint = f"gh-issue-{repo}-{issue_num}"
            repo_issue_key = f"{repo}#{issue_num}"
            if fingerprint not in self._known_fps:
                self._known_fps.add(fingerprint)
                changed = True
            if repo_issue_key not in self._known_repo_issues:
                self._known_repo_issues.add(repo_issue_key)
                changed = True
        if changed:
            self._save_persisted_state()

    def _fetch_open_issues(self) -> list:
        """Use gh CLI to fetch open issues."""
        try:
            result = subprocess.run(
                [
                    "gh", "issue", "list",
                    "--repo", self._repo,
                    "--state", "open",
                    "--limit", str(self._config.max_issues_per_scan * 3),
                    "--json", "number,title,body,labels,assignees,createdAt",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.warning("gh issue list failed: %s", result.stderr.strip())
                return []
            return json.loads(result.stdout) if result.stdout.strip() else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("GitHub issue scan failed: %s", exc)
            return []

    def _has_merged_pr_for_issue(self, issue_num: int) -> bool:
        """Check if the issue has merged pull requests that fix it.

        Returns True if any merged PR references this issue (via closes/fixes).
        This prevents re-triaging issues that already have merged PRs (issue #376).
        """
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "list",
                    "--repo", self._repo,
                    "--state", "merged",
                    "--search", f"closes:#{issue_num}",
                    "--json", "number",
                    "--limit", "1",
                ],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                logger.debug("Failed to query merged PRs for issue #%d: %s", issue_num, result.stderr.strip())
                return False

            prs = json.loads(result.stdout.strip() or "[]")
            if prs:
                logger.info("Issue #%d has merged PR(s) — skipping", issue_num)
                return True
            return False

        except Exception as exc:  # noqa: BLE001
            logger.warning("Error checking merged PRs for issue #%d: %s", issue_num, exc)
            return False

    def _issue_to_ticket(self, issue: dict, labels: Set[str]) -> Optional[SWETicket]:
        """Convert a GitHub issue dict to a SWETicket."""
        issue_num = issue.get("number")
        title = issue.get("title", "").strip()
        body = issue.get("body", "").strip()

        # Determine severity from labels — pick highest matching severity
        severity = TicketSeverity.MEDIUM  # default
        for label_name in _SEVERITY_PRIORITY:
            if label_name in labels:
                severity = _SEVERITY_MAP[label_name]
                break

        fingerprint = f"gh-issue-{self._repo}-{issue_num}"

        # Detect module from labels (module:xxx convention)
        module = "github"
        for lbl in labels:
            if lbl.startswith("module:"):
                module = lbl.replace("module:", "").strip()
                break

        depends_on = self._extract_dependency_ticket_ids(body)

        ticket = SWETicket(
            title=f"[GH#{issue_num}] {title}"[:120],
            description=body[:2000] if body else title,
            severity=severity,
            source_module=module,
            metadata={
                "fingerprint": fingerprint,
                "github_issue": issue_num,
                "github_repo": self._config.repo,
                "repo": self._config.repo,
                "source": "github_scanner",
                "github_labels": sorted(labels),
                "depends_on": depends_on,
            },
            blocked_by=list(depends_on),
        )

        # Assign if issue has assignees
        assignees = issue.get("assignees", [])
        if assignees:
            ticket.assigned_to = assignees[0].get("login", "")

        return ticket

    def _extract_dependency_ticket_ids(self, body: str) -> List[str]:
        if not body:
            return []

        dependencies: List[str] = []
        seen_deps: Set[str] = set()
        for match in _DEPENDS_ON_MARKER.finditer(body):
            marker_value = match.group(1)
            for issue_match in _ISSUE_NUMBER.finditer(marker_value):
                dep_ticket_id = f"gh-issue-{self._repo}-{issue_match.group(1)}"
                if dep_ticket_id not in seen_deps:
                    seen_deps.add(dep_ticket_id)
                    dependencies.append(dep_ticket_id)
        return dependencies
