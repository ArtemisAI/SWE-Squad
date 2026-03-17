"""
Autonomous SWE Team — Agent-to-Agent development governance.

Provides the foundational module for an autonomous software engineering
team that monitors production systems, triages issues, coordinates
investigation and development, enforces stability gates, and manages
deployments — all via the A2A protocol.

Lifecycle:  detect → triage → investigate → develop → test → deploy → monitor

See docs/architecture/cr_026_autonomous_swe_team.md for the full design.
"""

from src.swe_team.models import (
    AgentRole,
    GovernanceVerdict,
    SWEAgentConfig,
    SWETicket,
    StabilityReport,
    TicketSeverity,
    TicketStatus,
)
from src.swe_team.events import SWEEvent, SWEEventType
from src.swe_team.config import (
    GovernanceConfig,
    MonitorConfig,
    SWETeamConfig,
    load_config,
)
from src.swe_team.monitor_agent import MonitorAgent
from src.swe_team.triage_agent import TriageAgent
from src.swe_team.investigator import InvestigatorAgent
from src.swe_team.ralph_wiggum import RalphWiggumGate
from src.swe_team.governance import DeploymentGovernor, DeploymentRecord, check_fix_complexity
from src.swe_team.developer import DeveloperAgent
from src.swe_team.creative_agent import CreativeAgent
from src.swe_team.distiller import TrajectoryDistiller
from src.swe_team.ticket_store import TicketStore
from src.swe_team.supabase_store import SupabaseTicketStore

__all__ = [
    # Models
    "AgentRole",
    "GovernanceVerdict",
    "SWEAgentConfig",
    "SWETicket",
    "StabilityReport",
    "TicketSeverity",
    "TicketStatus",
    # Events
    "SWEEvent",
    "SWEEventType",
    # Config
    "GovernanceConfig",
    "MonitorConfig",
    "SWETeamConfig",
    "load_config",
    # Agents
    "MonitorAgent",
    "TriageAgent",
    "InvestigatorAgent",
    # Governance
    "RalphWiggumGate",
    "DeploymentGovernor",
    "DeploymentRecord",
    "check_fix_complexity",
    "DeveloperAgent",
    "CreativeAgent",
    "TrajectoryDistiller",
    # Storage
    "TicketStore",
    "SupabaseTicketStore",
]
