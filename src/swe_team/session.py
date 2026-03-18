"""
Session tagging and naming for SWE-Squad observability.

Every SWE-Squad operation (investigation, development, review) gets a
unique session tag for end-to-end tracing across logs, GitHub comments,
commits, and the Claude Code console.

Format: SWE-SQUAD-{TYPE}-{ID}
  - SWE-SQUAD-ISSUE#42      — working on GitHub issue #42
  - SWE-SQUAD-TICKET-a1b2c3  — working on ticket a1b2c3 (no GH issue)
  - SWE-SQUAD-CYCLE-20260318T1300 — routine monitoring cycle
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def make_session_tag(
    issue_number: Optional[int] = None,
    ticket_id: Optional[str] = None,
    cycle: bool = False,
) -> str:
    """Generate a unique session tag for tracing.

    Priority: issue_number > ticket_id > cycle timestamp.
    """
    trace = str(uuid.uuid4())[:8]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")

    if issue_number:
        tag = f"SWE-SQUAD-ISSUE#{issue_number}"
    elif ticket_id:
        tag = f"SWE-SQUAD-TICKET-{ticket_id[:12]}"
    elif cycle:
        tag = f"SWE-SQUAD-CYCLE-{ts}"
    else:
        tag = f"SWE-SQUAD-SESSION-{ts}"

    return f"{tag} [trace:{trace}]"


def session_header(tag: str) -> str:
    """Format a session header for GitHub comments and log entries."""
    return (
        f"**Session:** `{tag}`\n"
        f"**Started:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"**Agent:** SWE-Squad (Claude Code)\n"
    )


def log_session_start(tag: str) -> None:
    """Log the start of a tagged session."""
    logger.info("=" * 60)
    logger.info("SESSION START: %s", tag)
    logger.info("=" * 60)


def log_session_end(tag: str, outcome: str = "completed") -> None:
    """Log the end of a tagged session."""
    logger.info("SESSION END: %s — %s", tag, outcome)
