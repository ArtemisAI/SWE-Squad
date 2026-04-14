"""SWE-Squad WebUI module.

Provides the web interface components for the SWE-Squad dashboard,
including user management, authentication, onboarding, and budget APIs.

Main exports:
    - UserStore: SQLite-backed user and secrets store
    - BudgetPolicyResponse, BudgetIncident, BudgetStatusResponse: Budget API dataclasses
    - budget_handle_get, budget_handle_post, budget_handle_put, budget_handle_delete: Budget API route handlers
    - Agent, AgentRun, AgentStats, AgentKey, Model: Agents API dataclasses
    - agents_handle_get, agents_handle_post, agents_handle_put, agents_handle_delete: Agents API route handlers
"""
from __future__ import annotations

# User management
from .user_store import UserStore

# Budget API
from .budgets_api import (
    BudgetPolicyResponse,
    BudgetIncident,
    BudgetStatusResponse,
    handle_get as budget_handle_get,
    handle_post as budget_handle_post,
    handle_put as budget_handle_put,
    handle_delete as budget_handle_delete,
)

# Agents API
from .agents_api import (
    Agent,
    AgentRun,
    AgentStats,
    AgentKey,
    Model,
    handle_get as agents_handle_get,
    handle_post as agents_handle_post,
    handle_put as agents_handle_put,
    handle_delete as agents_handle_delete,
)

__all__ = [
    # User management
    "UserStore",
    # Budget API dataclasses
    "BudgetPolicyResponse",
    "BudgetIncident",
    "BudgetStatusResponse",
    # Budget API handlers
    "budget_handle_get",
    "budget_handle_post",
    "budget_handle_put",
    "budget_handle_delete",
    # Agents API dataclasses
    "Agent",
    "AgentRun",
    "AgentStats",
    "AgentKey",
    "Model",
    # Agents API handlers
    "agents_handle_get",
    "agents_handle_post",
    "agents_handle_put",
    "agents_handle_delete",
]
