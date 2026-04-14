from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, List, Optional

from src.swe_team.config import TeamConfig
from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.team_registry import TeamRegistry


_SEVERITY_ORDER = {
    TicketSeverity.CRITICAL: 0,
    TicketSeverity.HIGH: 1,
    TicketSeverity.MEDIUM: 2,
    TicketSeverity.LOW: 3,
}
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")


@dataclass
class AssignmentDecision:
    ticket_id: str
    team_id: str
    reason: str


class WorkloadDistributor:
    def __init__(self, team_registry: TeamRegistry, dependency_graph: Any) -> None:
        self._team_registry = team_registry
        self._dep_graph = dependency_graph
        self._last_tickets_by_id: dict[str, SWETicket] = {}

    def distribute(self, unassigned_tickets: List[SWETicket]) -> List[AssignmentDecision]:
        self._last_tickets_by_id = {t.ticket_id: t for t in unassigned_tickets}
        ready = self._dep_graph.get_ready_tickets(unassigned_tickets)
        sorted_tickets = sorted(ready, key=lambda t: _SEVERITY_ORDER.get(t.severity, 99))
        decisions: List[AssignmentDecision] = []
        for ticket in sorted_tickets:
            team = self._find_best_team(ticket)
            if not team:
                continue
            reason = self._build_reason(ticket, team)
            decisions.append(AssignmentDecision(ticket.ticket_id, team.name, reason))
        return decisions

    def _find_best_team(self, ticket: SWETicket) -> Optional[TeamConfig]:
        role = self._required_role(ticket)
        teams = self._team_registry.get_available_teams(role_filter=role)
        if not teams:
            # Fallback: try all teams if role-filtered list is empty
            teams = self._team_registry.get_available_teams()
        if not teams:
            return None

        terms = self._specialization_terms(ticket)
        stalled = self._is_stalled(ticket)
        is_critical = ticket.severity in (TicketSeverity.CRITICAL,)
        is_complex = self._is_complex_ticket(ticket)

        candidates: List[tuple[int, int, int, TeamConfig]] = []
        for team in teams:
            if stalled and ticket.assigned_to and team.name == ticket.assigned_to:
                continue
            available_capacity = max(team.max_concurrent - self._team_registry.get_team_load(team.name), 0)
            specialization_score = sum(1 for s in team.specialization if s in terms)

            # Tier-based routing: critical/complex → senior, dev → standard, investigation → economy
            tier = getattr(team, 'tier', 'standard')
            tier_bonus = 0
            if (is_critical or is_complex) and tier == 'senior':
                tier_bonus = 10  # strongly prefer senior tier for complex work
            elif role == 'developer' and tier == 'standard':
                tier_bonus = 5   # prefer standard tier for development
            elif role == 'investigator' and tier == 'economy':
                tier_bonus = 5   # prefer economy tier for investigation

            candidates.append((tier_bonus + specialization_score, available_capacity, specialization_score, team))

        if not candidates:
            return None
        candidates.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        return candidates[0][3]

    def _is_complex_ticket(self, ticket: SWETicket) -> bool:
        """Heuristic: ticket is complex if it has architecture/security labels or long description."""
        labels = {str(lbl).lower() for lbl in ticket.labels}
        complex_labels = {"architecture", "security", "critical", "orchestration", "complex", "regression"}
        if labels & complex_labels:
            return True
        desc = ticket.description or ""
        return len(desc) > 2000  # long descriptions usually mean complex tickets

    def _apply_assignments(self, decisions: List[AssignmentDecision], *, dry_run: bool = False) -> int:
        applied = 0
        for decision in decisions:
            ticket = self._last_tickets_by_id.get(decision.ticket_id)
            if ticket is None:
                continue
            issue_num = ticket.metadata.get("github_issue")
            repo = ticket.metadata.get("repo")
            if not issue_num or not repo:
                continue
            team = self._team_registry.get_team(decision.team_id)
            gh_login = getattr(team, "github_account", "")
            if not gh_login or not self._is_valid_assignment_target(repo, gh_login, issue_num):
                continue
            if dry_run:
                applied += 1
                continue
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(issue_num),
                    "--repo",
                    str(repo),
                    "--add-assignee",
                    gh_login,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                applied += 1
        return applied

    def _is_valid_assignment_target(self, repo: Any, gh_login: str, issue_num: Any) -> bool:
        if not isinstance(repo, str) or not _REPO_PATTERN.match(repo):
            return False
        if not _LOGIN_PATTERN.match(gh_login):
            return False
        return str(issue_num).isdigit()

    def _build_reason(self, ticket: SWETicket, team: TeamConfig) -> str:
        reason_parts = [f"capacity {self._team_registry.get_team_load(team.name)}/{team.max_concurrent}"]
        if self._is_stalled(ticket) and ticket.assigned_to and ticket.assigned_to != team.name:
            reason_parts.append(f"reassigned stalled ticket from {ticket.assigned_to}")
        terms = self._specialization_terms(ticket)
        matches = [s for s in team.specialization if s in terms]
        if matches:
            reason_parts.append(f"specialization match: {', '.join(matches)}")
        reason_parts.append(f"role={team.role}")
        reason_parts.append(f"priority={ticket.severity.value}")
        return "; ".join(reason_parts)

    def _required_role(self, ticket: SWETicket) -> str:
        required = str(ticket.metadata.get("required_role", "")).lower()
        if required:
            return required
        if ticket.status == TicketStatus.INVESTIGATING:
            return "investigator"
        labels = {str(lbl).lower() for lbl in ticket.labels}
        if "investigation" in labels:
            return "investigator"
        return "developer"

    def _specialization_terms(self, ticket: SWETicket) -> set[str]:
        terms = {str(lbl).lower() for lbl in ticket.labels}
        text = f"{ticket.title} {ticket.description}".lower()
        # Frontend/WebUI terms
        for kw in ("frontend", "webui", "react", "typescript", "vite", "ui/", "component"):
            if kw in text:
                terms.add(kw.rstrip("/"))
        # Architecture/complex terms
        for kw in ("architecture", "security", "orchestration", "critical", "complex", "regression"):
            if kw in text:
                terms.add(kw)
        # Investigation terms
        for kw in ("investigation", "triage", "research", "analysis", "documentation"):
            if kw in text:
                terms.add(kw)
        # Feature/enhancement
        if "[webui]" in text:
            terms.update({"webui", "frontend", "feature", "enhancement"})
        return terms

    def _is_stalled(self, ticket: SWETicket) -> bool:
        if ticket.status not in (TicketStatus.INVESTIGATING, TicketStatus.IN_DEVELOPMENT):
            return False
        try:
            updated = datetime.fromisoformat(ticket.updated_at)
        except ValueError:
            return False
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return updated <= datetime.now(timezone.utc) - timedelta(hours=2)
