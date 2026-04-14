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
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.swe_team.models import SWETicket, TicketSeverity
from src.swe_team.session import make_session_tag, session_header

logger = logging.getLogger(__name__)

_REPO = os.environ.get("SWE_GITHUB_REPO", "")
_ESCALATION_ASSIGNEE = os.environ.get("SWE_GITHUB_ACCOUNT", "")

# Optional auth provider for recording GitHub auth state transitions.
_auth_provider: object | None = None


def set_auth_provider(provider: object) -> None:
    """Set the module-level auth provider for recording auth state."""
    global _auth_provider
    _auth_provider = provider


def _record_github_auth_result(stderr: str, success: bool) -> None:
    """Record auth success/failure for the github provider if wired."""
    if _auth_provider is None:
        return
    try:
        if success:
            _auth_provider.record_auth_success("github")
        else:
            # Check for auth-related errors in gh CLI stderr
            lower = stderr.lower()
            if any(m in lower for m in ("401", "403", "auth", "forbidden", "unauthorized", "credential")):
                _auth_provider.record_auth_failure("github", f"gh CLI auth error: {stderr[:200]}")
    except Exception:
        pass  # best-effort
_TITLE_PREFIX = "[SWE-AUTO]"

_LABEL_TEAM = os.environ.get("SWE_LABEL_TEAM", "swe-team")
_LABEL_HITL = os.environ.get("SWE_LABEL_HITL", "needs-human-review")
_LABEL_AUTO = os.environ.get("SWE_LABEL_AUTO", "auto-detected")

_GITHUB_FAILURE_THRESHOLD = 3
_GITHUB_RETRY_AFTER = timedelta(minutes=10)
_GITHUB_HITL_ESCALATION_AFTER = timedelta(minutes=30)

_GH_CONSECUTIVE_FAILURES = 0
_GH_FIRST_FAILURE_AT: datetime | None = None
_GH_PAUSED_UNTIL: datetime | None = None
_GH_UNAVAILABLE_ALERT_SENT = False
_GH_HITL_ESCALATION_SENT = False


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _send_telegram_alert(message: str) -> None:
    """Best-effort Telegram alert helper (never raises)."""
    try:
        from src.swe_team.notifier import _send
        _send(message)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send GitHub health alert")


def _reset_github_circuit_breaker_state() -> None:
    """Reset GitHub CLI circuit-breaker state."""
    global _GH_CONSECUTIVE_FAILURES
    global _GH_FIRST_FAILURE_AT
    global _GH_PAUSED_UNTIL
    global _GH_UNAVAILABLE_ALERT_SENT
    global _GH_HITL_ESCALATION_SENT

    _GH_CONSECUTIVE_FAILURES = 0
    _GH_FIRST_FAILURE_AT = None
    _GH_PAUSED_UNTIL = None
    _GH_UNAVAILABLE_ALERT_SENT = False
    _GH_HITL_ESCALATION_SENT = False


def _maybe_send_hitl_escalation(now: datetime, *, reason: str) -> None:
    global _GH_HITL_ESCALATION_SENT
    if _GH_HITL_ESCALATION_SENT or _GH_FIRST_FAILURE_AT is None:
        return
    if now - _GH_FIRST_FAILURE_AT < _GITHUB_HITL_ESCALATION_AFTER:
        return
    unavailable_for = now - _GH_FIRST_FAILURE_AT
    mins = int(unavailable_for.total_seconds() // 60)
    _send_telegram_alert(
        "<b>🚨 HITL escalation — GitHub CLI unavailable</b>\n"
        f"Unavailability: {mins} minutes\n"
        f"Reason: {reason[:300]}\n"
        "Automation requiring GitHub is blocked and needs manual intervention."
    )
    _GH_HITL_ESCALATION_SENT = True


def _record_github_cli_failure(reason: str) -> None:
    global _GH_CONSECUTIVE_FAILURES
    global _GH_FIRST_FAILURE_AT
    global _GH_PAUSED_UNTIL
    global _GH_UNAVAILABLE_ALERT_SENT

    now = _now_utc()
    if _GH_FIRST_FAILURE_AT is None:
        _GH_FIRST_FAILURE_AT = now
    _GH_CONSECUTIVE_FAILURES += 1
    logger.warning(
        "GitHub CLI failure #%d: %s",
        _GH_CONSECUTIVE_FAILURES,
        reason[:300],
    )

    if _GH_CONSECUTIVE_FAILURES >= _GITHUB_FAILURE_THRESHOLD:
        _GH_PAUSED_UNTIL = now + _GITHUB_RETRY_AFTER
        if not _GH_UNAVAILABLE_ALERT_SENT:
            _send_telegram_alert(
                "<b>⚠️ GitHub CLI unavailable</b>\n"
                f"Consecutive failures: {_GH_CONSECUTIVE_FAILURES}\n"
                f"Paused until: {_GH_PAUSED_UNTIL.isoformat()}\n"
                f"Last error: {reason[:300]}"
            )
            _GH_UNAVAILABLE_ALERT_SENT = True

    _maybe_send_hitl_escalation(now, reason=reason)


def _record_github_cli_success() -> None:
    if _GH_CONSECUTIVE_FAILURES == 0 and _GH_FIRST_FAILURE_AT is None:
        return
    logger.info("GitHub CLI recovered after %d consecutive failure(s)", _GH_CONSECUTIVE_FAILURES)
    _reset_github_circuit_breaker_state()


def _is_github_circuit_open() -> bool:
    global _GH_PAUSED_UNTIL
    now = _now_utc()
    if _GH_PAUSED_UNTIL and now >= _GH_PAUSED_UNTIL:
        logger.info("GitHub circuit breaker cooldown expired; retrying operations")
        _GH_PAUSED_UNTIL = None
        return False
    if _GH_PAUSED_UNTIL and now < _GH_PAUSED_UNTIL:
        _maybe_send_hitl_escalation(now, reason="GitHub circuit breaker remains open")
        return True
    return False


def _run_gh(cmd: List[str], *, timeout: int, op_name: str) -> subprocess.CompletedProcess[str] | None:
    if _is_github_circuit_open():
        logger.warning(
            "Skipping GitHub operation %s while circuit breaker is open until %s",
            op_name,
            _GH_PAUSED_UNTIL.isoformat() if _GH_PAUSED_UNTIL else "unknown",
        )
        return None

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        _record_github_cli_failure(f"{op_name}: {exc}")
        return None

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"rc={result.returncode}"
        _record_github_cli_failure(f"{op_name}: {detail}")
    else:
        _record_github_cli_success()
    return result


def create_github_issue(ticket: SWETicket, *, repo: str = "") -> Optional[int]:
    """Create a GitHub issue from a SWE ticket. Returns issue number or None.

    Only creates issues for HIGH or CRITICAL severity tickets.

    Parameters
    ----------
    repo:
        Target repo (``owner/repo``).  Falls back to ``ticket.metadata["repo"]``,
        then to the ``SWE_GITHUB_REPO`` env var.
    """
    if ticket.severity not in (TicketSeverity.CRITICAL, TicketSeverity.HIGH):
        logger.debug(
            "Skipping GitHub issue for %s severity ticket %s",
            ticket.severity.value,
            ticket.ticket_id,
        )
        return None

    target_repo = repo or ticket.metadata.get("repo", "")
    if not target_repo:
        # Do NOT fall back to SWE_GITHUB_REPO — monitor-detected tickets
        # without an explicit repo must not create issues on sandbox repos.
        logger.debug("create_github_issue: no target repo for ticket %s — skipping", ticket.ticket_id)
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
    labels = f"{_LABEL_TEAM},{_LABEL_AUTO},{severity_label}"

    try:
        result = _run_gh(
            [
                "gh", "issue", "create",
                "--repo", target_repo,
                "--title", title,
                "--body", body,
                "--label", labels,
            ],
            timeout=30,
            op_name="gh issue create",
        )
        if result is None:
            return None
        if result.returncode != 0:
            logger.warning(
                "gh issue create failed (rc=%d): %s",
                result.returncode,
                result.stderr.strip(),
            )
            _record_github_auth_result(result.stderr, success=False)
            return None

        # Parse issue number from output like
        # "https://github.com/owner/repo/issues/123"
        output = result.stdout.strip()
        if "/issues/" in output:
            issue_num = int(output.rsplit("/issues/", 1)[1])
            logger.info("Created GitHub issue #%d for ticket %s", issue_num, ticket.ticket_id)
            _record_github_auth_result("", success=True)
            return issue_num

        logger.warning("Could not parse issue number from gh output: %s", output)
        return None

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to create GitHub issue: %s", exc)
        return None


def comment_on_issue(issue_number: int, comment: str, *, repo: str = "") -> bool:
    """Add a comment to an existing GitHub issue. Returns True on success.

    Parameters
    ----------
    repo:
        Target repo (``owner/repo``).  Falls back to ``SWE_GITHUB_REPO`` env var.
    """
    target_repo = repo or _REPO
    if not target_repo:
        logger.warning("comment_on_issue: no target repo for issue #%d", issue_number)
        return False
    try:
        result = _run_gh(
            [
                "gh", "issue", "comment", str(issue_number),
                "--repo", target_repo,
                "--body", comment,
            ],
            timeout=15,
            op_name="gh issue comment",
        )
        if result is None:
            return False
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
    """Escalate a GitHub issue to the configured assignee for human intervention.

    - Posts a structured HITL comment
    - Adds the ``needs-human-review`` label
    - Assigns the configured ``SWE_GITHUB_ACCOUNT`` user as issue owner
    - Removes the ``swe-team`` label so the squad stops iterating

    Returns True if all steps succeeded.
    """
    target_repo = repo or _REPO
    if not target_repo:
        logger.warning("escalate_to_human: no repo configured, skipping")
        return False

    assignee = _ESCALATION_ASSIGNEE
    mention = f"@{assignee}" if assignee else "the repository owner"

    comment = (
        "## 🙋 Human Intervention Required\n\n"
        f"**Ticket:** `{ticket_id}`\n\n"
        f"**Reason:** {reason}\n\n"
        "SWE-Squad has determined this issue **cannot be resolved automatically** "
        "because it requires access to external accounts, credentials, infrastructure, "
        "or a compliance/policy decision outside the agent's authority.\n\n"
        f"**Action required from {mention}:**\n"
        f"{reason}\n\n"
        "Once resolved, please add a comment describing the action taken and close or "
        "re-label this issue so the pipeline can resume.\n\n"
        "---\n*Escalated by SWE-Squad triage gate*"
    )

    ok = True

    # 1. Post explanatory comment
    try:
        r = _run_gh(
            ["gh", "issue", "comment", str(issue_number), "--repo", target_repo, "--body", comment],
            timeout=20,
            op_name="gh issue comment (escalate)",
        )
        if r is None:
            ok = False
        elif r.returncode != 0:
            logger.warning("escalate comment failed: %s", r.stderr.strip()[:200])
            ok = False
    except Exception as exc:
        logger.warning("escalate comment error: %s", exc)
        ok = False

    # 2. Add needs-human-review label
    try:
        _run_gh(
            ["gh", "issue", "edit", str(issue_number), "--repo", target_repo,
             "--add-label", _LABEL_HITL],
            timeout=20,
            op_name="gh issue edit add-label",
        )
    except Exception:
        pass

    # 3. Remove swe-team label so the squad stops picking this up
    try:
        _run_gh(
            ["gh", "issue", "edit", str(issue_number), "--repo", target_repo,
             "--remove-label", _LABEL_TEAM],
            timeout=20,
            op_name="gh issue edit remove-label",
        )
    except Exception:
        pass

    # 4. Assign to configured escalation user
    if assignee:
        try:
            _run_gh(
                ["gh", "issue", "edit", str(issue_number), "--repo", target_repo,
                 "--add-assignee", assignee],
                timeout=20,
                op_name="gh issue edit add-assignee",
            )
        except Exception:
            pass

    logger.info("Escalated issue #%d to human:%s | %s", issue_number, assignee or "unset", reason[:80])
    return ok


def find_comment_by_text(issue_number: int, search_text: str, repo: str = "") -> Optional[int]:
    """Return the ID of the first comment containing search_text."""
    target_repo = repo or _REPO
    if not target_repo:
        return None
    try:
        result = _run_gh(
            [
                "gh", "api",
                f"repos/{target_repo}/issues/{issue_number}/comments",
            ],
            timeout=20,
            op_name="gh api issue comments list",
        )
        if result is None:
            return None
        if result.returncode != 0:
            return None
        
        comments = json.loads(result.stdout.strip() or "[]")
        for comment in comments:
            if search_text in (comment.get("body") or ""):
                return int(comment.get("id"))
        return None
    except Exception:
        return None


def update_github_comment(comment_id: int, new_body: str, repo: str = "") -> bool:
    """Edit an existing GitHub issue comment in-place (for live checklist updates)."""
    target_repo = repo or _REPO
    if not target_repo or not comment_id:
        return False
    try:
        result = _run_gh(
            [
                "gh", "api",
                f"repos/{target_repo}/issues/comments/{comment_id}",
                "-X", "PATCH",
                "-f", f"body={new_body}",
            ],
            timeout=20,
            op_name="gh api issue comment patch",
        )
        if result is None:
            return False
        if result.returncode != 0:
            logger.warning("update_github_comment failed (rc=%d): %s", result.returncode, result.stderr.strip()[:200])
            return False
        return True
    except Exception as exc:
        logger.warning("update_github_comment error: %s", exc)
        return False


def _find_existing_swe_comment(issue_number: int, repo: str) -> Optional[int]:
    """Return existing SWE-Squad claim comment ID for *issue_number* if present.

    Checks issue comments for either a SWE-Squad marker in the body or the
    configured bot account as the comment author.
    """
    try:
        result = _run_gh(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{issue_number}/comments",
                "--paginate",
            ],
            op_name="gh api issue comments paginate",
            timeout=20,
        )
        if result is None:
            return None
        if result.returncode != 0:
            logger.warning(
                "existing comment lookup failed (rc=%d): %s",
                result.returncode,
                result.stderr.strip()[:200],
            )
            return None

        comments = json.loads(result.stdout.strip() or "[]")
        bot_login = (_ESCALATION_ASSIGNEE or "").lower()
        for comment in comments:
            body = str(comment.get("body") or "")
            user_login = str(((comment.get("user") or {}).get("login") or "")).lower()
            is_swe_marker = "## 🤖 SWE-Squad — Working on this" in body
            is_bot_author = bool(bot_login and user_login == bot_login)
            if is_swe_marker or is_bot_author:
                cid = comment.get("id")
                return int(cid) if cid is not None else None
        return None
    except Exception as exc:
        logger.warning("existing comment lookup error: %s", exc)
        return None


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
    Checks for existing comments to avoid duplicate posting.
    """
    target_repo = repo or _REPO
    if not target_repo:
        return None

    # Check for existing SWE-Squad comments on this issue
    existing_comment_id = _find_existing_swe_comment(issue_number, target_repo)
    if existing_comment_id is not None:
        logger.info("Issue #%d already claimed (comment #%d), skipping duplicate claim", issue_number, existing_comment_id)
        return existing_comment_id

    tag = make_session_tag(issue_number=issue_number, ticket_id=ticket_id)
    header = session_header(tag)
    checklist_md = "\n".join(f"- [ ] {item}" for item in checklist)
    body = (
        f"{header}\n"
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
        # Use gh api to create comment and get the comment ID
        result = _run_gh(
            [
                "gh", "api",
                f"repos/{target_repo}/issues/{issue_number}/comments",
                "-f", f"body={body}",
            ],
            timeout=20,
            op_name="gh api issue comments create",
        )
        if result is None:
            return None
        if result.returncode != 0:
            logger.warning("claim_issue comment failed (rc=%d): %s", result.returncode, result.stderr.strip()[:200])
            return None
        
        # Parse the comment ID from the JSON response.
        # Guard against empty body (transient 200 with no payload).
        comment_data = json.loads(result.stdout.strip() or "{}")
        comment_id = comment_data.get("id")
        if not comment_id:
            logger.warning(
                "claim_issue: no comment ID in API response for issue #%d", issue_number
            )
            return None

        logger.info("Claimed issue #%d for ticket %s | comment_id=%s | trace=%s", issue_number, ticket_id, comment_id, trace_id)
        return comment_id
    except Exception as exc:
        logger.warning("claim_issue error: %s", exc)
        return None


def close_github_issue(repo: str, issue_number: int, comment: str = "") -> bool:
    """Close a GitHub issue, optionally posting a final comment first.

    Uses ``gh issue close`` CLI.  Best-effort: logs a warning on failure but
    never raises.

    Parameters
    ----------
    repo:
        Target repo in ``owner/repo`` format.
    issue_number:
        The GitHub issue number to close.
    comment:
        Optional comment body to post before closing (e.g. PR link).

    Returns
    -------
    bool
        True if the issue was closed successfully, False otherwise.
    """
    if not repo:
        logger.warning("close_github_issue: no repo provided for issue #%d", issue_number)
        return False

    if comment:
        try:
            r = _run_gh(
                ["gh", "issue", "comment", str(issue_number), "--repo", repo, "--body", comment],
                timeout=15,
                op_name="gh issue comment (close)",
            )
            if r is not None and r.returncode != 0:
                logger.warning(
                    "close_github_issue: comment failed (rc=%d): %s",
                    r.returncode,
                    r.stderr.strip()[:200],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("close_github_issue: comment error: %s", exc)

    try:
        result = _run_gh(
            ["gh", "issue", "close", str(issue_number), "--repo", repo],
            timeout=15,
            op_name="gh issue close",
        )
        if result is None:
            _record_github_auth_result("GitHub circuit breaker open", success=False)
            return False
        if result.returncode != 0:
            logger.warning(
                "close_github_issue: gh issue close failed (rc=%d): %s",
                result.returncode,
                result.stderr.strip()[:200],
            )
            _record_github_auth_result(result.stderr, success=False)
            return False
        logger.info("Closed GitHub issue #%d in %s", issue_number, repo)
        _record_github_auth_result("", success=True)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("close_github_issue: failed to close issue #%d: %s", issue_number, exc)
        return False


def find_existing_issue(ticket: SWETicket) -> Optional[int]:
    """Check if a GitHub issue already exists for this ticket.

    Searches by fingerprint in issue body or by title prefix match.
    Returns the issue number if found, None otherwise.
    """
    fp = ticket.metadata.get("fingerprint", "")

    try:
        # Search for issues with our prefix
        result = _run_gh(
            [
                "gh", "issue", "list",
                "--repo", _REPO,
                "--state", "open",
                "--search", f"{_TITLE_PREFIX} {ticket.title[:40]}",
                "--json", "number,title,body",
            ],
            op_name="gh issue list",
            timeout=15,
        )
        if result is None:
            return None
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
