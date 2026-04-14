"""PR Review Scanner — detects "changes requested" on SWE-Squad PRs.

When a developer agent opens a PR and a reviewer requests changes, this
module detects the feedback and surfaces it so the developer agent can
rework the fix on the next pipeline cycle.

Branch name convention: ``swe-fix/ticket-{ticket_id}``
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Branch prefix used by the developer agent when creating PR branches.
_BRANCH_PREFIX = "swe-fix/ticket-"

# GitHub GraphQL review decision value that signals rework is needed.
_CHANGES_REQUESTED = "CHANGES_REQUESTED"


@dataclass
class PRReviewResult:
    """Outcome of scanning a single open PR for review feedback."""

    pr_number: int
    pr_title: str
    head_branch: str
    ticket_id: Optional[str]  # None if branch doesn't match convention
    review_decision: str  # e.g. "CHANGES_REQUESTED", "APPROVED", ""
    review_comments: str  # concatenated reviewer feedback text
    repo: str = ""


@dataclass
class PRReviewScannerConfig:
    """Configuration for the PR review scanner."""

    repo: str = ""  # owner/repo
    enabled: bool = True
    max_prs_per_scan: int = 20
    branch_prefix: str = _BRANCH_PREFIX


class PRReviewScanner:
    """Scans open PRs for reviewer feedback and maps them back to SWE tickets."""

    def __init__(self, config: PRReviewScannerConfig) -> None:
        self._config = config
        self._repo = config.repo

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> List[PRReviewResult]:
        """Return all open SWE-Squad PRs that have received review feedback.

        Only PRs with a ``CHANGES_REQUESTED`` review decision are returned
        unless the PR branch matches the SWE branch convention — those are
        always included so the caller can inspect them regardless of decision.
        """
        if not self._config.enabled or not self._repo:
            return []

        raw_prs = self._fetch_open_prs()
        results: List[PRReviewResult] = []

        for pr in raw_prs:
            branch = pr.get("headRefName", "")
            ticket_id = self._extract_ticket_id(branch)

            # Only track PRs that follow the SWE branch convention.
            if ticket_id is None:
                continue

            review_decision = pr.get("reviewDecision") or ""
            reviews = pr.get("reviews", {})
            nodes = reviews.get("nodes", []) if isinstance(reviews, dict) else []

            review_comments = self._extract_review_comments(nodes)

            result = PRReviewResult(
                pr_number=pr.get("number", 0),
                pr_title=pr.get("title", ""),
                head_branch=branch,
                ticket_id=ticket_id,
                review_decision=review_decision,
                review_comments=review_comments,
                repo=self._repo,
            )
            results.append(result)

        logger.debug(
            "PR review scan: %d SWE PRs found, %d with CHANGES_REQUESTED",
            len(results),
            sum(1 for r in results if r.review_decision == _CHANGES_REQUESTED),
        )
        return results

    def scan_changes_requested(self) -> List[PRReviewResult]:
        """Return only PRs where reviewers have requested changes."""
        return [r for r in self.scan() if r.review_decision == _CHANGES_REQUESTED]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def extract_ticket_id(branch_name: str, prefix: str = _BRANCH_PREFIX) -> Optional[str]:
        """Parse *ticket_id* from a branch name like ``swe-fix/ticket-abc123``.

        Returns ``None`` if the branch does not follow the convention.
        This is a pure static helper exposed for testing.
        """
        if not branch_name.startswith(prefix):
            return None
        suffix = branch_name[len(prefix):]
        # ticket_id is hex alphanumeric (12 chars) optionally followed by extra
        # segments (e.g. retry counter).  Accept any non-empty suffix.
        if not suffix:
            return None
        # Strip any trailing suffixes separated by "-" that aren't part of the ID.
        # The ticket ID itself is a 12-char hex string; be permissive for robustness.
        match = re.match(r"^([a-f0-9]{12})", suffix)
        if match:
            return match.group(1)
        # Fall back: return whole suffix up to first "/" or end
        clean = suffix.split("/")[0]
        return clean if clean else None

    def _extract_ticket_id(self, branch_name: str) -> Optional[str]:
        return self.extract_ticket_id(branch_name, prefix=self._config.branch_prefix)

    @staticmethod
    def _extract_review_comments(review_nodes: list) -> str:
        """Concatenate all reviewer body texts from ``reviews.nodes``."""
        parts: List[str] = []
        for node in review_nodes:
            author = (node.get("author") or {}).get("login", "reviewer")
            state = node.get("state", "")
            body = (node.get("body") or "").strip()
            if body:
                parts.append(f"**{author}** ({state}):\n{body}")
        return "\n\n".join(parts)

    def _fetch_open_prs(self) -> list:
        """Use ``gh pr list`` to fetch open PRs with review information."""
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "list",
                    "--repo", self._repo,
                    "--state", "open",
                    "--limit", str(self._config.max_prs_per_scan),
                    "--json",
                    "number,title,reviewDecision,reviews,headRefName",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning(
                    "gh pr list failed (repo=%s): %s",
                    self._repo,
                    result.stderr.strip(),
                )
                return []
            return json.loads(result.stdout) if result.stdout.strip() else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("PR review scan failed: %s", exc)
            return []
