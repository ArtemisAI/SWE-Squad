"""
Developer agent for the Autonomous SWE Team.

Uses a keep/discard loop with git as the state machine to attempt fixes
and only keep changes that pass tests and complexity gates.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple, Union

from src.swe_team.governance import check_fix_complexity
from src.swe_team.models import SWETicket, TicketStatus

logger = logging.getLogger(__name__)

_DEFAULT_PROGRAM_PATH = Path("config/swe_team/programs/fix.md")
_DEFAULT_CLAUDE_PATH = "/usr/bin/claude"
_DEFAULT_MAX_ATTEMPTS = 3
_BUG_TIMEBOX_SECONDS = 15 * 60
_FEATURE_TIMEBOX_SECONDS = 30 * 60
_TEST_OUTPUT_MAX_CHARS = 300
_ERROR_DISPLAY_MAX_CHARS = 120


class DeveloperAgent:
    """Attempts ticket fixes using Claude Code CLI and a keep/discard loop."""

    AGENT_NAME = "swe_developer"

    def __init__(
        self,
        *,
        repo_root: Union[Path, str] = ".",
        program_path: Union[Path, str] = _DEFAULT_PROGRAM_PATH,
        claude_path: str = _DEFAULT_CLAUDE_PATH,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        test_command: Optional[List[str]] = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._program_path = Path(program_path)
        self._claude_path = claude_path
        self._max_attempts = max_attempts
        self._program_cache: Optional[str] = None
        self._test_command = test_command or self._default_test_command()

    def attempt_fix(self, ticket: SWETicket) -> bool:
        """Run the keep/discard loop for *ticket*."""
        if not self._eligible(ticket):
            return False

        ticket.transition(TicketStatus.IN_DEVELOPMENT)
        branch = self._ensure_branch(ticket)
        ticket.metadata["branch"] = branch
        ticket.metadata.setdefault("pr_number", None)
        attempts = list(ticket.metadata.get("attempts", []))

        last_error = None  # Feed failures into next attempt (Ralph Wiggum loop)
        for attempt_num in range(self._max_attempts):
            attempt_start = time.monotonic()
            attempt_timestamp = datetime.now(timezone.utc).isoformat()
            base_sha = ""

            attempt_record = {
                "timestamp": attempt_timestamp,
                "duration_s": 0,
                "result": "fail",
                "attempt": attempt_num + 1,
            }

            try:
                base_sha = self._git(["git", "rev-parse", "HEAD"]).strip()
                prompt = self._build_prompt(ticket, last_error=last_error, attempt=attempt_num + 1)
                if prompt is None:
                    attempt_record["error"] = "Fix prompt template missing"
                    attempts.append(attempt_record)
                    break

                timebox = self._timebox_seconds(ticket)
                deadline = time.monotonic() + timebox

                model = self._select_model(ticket)
                logger.info("Dev attempt %d for %s (model=%s)", attempt_num + 1, ticket.ticket_id, model)
                self._run_claude(prompt, timeout=self._remaining(deadline), model=model)

                tests_ok, test_error = self._run_tests(deadline)
                if not tests_ok:
                    attempt_record["error"] = test_error
                    last_error = test_error  # Ralph Wiggum: feed failure forward
                    self._reset_to(base_sha)
                    attempts.append(attempt_record)
                    continue

                lines_changed, files_changed = self._diff_stats()
                allowed_modules = set()
                if ticket.source_module:
                    allowed_modules.add(ticket.source_module)
                ok, reason = check_fix_complexity(
                    files_changed,
                    lines_changed,
                    allowed_modules=allowed_modules or None,
                )
                if not ok:
                    attempt_record["error"] = reason
                    last_error = reason  # Ralph Wiggum: feed failure forward
                    self._reset_to(base_sha)
                    attempts.append(attempt_record)
                    continue

                if not files_changed:
                    attempt_record["error"] = "No changes produced"
                    last_error = "No changes produced"
                    self._reset_to(base_sha)
                    attempts.append(attempt_record)
                    continue

                self._git(["git", "add", "-A"])
                if self._git(["git", "diff", "--cached", "--name-only"]).strip() == "":
                    attempt_record["error"] = "No staged changes to commit"
                    self._reset_to(base_sha)
                    attempts.append(attempt_record)
                    continue

                self._git(["git", "commit", "-m", f"swe-fix: {ticket.ticket_id}"])
                self._record_automation(ticket)

                attempt_record["result"] = "pass"
                attempt_record["branch"] = branch
                attempt_record["files_changed"] = len(files_changed)
                attempt_record["lines_changed"] = lines_changed
                attempts.append(attempt_record)

                ticket.transition(TicketStatus.IN_REVIEW)
                ticket.metadata["attempts"] = attempts
                return True

            except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
                attempt_record["error"] = str(exc)
                if base_sha:
                    self._reset_to(base_sha)
                attempts.append(attempt_record)
            finally:
                attempt_record["duration_s"] = round(
                    time.monotonic() - attempt_start, 2
                )

        ticket.metadata["attempts"] = attempts
        self._escalate(ticket)
        return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _eligible(self, ticket: SWETicket) -> bool:
        if not ticket.investigation_report:
            return False
        return ticket.status in (
            TicketStatus.INVESTIGATION_COMPLETE,
            TicketStatus.IN_DEVELOPMENT,
        )

    def _ensure_branch(self, ticket: SWETicket) -> str:
        branch = f"swe-fix/ticket-{ticket.ticket_id}"
        self._git(["git", "checkout", "-B", branch])
        return branch

    def _build_prompt(
        self, ticket: SWETicket, *, last_error: Optional[str] = None, attempt: int = 1
    ) -> Optional[str]:
        template = self._load_program()
        if not template:
            return None
        try:
            prompt = template.format(
                ticket_id=ticket.ticket_id,
                title=ticket.title,
                severity=ticket.severity.value,
                source_module=ticket.source_module or "unknown",
                investigation_report=ticket.investigation_report or "No report provided.",
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Invalid fix.md template: %s", exc)
            return None

        # Ralph Wiggum loop: feed previous failure into the next attempt
        if last_error and attempt > 1:
            prompt += (
                f"\n\n## Previous Attempt Failed (attempt {attempt - 1})\n"
                f"The last fix attempt failed with this error:\n"
                f"```\n{last_error[:500]}\n```\n"
                f"This is attempt {attempt}/{self._max_attempts}. "
                f"Analyze the failure above and try a different approach.\n"
            )
        return prompt

    def _load_program(self) -> Optional[str]:
        if self._program_cache is not None:
            return self._program_cache
        if not self._program_path.is_file():
            logger.warning("Fix program not found: %s", self._program_path)
            return None
        self._program_cache = self._program_path.read_text(encoding="utf-8")
        return self._program_cache

    def _select_model(self, ticket: SWETicket) -> str:
        """Opus for CRITICAL or after 2+ failed attempts, sonnet otherwise."""
        from src.swe_team.models import TicketSeverity
        if ticket.severity == TicketSeverity.CRITICAL:
            return "opus"
        attempts = ticket.metadata.get("attempts", [])
        failed = sum(1 for a in attempts if a.get("result") == "fail")
        if failed >= 2:
            return "opus"  # Escalate after 2 failures
        return "sonnet"

    def _run_claude(self, prompt: str, *, timeout: int, model: str = "sonnet") -> None:
        # Developer agent needs full tool access (Edit, Write, Bash) to make fixes.
        # Uses -p (print) with --dangerously-skip-permissions for headless operation.
        # Opus orchestrates sub-agents for complex bugs, Sonnet for routine fixes.
        result = subprocess.run(
            [
                self._claude_path,
                "--dangerously-skip-permissions",
                "--model", model,
                "--allowedTools", "Read", "Edit", "Write", "Bash(git:*)", "Bash(pytest:*)", "Bash(python3:*)", "Grep", "Glob",
                "-p",  # print mode with tool access
            ],
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=self._repo_root,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Claude CLI failed")

    def _run_tests(self, deadline: float) -> Tuple[bool, str]:
        remaining = self._remaining(deadline)
        result = subprocess.run(
            self._test_command,
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            timeout=remaining,
        )
        if result.returncode == 0:
            return True, ""
        output = (result.stdout + "\n" + result.stderr).strip()
        return False, output[-_TEST_OUTPUT_MAX_CHARS:] if output else "Tests failed"

    def _diff_stats(self) -> Tuple[int, List[str]]:
        files = [
            line for line in self._git(["git", "diff", "--name-only"]).splitlines()
            if line
        ]
        lines_changed = 0
        for line in self._git(["git", "diff", "--numstat"]).splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added, removed = parts[0], parts[1]
            if added.isdigit():
                lines_changed += int(added)
            if removed.isdigit():
                lines_changed += int(removed)
        return lines_changed, files

    def _reset_to(self, sha: str) -> None:
        self._git(["git", "reset", "--hard", sha])

    def _record_automation(self, ticket: SWETicket) -> None:
        """Store deterministic automation steps for successful fixes."""
        try:
            from src.swe_team.distiller import TrajectoryDistiller
        except Exception as exc:
            logger.warning("Trajectory distiller unavailable: %s", exc)
            return

        fingerprint = ticket.metadata.get("fingerprint")
        if not fingerprint:
            return

        patch = self._git(["git", "show", "--format=", "HEAD"]).strip()
        if not patch:
            return

        distiller = TrajectoryDistiller(repo_root=self._repo_root)
        distiller.record_patch(ticket, patch)

    def _git(self, cmd: List[str]) -> str:
        result = subprocess.run(
            cmd,
            cwd=self._repo_root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git command failed")
        return result.stdout

    def _timebox_seconds(self, ticket: SWETicket) -> int:
        if self._is_feature(ticket):
            return _FEATURE_TIMEBOX_SECONDS
        return _BUG_TIMEBOX_SECONDS

    @staticmethod
    def _is_feature(ticket: SWETicket) -> bool:
        labels = {label.lower() for label in (ticket.labels or [])}
        if "feature" in labels or "enhancement" in labels:
            return True
        title = ticket.title.lower()
        return "feature" in title or "enhancement" in title

    def _remaining(self, deadline: float) -> int:
        remaining = max(1, int(deadline - time.monotonic()))
        return remaining

    def _default_test_command(self) -> List[str]:
        venv_python = self._repo_root / ".venv" / "bin" / "python3"
        if venv_python.exists():
            return [str(venv_python), "-m", "pytest", "tests/unit/", "-x", "-q"]
        return ["python", "-m", "pytest", "tests/unit/", "-x", "-q"]

    def _escalate(self, ticket: SWETicket) -> None:
        attempts = ticket.metadata.get("attempts", [])
        summary_lines = [
            f"Ticket {ticket.ticket_id} failed after {len(attempts)} attempt(s).",
            f"Module: {ticket.source_module or 'unknown'}",
        ]
        for attempt in attempts[-3:]:
            summary_lines.append(
                f"- {attempt.get('timestamp')} result={attempt.get('result')} "
                f"error={attempt.get('error', '')[:_ERROR_DISPLAY_MAX_CHARS]}"
            )
        message = "<b>🧑‍💻 SWE Dev escalation</b>\n" + "\n".join(summary_lines)
        self._send_telegram(message)

    @staticmethod
    def _send_telegram(message: str) -> None:
        from src.notifications.telegram import send_telegram_alert

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(send_telegram_alert(message, parse_mode="HTML"))
            except Exception:
                logger.exception("Failed to send developer escalation via asyncio.run")
        else:
            task = loop.create_task(send_telegram_alert(message, parse_mode="HTML"))
            task.add_done_callback(_handle_telegram_task_result)


def _handle_telegram_task_result(task: asyncio.Task[None]) -> None:
    """Log exceptions from async Telegram escalation tasks."""
    try:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.exception("Developer escalation task failed: %s", exc)
    except Exception:
        logger.exception("Failed to inspect developer escalation task")
