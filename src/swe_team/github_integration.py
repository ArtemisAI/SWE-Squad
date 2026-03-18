"""
GitHub issue integration for the Autonomous SWE Team.

Creates and manages GitHub issues from SWE tickets using the ``gh`` CLI
(assumed to be pre-authenticated via ``gh auth login``).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import List, Optional

from src.swe_team.models import SWETicket, TicketSeverity

logger = logging.getLogger(__name__)

_REPO = os.environ.get("SWE_GITHUB_REPO", "")
_TITLE_PREFIX = "[SWE-AUTO]"


def create_github_issue(ticket: SWETicket) -> Optional[int]:
    """Create a GitHub issue from a SWE ticket. Returns issue number or None.

    Only creates issues for HIGH or CRITICAL severity tickets.
    """
    if ticket.severity not in (TicketSeverity.CRITICAL, TicketSeverity.HIGH):
        logger.debug(
            "Skipping GitHub issue for %s severity ticket %s",
            ticket.severity.value,
            ticket.ticket_id,
        )
        return None

    title = f"{_TITLE_PREFIX} {ticket.title[:80]}"

    body_parts = [
        f"## Auto-detected by SWE Team",
        "",
        f"**Ticket ID:** `{ticket.ticket_id}`",
        f"**Severity:** {ticket.severity.value.upper()}",
        f"**Module:** {ticket.source_module or 'unknown'}",
        f"**Assigned to:** {ticket.assigned_to or 'unassigned'}",
    ]
    if ticket.description:
        body_parts.extend(["", "### Description", "", ticket.description[:500]])
    if ticket.error_log:
        body_parts.extend(["", "### Error log", "", f"```\n{ticket.error_log[:400]}\n```"])

    fp = ticket.metadata.get("fingerprint", "")
    if fp:
        body_parts.extend(["", f"<!-- fingerprint:{fp} -->"])

    body = "\n".join(body_parts)

    severity_label = f"severity: {ticket.severity.value}"
    labels = f"swe-team,auto-detected,{severity_label}"

    try:
        result = subprocess.run(
            [
                "gh", "issue", "create",
                "--repo", _REPO,
                "--title", title,
                "--body", body,
                "--label", labels,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "gh issue create failed (rc=%d): %s",
                result.returncode,
                result.stderr.strip(),
            )
            return None

        # Parse issue number from output like
        # "https://github.com/owner/repo/issues/123"
        output = result.stdout.strip()
        if "/issues/" in output:
            issue_num = int(output.rsplit("/issues/", 1)[1])
            logger.info("Created GitHub issue #%d for ticket %s", issue_num, ticket.ticket_id)
            return issue_num

        logger.warning("Could not parse issue number from gh output: %s", output)
        return None

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to create GitHub issue: %s", exc)
        return None


def comment_on_issue(issue_number: int, comment: str) -> bool:
    """Add a comment to an existing GitHub issue. Returns True on success."""
    try:
        result = subprocess.run(
            [
                "gh", "issue", "comment", str(issue_number),
                "--repo", _REPO,
                "--body", comment,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            logger.warning(
                "gh issue comment failed (rc=%d): %s",
                result.returncode,
                result.stderr.strip(),
            )
            return False
        return True

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to comment on issue #%d: %s", issue_number, exc)
        return False


def escalate_to_human(
    issue_number: int,
    ticket_id: str,
    reason: str,
    repo: str = "",
) -> bool:
    """Escalate a GitHub issue to @ArtemisAI for human intervention.

    - Posts a structured HITL comment
    - Adds the ``needs-human-review`` label
    - Assigns @ArtemisAI as issue owner
    - Removes the ``swe-team`` label so the squad stops iterating

    Returns True if all steps succeeded.
    """
    target_repo = repo or _REPO
    if not target_repo:
        logger.warning("escalate_to_human: no repo configured, skipping")
        return False

    comment = (
        "## 🙋 Human Intervention Required\n\n"
        f"**Ticket:** `{ticket_id}`\n\n"
        f"**Reason:** {reason}\n\n"
        "SWE-Squad has determined this issue **cannot be resolved automatically** "
        "because it requires access to external accounts, credentials, infrastructure, "
        "or a compliance/policy decision outside the agent's authority.\n\n"
        "**Action required from @ArtemisAI:**\n"
        f"{reason}\n\n"
        "Once resolved, please add a comment describing the action taken and close or "
        "re-label this issue so the pipeline can resume.\n\n"
        "---\n*Escalated by SWE-Squad triage gate*"
    )

    ok = True

    # 1. Post explanatory comment
    try:
        r = subprocess.run(
            ["gh", "issue", "comment", str(issue_number), "--repo", target_repo, "--body", comment],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            logger.warning("escalate comment failed: %s", r.stderr.strip()[:200])
            ok = False
    except Exception as exc:
        logger.warning("escalate comment error: %s", exc)
        ok = False

    # 2. Add needs-human-review label
    try:
        subprocess.run(
            ["gh", "issue", "edit", str(issue_number), "--repo", target_repo,
             "--add-label", "needs-human-review"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        pass

    # 3. Remove swe-team label so the squad stops picking this up
    try:
        subprocess.run(
            ["gh", "issue", "edit", str(issue_number), "--repo", target_repo,
             "--remove-label", "swe-team"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        pass

    # 4. Assign to ArtemisAI
    try:
        subprocess.run(
            ["gh", "issue", "edit", str(issue_number), "--repo", target_repo,
             "--add-assignee", "ArtemisAI"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        pass

    logger.info("Escalated issue #%d to human:ArtemisAI | %s", issue_number, reason[:80])
    return ok


def update_github_comment(comment_id: int, new_body: str, repo: str = "") -> bool:
    """Edit an existing GitHub issue comment in-place (for live checklist updates)."""
    target_repo = repo or _REPO
    if not target_repo or not comment_id:
        return False
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{target_repo}/issues/comments/{comment_id}",
                "-X", "PATCH",
                "-f", f"body={new_body}",
            ],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            logger.warning("update_github_comment failed (rc=%d): %s", result.returncode, result.stderr.strip()[:200])
            return False
        return True
    except Exception as exc:
        logger.warning("update_github_comment error: %s", exc)
        return False


def claim_issue(
    issue_number: int,
    ticket_id: str,
    trace_id: str,
    ticket_type: str,
    checklist: List[str],
    repo: str = "",
) -> Optional[int]:
    """Post a 'SWE-Squad claimed' comment with a live-updatable checklist.

    Returns the GitHub comment ID (needed for future updates via update_github_comment).
    """
    target_repo = repo or _REPO
    if not target_repo:
        return None

    checklist_md = "\n".join(f"- [ ] {item}" for item in checklist)
    body = (
        f"## 🤖 SWE-Squad — Working on this\n\n"
        f"| | |\n|---|---|\n"
        f"| **Ticket** | `{ticket_id}` |\n"
        f"| **Type** | `{ticket_type}` |\n"
        f"| **Trace ID** | `{trace_id}` |\n"
        f"| **Branch** | being created... |\n\n"
        f"> **Observability:** This is Claude Code session trace `{trace_id}`. "
        f"Search your Claude Code logs for this ID to follow the session. "
        f"Progress updates will appear in this comment as each step completes.\n\n"
        f"### Work Plan\n\n"
        f"{checklist_md}\n\n"
        f"---\n*Last updated: starting...*"
    )

    try:
        result = subprocess.run(
            [
                "gh", "issue", "comment", str(issue_number),
                "--repo", target_repo,
                "--body", body,
                "--json", "id",
            ],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            logger.warning("claim_issue comment failed (rc=%d): %s", result.returncode, result.stderr.strip()[:200])
            return None
        data = json.loads(result.stdout.strip())
        comment_id = data.get("id")
        logger.info("Claimed issue #%d for ticket %s | comment_id=%s | trace=%s", issue_number, ticket_id, comment_id, trace_id)
        return comment_id
    except Exception as exc:
        logger.warning("claim_issue error: %s", exc)
        return None


def find_existing_issue(ticket: SWETicket) -> Optional[int]:
    """Check if a GitHub issue already exists for this ticket.

    Searches by fingerprint in issue body or by title prefix match.
    Returns the issue number if found, None otherwise.
    """
    fp = ticket.metadata.get("fingerprint", "")

    try:
        # Search for issues with our prefix
        result = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", _REPO,
                "--state", "open",
                "--search", f"{_TITLE_PREFIX} {ticket.title[:40]}",
                "--json", "number,title,body",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            logger.warning("gh issue list failed: %s", result.stderr.strip())
            return None

        issues = json.loads(result.stdout.strip() or "[]")

        # Check by fingerprint first (most reliable)
        if fp:
            for issue in issues:
                body = issue.get("body", "")
                if f"fingerprint:{fp}" in body:
                    logger.debug(
                        "Found existing issue #%d by fingerprint %s",
                        issue["number"],
                        fp,
                    )
                    return issue["number"]

        # Fall back to title match
        short_title = ticket.title[:40].lower()
        for issue in issues:
            if short_title in issue.get("title", "").lower():
                logger.debug(
                    "Found existing issue #%d by title match",
                    issue["number"],
                )
                return issue["number"]

        return None

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to search for existing issues: %s", exc)
        return None
