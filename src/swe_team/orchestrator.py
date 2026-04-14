"""
Opus Orchestrator — plans work and delegates to sub-agents.

For CRITICAL tickets and complex features, Opus analyzes the request,
creates a structured plan with sub-tasks, then delegates each sub-task
to the appropriate model tier (Sonnet for implementation, Haiku for
tests/linting). Progress is tracked via GitHub checklist comments.

Model boundary enforced: only Claude models for code generation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from src.swe_team.models import SWETicket
from src.swe_team.preflight import PreflightCheck
from src.swe_team.providers.coding_engine.base import CodingEngine
from src.swe_team.rbac_middleware import require_permission
from src.swe_team.session import make_session_tag
from src.swe_team import github_integration

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

    AGENT_NAME = "swe_orchestrator"

    def __init__(
        self,
        claude_path: str = "",
        repo_root: Optional[Path] = None,
        model: str = "opus",
        issue_tracker: Optional[object] = None,
        engine: Optional[CodingEngine] = None,
        rbac_engine: Optional[object] = None,
        preflight: Optional[PreflightCheck] = None,
    ):
        self._repo_root = repo_root or Path.cwd()
        self._model = model
        self._issue_tracker = issue_tracker
        if engine is not None:
            self._engine: CodingEngine = engine
        else:
            from src.swe_team.providers.coding_engine.claude import ClaudeCodeEngine
            self._engine = ClaudeCodeEngine(default_model=model)
        # RBAC — optional, backward compatible
        self._rbac_engine = rbac_engine
        self._agent_name: str = self.AGENT_NAME
        # Injected preflight — optional, backward compatible
        self._preflight: Optional[PreflightCheck] = preflight

    @require_permission("orchestration")
    def plan(self, ticket: SWETicket) -> OrchestrationPlan:
        """Use Opus to analyze the ticket and create a structured plan."""
        # Preflight: validate execution context before planning
        if self._preflight is not None:
            preflight_result = self._preflight.run()
            if not preflight_result.passed:
                logger.warning(
                    "Preflight FAILED for orchestration of ticket %s: %s",
                    ticket.ticket_id,
                    preflight_result.summary(),
                )
                ticket.metadata["preflight_failure"] = preflight_result.failures
                raise RuntimeError(f"Preflight failed: {preflight_result.summary()}")

        session_tag = make_session_tag(
            issue_number=ticket.metadata.get("github_issue"),
            ticket_id=ticket.ticket_id,
        )

        prompt = self._build_plan_prompt(ticket)

        # Debug logging before CLI call
        logger.debug("Orchestrator plan CLI call: model=%s, prompt_preview=%s", self._model, prompt[:100])

        try:
            result = self._engine.run(
                prompt,
                model=self._model,
                timeout=600,
                cwd=str(self._repo_root),
            )
            if not result.success:
                # Enhanced error logging: capture both stdout and stderr
                stdout_preview = result.stdout[:200] if result.stdout else "(empty)"
                stderr_preview = result.stderr[:500] if result.stderr else "(empty)"
                error_msg = f"Orchestrator plan CLI failed (exit {result.returncode})"

                # Check stdout for API errors when stderr is empty (JSON-mode errors)
                if not result.stderr:
                    error_source = stdout_preview if "error" in stdout_preview.lower() else stderr_preview
                    if "429" in stdout_preview or "rate limit" in stdout_preview.lower():
                        error_msg += " (rate limit)"
                    logger.error(
                        "%s - stdout_preview=%s, stderr_preview=%s",
                        error_msg, stdout_preview, stderr_preview
                    )
                    raise RuntimeError(f"{error_msg}: {error_source}")
                else:
                    logger.error("%s: %s (stdout: %s)", error_msg, stderr_preview, stdout_preview)
                    raise RuntimeError(f"{error_msg}: {stderr_preview}")
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

        # Debug logging before CLI call
        logger.debug(
            "Sub-task %s CLI call: model=%s, prompt_preview=%s",
            task.id, task.model, prompt[:100]
        )

        try:
            result = self._engine.run(
                prompt,
                model=task.model,
                timeout=900,
                cwd=str(self._repo_root),
            )
            if not result.success:
                # Enhanced error logging: capture both stdout and stderr
                stdout_preview = result.stdout[:200] if result.stdout else "(empty)"
                stderr_preview = result.stderr[:500] if result.stderr else "(empty)"
                error_msg = f"Sub-task {task.id} CLI failed (exit {result.returncode})"

                # Check stdout for API errors when stderr is empty (JSON-mode errors)
                if not result.stderr:
                    error_source = stdout_preview if "error" in stdout_preview.lower() else stderr_preview
                    if "429" in stdout_preview or "rate limit" in stdout_preview.lower():
                        error_msg += " (rate limit)"
                    logger.error(
                        "%s - stdout_preview=%s, stderr_preview=%s",
                        error_msg, stdout_preview, stderr_preview
                    )
                    raise RuntimeError(f"{error_msg}: {error_source}")
                else:
                    logger.error("%s: %s (stdout: %s)", error_msg, stderr_preview, stdout_preview)
                    raise RuntimeError(f"{error_msg}: {stderr_preview}")
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
                if self._issue_tracker is not None and hasattr(self._issue_tracker, "update_comment"):
                    self._issue_tracker.update_comment(comment_id, plan.to_checklist(), repo=repo)
                else:
                    github_integration.update_github_comment(comment_id, plan.to_checklist(), repo=repo)
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
