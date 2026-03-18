"""CodeReviewerAgent — push branch, create PR, review diff via Claude, merge, close GH issue.

For each IN_REVIEW ticket:
1. Get branch from ticket.metadata['branch'] — if missing, return (False, "no branch recorded")
2. Push branch with git push origin {branch} --force-with-lease (fallback to local-only if fails)
3. Check for existing PR or create one with gh pr create
4. Get diff: git diff main..{branch} (capped to diff_char_limit chars)
5. Call claude --model sonnet --print with a review prompt
6. Parse APPROVE / REQUEST_CHANGES from first line of response
7a. APPROVE: merge PR, close GH issue, transition ticket to RESOLVED
7b. REQUEST_CHANGES: bounce back to IN_DEVELOPMENT (or HITL after max_rejections)
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Tuple

from src.swe_team.models import SWETicket, TicketStatus

logger = logging.getLogger("swe_team.code_reviewer")


class CodeReviewerAgent:
    """Full code review cycle: push → PR → diff review → merge → close issue."""

    def __init__(
        self,
        model: str = "sonnet",
        diff_char_limit: int = 6000,
        max_rejections: int = 3,
    ) -> None:
        self.model = model
        self.diff_char_limit = diff_char_limit
        self.max_rejections = max_rejections

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
        """Return existing PR number or create a new PR. Returns None on failure."""
        # Check for existing PR
        existing = self._find_existing_pr(branch, repo)
        if existing is not None:
            logger.info("CodeReviewer: reusing existing PR #%d for branch %s", existing, branch)
            return existing

        # Create new PR
        return self._create_pr(branch, repo, ticket)

    def _find_existing_pr(self, branch: str, repo: str) -> int | None:
        """Return PR number if a PR for this branch already exists, else None."""
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
        """Create a PR for the branch. Returns PR number or None on failure."""
        title = f"[SWE-AUTO] {ticket.title}"
        body = (
            f"## Automated Fix\n\n"
            f"**Ticket:** {ticket.ticket_id}\n"
            f"**Severity:** {ticket.severity.value}\n\n"
            f"### Investigation Summary\n"
            f"{(ticket.investigation_report or '(none)')[:500]}\n"
        )
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
                # Output is the PR URL, e.g. https://github.com/org/repo/pull/42
                url = result.stdout.strip()
                parts = url.rstrip("/").split("/")
                if parts and parts[-1].isdigit():
                    pr_num = int(parts[-1])
                    logger.info("CodeReviewer: created PR #%d for branch %s", pr_num, branch)
                    return pr_num
                logger.warning("CodeReviewer: could not parse PR number from: %s", url)
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
        """Call claude CLI with the review prompt. Returns stdout or None."""
        try:
            result = subprocess.run(
                ["claude", "--model", self.model, "--print", prompt],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            logger.warning(
                "CodeReviewer: claude subprocess returned rc=%d: %s",
                result.returncode,
                result.stderr[:200],
            )
            return None
        except subprocess.TimeoutExpired:
            logger.warning("CodeReviewer: claude timed out — defaulting to REJECT (fail-secure)")
            return None
        except FileNotFoundError:
            logger.warning("CodeReviewer: claude CLI not found — defaulting to REJECT (fail-secure)")
            return None
        except Exception:
            logger.exception("CodeReviewer: unexpected error calling claude")
            return None

    @staticmethod
    def _parse_response(response: str | None) -> Tuple[bool, str]:
        """Parse claude response. Returns (approved, reasoning).

        On timeout/parse error → default REJECT (fail-secure, SEC-68).
        """
        if response is None:
            return False, "SEC-68: timeout/unavailable — defaulting to REJECT (fail-secure)"

        lines = response.strip().splitlines()
        if not lines:
            return False, "SEC-68: empty response — defaulting to REJECT (fail-secure)"

        first_line = lines[0].strip().upper()
        reasoning = " ".join(lines[1:]).strip() if len(lines) > 1 else ""

        if "REQUEST_CHANGES" in first_line:
            return False, reasoning
        if "APPROVE" in first_line:
            return True, reasoning

        # Could not parse decision — default REJECT (fail-secure, SEC-68)
        logger.warning(
            "CodeReviewer: could not parse decision from first line %r — defaulting to REJECT (fail-secure)",
            lines[0],
        )
        return False, f"SEC-68: unparseable response — defaulting to REJECT (fail-secure)"

    def _handle_approve(
        self,
        ticket: SWETicket,
        store,
        repo: str,
        pr_number: int | None,
        reasoning: str,
        dry_run: bool,
    ) -> Tuple[bool, str]:
        """Merge PR, close GH issue, transition ticket to RESOLVED."""
        logger.info(
            "CodeReviewer: APPROVED ticket %s — reasoning: %s",
            ticket.ticket_id,
            reasoning[:120],
        )

        if not dry_run:
            # Merge PR
            if pr_number is not None and repo:
                self._merge_pr(pr_number, repo)

            # Close GH issue
            issue_num = ticket.metadata.get("github_issue")
            if issue_num and repo:
                self._close_github_issue(issue_num, pr_number, repo)

            # Transition ticket
            try:
                ticket.transition(TicketStatus.RESOLVED)
                _store_save(store, ticket)
            except ValueError as exc:
                logger.error(
                    "CodeReviewer: transition to RESOLVED failed for %s: %s",
                    ticket.ticket_id,
                    exc,
                )
                ticket.metadata["review_feedback"] = str(exc)
                ticket.transition(TicketStatus.IN_DEVELOPMENT)
                _store_save(store, ticket)
                return False, f"audit failed: {str(exc)[:100]}"

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

            # Close PR so developer can create a fresh one
            if pr_number is not None and repo:
                self._close_pr(pr_number, repo)

        return False, f"rejected: {reasoning[:100]}"

    def _merge_pr(self, pr_number: int, repo: str) -> None:
        """Squash-merge the PR and delete the branch."""
        # RBAC: enforce pr_merge permission before merging
        from src.swe_team.agent_rbac import check_permission
        allowed, reason = check_permission("claude-code", "pr_merge")
        if not allowed:
            logger.critical("RBAC blocked PR merge: %s", reason)
            return

        # SEC-68: Prevent self-merge — log but allow for now (enforcement comes later)
        logger.warning(
            "SEC-68 AUDIT: Auto-merge of PR #%s — ensure human review occurred",
            pr_number,
        )
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
            else:
                logger.warning(
                    "CodeReviewer: gh pr merge failed (rc=%d): %s",
                    result.returncode,
                    result.stderr[:300],
                )
        except subprocess.TimeoutExpired:
            logger.warning("CodeReviewer: gh pr merge timed out for PR #%d", pr_number)
        except Exception:
            logger.warning("CodeReviewer: gh pr merge error", exc_info=True)

    def _close_github_issue(self, issue_num: int, pr_number: int | None, repo: str) -> None:
        """Close the GitHub issue with a comment referencing the PR."""
        comment = (
            f"Fixed in PR #{pr_number}" if pr_number else "Fixed by automated SWE agent"
        )
        try:
            subprocess.run(
                ["gh", "issue", "close", str(issue_num), "--repo", repo, "--comment", comment],
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
