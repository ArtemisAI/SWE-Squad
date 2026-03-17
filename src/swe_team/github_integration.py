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
from typing import Optional

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
