"""
Opus Orchestrator — plans work and delegates to sub-agents.

For CRITICAL tickets and complex features, Opus analyzes the request,
creates a structured plan with sub-tasks, then delegates each sub-task
to the appropriate model tier (Sonnet for implementation, Haiku for
tests/linting). Progress is tracked via GitHub checklist comments.

SEC-68 compliant: only Claude models for code generation.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from src.swe_team.models import SWETicket
from src.swe_team.session import make_session_tag
from src.swe_team.github_integration import update_github_comment

logger = logging.getLogger(__name__)


@dataclass
class SubTask:
    """A single sub-task in an orchestration plan."""
    id: str = ""
    description: str = ""
    model: str = "sonnet"  # Which model tier handles this
    status: str = "pending"  # pending, running, completed, failed
    result: str = ""
    files_to_read: List[str] = field(default_factory=list)
    files_to_modify: List[str] = field(default_factory=list)


@dataclass
class OrchestrationPlan:
    """A structured plan for fixing a ticket."""
    ticket_id: str = ""
    session_tag: str = ""
    analysis: str = ""
    sub_tasks: List[SubTask] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_checklist(self) -> str:
        """Render as a GitHub-flavored markdown checklist."""
        lines = [f"## Orchestration Plan", f"**Session:** `{self.session_tag}`", ""]
        if self.analysis:
            lines.extend([f"### Analysis", self.analysis[:500], ""])
        lines.append("### Sub-tasks")
        for t in self.sub_tasks:
            check = "x" if t.status == "completed" else " "
            model_badge = f"[{t.model}]"
            status = f" — {t.status}" if t.status not in ("pending", "completed") else ""
            lines.append(f"- [{check}] {model_badge} {t.description}{status}")
        return "\n".join(lines)


class OrchestratorAgent:
    """Opus-level orchestrator that plans and delegates work."""

    def __init__(
        self,
        claude_path: str = "",
        repo_root: Optional[Path] = None,
        model: str = "opus",
    ):
        import shutil, os
        self._claude_path = claude_path or os.environ.get("CLAUDE_CLI_PATH", "") or shutil.which("claude") or "/usr/bin/claude"
        self._repo_root = repo_root or Path.cwd()
        self._model = model

    def plan(self, ticket: SWETicket) -> OrchestrationPlan:
        """Use Opus to analyze the ticket and create a structured plan."""
        session_tag = make_session_tag(
            issue_number=ticket.metadata.get("github_issue"),
            ticket_id=ticket.ticket_id,
        )

        prompt = self._build_plan_prompt(ticket)

        try:
            result = subprocess.run(
                [
                    self._claude_path, "--print",
                    "--model", self._model,
                    "--dangerously-skip-permissions",
                    "--max-turns", "1",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(self._repo_root),
            )
            if result.returncode != 0:
                logger.error("Orchestrator plan CLI failed (exit %d): %s", result.returncode, result.stderr[:500])
                raise RuntimeError(f"Claude CLI failed (exit {result.returncode}): {result.stderr[:500]}")
            raw = result.stdout.strip()
        except RuntimeError:
            raise
        except Exception as exc:
            logger.error("Orchestrator plan failed: %s", exc)
            raw = ""

        plan = self._parse_plan(raw, ticket.ticket_id, session_tag)
        logger.info("Orchestration plan for %s: %d sub-tasks", ticket.ticket_id, len(plan.sub_tasks))
        return plan

    def execute_subtask(self, task: SubTask, ticket: SWETicket) -> str:
        """Execute a single sub-task using the specified model."""
        from src.swe_team.model_boundary import enforce_code_generation_boundary

        # Validate model boundary for code tasks
        if task.files_to_modify or _is_code_gen_task(task.description):
            enforce_code_generation_boundary(task.model, task="develop")

        files_context = ""
        for f in task.files_to_read[:5]:
            fpath = self._repo_root / f
            if fpath.is_file():
                try:
                    content = fpath.read_text()[:3000]
                    files_context += f"\n### {f}\n```\n{content}\n```\n"
                except UnicodeDecodeError:
                    logger.warning("Skipping binary/undecodable file: %s", f)

        prompt = (
            f"You are working on sub-task: {task.description}\n"
            f"Ticket: {ticket.title}\n"
            f"{files_context}\n"
            f"Complete this sub-task. Be concise and focused."
        )

        try:
            result = subprocess.run(
                [
                    self._claude_path, "--print",
                    "--model", task.model,
                    "--dangerously-skip-permissions",
                    "--max-turns", "3",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(self._repo_root),
            )
            if result.returncode != 0:
                logger.error("Sub-task %s CLI failed (exit %d): %s", task.id, result.returncode, result.stderr[:500])
                raise RuntimeError(f"Claude CLI failed (exit {result.returncode}): {result.stderr[:500]}")
            return result.stdout.strip()
        except RuntimeError:
            raise
        except Exception as exc:
            logger.error("Sub-task %s failed: %s", task.id, exc)
            return ""

    def update_progress(self, plan: OrchestrationPlan, comment_id: Optional[int] = None, repo: str = "") -> None:
        """Update the GitHub checklist comment with current progress."""
        if comment_id and repo:
            try:
                update_github_comment(comment_id, plan.to_checklist(), repo=repo)
            except Exception:
                logger.warning("Failed to update progress comment")

    def _build_plan_prompt(self, ticket: SWETicket) -> str:
        return (
            f"You are an orchestrator planning work for a SWE ticket.\n\n"
            f"## Ticket\n"
            f"**Title:** {ticket.title}\n"
            f"**Severity:** {ticket.severity.value}\n"
            f"**Type:** {getattr(ticket, 'ticket_type', 'unknown')}\n"
            f"**Module:** {ticket.source_module or 'unknown'}\n"
            f"**Description:** {ticket.description[:500]}\n"
            f"**Error log:** {(ticket.error_log or '')[:300]}\n\n"
            f"## Instructions\n"
            f"Create a plan with 3-7 sub-tasks. For each sub-task specify:\n"
            f"1. A one-line description\n"
            f"2. Which model should handle it (haiku for simple/tests, sonnet for implementation, opus only for complex analysis)\n"
            f"3. Which files to read\n"
            f"4. Which files to modify\n\n"
            f"Format each sub-task as:\n"
            f"TASK: description | MODEL: sonnet | READ: file1.py, file2.py | MODIFY: file3.py\n"
        )

    def _parse_plan(self, raw: str, ticket_id: str, session_tag: str) -> OrchestrationPlan:
        plan = OrchestrationPlan(ticket_id=ticket_id, session_tag=session_tag)

        if not raw:
            plan.analysis = "Opus planning unavailable — falling back to single-agent mode"
            plan.sub_tasks = [SubTask(id="1", description="Investigate and fix", model="sonnet")]
            return plan

        lines = raw.split("\n")
        analysis_lines = []
        task_count = 0

        for line in lines:
            line = line.strip()
            if line.upper().startswith("TASK:"):
                task_count += 1
                parts = line.split("|")
                desc = parts[0].replace("TASK:", "").strip()
                model = "sonnet"
                read_files: List[str] = []
                modify_files: List[str] = []
                for p in parts[1:]:
                    p = p.strip()
                    if p.upper().startswith("MODEL:"):
                        model = p.split(":", 1)[1].strip().lower()
                    elif p.upper().startswith("READ:"):
                        read_files = [f.strip() for f in p.split(":", 1)[1].split(",") if f.strip()]
                    elif p.upper().startswith("MODIFY:"):
                        modify_files = [f.strip() for f in p.split(":", 1)[1].split(",") if f.strip()]
                plan.sub_tasks.append(SubTask(
                    id=str(task_count),
                    description=desc,
                    model=model,
                    files_to_read=read_files,
                    files_to_modify=modify_files,
                ))
            else:
                analysis_lines.append(line)

        plan.analysis = "\n".join(analysis_lines[:10]).strip()

        if not plan.sub_tasks:
            plan.sub_tasks = [SubTask(id="1", description="Investigate and fix", model="sonnet")]

        return plan


def _is_code_gen_task(description: str) -> bool:
    """Return True if the task description suggests code generation work."""
    keywords = ["implement", "fix", "write", "create", "modify"]
    desc_lower = description.lower()
    return any(w in desc_lower for w in keywords)
