"""CodeReviewerAgent — push branch, create PR, review diff via Claude, merge, close GH issue.

For each IN_REVIEW ticket:
1. Get branch from ticket.metadata['branch'] — if missing, return (False, "no branch recorded")
2. Push branch with git push origin {branch} --force-with-lease (fallback to local-only if fails)
3. Check for existing PR or create one with gh pr create
4. Get diff: git diff main..{branch} (capped to diff_char_limit chars)
5. Call claude --model sonnet --print with a review prompt
6. Parse APPROVE / REQUEST_CHANGES from first line of response
7a. APPROVE: merge PR, close GH issue, transition ticket to VERIFYING
7b. REQUEST_CHANGES: bounce back to IN_DEVELOPMENT (or HITL after max_rejections)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from src.swe_team.fix_verifier import FixVerifier
from src.swe_team.models import SWETicket, TicketStatus
from src.swe_team.providers.coding_engine.base import CodingEngine, EngineResult
from src.swe_team.providers.issue_tracker.base import IssueTracker
from src.swe_team.rbac_middleware import require_permission

# Model tier defaults — read from env. Never hardcode model names in agent files.
_MODEL_T2 = os.environ.get("SWE_MODEL_T2", "sonnet")
_DEFAULT_VERIFICATION_WINDOW_MINUTES = 30

logger = logging.getLogger("swe_team.code_reviewer")


def _get_pr_lifecycle(ticket: SWETicket) -> dict:
    lifecycle = ticket.metadata.get("pr_lifecycle")
    if not isinstance(lifecycle, dict):
        lifecycle = {}
        ticket.metadata["pr_lifecycle"] = lifecycle
    return lifecycle


class CodeReviewerAgent:
    """Full code review cycle: push → PR → diff review → merge → close issue."""

    AGENT_NAME = "swe_reviewer"

    def __init__(
        self,
        model: str = _MODEL_T2,
        diff_char_limit: int = 6000,
        max_rejections: int = 3,
        rbac_engine: Optional[object] = None,
        engine: Optional[CodingEngine] = None,
        issue_tracker: Optional[IssueTracker] = None,
    ) -> None:
        self.model = model
        self.diff_char_limit = diff_char_limit
        self.max_rejections = max_rejections
        # RBAC — optional, backward compatible
        self._rbac_engine = rbac_engine
        self._agent_name: str = self.AGENT_NAME

        # CodingEngine — pluggable Claude CLI backend (architecture rule: no direct subprocess)
        if engine is not None:
            self._engine = engine
        else:
            from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine
            self._engine = ClaudeCodeEngine(default_model=model)

        # IssueTracker — pluggable issue tracker backend
        self._issue_tracker = issue_tracker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def review(
        self,
        ticket: SWETicket,
        store,
        repo_root: str,
        dry_run: bool = False,
    ) -> Tuple[bool, str]:
        """Full code review cycle for one IN_REVIEW ticket.

        Returns (approved: bool, feedback: str).
        """
        branch = ticket.metadata.get("branch", "")
        if not branch:
            logger.warning("CodeReviewer: no branch recorded for ticket %s", ticket.ticket_id)
            return False, "no branch recorded"

        repo = ticket.metadata.get("repo", "")

        # ── Step 2: Push branch ──────────────────────────────────────
        push_ok = self._push_branch(branch, repo_root)
        if not push_ok:
            logger.warning(
                "CodeReviewer: push failed for %s branch=%s — proceeding with local-only path",
                ticket.ticket_id,
                branch,
            )

        # ── Step 3: PR ───────────────────────────────────────────────
        pr_number = None
        if push_ok and repo:
            pr_number = self._ensure_pr(branch, repo, ticket)

        # ── Step 4: Get diff ─────────────────────────────────────────
        diff = self._get_diff(branch, repo_root)

        # ── Step 5: Claude review ────────────────────────────────────
        prompt = self._build_review_prompt(ticket, diff)
        response = self._call_claude(prompt)

        # ── Step 6: Parse response ───────────────────────────────────
        approved, reasoning = self._parse_response(response)
        lifecycle = _get_pr_lifecycle(ticket)
        if "first_review_at" not in lifecycle:
            lifecycle["first_review_at"] = datetime.now(timezone.utc).isoformat()
        lifecycle["review_decision"] = "approved" if approved else "changes_requested"
        lifecycle["review_cycles"] = int(lifecycle.get("review_cycles", 0)) + 1

        # ── Step 7: Act on decision ──────────────────────────────────
        if approved:
            return self._handle_approve(ticket, store, repo, pr_number, reasoning, dry_run)
        else:
            return self._handle_request_changes(ticket, store, repo, pr_number, reasoning, dry_run)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _push_branch(self, branch: str, repo_root: str) -> bool:
        """Push branch to origin. Returns True on success, False on any failure."""
        try:
            result = subprocess.run(
                ["git", "push", "origin", branch, "--force-with-lease"],
                capture_output=True,
                text=True,
                cwd=repo_root,
                timeout=60,
            )
            if result.returncode == 0:
                logger.info("CodeReviewer: pushed branch %s", branch)
                return True
            logger.warning(
                "CodeReviewer: git push failed (rc=%d): %s",
                result.returncode,
                result.stderr[:300],
            )
            return False
        except subprocess.TimeoutExpired:
            logger.warning("CodeReviewer: git push timed out for branch %s", branch)
            return False
        except Exception:
            logger.warning("CodeReviewer: git push error for branch %s", branch, exc_info=True)
            return False

    def _ensure_pr(self, branch: str, repo: str, ticket: SWETicket) -> int | None:
        """Return existing PR number or create a new PR. Returns None on failure.

        Always populates ``ticket.metadata["pr_number"]`` (and ``pr_url`` when
        available) so that the PR-verification gate in ``_handle_approve`` can
        confirm a real PR exists before resolving the ticket.
        """
        # Check for existing PR
        existing = self._find_existing_pr(branch, repo)
        if existing is not None:
            logger.info("CodeReviewer: reusing existing PR #%d for branch %s", existing, branch)
            # Persist so the verification gate sees it even on a reused PR
            ticket.metadata["pr_number"] = existing
            if not ticket.metadata.get("pr_url"):
                ticket.metadata["pr_url"] = f"https://github.com/{repo}/pull/{existing}"
            return existing

        # Create new PR
        return self._create_pr(branch, repo, ticket)

    def _find_existing_pr(self, branch: str, repo: str) -> int | None:
        """Return PR number if a PR for this branch already exists, else None."""
        if self._issue_tracker is not None:
            try:
                pr_info = self._issue_tracker.find_pr(branch, repo=repo)
                if pr_info is not None:
                    return int(pr_info["number"])
                return None
            except Exception:
                logger.warning(
                    "CodeReviewer: IssueTracker.find_pr failed, falling back to gh CLI",
                    exc_info=True,
                )
        # Fallback: direct gh CLI
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--head", branch, "--repo", repo, "--json", "number"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                prs = json.loads(result.stdout)
                if prs:
                    return int(prs[0]["number"])
        except subprocess.TimeoutExpired:
            logger.warning("CodeReviewer: gh pr list timed out for branch %s", branch)
        except Exception:
            logger.warning("CodeReviewer: gh pr list error for branch %s", branch, exc_info=True)
        return None

    def _create_pr(self, branch: str, repo: str, ticket: SWETicket) -> int | None:
        """Create a PR for the branch. Returns PR number or None on failure.

        On success, stores ``pr_url`` and ``pr_number`` in ``ticket.metadata``
        so that downstream gates can verify a PR exists before resolving.
        """
        title = f"[SWE-AUTO] {ticket.title}"
        body = (
            f"## Automated Fix\n\n"
            f"**Ticket:** {ticket.ticket_id}\n"
            f"**Severity:** {ticket.severity.value}\n\n"
            f"### Investigation Summary\n"
            f"{(ticket.investigation_report or '(none)')[:500]}\n"
        )
        if self._issue_tracker is not None:
            try:
                url = self._issue_tracker.create_pr(title, body, branch, repo=repo)
                if url:
                    logger.info("CodeReviewer: pr_url=%s branch=%s", url, branch)
                    parts = url.rstrip("/").split("/")
                    if parts and parts[-1].isdigit():
                        pr_num = int(parts[-1])
                        logger.info("CodeReviewer: created PR #%d for branch %s", pr_num, branch)
                        ticket.metadata["pr_url"] = url
                        ticket.metadata["pr_number"] = pr_num
                        _get_pr_lifecycle(ticket)["pr_created_at"] = datetime.now(timezone.utc).isoformat()
                        return pr_num
                    logger.warning("CodeReviewer: could not parse PR number from: %s", url)
                    # Still store the URL even if number cannot be parsed
                    ticket.metadata["pr_url"] = url
                return None
            except Exception:
                logger.warning(
                    "CodeReviewer: IssueTracker.create_pr failed, falling back to gh CLI",
                    exc_info=True,
                )
        # Fallback: direct gh CLI
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--head", branch,
                    "--title", title,
                    "--body", body,
                    "--repo", repo,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                url = result.stdout.strip()
                logger.info("CodeReviewer: pr_url=%s branch=%s", url, branch)
                parts = url.rstrip("/").split("/")
                if parts and parts[-1].isdigit():
                    pr_num = int(parts[-1])
                    logger.info("CodeReviewer: created PR #%d for branch %s", pr_num, branch)
                    ticket.metadata["pr_url"] = url
                    ticket.metadata["pr_number"] = pr_num
                    _get_pr_lifecycle(ticket)["pr_created_at"] = datetime.now(timezone.utc).isoformat()
                    return pr_num
                logger.warning("CodeReviewer: could not parse PR number from: %s", url)
                # Still store the URL even if number cannot be parsed
                ticket.metadata["pr_url"] = url
            else:
                logger.warning(
                    "CodeReviewer: gh pr create failed (rc=%d): %s",
                    result.returncode,
                    result.stderr[:300],
                )
        except subprocess.TimeoutExpired:
            logger.warning("CodeReviewer: gh pr create timed out for branch %s", branch)
        except Exception:
            logger.warning("CodeReviewer: gh pr create error", exc_info=True)
        return None

    def _get_diff(self, branch: str, repo_root: str) -> str:
        """Get git diff main..{branch}. Caps to diff_char_limit chars."""
        try:
            result = subprocess.run(
                ["git", "diff", "main.." + branch, "--"],
                capture_output=True,
                text=True,
                cwd=repo_root,
                timeout=30,
            )
            if result.returncode == 0:
                diff = result.stdout
                if len(diff) > self.diff_char_limit:
                    diff = diff[: self.diff_char_limit] + "\n\n[... diff truncated for review ...]"
                return diff
            logger.warning(
                "CodeReviewer: git diff failed (rc=%d): %s",
                result.returncode,
                result.stderr[:200],
            )
        except subprocess.TimeoutExpired:
            logger.warning("CodeReviewer: git diff timed out for branch %s", branch)
        except Exception:
            logger.warning("CodeReviewer: git diff error", exc_info=True)
        return "(diff unavailable)"

    def _build_review_prompt(self, ticket: SWETicket, diff: str) -> str:
        report = (ticket.investigation_report or "").strip()[:1500] or "(no investigation report)"
        return (
            f"You are a senior code reviewer. Review the following diff.\n\n"
            f"Ticket: {ticket.title}\n"
            f"Severity: {ticket.severity.value}\n\n"
            f"Investigation report:\n{report}\n\n"
            f"Diff:\n```\n{diff}\n```\n\n"
            "Review this diff. Check correctness, security vulnerabilities (OWASP top 10), "
            "unintended side effects, test coverage gaps. "
            "Reply with exactly APPROVE or REQUEST_CHANGES on the first line, "
            "then one paragraph of reasoning."
        )

    def _call_claude(self, prompt: str) -> str | None:
        """Call coding engine with the review prompt. Returns stdout or None.

        Uses the CodingEngine interface instead of direct subprocess calls
        (architecture rule: all Claude CLI calls go through CodingEngine).

        When raise_on_timeout=False, the ClaudeCodeEngine returns EngineResult
        with metadata={"error_type": "timeout"} on timeout.
        """
        try:
            result: EngineResult = self._engine.run(
                prompt, model=self.model, timeout=300, raise_on_timeout=False,
            )
            if result.success:
                return result.stdout.strip()
            # Check for timeout error (non-exception path when raise_on_timeout=False)
            error_type = result.metadata.get("error_type")
            if error_type == "timeout":
                logger.warning(
                    "CodeReviewer: review timed out after 300s (ticket will default to REJECT)"
                )
            else:
                logger.warning(
                    "CodeReviewer: engine returned rc=%d: %s",
                    result.returncode,
                    result.stderr[:200],
                )
            return None
        except Exception:
            logger.exception("CodeReviewer: unexpected error calling coding engine")
            return None

    @staticmethod
    def _parse_response(response: str | None) -> Tuple[bool, str]:
        """Parse claude response. Returns (approved, reasoning).

        On timeout/parse error → default REJECT (fail-secure).
        """
        if response is None:
            return False, "timeout/unavailable — defaulting to REJECT (fail-secure)"

        lines = response.strip().splitlines()
        if not lines:
            return False, "empty response — defaulting to REJECT (fail-secure)"

        first_line = lines[0].strip().upper()
        reasoning = " ".join(lines[1:]).strip() if len(lines) > 1 else ""

        if "REQUEST_CHANGES" in first_line:
            return False, reasoning
        if "APPROVE" in first_line:
            return True, reasoning

        # Could not parse decision — default REJECT (fail-secure)
        logger.warning(
            "CodeReviewer: could not parse decision from first line %r — defaulting to REJECT (fail-secure)",
            lines[0],
        )
        return False, "unparseable response — defaulting to REJECT (fail-secure)"

    def _handle_approve(
        self,
        ticket: SWETicket,
        store,
        repo: str,
        pr_number: int | None,
        reasoning: str,
        dry_run: bool,
    ) -> Tuple[bool, str]:
        """Merge PR, close GH issue, transition ticket to VERIFYING.

        PR-verification gate (issue #367): resolution is BLOCKED if no PR
        exists.  A missing PR means the branch was never pushed to GitHub and
        merging / closing the issue would be a no-op that silently marks the
        ticket resolved without any visible artefact.
        """
        logger.info(
            "CodeReviewer: APPROVED ticket %s — reasoning: %s",
            ticket.ticket_id,
            reasoning[:120],
        )

        if not dry_run:
            # ── PR-verification gate ──────────────────────────────────
            # Block resolution if no PR URL/number is present in metadata.
            # This covers both the case where push failed and the case where
            # _ensure_pr was skipped because repo was empty.
            pr_url_meta = ticket.metadata.get("pr_url")
            pr_num_meta = ticket.metadata.get("pr_number")
            has_pr = (pr_number is not None) or bool(pr_url_meta) or (pr_num_meta is not None)
            if not has_pr:
                logger.warning(
                    "CodeReviewer: PR-GATE BLOCKED resolution of ticket %s — "
                    "no PR found (pr_number=%s pr_url=%s). "
                    "Sending back to IN_DEVELOPMENT with needs_pr=True.",
                    ticket.ticket_id,
                    pr_number,
                    pr_url_meta,
                )
                ticket.metadata["needs_pr"] = True
                ticket.metadata["review_feedback"] = (
                    "PR-verification gate: development succeeded but no PR was created. "
                    "Ticket held in development until a PR is present."
                )
                ticket.transition(TicketStatus.IN_DEVELOPMENT)
                _store_save(store, ticket)
                return False, "pr_gate: no PR found — ticket returned to IN_DEVELOPMENT"
            # RBAC: Developer/reviewer agents MUST NOT self-merge.
            # Only the SWE-Manager (orchestrator) or a human with merge authority
            # may merge PRs to main. The reviewer marks the ticket as approved
            # and transitions to IN_REVIEW — merge is a separate privileged action.
            #
            # See issue #591: 20+ unauthorized self-merges caused regressions.
            pr_to_merge = pr_number
            if pr_to_merge is None:
                meta_pr_num = ticket.metadata.get("pr_number")
                if isinstance(meta_pr_num, int):
                    pr_to_merge = meta_pr_num
                elif isinstance(meta_pr_num, str) and meta_pr_num.isdigit():
                    pr_to_merge = int(meta_pr_num)
            # Record approval but DO NOT merge — leave for SWE-Manager
            merged_ok = False
            if pr_to_merge is not None:
                logger.info(
                    "CodeReviewer: APPROVED PR #%d for ticket %s — "
                    "awaiting SWE-Manager or human merge (RBAC enforced)",
                    pr_to_merge, ticket.ticket_id,
                )
                ticket.metadata["pr_approved"] = True
                ticket.metadata["pr_approved_at"] = datetime.now(timezone.utc).isoformat()
                ticket.metadata["awaiting_merge"] = True
                _get_pr_lifecycle(ticket)["approved_at"] = datetime.now(timezone.utc).isoformat()

            # RBAC: Do NOT close GH issue or transition to VERIFYING.
            # The ticket stays in IN_REVIEW with pr_approved=True until
            # the SWE-Manager or human merges the PR and advances the state.
            _store_save(store, ticket)
            return True, f"approved: PR #{pr_to_merge} approved — awaiting authorized merge"

        return True, f"approved: {reasoning[:100]}"

    def _handle_request_changes(
        self,
        ticket: SWETicket,
        store,
        repo: str,
        pr_number: int | None,
        reasoning: str,
        dry_run: bool,
    ) -> Tuple[bool, str]:
        """Bounce ticket back to IN_DEVELOPMENT or escalate to HITL."""
        rejections = ticket.metadata.get("review_rejections", 0) + 1
        logger.warning(
            "CodeReviewer: REQUEST_CHANGES for ticket %s (rejection #%d): %s",
            ticket.ticket_id,
            rejections,
            reasoning[:120],
        )

        if not dry_run:
            ticket.metadata["review_rejections"] = rejections
            ticket.metadata["review_feedback"] = reasoning

            # Store feedback in last attempt if present
            attempts = ticket.metadata.get("attempts", [])
            if attempts:
                attempts[-1]["review_feedback"] = reasoning

            if rejections >= self.max_rejections:
                ticket.metadata["needs_hitl"] = True
                logger.warning(
                    "CodeReviewer: ticket %s has %d rejections — escalating to HITL",
                    ticket.ticket_id,
                    rejections,
                )
                _store_save(store, ticket)
                return False, "hitl: max rejections reached"

            ticket.transition(TicketStatus.IN_DEVELOPMENT)
            _store_save(store, ticket)

        return False, f"rejected: {reasoning[:100]}"

    @require_permission("pr_merge")
    def _merge_pr(self, pr_number: int, repo: str, *, require_human_review: bool = True) -> bool:
        """Squash-merge the PR and delete the branch.

        Returns True if the merge was executed, False if blocked.

        Parameters
        ----------
        pr_number:
            GitHub PR number to merge.
        repo:
            ``owner/repo`` slug.
        require_human_review:
            When True (default) the merge is **blocked** unless the PR carries
            a ``human-reviewed`` label.  Set to False only in tests or
            explicitly opted-out contexts.
        """
        # Block self-merge without human review
        if require_human_review:
            has_human_review = self._check_human_reviewed_label(pr_number, repo)
            if not has_human_review:
                logger.error(
                    "BLOCKED: Auto-merge of PR #%s in %s is not permitted — "
                    "no 'human-reviewed' label found. A human must approve before merge.",
                    pr_number,
                    repo,
                )
                return False
            logger.info(
                "PR #%s in %s has 'human-reviewed' label — proceeding with merge",
                pr_number,
                repo,
            )

        if self._issue_tracker is not None:
            try:
                ok = self._issue_tracker.merge_pr(pr_number, repo=repo)
                if ok:
                    logger.info("CodeReviewer: merged PR #%d in %s via IssueTracker", pr_number, repo)
                    return True
                logger.warning("CodeReviewer: IssueTracker.merge_pr returned False, falling back to gh CLI")
            except Exception:
                logger.warning(
                    "CodeReviewer: IssueTracker.merge_pr failed, falling back to gh CLI",
                    exc_info=True,
                )
        # Fallback: direct gh CLI
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "merge", str(pr_number),
                    "--squash",
                    "--repo", repo,
                    "--delete-branch",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                logger.info("CodeReviewer: merged PR #%d in %s", pr_number, repo)
                return True
            else:
                logger.warning(
                    "CodeReviewer: gh pr merge failed (rc=%d): %s",
                    result.returncode,
                    result.stderr[:300],
                )
                return False
        except subprocess.TimeoutExpired:
            logger.warning("CodeReviewer: gh pr merge timed out for PR #%d", pr_number)
            return False
        except Exception:
            logger.warning("CodeReviewer: gh pr merge error", exc_info=True)
            return False

    def _check_human_reviewed_label(self, pr_number: int, repo: str) -> bool:
        """Return True if the PR has a 'human-reviewed' label on GitHub."""
        if self._issue_tracker is not None:
            try:
                labels = self._issue_tracker.get_pr_labels(pr_number, repo=repo)
                return "human-reviewed" in labels
            except Exception:
                logger.warning(
                    "CodeReviewer: IssueTracker.get_pr_labels failed, falling back to gh CLI",
                    exc_info=True,
                )
        # Fallback: direct gh CLI
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "view", str(pr_number),
                    "--repo", repo,
                    "--json", "labels",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                labels = [lbl.get("name", "") for lbl in data.get("labels", [])]
                return "human-reviewed" in labels
        except subprocess.TimeoutExpired:
            logger.warning(
                "CodeReviewer: gh pr view timed out checking labels for PR #%d", pr_number
            )
        except Exception:
            logger.warning(
                "CodeReviewer: error checking human-reviewed label for PR #%d", pr_number,
                exc_info=True,
            )
        return False

    def _close_github_issue(self, issue_num: int, pr_number: int | None, repo: str) -> None:
        """Close the GitHub issue with a comment referencing the PR.

        Uses IssueTracker interface when available; falls back to gh CLI.
        """
        comment_body = (
            f"Fixed in PR #{pr_number}" if pr_number else "Fixed by automated SWE agent"
        )
        if self._issue_tracker is not None:
            try:
                self._issue_tracker.comment(str(issue_num), comment_body)
                self._issue_tracker.close_issue(str(issue_num))
                logger.info("CodeReviewer: closed GH issue #%d in %s via IssueTracker", issue_num, repo)
                return
            except Exception:
                logger.warning(
                    "CodeReviewer: IssueTracker.close_issue failed, falling back to gh CLI",
                    exc_info=True,
                )
        # Fallback: direct gh CLI
        try:
            subprocess.run(
                ["gh", "issue", "close", str(issue_num), "--repo", repo, "--comment", comment_body],
                capture_output=True,
                text=True,
                timeout=30,
            )
            logger.info("CodeReviewer: closed GH issue #%d in %s", issue_num, repo)
        except subprocess.TimeoutExpired:
            logger.warning("CodeReviewer: gh issue close timed out for issue #%d", issue_num)
        except Exception:
            logger.warning("CodeReviewer: gh issue close error", exc_info=True)

    def _close_pr(self, pr_number: int, repo: str) -> None:
        """Close (not merge) a PR as part of rejection cleanup."""
        if self._issue_tracker is not None:
            try:
                ok = self._issue_tracker.close_pr(pr_number, repo=repo)
                if ok:
                    logger.info("CodeReviewer: closed PR #%d in %s via IssueTracker", pr_number, repo)
                    return
                logger.warning("CodeReviewer: IssueTracker.close_pr returned False, falling back to gh CLI")
            except Exception:
                logger.warning(
                    "CodeReviewer: IssueTracker.close_pr failed, falling back to gh CLI",
                    exc_info=True,
                )
        # Fallback: direct gh CLI
        try:
            result = subprocess.run(
                ["gh", "pr", "close", str(pr_number), "--repo", repo],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                logger.info("CodeReviewer: closed PR #%d in %s", pr_number, repo)
            else:
                logger.warning(
                    "CodeReviewer: gh pr close failed (rc=%d): %s",
                    result.returncode,
                    result.stderr[:200],
                )
        except subprocess.TimeoutExpired:
            logger.warning("CodeReviewer: gh pr close timed out for PR #%d", pr_number)
        except Exception:
            logger.warning("CodeReviewer: gh pr close error", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store_save(store, ticket: SWETicket) -> None:
    """Persist ticket using whichever save method the store exposes."""
    if hasattr(store, "save"):
        store.save(ticket)
    elif hasattr(store, "add"):
        store.add(ticket)
    else:
        logger.error(
            "CodeReviewer: store has no save/add method — ticket %s not persisted",
            ticket.ticket_id,
        )
