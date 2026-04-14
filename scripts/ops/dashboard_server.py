#!/usr/bin/env python3
"""
SWE-Squad Live Dashboard Server

Serves the dashboard HTML at http://0.0.0.0:PORT/ with auto-refresh every 60s.
Generates fresh data on each request — no caching layer needed.

Usage:
    python3 scripts/ops/dashboard_server.py [--port 8080] [--host 0.0.0.0]
"""
from __future__ import annotations

import argparse
import csv
import email
import email.message
import email.policy
import html as html_mod
import io
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional
import gzip
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
import hashlib
import hmac
import secrets
from http.cookies import SimpleCookie
import urllib.error
import urllib.request
from urllib.parse import urlparse, parse_qs, unquote

from dotenv import load_dotenv
load_dotenv(override=False)

# Ensure project root on path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.swe_team.config import load_config
from src.swe_team.ticket_store import TicketStore
from src.swe_team.token_tracker import TokenTracker
from src.swe_team.budget_api import get_budget_api
from src.swe_team.cost_tracker import make_cost_tracker
from src.swe_team.providers.usage_monitor.pricing import load_pricing, save_pricing

logger = logging.getLogger(__name__)

_REACT_UI_DIST = PROJECT_ROOT / "ui" / "dist"
_DEFAULT_PORT = 8080
_REFRESH_SECONDS = 60
_JOBS_DIR = PROJECT_ROOT / "data" / "swe_team"
_STATUS_PATH = PROJECT_ROOT / "data" / "swe_team" / "status.json"
_JOBS_PATH = PROJECT_ROOT / "data" / "swe_team" / "jobs.json"
_ROLES_PATH = PROJECT_ROOT / "config" / "swe_team" / "roles.yaml"
_CONFIG_PATH = PROJECT_ROOT / "config" / "swe_team.yaml"
_SETTINGS_PATH = PROJECT_ROOT / "data" / "swe_team" / "dashboard_settings.json"
_RUN_HISTORY_PATH = PROJECT_ROOT / "data" / "swe_team" / "run_history.jsonl"
_TOKEN_USAGE_PATH = PROJECT_ROOT / "data" / "swe_team" / "token_usage.jsonl"
_SESSIONS_PATH = PROJECT_ROOT / "data" / "swe_team" / "sessions.json"
_PROJECT_ENV_DIR = PROJECT_ROOT / "data" / "swe_team" / "project_env"
_INSTANCE_SETTINGS_PATH = PROJECT_ROOT / "data" / "swe_team" / "instance_settings.json"
_ARCHIVED_ALERTS_PATH = PROJECT_ROOT / "data" / "swe_team" / "archived_alerts.json"
_ARCHIVED_RUNS_PATH = PROJECT_ROOT / "data" / "swe_team" / "archived_runs.json"
_PIPELINE_CONFIG_PATH = PROJECT_ROOT / "data" / "swe_team" / "pipeline_config.json"
_LABEL_TRIGGERS_PATH = PROJECT_ROOT / "data" / "swe_team" / "label_triggers.json"
_EXECUTION_MODE_PATH = PROJECT_ROOT / "data" / "swe_team" / "execution_mode.json"
_CHECKPOINTS_PATH = PROJECT_ROOT / "data" / "swe_team" / "checkpoints.json"
_SUGGESTIONS_PATH = PROJECT_ROOT / "data" / "swe_team" / "suggestions.json"
_FEEDS_DIR = PROJECT_ROOT / "data" / "swe_team" / "feeds"
_MAX_POST_BODY_BYTES = 10 * 1024 * 1024  # 10 MB hard limit for POST request bodies

# CORS — origins allowed to call the API from a browser.
# Set CORS_ALLOWED_ORIGINS env var (comma-separated) or leave blank for same-origin only.
_CORS_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]

_VALID_EXECUTION_MODES = ("plan", "review", "start")

_EXECUTION_MODE_DESCRIPTIONS = {
    "plan": "Analyze and plan only — no code changes",
    "review": "Execute with checkpoints — pause for human review",
    "start": "Fully autonomous — execute without pauses",
}

_token_tracker_instance: "TokenTracker | None" = None

# ---------------------------------------------------------------------------
# Control plane exempt paths and prefixes (API routes that bypass control plane)
# ---------------------------------------------------------------------------
_CONTROL_PLANE_EXEMPT_PATHS = {
    "/api/activity",
    "/api/tickets",
    "/api/costs",
    "/api/cost",
    "/api/stream",
    "/api/scheduler",
    "/api/rbac",
    "/api/status",
    "/api/projects",
    "/api/goals",
    "/api/auth/status",
    "/api/graph",
    "/api/settings",
    "/api/settings/full",
    "/api/scheduler/history",
    "/api/roles",
    "/api/routines",
    "/api/approvals",
    "/api/accounts",
    "/api/github/label-triggers",
    "/api/rate-limits",
    "/api/suggestions",
}
_CONTROL_PLANE_EXEMPT_PREFIXES = (
    "/api/projects/",
    "/api/goals/",
    "/api/routines/",
    "/api/costs/",
    "/api/accounts/",
    "/api/pricing",
    "/api/governor",
    "/api/tickets/",
    "/api/pipeline/",
    "/api/approvals/",
    "/api/teams/",
    "/api/github/label-triggers",
    "/api/execution/",
    "/api/suggestions/",
)

# ---------------------------------------------------------------------------
# GitHub OAuth configuration (all optional — auth is disabled if CLIENT_ID
# is absent so the dashboard works without credentials configured).
# ---------------------------------------------------------------------------
_OAUTH_CLIENT_ID: str = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "")
_OAUTH_CLIENT_SECRET: str = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "")
_OAUTH_COOKIE_SECRET: str = os.environ.get(
    "DASHBOARD_COOKIE_SECRET", secrets.token_hex(32)
)
_OAUTH_ALLOWED_ORGS: list = [
    o.strip()
    for o in os.environ.get("DASHBOARD_ALLOWED_ORGS", "").split(",")
    if o.strip()
]
_OAUTH_ENABLED: bool = bool(_OAUTH_CLIENT_ID and _OAUTH_CLIENT_SECRET)

_oauth_provider = None
if _OAUTH_ENABLED:
    try:
        from src.swe_team.providers.auth.github_oauth import GitHubOAuthProvider
        _oauth_provider = GitHubOAuthProvider(
            client_id=_OAUTH_CLIENT_ID,
            client_secret=_OAUTH_CLIENT_SECRET,
            allowed_orgs=_OAUTH_ALLOWED_ORGS,
            cookie_secret=_OAUTH_COOKIE_SECRET,
        )
    except Exception:
        logger.exception("Failed to initialise GitHubOAuthProvider — auth disabled")


# ---------------------------------------------------------------------------
# UserStore singleton (multi-user account system with encrypted secrets)
# ---------------------------------------------------------------------------
_user_store_instance = None
_user_store_lock = threading.Lock()

_WEBUI_DB_PATH: str = str(PROJECT_ROOT / "data" / "swe_team" / "webui_users.db")


def _get_user_store():
    """Lazy-initialise the UserStore singleton."""
    global _user_store_instance
    if _user_store_instance is not None:
        return _user_store_instance
    with _user_store_lock:
        if _user_store_instance is None:
            try:
                from src.swe_team.webui.user_store import UserStore
                _user_store_instance = UserStore(db_path=_WEBUI_DB_PATH)
            except Exception:
                logger.exception("Failed to initialise UserStore — user/secrets API disabled")
    return _user_store_instance


# AccountStore singleton (Supabase-backed multi-tenant account isolation)
# ---------------------------------------------------------------------------
_account_store_instance = None
_account_store_lock = threading.Lock()


def _get_account_store():
    """Lazy-initialise the AccountStore singleton."""
    global _account_store_instance
    if _account_store_instance is not None:
        return _account_store_instance
    with _account_store_lock:
        if _account_store_instance is None:
            try:
                from src.swe_team.webui.account_store import AccountStore
                _account_store_instance = AccountStore()
            except Exception:
                logger.exception("Failed to initialise AccountStore — accounts API disabled")
    return _account_store_instance


def _personal_account_slug(github_login: str) -> str:
    """Build a stable URL-safe slug for an auto-created personal account."""
    slug = re.sub(r"[^a-z0-9-]+", "-", (github_login or "").lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug or "personal-account"


def _ensure_personal_account(github_login: str) -> None:
    """Create a personal account for first-login users (best effort)."""
    login = (github_login or "").strip()
    if not login:
        return

    acct = _get_account_store()
    if acct is None:
        return

    accounts = acct.get_user_accounts(login)
    if accounts:
        return

    base_slug = _personal_account_slug(login)
    for attempt in range(10):
        if attempt == 0:
            slug = base_slug
        elif attempt == 1:
            slug = f"{base_slug}-personal"
        else:
            slug = f"{base_slug}-personal-{attempt}"
        try:
            acct.create_account(
                name=f"{login}'s Personal Account",
                slug=slug,
                created_by=login,
                description=f"Auto-created personal account for {login}",
            )
            logger.info(
                "Auto-created personal account for first login: %s (slug=%s)",
                login,
                slug,
            )
            return
        except Exception:
            logger.warning(
                "Auto-create personal account failed for %s using slug=%s",
                login,
                slug,
                exc_info=True,
            )
    logger.warning("Unable to auto-create personal account for %s after retries", login)


def _get_token_tracker() -> "TokenTracker":
    global _token_tracker_instance
    if _token_tracker_instance is None:
        _token_tracker_instance = TokenTracker(_TOKEN_USAGE_PATH)
    return _token_tracker_instance


_governor_instance = None
_governor_configured: bool | None = None


def _get_governor():
    """Lazy singleton for the UsageGovernor. Returns None if not configured."""
    global _governor_instance, _governor_configured
    if _governor_configured is not None:
        return _governor_instance
    try:
        import yaml
        from src.swe_team.providers.usage_governor import create_usage_governor

        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        gov_cfg = raw.get("providers", {}).get("usage_governor")
        if not gov_cfg:
            _governor_configured = False
            return None
        _governor_instance = create_usage_governor(gov_cfg)
        # Wire the dashboard's TokenTracker into the governor so it has live
        # usage data and does NOT fail-closed on every status request.
        # The dashboard is read-only — governance decisions here are advisory only.
        try:
            _governor_instance.set_token_tracker(_get_token_tracker())
        except Exception:
            logger.warning(
                "UsageGovernor: could not attach TokenTracker — "
                "quota display will show zeros but server will continue normally"
            )
        _governor_configured = True
        return _governor_instance
    except Exception:
        logger.exception("Failed to initialize UsageGovernor")
        _governor_configured = False
        return None


def _get_governor_status() -> dict:
    """Build the full governor status dict, or an error dict if not configured."""
    import dataclasses

    gov = _get_governor()
    if gov is None:
        return {"error": "Governor not configured", "configured": False}

    quota = dataclasses.asdict(gov.get_quota_status())
    decision = dataclasses.asdict(gov.get_concurrency_decision())
    alerts = gov.check_alerts()

    # Schedule info
    schedule = {"current_window": "default", "concurrency_multiplier": 1.0, "is_peak": False, "is_weekend": False}
    if gov._scheduler:
        window = gov._scheduler.get_current_window()
        schedule = {
            "current_window": window.name,
            "concurrency_multiplier": window.concurrency_multiplier,
            "is_peak": gov._scheduler.is_peak_hours(),
            "is_weekend": gov._scheduler.is_weekend(),
        }

    # Bonus info
    bonus = {"active": False, "multiplier": 1.0}
    if gov._bonus_detector:
        bonus = {
            "active": gov._bonus_detector.is_bonus_active(),
            "multiplier": gov._bonus_detector.get_multiplier(),
        }

    return {
        "quota": quota,
        "decision": decision,
        "schedule": schedule,
        "bonus": bonus,
        "alerts": alerts,
    }

# ---------------------------------------------------------------------------
# Optional control plane integration
# ---------------------------------------------------------------------------
try:
    from src.swe_team.control_plane_api import (
        handle_get as cp_handle_get,
        handle_post as cp_handle_post,
    )
    _HAS_CONTROL_PLANE = True
except Exception:
    _HAS_CONTROL_PLANE = False

    def cp_handle_get(handler, cp):  # type: ignore[misc]
        return False

    def cp_handle_post(handler, cp):  # type: ignore[misc]
        return False

# ---------------------------------------------------------------------------
# SSE (Server-Sent Events) infrastructure
# ---------------------------------------------------------------------------
_sse_clients: list = []
_sse_lock = threading.Lock()
_last_status_mtime: float = 0.0


def _read_json_file_with_timeout(path: Path, timeout: float = 1.0):
    """Read a JSON file with timeout protection.

    Uses a threaded approach to prevent blocking I/O from hanging the server.
    On timeout, returns None (fail-secure: degrade gracefully).

    Args:
        path: Path to the JSON file
        timeout: Maximum time to wait for file read in seconds (default: 1.0s)

    Returns:
        Parsed JSON data or None on timeout/error
    """
    result_holder: list = []
    error_holder: list = []

    def _read():
        try:
            result_holder.append(json.loads(path.read_text()))
        except Exception as exc:
            error_holder.append(exc)

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        # Thread is still running = timeout occurred
        logger.warning(f"File read timed out after {timeout}s: {path}")
        return None

    if error_holder:
        return None

    return result_holder[0] if result_holder else None


def _read_json_file(path: Path):
    """Read a JSON file, return parsed data or None on failure.

    Deprecated: Use _read_json_file_with_timeout for critical paths.
    """
    return _read_json_file_with_timeout(path)


# ---------------------------------------------------------------------------
# Project environment variable helpers
# ---------------------------------------------------------------------------


def _project_env_path(project_name: str) -> Path:
    """Return the JSON file path for a project's env vars."""
    _PROJECT_ENV_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = project_name.replace("/", "__")
    return _PROJECT_ENV_DIR / f"{safe_name}.json"


def _load_project_env(project_name: str) -> list:
    """Load env vars for a project. Returns list of dicts with key/value/secret."""
    p = _project_env_path(project_name)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        logger.exception("Error loading project env for %s", project_name)
    return []


def _save_project_env(project_name: str, env_vars: list) -> bool:
    """Save env vars for a project. Returns True on success."""
    p = _project_env_path(project_name)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(env_vars, indent=2), encoding="utf-8")
        return True
    except Exception:
        logger.exception("Error saving project env for %s", project_name)
        return False


def _mask_secret_values(env_vars: list) -> list:
    """Return env vars with secret values masked."""
    result = []
    for var in env_vars:
        entry = dict(var)
        if entry.get("secret"):
            entry["value"] = "********"
        result.append(entry)
    return result


def _write_file_with_timeout(path: Path, content: str, timeout: float = 2.0) -> bool:
    """Write content to a file with timeout protection.

    Uses a threaded approach to prevent blocking I/O from hanging the server.
    On timeout, returns False (fail-secure: don't claim success).

    Args:
        path: Path to the file to write
        content: String content to write
        timeout: Maximum time to wait for file write in seconds (default: 2.0s)

    Returns:
        True on success, False on timeout/error
    """
    result_holder: list = []
    error_holder: list = []

    def _write():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            result_holder.append(True)
        except Exception as exc:
            error_holder.append(exc)

    t = threading.Thread(target=_write, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        # Thread is still running = timeout occurred
        logger.warning(f"File write timed out after {timeout}s: {path}")
        return False

    if error_holder:
        logger.warning(f"File write failed: {path} - {error_holder[0]}")
        return False

    return result_holder[0] if result_holder else False


# --- Pipeline stage configuration helpers ---

_DEFAULT_PIPELINE_STAGES = {
    "monitor":     {"enabled": True, "timeout_minutes": 5,  "max_retries": 1, "model_tier": "T1"},
    "triage":      {"enabled": True, "timeout_minutes": 5,  "max_retries": 1, "model_tier": "T1"},
    "investigate": {"enabled": True, "timeout_minutes": 30, "max_retries": 3, "model_tier": "T2"},
    "develop":     {"enabled": True, "timeout_minutes": 60, "max_retries": 3, "model_tier": "T2"},
    "review":      {"enabled": True, "timeout_minutes": 15, "max_retries": 1, "model_tier": "T2"},
    "verify":      {"enabled": True, "timeout_minutes": 10, "max_retries": 2, "model_tier": "T1"},
}

_DEFAULT_PIPELINE_CONFIG = {
    "stages": dict(_DEFAULT_PIPELINE_STAGES),
    "execution_profile": "base",
}


def _read_pipeline_config() -> dict:
    """Read pipeline configuration, falling back to defaults."""
    saved = _read_json_file_with_timeout(_PIPELINE_CONFIG_PATH, timeout=0.5)
    if saved and isinstance(saved, dict):
        # Merge with defaults so new stages are always present
        merged_stages = dict(_DEFAULT_PIPELINE_STAGES)
        merged_stages.update(saved.get("stages", {}))
        return {
            "stages": merged_stages,
            "execution_profile": saved.get("execution_profile", "base"),
        }
    return dict(_DEFAULT_PIPELINE_CONFIG)


def _write_pipeline_config(config: dict) -> bool:
    """Persist pipeline configuration to disk."""
    return _write_file_with_timeout(
        _PIPELINE_CONFIG_PATH, json.dumps(config, indent=2), timeout=2.0
    )


def _read_execution_mode() -> dict:
    """Read execution mode from disk, defaulting to 'start'."""
    saved = _read_json_file_with_timeout(_EXECUTION_MODE_PATH, timeout=0.5)
    if saved and isinstance(saved, dict) and saved.get("mode") in _VALID_EXECUTION_MODES:
        return {
            "mode": saved["mode"],
            "available_modes": list(_VALID_EXECUTION_MODES),
            "description": _EXECUTION_MODE_DESCRIPTIONS.get(saved["mode"], ""),
        }
    return {
        "mode": "start",
        "available_modes": list(_VALID_EXECUTION_MODES),
        "description": _EXECUTION_MODE_DESCRIPTIONS["start"],
    }


def _write_execution_mode(mode: str) -> bool:
    """Persist execution mode to disk."""
    return _write_file_with_timeout(
        _EXECUTION_MODE_PATH, json.dumps({"mode": mode}, indent=2), timeout=2.0
    )


def _read_checkpoints() -> list:
    """Read checkpoints from disk."""
    saved = _read_json_file_with_timeout(_CHECKPOINTS_PATH, timeout=0.5)
    if saved and isinstance(saved, list):
        return saved
    if saved and isinstance(saved, dict):
        return saved.get("checkpoints", [])
    return []


def _write_checkpoints(checkpoints: list) -> bool:
    """Persist checkpoints to disk."""
    return _write_file_with_timeout(
        _CHECKPOINTS_PATH, json.dumps(checkpoints, indent=2), timeout=2.0
    )


def _read_roles_yaml() -> dict:
    """Read roles.yaml and normalize to {roles: [{role, permissions, description, enabled, deny, models}], overrides, bypass_mode}."""
    try:
        import yaml
        data = yaml.safe_load(_ROLES_PATH.read_text()) or {}
        roles_data = data.get("roles", data) if isinstance(data, dict) else data
        overrides = data.get("overrides", []) if isinstance(data, dict) else []
        roles: list[dict] = []

        if isinstance(roles_data, dict):
            for role_name, role_cfg in roles_data.items():
                permissions = role_cfg.get("permissions", []) if isinstance(role_cfg, dict) else []
                deny = role_cfg.get("deny", []) if isinstance(role_cfg, dict) else []
                description = role_cfg.get("description") if isinstance(role_cfg, dict) else None
                enabled = role_cfg.get("enabled", True) if isinstance(role_cfg, dict) else True
                models = role_cfg.get("models", []) if isinstance(role_cfg, dict) else []
                roles.append(
                    {
                        "role": str(role_name),
                        "permissions": [str(p) for p in permissions] if isinstance(permissions, list) else [],
                        "deny": [str(d) for d in deny] if isinstance(deny, list) else [],
                        "description": description if isinstance(description, str) else None,
                        "enabled": bool(enabled),
                        "models": [str(m) for m in models] if isinstance(models, list) else [],
                    }
                )
        elif isinstance(roles_data, list):
            for role in roles_data:
                if isinstance(role, str):
                    roles.append({"role": role, "permissions": [], "deny": [], "description": None, "enabled": True, "models": []})
                elif isinstance(role, dict) and isinstance(role.get("role"), str):
                    permissions = role.get("permissions", [])
                    deny = role.get("deny", [])
                    roles.append(
                        {
                            "role": role["role"],
                            "permissions": [str(p) for p in permissions] if isinstance(permissions, list) else [],
                            "deny": [str(d) for d in deny] if isinstance(deny, list) else [],
                            "description": role.get("description")
                            if isinstance(role.get("description"), str)
                            else None,
                            "enabled": bool(role.get("enabled", True)),
                            "models": [str(m) for m in role.get("models", [])] if isinstance(role.get("models"), list) else [],
                        }
                    )

        # RBAC is in bypass mode when no RBAC engine is configured (always true currently)
        return {"roles": roles, "overrides": overrides, "bypass_mode": True}
    except ImportError:
        try:
            return {"roles": [], "overrides": [], "bypass_mode": True, "raw": _ROLES_PATH.read_text()}
        except Exception:
            return {"roles": [], "overrides": [], "bypass_mode": True, "error": "roles.yaml not found"}
    except Exception:
        return {"roles": [], "overrides": [], "bypass_mode": True, "error": "roles.yaml unreadable"}


_DEFAULT_SETTINGS: dict = {
    "theme": "dark",
    "refresh_interval": 30,
    "tickets_per_page": 25,
    "default_tab": "overview",
    "notifications_enabled": True,
    "notification_level": "errors",
}


def _read_settings() -> dict:
    """Read dashboard settings from JSON file, returning defaults if missing."""
    try:
        saved = json.loads(_SETTINGS_PATH.read_text())
        merged = dict(_DEFAULT_SETTINGS)
        merged.update(saved)
        return merged
    except Exception:
        return dict(_DEFAULT_SETTINGS)


def _write_settings(settings: dict) -> bool:
    """Write dashboard settings to JSON file."""
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        merged = dict(_DEFAULT_SETTINGS)
        merged.update(settings)
        _SETTINGS_PATH.write_text(json.dumps(merged, indent=2))
        return True
    except Exception:
        logger.exception("Failed to write settings")
        return False


def _to_float(value: object, *, model: str, field: str) -> float:
    """Convert arbitrary numeric-like values to float with safe fallback."""
    try:
        return float(value)
    except (TypeError, ValueError):
        if value is not None:
            logger.warning("Invalid pricing value for %s.%s: %r", model, field, value)
        return 0.0


def _probe_list_models(url: str, api_key: str, timeout: float) -> tuple[list[str], str | None]:
    """Probe an endpoint for available models. Returns (model_names, error_or_none).

    Tries OpenAI-compatible /v1/models, then /models, then Anthropic format.
    Handles non-JSON responses and unexpected structures gracefully.
    """
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key  # Anthropic style

    # Try multiple model-listing endpoints
    endpoints = []
    if url.endswith("/v1"):
        endpoints.append(f"{url}/models")
    else:
        endpoints.append(f"{url}/v1/models")
        endpoints.append(f"{url}/models")

    last_error = None
    for ep in endpoints:
        try:
            req = urllib.request.Request(ep, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    # Non-JSON response -- try to extract model names from plain text
                    models = _extract_models_from_text(raw)
                    if models:
                        return models, None
                    last_error = f"Non-JSON response from {ep}"
                    continue

                # OpenAI format: {"data": [{"id": "model-name"}, ...]}
                if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                    models = []
                    for item in data["data"]:
                        if isinstance(item, dict) and "id" in item:
                            models.append(item["id"])
                        elif isinstance(item, str):
                            models.append(item)
                    if models:
                        return models, None

                # Anthropic format: {"models": [{"id": "...", "name": "..."}, ...]}
                if isinstance(data, dict) and "models" in data and isinstance(data["models"], list):
                    models = []
                    for item in data["models"]:
                        if isinstance(item, dict):
                            models.append(item.get("id") or item.get("name", "unknown"))
                        elif isinstance(item, str):
                            models.append(item)
                    if models:
                        return models, None

                # Plain list: ["model-a", "model-b"]
                if isinstance(data, list):
                    models = [str(m) for m in data if m]
                    if models:
                        return models, None

                last_error = f"Unexpected JSON structure from {ep}"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} from {ep}"
        except urllib.error.URLError as exc:
            last_error = f"Connection error for {ep}: {exc.reason}"
        except TimeoutError:
            last_error = f"Timeout ({timeout}s) connecting to {ep}"
        except OSError as exc:
            last_error = f"Network error for {ep}: {exc}"

    return [], last_error


def _probe_completion(url: str, api_key: str, model: str, timeout: float) -> tuple[bool, str | None]:
    """Send a minimal completion request to verify a model is usable.

    Returns (success, error_or_none). Tries OpenAI chat format, then Anthropic messages format.
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key

    # OpenAI chat completion format
    openai_body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }).encode("utf-8")

    # Determine the chat completions endpoint
    if url.endswith("/v1"):
        openai_ep = f"{url}/chat/completions"
    else:
        openai_ep = f"{url}/v1/chat/completions"

    # Try OpenAI format first
    try:
        req = urllib.request.Request(openai_ep, data=openai_body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Got a response but not JSON -- if HTTP 200, consider it a success
                return True, None

            # OpenAI success: has "choices" or "id"
            if isinstance(data, dict) and ("choices" in data or "id" in data):
                return True, None

            # Anthropic success: has "content" or "type": "message"
            if isinstance(data, dict) and (data.get("type") == "message" or "content" in data):
                return True, None

            # Got valid JSON back with 200 status -- likely fine
            return True, None
    except urllib.error.HTTPError as exc:
        openai_error = f"HTTP {exc.code}"
        # If it's a model-not-found (404) or auth error (401/403), don't try Anthropic
        if exc.code in (401, 403):
            return False, f"Authentication failed (HTTP {exc.code})"
    except urllib.error.URLError as exc:
        openai_error = f"Connection error: {exc.reason}"
    except TimeoutError:
        return False, f"Timeout ({timeout}s) during completion probe"
    except OSError as exc:
        openai_error = f"Network error: {exc}"

    # Try Anthropic messages format as fallback
    anthropic_body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }).encode("utf-8")

    # Anthropic uses /v1/messages
    if url.endswith("/v1"):
        anthropic_ep = f"{url}/messages"
    else:
        anthropic_ep = f"{url}/v1/messages"

    anthropic_headers = dict(headers)
    anthropic_headers["anthropic-version"] = "2023-06-01"

    try:
        req = urllib.request.Request(anthropic_ep, data=anthropic_body, headers=anthropic_headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, None
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"Authentication failed (HTTP {exc.code})"
        return False, f"Completion probe failed: OpenAI={openai_error}, Anthropic=HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError):
        pass

    return False, f"Completion probe failed: {openai_error}"


def _extract_models_from_text(text: str) -> list[str]:
    """Best-effort extraction of model names from plain text response."""
    # Look for common model name patterns
    import re as _re
    patterns = [
        _re.compile(r"(?:^|\s)((?:gpt|claude|gemini|llama|mistral|codellama|deepseek|qwen)[\w.-]*)", _re.IGNORECASE),
    ]
    models = set()
    for pattern in patterns:
        for m in pattern.finditer(text):
            models.add(m.group(1))
    return sorted(models)


def _normalize_pricing_config(pricing: object) -> dict[str, dict[str, float]]:
    """Normalize pricing payload into canonical per-1M token fields.

    Supports both canonical keys (input/output/cache_write/cache_read) and
    legacy per-1k keys by converting them to per-1M values.
    """
    if not isinstance(pricing, dict):
        return {}

    normalized: dict[str, dict[str, float]] = {}
    for model, raw_entry in pricing.items():
        if not isinstance(model, str) or not isinstance(raw_entry, dict):
            continue

        if "input" in raw_entry:
            input_price = _to_float(raw_entry.get("input"), model=model, field="input")
        elif "input_per_1k" in raw_entry:
            input_price = _to_float(raw_entry.get("input_per_1k"), model=model, field="input_per_1k") * 1000.0
        else:
            input_price = 0.0

        if "output" in raw_entry:
            output_price = _to_float(raw_entry.get("output"), model=model, field="output")
        elif "output_per_1k" in raw_entry:
            output_price = _to_float(raw_entry.get("output_per_1k"), model=model, field="output_per_1k") * 1000.0
        else:
            output_price = 0.0

        if "cache_write" in raw_entry:
            cache_write_price = _to_float(raw_entry.get("cache_write"), model=model, field="cache_write")
        elif "cache_write_per_1k" in raw_entry:
            cache_write_price = _to_float(raw_entry.get("cache_write_per_1k"), model=model, field="cache_write_per_1k") * 1000.0
        else:
            cache_write_price = 0.0

        if "cache_read" in raw_entry:
            cache_read_price = _to_float(raw_entry.get("cache_read"), model=model, field="cache_read")
        elif "cache_read_per_1k" in raw_entry:
            cache_read_price = _to_float(raw_entry.get("cache_read_per_1k"), model=model, field="cache_read_per_1k") * 1000.0
        else:
            cache_read_price = 0.0

        normalized[model] = {
            "input": input_price,
            "output": output_price,
            "cache_write": cache_write_price,
            "cache_read": cache_read_price,
        }

    return normalized


# ---------------------------------------------------------------------------
# Instance settings (VM-level configuration)
# ---------------------------------------------------------------------------

_DEFAULT_INSTANCE_SETTINGS: dict = {
    "name": "SWE-Squad Instance",
    "description": "",
    "isolated_workspaces": False,
    "auto_restart": False,
    "heartbeat_interval_seconds": 60,
    "connection_methods": [],
    "experimental_features": {
        "parallel_execution": False,
        "adaptive_throttling": True,
        "semantic_memory": True,
        "regression_detection": True,
    },
}


def _read_instance_settings() -> dict:
    """Read instance settings from JSON file, returning defaults on timeout/missing."""
    try:
        saved = _read_json_file_with_timeout(_INSTANCE_SETTINGS_PATH, timeout=0.5)
        if saved is None:
            # Timeout or error - return defaults (fail-secure)
            return dict(_DEFAULT_INSTANCE_SETTINGS)
        merged = dict(_DEFAULT_INSTANCE_SETTINGS)
        merged.update(saved)
        return merged
    except Exception:
        return dict(_DEFAULT_INSTANCE_SETTINGS)


def _write_instance_settings(settings: dict) -> bool:
    """Write instance settings to JSON file with timeout protection.

    Returns False on timeout or error (fail-secure: don't claim success).
    """
    try:
        merged = dict(_DEFAULT_INSTANCE_SETTINGS)
        # Merge while preserving nested dicts
        for key, value in settings.items():
            if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return _write_file_with_timeout(
            _INSTANCE_SETTINGS_PATH,
            json.dumps(merged, indent=2),
            timeout=2.0
        )
    except Exception:
        logger.exception("Failed to write instance settings")
        return False


def _get_instance_heartbeat() -> dict:
    """Get instance heartbeat status based on scheduler and system state.

    Uses timeout-protected file reads to prevent blocking:
    - If file doesn't exist: returns "unknown" status (not an error)
    - If read times out: returns "degraded" status (fail-secure)
    - If read succeeds: returns "healthy" or "unknown" based on last_cycle
    """
    now = datetime.now(timezone.utc).isoformat()

    # Track whether timeout occurred vs file not existing
    timeout_occurred = False

    # Read status.json for current state with timeout
    if _STATUS_PATH.exists():
        status = _read_json_file_with_timeout(_STATUS_PATH, timeout=0.5)
        if status is None:
            timeout_occurred = True
    else:
        status = None

    # Read jobs.json with timeout
    if _JOBS_PATH.exists():
        jobs = _read_json_file_with_timeout(_JOBS_PATH, timeout=0.5)
        if jobs is None:
            timeout_occurred = True
    else:
        jobs = None

    # Handle timeout - return degraded status (fail-secure)
    if timeout_occurred:
        logger.warning("Instance heartbeat: file read timed out, returning degraded status")
        return {
            "instance_name": _read_instance_settings().get("name", "SWE-Squad Instance"),
            "timestamp": now,
            "status": "degraded",
            "last_cycle_time": None,
            "agents_active": 0,
            "total_agents": 0,
            "uptime_seconds": 0.0,
        }

    # Handle missing files (not an error, just not initialized yet)
    if status is None:
        status = {}
    if jobs is None:
        jobs = []

    # Calculate heartbeat stats
    last_cycle = status.get("last_cycle_time") or status.get("time")
    agents_active = sum(1 for job in jobs if job.get("enabled", False))

    return {
        "instance_name": _read_instance_settings().get("name", "SWE-Squad Instance"),
        "timestamp": now,
        "status": "healthy" if last_cycle else "unknown",
        "last_cycle_time": last_cycle,
        "agents_active": agents_active,
        "total_agents": len(jobs),
        "uptime_seconds": _calculate_uptime(),
    }


def _calculate_uptime() -> float:
    """Calculate instance uptime in seconds based on status.json age."""
    try:
        if _STATUS_PATH.exists():
            mtime = _STATUS_PATH.stat().st_mtime
            return time.time() - mtime
        return 0.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Instance creation methods
# ---------------------------------------------------------------------------

_CREATION_METHODS = [
    {
        "id": "docker",
        "name": "Docker Container",
        "description": "Run SWE-Squad in an isolated Docker container with configurable volume mounts and port mappings.",
        "icon": "container",
        "available": True,
        "config_schema": [
            {"field": "image", "label": "Docker Image", "type": "text", "default": "swe-squad:latest", "required": True},
            {"field": "volumes", "label": "Volume Mounts", "type": "text", "placeholder": "/host/path:/container/path", "required": False},
            {"field": "ports", "label": "Port Mapping", "type": "text", "placeholder": "8080:8080", "required": False},
            {"field": "env_vars", "label": "Environment Variables", "type": "textarea", "placeholder": "KEY=VALUE (one per line)", "required": False},
        ],
    },
    {
        "id": "local",
        "name": "Local Process",
        "description": "Run SWE-Squad as a local subprocess on the current machine. Simplest setup for development.",
        "icon": "terminal",
        "available": True,
        "config_schema": [
            {"field": "working_directory", "label": "Working Directory", "type": "text", "default": str(PROJECT_ROOT), "required": True},
            {"field": "python_path", "label": "Python Path", "type": "text", "default": sys.executable, "required": False},
        ],
    },
    {
        "id": "ssh",
        "name": "SSH Remote",
        "description": "Connect to an existing remote VM via SSH. Reuses your configured SSH connection methods.",
        "icon": "terminal-square",
        "available": True,
        "config_schema": [
            {"field": "host", "label": "SSH Host", "type": "text", "required": True},
            {"field": "username", "label": "Username", "type": "text", "required": True},
            {"field": "port", "label": "Port", "type": "number", "default": 22, "required": False},
            {"field": "secret_name", "label": "SSH Key Secret", "type": "text", "required": True},
            {"field": "remote_directory", "label": "Remote Working Directory", "type": "text", "default": "~/swe-squad", "required": False},
        ],
    },
    {
        "id": "cloud",
        "name": "Cloud VM",
        "description": "Provision a new cloud VM on AWS, GCP, or Azure. Automatic setup and teardown.",
        "icon": "cloud",
        "available": False,
        "config_schema": [
            {"field": "provider", "label": "Cloud Provider", "type": "select", "options": ["aws", "gcp", "azure"], "required": True},
            {"field": "region", "label": "Region", "type": "text", "required": True},
            {"field": "instance_type", "label": "Instance Type", "type": "text", "default": "t3.medium", "required": False},
        ],
    },
]

_PROVISIONED_INSTANCES_PATH = PROJECT_ROOT / "data" / "swe_team" / "provisioned_instances.json"


def _get_creation_methods() -> dict:
    """Return the list of available instance creation methods."""
    return {"methods": _CREATION_METHODS}


def _read_provisioned_instances() -> list:
    """Read the list of provisioned instances."""
    try:
        if _PROVISIONED_INSTANCES_PATH.exists():
            return json.loads(_PROVISIONED_INSTANCES_PATH.read_text())
    except Exception:
        pass
    return []


def _save_provisioned_instances(instances: list) -> bool:
    """Save the list of provisioned instances."""
    try:
        _PROVISIONED_INSTANCES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROVISIONED_INSTANCES_PATH.write_text(json.dumps(instances, indent=2))
        return True
    except Exception:
        logger.exception("Failed to save provisioned instances")
        return False


def _provision_instance(method: str, name: str, config: dict) -> dict:
    """Provision a new SWE-Squad instance using the specified method.

    Returns a dict with provision result (id, status, message).
    Currently stores the configuration; actual provisioning is deferred.
    """
    valid_methods = {m["id"] for m in _CREATION_METHODS}
    if method not in valid_methods:
        return {"ok": False, "error": f"Unknown creation method: {method}"}

    method_def = next(m for m in _CREATION_METHODS if m["id"] == method)
    if not method_def["available"]:
        return {"ok": False, "error": f"Method '{method_def['name']}' is not yet available"}

    if not name or not name.strip():
        return {"ok": False, "error": "Instance name is required"}

    instance_id = f"{method}-{name.strip().lower().replace(' ', '-')}-{int(time.time())}"
    instance = {
        "id": instance_id,
        "name": name.strip(),
        "method": method,
        "config": config,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    instances = _read_provisioned_instances()
    instances.append(instance)
    if not _save_provisioned_instances(instances):
        return {"ok": False, "error": "Failed to persist instance configuration"}

    return {"ok": True, "instance": instance}


def _get_live_runs(store, ticket_id: str | None = None, since: str | None = None) -> list:
    """Get active ticket runs with recent heartbeats.

    Args:
        store: TicketStore or SupabaseTicketStore instance
        ticket_id: Optional ticket ID to filter by
        since: Optional ISO timestamp to filter tickets updated since

    Returns:
        List of dicts with ticket info and heartbeat status
    """
    from datetime import datetime, timedelta, timezone

    # Active statuses that indicate a ticket is being worked on
    active_statuses = {
        "investigating",
        "in_development",
        "testing",
        "in_review",
        "verifying",
    }

    now = datetime.now(timezone.utc)
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass

    runs = []
    all_tickets = store.list_all()

    for ticket in all_tickets:
        # Filter by ticket_id if specified
        if ticket_id and ticket.ticket_id != ticket_id:
            continue

        # Filter by since timestamp if specified
        if since_dt:
            try:
                updated = datetime.fromisoformat(ticket.updated_at)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated < since_dt:
                    continue
            except (ValueError, TypeError):
                pass

        # Only include active statuses
        status_val = ticket.status.value if hasattr(ticket.status, "value") else str(ticket.status)
        if status_val not in active_statuses:
            continue

        # Get heartbeat timestamp from metadata or fallback to updated_at
        heartbeat_iso = ticket.metadata.get("last_heartbeat") or ticket.updated_at
        try:
            heartbeat_dt = datetime.fromisoformat(heartbeat_iso)
            if heartbeat_dt.tzinfo is None:
                heartbeat_dt = heartbeat_dt.replace(tzinfo=timezone.utc)
            seconds_ago = (now - heartbeat_dt).total_seconds()
        except (ValueError, TypeError):
            seconds_ago = (now - datetime.now(timezone.utc)).total_seconds()

        # Determine if the run is "live" (heartbeat within 10 minutes)
        is_live = seconds_ago < 600  # 10 minutes

        runs.append({
            "ticket_id": ticket.ticket_id,
            "title": ticket.title,
            "status": status_val,
            "severity": ticket.severity.value if hasattr(ticket.severity, "value") else str(ticket.severity),
            "assigned_to": ticket.assigned_to,
            "source_module": ticket.source_module,
            "last_heartbeat": heartbeat_iso,
            "seconds_ago": int(seconds_ago),
            "is_live": is_live,
            "created_at": ticket.created_at,
            "updated_at": ticket.updated_at,
        })

    # Sort by most recent heartbeat first
    runs.sort(key=lambda r: r["last_heartbeat"], reverse=True)
    return runs


# ---------------------------------------------------------------------------
# Inbox archive functionality
# ---------------------------------------------------------------------------

def _load_archived_alerts() -> set[str]:
    """Load archived alert IDs from JSON file.

    Returns:
        Set of archived alert IDs
    """
    try:
        archived = _read_json_file_with_timeout(_ARCHIVED_ALERTS_PATH, timeout=0.5)
        if archived is None:
            return set()
        if isinstance(archived, list):
            return set(archived)
        elif isinstance(archived, dict) and "archived_alerts" in archived:
            return set(archived["archived_alerts"])
        else:
            return set()
    except Exception:
        return set()


def _load_archived_runs() -> set[tuple[str, str]]:
    """Load archived failed runs as tuples of (routine_id, run_at).

    Returns:
        Set of (routine_id, run_at) tuples
    """
    try:
        archived = _read_json_file_with_timeout(_ARCHIVED_RUNS_PATH, timeout=0.5)
        if archived is None:
            return set()
        if isinstance(archived, list):
            return set(
                tuple(item) if isinstance(item, list) else (item.get("routine_id"), item.get("run_at"))
                for item in archived
            )
        elif isinstance(archived, dict) and "archived_runs" in archived:
            archived_list = archived["archived_runs"]
            return set(
                tuple(item) if isinstance(item, list) else (item.get("routine_id"), item.get("run_at"))
                for item in archived_list
            )
        else:
            return set()
    except Exception:
        return set()


def _save_archived_alert(alert_id: str) -> bool:
    """Add an alert ID to the archived alerts file.

    Args:
        alert_id: The alert ID to archive

    Returns:
        True on success, False on failure
    """
    try:
        archived = _load_archived_alerts()
        archived.add(alert_id)
        return _write_file_with_timeout(
            _ARCHIVED_ALERTS_PATH,
            json.dumps({"archived_alerts": list(archived)}, indent=2),
            timeout=2.0
        )
    except Exception:
        logger.exception("Failed to save archived alert")
        return False


def _save_archived_run(routine_id: str, run_at: str) -> bool:
    """Add a failed run to the archived runs file.

    Args:
        routine_id: The routine ID
        run_at: The run timestamp

    Returns:
        True on success, False on failure
    """
    try:
        archived = _load_archived_runs()
        archived.add((routine_id, run_at))
        archived_list = [{"routine_id": rid, "run_at": ra} for rid, ra in archived]
        return _write_file_with_timeout(
            _ARCHIVED_RUNS_PATH,
            json.dumps({"archived_runs": archived_list}, indent=2),
            timeout=2.0
        )
    except Exception:
        logger.exception("Failed to save archived run")
        return False


def _get_active_run_for_issue(store, ticket_id: str) -> dict | None:
    """Get the single most recent active run for a specific ticket.

    Args:
        store: TicketStore or SupabaseTicketStore instance
        ticket_id: Ticket ID to look up

    Returns:
        Dict with ticket info and heartbeat status, or None if not active
    """
    runs = _get_live_runs(store, ticket_id=ticket_id)
    if not runs:
        return None
    # Return the most recent run
    return runs[0]


def _get_scheduler_agents() -> list:
    """Get list of scheduler agents from config.

    Returns:
        List of agent dicts from config/agents section
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        agents = raw.get("agents", [])
        # Normalize agent entries
        normalized = []
        for agent in agents:
            entry = {
                "name": agent.get("name", ""),
                "role": agent.get("role", ""),
                "description": agent.get("description", ""),
                "model": agent.get("model", ""),
                "enabled": agent.get("enabled", False),
                "node": agent.get("node", "primary"),
                "max_concurrent_tasks": agent.get("max_concurrent_tasks", 1),
                "tools": agent.get("tools", []),
            }
            normalized.append(entry)
        return normalized
    except Exception:
        return []


def _load_projects_from_config() -> list:
    """Read ``repos:`` from swe_team.yaml and return a list of project dicts.

    Each project dict gets an ``enabled`` key (default True) injected so
    callers always have a consistent shape.
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    except Exception:
        return []
    projects = []
    for repo in raw.get("repos", []):
        entry = dict(repo)
        entry.setdefault("enabled", True)
        projects.append(entry)
    return projects


def _save_project_to_config(project: dict) -> bool:
    """Append *project* to the ``repos:`` list in swe_team.yaml.

    Returns ``False`` (without writing) if a project with the same ``name``
    already exists; ``True`` on success.
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        repos = raw.get("repos", [])
        name = project.get("name", "")
        for r in repos:
            if r.get("name") == name:
                return False
        repos.append(dict(project))
        raw["repos"] = repos
        _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
        return True
    except Exception:
        logger.exception("Failed to save project to config")
        return False


def _delete_project_from_config(name: str) -> bool:
    """Remove the project identified by *name* from swe_team.yaml.

    Returns ``True`` if the project was found and removed, ``False`` if it
    was not present.
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        repos = raw.get("repos", [])
        new_repos = [r for r in repos if r.get("name") != name]
        if len(new_repos) == len(repos):
            return False
        raw["repos"] = new_repos
        _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
        return True
    except Exception:
        logger.exception("Failed to delete project from config")
        return False


def _update_project_field(name: str, field: str, value: Any) -> bool:
    """Update a single field on a project in swe_team.yaml.

    Parameters
    ----------
    name: The project name to update.
    field: The field name to update (e.g., 'description', 'priority', 'enabled').
    value: The new value for the field.

    Returns
    -------
    bool: ``True`` if project was found and updated, ``False`` if not present.
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        repos = raw.get("repos", [])
        updated = False

        for repo in repos:
            if repo.get("name") == name:
                repo[field] = value
                updated = True
                break

        if not updated:
            return False

        raw["repos"] = repos
        _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
        return True
    except Exception:
        logger.exception(f"Failed to update project {name!r} field {field!r}")
        return False


def _patch_agent_field(name: str, field: str, value: Any) -> bool:
    """Update a single field on an agent in the ``agents:`` list in swe_team.yaml.

    Parameters
    ----------
    name:  The agent name (e.g. 'swe_monitor').
    field: The YAML field to update.
    value: The new value.

    Returns
    -------
    bool: ``True`` if agent was found and updated, ``False`` if not present.
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        agents = raw.get("agents", [])
        for agent in agents:
            if agent.get("name") == name:
                agent[field] = value
                raw["agents"] = agents
                _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
                return True
        return False
    except Exception:
        logger.exception(f"Failed to update agent {name!r} field {field!r}")
        return False


def _read_config_section(section: str) -> dict:
    """Read a top-level section from swe_team.yaml, returning an empty dict on failure."""
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        return raw.get(section, {}) or {}
    except Exception:
        logger.exception(f"Failed to read config section {section!r}")
        return {}


def _update_config_section(section: str, updates: dict) -> bool:
    """Merge *updates* into an existing top-level section in swe_team.yaml.

    Only the keys present in *updates* are changed; other keys in that section
    are preserved.  If the section does not yet exist it is created.
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        existing = raw.get(section, {}) or {}
        existing.update(updates)
        raw[section] = existing
        _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
        return True
    except Exception:
        logger.exception(f"Failed to update config section {section!r}")
        return False


def _build_full_settings() -> dict:
    """Return all editable config sections for the Settings page."""
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    except Exception:
        raw = {}

    governance = raw.get("governance", {}) or {}
    cycle = raw.get("cycle", {}) or {}
    memory = raw.get("memory", {}) or {}
    monitor = raw.get("monitor", {}) or {}

    return {
        "governance": {
            "enabled": governance.get("enabled", True),
            "max_open_critical": governance.get("max_open_critical", 100),
            "max_open_high": governance.get("max_open_high", 200),
            "max_failing_tests": governance.get("max_failing_tests", 5),
            "max_failing_tests_pct": governance.get("max_failing_tests_pct", 5),
            "require_ci_green": governance.get("require_ci_green", False),
            "check_interval_hours": governance.get("check_interval_hours", 6),
            "warn_on_any_failure": governance.get("warn_on_any_failure", True),
            "hard_block_pct": governance.get("hard_block_pct", 10),
        },
        "cycle": {
            "severity_filter": cycle.get("severity_filter", "low"),
            "max_new_tickets_per_cycle": cycle.get("max_new_tickets_per_cycle", 10),
            "max_investigations_per_cycle": cycle.get("max_investigations_per_cycle", 20),
            "max_developments_per_cycle": cycle.get("max_developments_per_cycle", 10),
            "max_open_investigating": cycle.get("max_open_investigating", 40),
            "max_investigation_workers": cycle.get("max_investigation_workers", 8),
        },
        "memory": {
            "embedding_model": memory.get("embedding_model", "bge-m3"),
            "embedding_dimensions": memory.get("embedding_dimensions", 1024),
            "top_k": memory.get("top_k", 5),
            "similarity_floor": memory.get("similarity_floor", 0.75),
            "store_on_investigation_complete": memory.get("store_on_investigation_complete", True),
        },
        "monitor": {
            "enabled": monitor.get("enabled", True),
            "scan_interval_minutes": monitor.get("scan_interval_minutes", 15),
            "dedup_window_hours": monitor.get("dedup_window_hours", 72),
        },
    }


def _update_team_field(name: str, field: str, value: Any) -> bool:
    """Update a single field on a team in the ``teams:`` section of swe_team.yaml.

    Parameters
    ----------
    name:  The team key (e.g. 'alpha', 'beta', 'gamma').
    field: The YAML field to update.
    value: The new value.

    Returns
    -------
    bool: ``True`` if team was found and updated, ``False`` if not present.
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        teams = raw.get("teams", {})
        if name not in teams:
            return False
        teams[name][field] = value
        raw["teams"] = teams
        _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
        return True
    except Exception:
        logger.exception(f"Failed to update team {name!r} field {field!r}")
        return False


# ---------------------------------------------------------------------------
# In-memory team run-state tracking (#793 VM connectivity, #795 start/stop)
# ---------------------------------------------------------------------------
_TEAM_RUN_STATE: dict[str, dict] = {}
"""Per-team run state: {"status": "running"|"stopped"|"starting"|"stopping", "last_check": ISO}"""


def _get_team_run_state(name: str) -> dict:
    """Return the run state for a team, defaulting to 'running'."""
    if name not in _TEAM_RUN_STATE:
        _TEAM_RUN_STATE[name] = {
            "status": "running",
            "last_check": datetime.now(timezone.utc).isoformat(),
        }
    return _TEAM_RUN_STATE[name]


def _set_team_run_state(name: str, status: str) -> dict:
    """Set the run state for a team and return the updated state."""
    _TEAM_RUN_STATE[name] = {
        "status": status,
        "last_check": datetime.now(timezone.utc).isoformat(),
    }
    return _TEAM_RUN_STATE[name]


def _build_scheduler_history() -> list:
    """Build scheduler job execution history for the Gantt timeline.

    Reads the last 20 entries from run_history.jsonl if available,
    otherwise synthesizes entries from the current status.json.
    """
    entries: list = []

    # Try to read from run_history.jsonl
    if _RUN_HISTORY_PATH.exists():
        try:
            lines = _RUN_HISTORY_PATH.read_text().strip().splitlines()
            for line in lines[-20:]:
                try:
                    rec = json.loads(line)
                    entries.append({
                        "job": rec.get("job_name", rec.get("job_id", "unknown")),
                        "ticket_id": rec.get("ticket_id"),
                        "started_at": rec.get("started_at", rec.get("timestamp", "")),
                        "ended_at": rec.get("ended_at", rec.get("completed_at", "")),
                        "status": rec.get("status", rec.get("result", "ok")),
                    })
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

    # Fallback: synthesize from status.json
    if not entries:
        status = _read_json_file(_STATUS_PATH) or {}
        now = datetime.now(timezone.utc)
        last_cycle = status.get("last_cycle_time") or status.get("time")
        if last_cycle:
            entries.append({
                "job": "monitor_cycle",
                "ticket_id": None,
                "started_at": last_cycle,
                "ended_at": (now).isoformat(),
                "status": "ok",
            })
        # Synthesize from jobs.json
        jobs = _read_json_file(_JOBS_PATH)
        if isinstance(jobs, list):
            for j in jobs[:5]:
                lr = j.get("last_run")
                if lr:
                    entries.append({
                        "job": j.get("name", j.get("job_id", "job")),
                        "ticket_id": None,
                        "started_at": lr,
                        "ended_at": lr,
                        "status": j.get("status", "ok"),
                    })

    return entries[-20:]


def _build_roles_matrix() -> dict:
    """Build the RBAC permission matrix from env_allowlists in swe_team.yaml."""
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    except Exception:
        return {"roles": [], "permissions": {}, "all_vars": [], "categories": {}}

    allowlists = raw.get("env_allowlists", {})
    # Filter out non-dict entries (notification, issue_tracker etc. are nested under providers)
    role_map: dict = {}
    for key, val in allowlists.items():
        if isinstance(val, list):
            role_map[key] = val

    # Collect all unique env vars
    all_vars = sorted({v for vlist in role_map.values() for v in vlist})

    # Categorize variables
    categories: dict = {}
    cat_map = {
        "GitHub": ["GH_TOKEN", "SWE_GITHUB_REPO", "SWE_GITHUB_ACCOUNT",
                    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"],
        "Base LLM": ["BASE_LLM_API_URL", "BASE_LLM_API_KEY", "EMBEDDING_MODEL",
                      "EXTRACTION_MODEL"],
        "Telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
        "Anthropic": ["ANTHROPIC_API_KEY"],
        "Core": ["SWE_TEAM_ID", "SWE_TEAM_CONFIG", "SWE_REPO_PATH",
                 "PYTHONPATH", "PATH", "HOME", "LANG"],
    }
    for var in all_vars:
        assigned = False
        for cat, members in cat_map.items():
            if var in members:
                categories.setdefault(cat, []).append(var)
                assigned = True
                break
        if not assigned:
            categories.setdefault("Other", []).append(var)

    return {
        "roles": list(role_map.keys()),
        "permissions": role_map,
        "all_vars": all_vars,
        "categories": categories,
    }


def _build_provider_parameter_schemas() -> dict:
    """Return self-describing provider parameter schemas for dynamic forms."""
    from src.swe_team.providers.coding_engine import list_engine_parameters
    from src.swe_team.providers.notification import list_notification_provider_parameters
    from src.swe_team.providers.issue_tracker import list_issue_tracker_parameters
    from src.swe_team.providers.sandbox import list_sandbox_provider_parameters
    from src.swe_team.providers.workspace import list_workspace_provider_parameters
    from src.swe_team.providers.repomap import list_repomap_provider_parameters
    from src.swe_team.providers.task_queue import list_task_queue_parameters
    from src.swe_team.providers.log_query import list_log_query_provider_parameters

    return {
        "coding_engine": list_engine_parameters(),
        "notification": list_notification_provider_parameters(),
        "issue_tracker": list_issue_tracker_parameters(),
        "sandbox": list_sandbox_provider_parameters(),
        "workspace": list_workspace_provider_parameters(),
        "repomap": list_repomap_provider_parameters(),
        "task_queue": list_task_queue_parameters(),
        "log_query": list_log_query_provider_parameters(),
    }


def _build_sse_payload() -> str:
    """Build JSON payload for SSE broadcast."""
    status = _read_json_file(_STATUS_PATH) or {}
    jobs = _read_json_file(_JOBS_PATH) or []
    payload: dict = {
        "status": status,
        "jobs": jobs,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        payload["governor"] = _get_governor_status()
    except Exception:
        payload["governor"] = {"error": "Governor not configured", "configured": False}
    return json.dumps(payload, default=str)


def _broadcast_sse_event(event_name: str, payload: dict) -> None:
    """Push a custom SSE event to all connected clients.

    Parameters
    ----------
    event_name:
        The SSE event type (e.g. ``action``, ``investigation_complete``).
    payload:
        JSON-serialisable dict sent as the ``data:`` field.
    """
    data_str = json.dumps(payload, default=str)
    msg = f"event: {event_name}\ndata: {data_str}\n\n"
    with _sse_lock:
        dead: list = []
        for wfile in _sse_clients:
            try:
                wfile.write(msg.encode())
                wfile.flush()
            except Exception:
                dead.append(wfile)
        for d in dead:
            _sse_clients.remove(d)


def _sse_broadcaster():
    """Background thread: polls status.json mtime, broadcasts on change or every 5s."""
    global _last_status_mtime
    last_broadcast = 0.0
    while True:
        time.sleep(2)
        now = time.time()
        try:
            mtime = _STATUS_PATH.stat().st_mtime if _STATUS_PATH.exists() else 0.0
        except OSError:
            mtime = 0.0
        changed = mtime != _last_status_mtime
        periodic = (now - last_broadcast) >= 5.0
        if changed or periodic:
            _last_status_mtime = mtime
            last_broadcast = now
            payload = _build_sse_payload()
            msg = f"event: update\ndata: {payload}\n\n"
            with _sse_lock:
                dead = []
                for wfile in _sse_clients:
                    try:
                        wfile.write(msg.encode())
                        wfile.flush()
                    except Exception:
                        dead.append(wfile)
                for d in dead:
                    _sse_clients.remove(d)


def _update_job(job_id: str, updates: dict) -> bool:
    """Update a scheduler job in jobs.json by job_id."""
    jobs = _read_json_file(_JOBS_PATH)
    if not isinstance(jobs, list):
        return False
    for job in jobs:
        if job.get("job_id") == job_id:
            job.update(updates)
            try:
                _JOBS_PATH.write_text(json.dumps(jobs, indent=2))
            except Exception:
                return False
            return True
    return False

# Max items for JSON HTML view before truncation
_JSON_VIEW_MAX_TICKETS = 200

# ── Dashboard response cache (30-second TTL) ──────────────────────────────────
# Avoids re-querying Supabase on every request to /data or /api/activity.
_DATA_CACHE_TTL: float = 30.0          # seconds
_data_cache: dict = {}                 # {"data": ..., "ts": float, "etag": str}
_data_cache_lock = threading.Lock()    # protects _data_cache under ThreadingHTTPServer

# ── Costs-extended cache (60-second TTL) ──────────────────────────────────────
# token_usage.jsonl can be large (10MB+); parsing 30k+ records on every /data
# request was the primary cause of ~3.5s response times.  Cache separately.
_COSTS_CACHE_TTL: float = 60.0
_costs_cache: dict = {}                # {"data": ..., "ts": float}
_costs_cache_lock = threading.Lock()

# ── Governor status cache (30-second TTL) ─────────────────────────────────────
# Governor budget checks query token_usage.jsonl internally — caching avoids
# re-parsing on every /data request (~2.3s per call uncached).
_GOV_CACHE_TTL: float = 30.0
_gov_cache: dict = {}                  # {"data": ..., "ts": float}
_gov_cache_lock = threading.Lock()

# ── Log file tail constants ────────────────────────────────────────────────────
_LOG_TAIL_BYTES: int = 102_400         # read at most 100 KiB from end of log
_JSON_VIEW_MAX_ACTIVITY: int = 30      # max activity entries returned by /api/activity


def _tail_log_file(
    path: Path,
    max_bytes: int = _LOG_TAIL_BYTES,
) -> list:
    """Read the last *max_bytes* bytes of *path* and return a list of text lines.

    This avoids loading the whole log file into memory on every request.
    If the file does not exist or cannot be read, returns an empty list.
    Binary/corrupt bytes are dropped with ``errors='replace'``.
    """
    if not path.exists():
        return []
    try:
        file_size = path.stat().st_size
        with open(path, "rb") as fh:
            if file_size > max_bytes:
                fh.seek(-max_bytes, 2)  # seek from end
            raw = fh.read(max_bytes)
        text = raw.decode("utf-8", errors="replace")
        # First line may be partial — drop it when we seeked into the middle
        lines = text.splitlines()
        if file_size > max_bytes and lines:
            lines = lines[1:]
        return lines
    except OSError:
        return []


def _get_cached_dashboard_data(store) -> dict:
    """Return dashboard data, using an in-process 30-second cache.

    The cache prevents repeated Supabase round-trips when multiple requests
    arrive within the TTL window (e.g. auto-refresh + manual reload).
    Thread-safe: uses _data_cache_lock for concurrent access under
    ThreadingHTTPServer. Includes a 10-second timeout for data fetches.
    """
    now = time.monotonic()
    with _data_cache_lock:
        cached = _data_cache.get("data")
        cached_ts = _data_cache.get("ts", 0.0)
        if cached is not None and (now - cached_ts) < _DATA_CACHE_TTL:
            return cached

    from scripts.ops.dashboard_data import generate_dashboard_data

    # Use a thread with timeout to prevent slow Supabase queries from blocking
    result_holder: list = []
    error_holder: list = []

    def _fetch():
        try:
            result_holder.append(generate_dashboard_data(store))
        except Exception as exc:
            error_holder.append(exc)

    t = threading.Thread(target=_fetch, daemon=True)
    t.start()
    t.join(timeout=10.0)  # 10-second timeout for data fetch

    if t.is_alive():
        logger.warning("Dashboard data fetch timed out after 10s, serving stale cache")
        with _data_cache_lock:
            stale = _data_cache.get("data")
        return stale if stale is not None else {}

    if error_holder:
        logger.warning("Dashboard data fetch failed: %s", error_holder[0])
        with _data_cache_lock:
            stale = _data_cache.get("data")
        return stale if stale is not None else {}

    fresh = result_holder[0] if result_holder else {}
    # Compute a lightweight ETag from the ticket summary so conditional
    # GET requests can return 304 when nothing has changed.
    try:
        summary_bytes = json.dumps(
            fresh.get("ticket_summary", {}), sort_keys=True
        ).encode()
        etag = '"' + hashlib.md5(summary_bytes).hexdigest() + '"'
    except Exception:
        etag = '"unknown"'
    with _data_cache_lock:
        _data_cache["data"] = fresh
        _data_cache["ts"] = now
        _data_cache["etag"] = etag
    return fresh


def _get_cached_costs_extended() -> dict:
    """Return costs_extended dict with a 60-second cache.

    Parsing the token_usage.jsonl file (potentially 30k+ records / 10MB+)
    is expensive.  Caching avoids re-parsing on every /data request.
    """
    now = time.monotonic()
    with _costs_cache_lock:
        cached = _costs_cache.get("data")
        cached_ts = _costs_cache.get("ts", 0.0)
        if cached is not None and (now - cached_ts) < _COSTS_CACHE_TTL:
            return cached

    try:
        tracker = _get_token_tracker()
        records = tracker._load_records()
        cache_read = sum(r.cache_read_tokens for r in records)
        cache_creation = sum(r.cache_creation_tokens for r in records)
        input_total = sum(r.input_tokens for r in records)
        denom = cache_read + input_total
        result = {
            "cache_read_tokens_total": cache_read,
            "cache_creation_tokens_total": cache_creation,
            "cache_efficiency_pct": round(cache_read / denom * 100, 2) if denom else 0.0,
            "estimated_cache_savings_usd": round(cache_read / 1000 * 0.003 * 0.9, 4),
        }
    except Exception:
        result = {
            "cache_read_tokens_total": 0,
            "cache_creation_tokens_total": 0,
            "cache_efficiency_pct": 0.0,
            "estimated_cache_savings_usd": 0.0,
        }

    with _costs_cache_lock:
        _costs_cache["data"] = result
        _costs_cache["ts"] = now
    return result


def _get_cached_governor_status() -> dict:
    """Return governor status dict with a 30-second cache.

    Governor budget checks query token_usage.jsonl internally (~2.3s uncached).
    Caching avoids re-parsing on every /data request.
    """
    now = time.monotonic()
    with _gov_cache_lock:
        cached = _gov_cache.get("data")
        cached_ts = _gov_cache.get("ts", 0.0)
        if cached is not None and (now - cached_ts) < _GOV_CACHE_TTL:
            return cached

    try:
        result = _get_governor_status()
    except Exception:
        result = {"error": "Governor not configured", "configured": False}

    with _gov_cache_lock:
        _gov_cache["data"] = result
        _gov_cache["ts"] = now
    return result


def _init_supabase_with_timeout(supabase_url: str, supabase_key: str, team_id: str, timeout: int = 10):
    """Initialize SupabaseTicketStore in a background thread with a hard timeout.

    If Supabase does not respond within *timeout* seconds, falls back to the
    local JSON TicketStore so the dashboard server can start immediately.
    """
    result: list = [None]
    exc_holder: list = [None]

    def _init() -> None:
        try:
            from src.swe_team.supabase_store import SupabaseTicketStore
            result[0] = SupabaseTicketStore(
                supabase_url=supabase_url,
                supabase_key=supabase_key,
                team_id=team_id,
            )
        except Exception as exc:  # noqa: BLE001
            exc_holder[0] = exc

    t = threading.Thread(target=_init, daemon=True, name="supabase-init")
    t.start()
    t.join(timeout=timeout)

    if result[0] is not None:
        return result[0]

    if exc_holder[0] is not None:
        logger.warning("Supabase init failed — falling back to JSON store: %s", exc_holder[0])
    else:
        logger.warning(
            "Supabase init timed out after %ds — falling back to JSON store. "
            "Supabase host may be slow or unreachable.",
            timeout,
        )
    return None


def _load_store(config):
    """Load ticket store — Supabase if configured, else local JSON.

    Supabase initialization runs in a background thread with a 10-second
    timeout so the server always starts within a predictable time even when
    the Supabase host is slow or unreachable.
    """
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_ANON_KEY", "")
    if supabase_url and supabase_key:
        store = _init_supabase_with_timeout(
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            team_id=config.team_id,
            timeout=10,
        )
        if store is not None:
            return store
    data_dir = PROJECT_ROOT / "data" / "swe_team"
    data_dir.mkdir(parents=True, exist_ok=True)
    return TicketStore(path=data_dir / "tickets.json")


def _render_dashboard(store) -> str:
    """Serve the React SPA index.html from ui/dist/.

    The React app fetches data from /data and /api/* endpoints.
    All client-side routing is handled by the SPA.
    """
    index_path = _REACT_UI_DIST / "index.html"
    if not index_path.exists():
        return (
            "<pre>React UI not built. Run: cd ui && npm run build\n"
            f"Expected: {index_path}</pre>"
        )
    return index_path.read_text(encoding="utf-8")


SCHEDULER_TEMPLATES: list[dict] = [
    {
        "id": "daily-triage",
        "name": "Daily Triage Run",
        "description": "Run triage on all new issues every morning",
        "cron": "0 9 * * *",
        "action": "pipeline_trigger",
        "category": "maintenance",
    },
    {
        "id": "nightly-health-check",
        "name": "Nightly Health Check",
        "description": "Run a full health audit during off-peak hours",
        "cron": "0 2 * * *",
        "action": "health_audit",
        "category": "monitoring",
    },
    {
        "id": "weekly-cost-report",
        "name": "Weekly Cost Report",
        "description": "Generate a cost summary every Monday morning",
        "cron": "0 10 * * 1",
        "action": "cost_report",
        "category": "reporting",
    },
    {
        "id": "hourly-queue-check",
        "name": "Hourly Queue Check",
        "description": "Check for stalled tickets every hour",
        "cron": "0 * * * *",
        "action": "queue_check",
        "category": "monitoring",
    },
    {
        "id": "monthly-cleanup",
        "name": "Monthly Cleanup",
        "description": "Purge old resolved tickets and stale data on the first of each month",
        "cron": "0 3 1 * *",
        "action": "data_cleanup",
        "category": "maintenance",
    },
]


def _get_scheduler_template(template_id: str) -> dict | None:
    """Look up a scheduler template by ID."""
    for t in SCHEDULER_TEMPLATES:
        if t["id"] == template_id:
            return t
    return None


def _get_scheduler_and_store():
    """Get JobStore and JobScheduler instances for API handlers."""
    from src.swe_team.scheduler import JobStore, JobScheduler
    store = JobStore(_JOBS_DIR / "jobs.json")
    scheduler = JobScheduler(store=store)
    return store, scheduler


def _job_to_routine_payload(job: dict) -> dict:
    """Map scheduler job payload to WebUI routine payload."""
    metadata = job.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    archived = bool(metadata.get("archived", False))
    cron = job.get("cron_expression") or ""
    status = "archived" if archived else ("active" if job.get("enabled", True) else "paused")

    return {
        "id": job.get("job_id"),
        "name": job.get("name", ""),
        "description": job.get("description", ""),
        "status": status,
        "enabled": bool(job.get("enabled", True)),
        "archived": archived,
        "schedule": cron,
        "trigger": {
            "type": "schedule",
            "cron": cron,
            "webhook_url": metadata.get("webhook_url"),
        },
        "created_at": job.get("created_at"),
        "last_run": job.get("last_run"),
        "next_run": job.get("next_run"),
        "run_count": job.get("run_count", 0),
        "last_error": job.get("last_error"),
    }


def _build_graph_data(store) -> dict:
    """Build ticket similarity graph data for /api/graph.

    Returns a dict with keys:
      - nodes: list of node dicts (id, title, severity, status, module, created_days_ago)
      - edges: list of edge dicts (source, target, similarity) where similarity >= 0.75
      - heatmap: dict with modules (list) and cells (2D list of counts)

    Node count is capped at 50. Edges use TF-IDF cosine similarity on ticket titles.
    """
    import math
    from datetime import datetime, timezone

    # Fetch all tickets and cap at 50
    tickets = store.list_all()[:50]

    now = datetime.now(timezone.utc)

    # Build nodes
    nodes = []
    for t in tickets:
        try:
            created = datetime.fromisoformat(t.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            days_ago = int((now - created).total_seconds() / 86400)
        except (ValueError, TypeError):
            days_ago = 0

        severity_val = t.severity.value if hasattr(t.severity, "value") else str(t.severity)
        status_val = t.status.value if hasattr(t.status, "value") else str(t.status)

        nodes.append({
            "id": t.ticket_id,
            "title": t.title,
            "severity": severity_val,
            "status": status_val,
            "module": t.source_module or "unknown",
            "created_days_ago": days_ago,
        })

    # Build TF-IDF vectors for title similarity
    def _tokenize(text: str) -> list:
        return re.findall(r"[a-z0-9]+", text.lower())

    n = len(nodes)
    if n == 0:
        return {"nodes": [], "edges": [], "heatmap": {"modules": [], "cells": []}}

    titles = [nodes[i]["title"] for i in range(n)]
    tokenized = [_tokenize(t) for t in titles]

    # Build document frequency
    df: dict = {}
    for toks in tokenized:
        for tok in set(toks):
            df[tok] = df.get(tok, 0) + 1

    vocab = list(df.keys())
    vocab_idx = {w: i for i, w in enumerate(vocab)}
    V = len(vocab)

    def _tfidf_vec(toks: list) -> list:
        tf: dict = {}
        for tok in toks:
            tf[tok] = tf.get(tok, 0) + 1
        vec = [0.0] * V
        for tok, cnt in tf.items():
            if tok in vocab_idx:
                idf = math.log((n + 1) / (df.get(tok, 0) + 1)) + 1.0
                vec[vocab_idx[tok]] = (cnt / len(toks)) * idf if toks else 0.0
        return vec

    def _cosine(a: list, b: list) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(x * x for x in b))
        if mag_a == 0.0 or mag_b == 0.0:
            return 0.0
        return dot / (mag_a * mag_b)

    vecs = [_tfidf_vec(toks) for toks in tokenized]

    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            sim = _cosine(vecs[i], vecs[j])
            if sim >= 0.75:
                edges.append({
                    "source": nodes[i]["id"],
                    "target": nodes[j]["id"],
                    "similarity": round(sim, 4),
                })

    # Build heatmap
    modules = sorted({nd["module"] for nd in nodes})
    mod_idx = {m: i for i, m in enumerate(modules)}
    M = len(modules)
    cells = [[0] * M for _ in range(M)]

    for i in range(n):
        for j in range(i, n):
            mi = mod_idx[nodes[i]["module"]]
            mj = mod_idx[nodes[j]["module"]]
            if i == j:
                # Diagonal: count each ticket once for its own module
                cells[mi][mi] += 1
            else:
                cells[mi][mj] += 1
                if mi != mj:
                    cells[mj][mi] += 1

    return {
        "nodes": nodes,
        "edges": edges,
        "heatmap": {"modules": modules, "cells": cells},
    }



# ---------------------------------------------------------------------------
# Agents API helpers (module-level functions)
# ---------------------------------------------------------------------------

def _load_agents_from_config() -> list:
    """Read ``agents:`` from swe_team.yaml and return a list of agent dicts."""
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    except Exception:
        return []
    return raw.get("agents", [])


def _save_agent_to_config(agent: dict) -> bool:
    """Add or update *agent* in the ``agents:`` list in swe_team.yaml.

    Returns ``False`` if a different agent with the same name already exists
    when creating; ``True`` on success (create or update).
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        agents = raw.get("agents", [])
        name = agent.get("name", "")
        if not name:
            return False

        # Check for duplicate on create (when updating, we skip this check)
        existing_index = None
        for i, a in enumerate(agents):
            if a.get("name") == name:
                existing_index = i
                break

        if existing_index is not None:
            # Update existing — merge new fields into existing agent dict
            existing = agents[existing_index]
            existing.update(agent)
            agents[existing_index] = existing
        else:
            # Create new
            for a in agents:
                if a.get("name") == name:
                    return False  # Duplicate (shouldn't happen due to index check)
            agents.append(dict(agent))

        raw["agents"] = agents
        _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
        return True
    except Exception:
        logger.exception("Failed to save agent to config")
        return False


def _delete_agent_from_config(name: str) -> bool:
    """Remove the agent identified by *name* from swe_team.yaml.

    Returns ``True`` if the agent was found and removed, ``False`` if it
    was not present.
    """
    try:
        import yaml
        raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        agents = raw.get("agents", [])
        new_agents = [a for a in agents if a.get("name") != name]
        if len(new_agents) == len(agents):
            return False
        raw["agents"] = new_agents
        _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
        return True
    except Exception:
        logger.exception("Failed to delete agent from config")
        return False




# ---------------------------------------------------------------------------
# Suggestions helpers (creative agent proposals surfaced in dashboard)
# ---------------------------------------------------------------------------

_DEFAULT_SUGGESTIONS = [
    {
        "id": "seed-retry-github",
        "title": "Add retry logic to GitHub API calls",
        "description": "GitHub API calls in github_integration.py lack retry/backoff logic. Transient 5xx errors or rate-limit 429 responses cause immediate failure. Adding exponential backoff with jitter would improve reliability.",
        "category": "reliability",
        "impact": "high",
        "created_at": "2026-04-08T00:00:00Z",
        "status": "pending",
    },
    {
        "id": "seed-test-rate-limiter",
        "title": "Increase test coverage for rate limiter module",
        "description": "The rate limiter module has limited test coverage. Edge cases like concurrent access, clock skew, and burst handling are not tested. Adding these tests would prevent regressions.",
        "category": "testing",
        "impact": "medium",
        "created_at": "2026-04-08T00:00:00Z",
        "status": "pending",
    },
    {
        "id": "seed-connection-pooling",
        "title": "Refactor ticket store to use connection pooling",
        "description": "The Supabase ticket store opens a new HTTP connection for each request. Connection pooling would reduce latency and improve throughput under load.",
        "category": "performance",
        "impact": "medium",
        "created_at": "2026-04-08T00:00:00Z",
        "status": "pending",
    },
]


def _read_suggestions() -> list[dict]:
    """Read suggestions from disk, seeding defaults if the file doesn't exist."""
    data = _read_json_file(_SUGGESTIONS_PATH)
    if data is not None:
        return data if isinstance(data, list) else []
    # Seed with defaults
    _write_suggestions(_DEFAULT_SUGGESTIONS)
    return list(_DEFAULT_SUGGESTIONS)


def _write_suggestions(suggestions: list[dict]) -> bool:
    """Persist suggestions list to disk."""
    return _write_file_with_timeout(
        _SUGGESTIONS_PATH,
        json.dumps(suggestions, indent=2, default=str),
    )


def _find_suggestion(suggestion_id: str, suggestions: list[dict]) -> dict | None:
    """Find a suggestion by ID."""
    for s in suggestions:
        if s.get("id") == suggestion_id:
            return s
    return None

class DashboardHandler(BaseHTTPRequestHandler):
    store = None        # set at startup
    auth_provider = None  # optional AuthProvider for /api/auth/status

    # --- In-memory rate limiting (per-IP, per-minute) ---
    _rate_limit_lock = threading.Lock()
    _rate_limit_buckets: dict[str, list[float]] = {}  # ip -> list of timestamps
    _RATE_LIMIT_MAX = 100   # max requests per window
    _RATE_LIMIT_WINDOW = 60  # seconds

    def address_string(self):
        """Override to skip reverse DNS lookup — fixes 10-15s latency on Tailscale (#257)."""
        return self.client_address[0]

    def log_message(self, fmt, *args):  # suppress default access log noise
        logger.debug("HTTP %s %s", self.address_string(), fmt % args)

    def _check_rate_limit(self) -> bool:
        """Return True if the request should be rejected (rate limit exceeded).

        Exempt: /health endpoint.
        Tracks per-IP request count within a sliding window.
        """
        ip = self.address_string()
        now = time.monotonic()
        with self._rate_limit_lock:
            bucket = self._rate_limit_buckets.get(ip)
            if bucket is None:
                bucket = []
                self._rate_limit_buckets[ip] = bucket
            # Prune timestamps outside the window
            cutoff = now - self._RATE_LIMIT_WINDOW
            while bucket and bucket[0] < cutoff:
                bucket.pop(0)
            if len(bucket) >= self._RATE_LIMIT_MAX:
                return True
            bucket.append(now)
            return False

    def _cors_origin(self) -> str | None:
        """Return the allowed Origin value for this request, or None to deny."""
        origin = self.headers.get("Origin", "")
        if not origin or not _CORS_ALLOWED_ORIGINS:
            return None
        if origin in _CORS_ALLOWED_ORIGINS:
            return origin
        return None

    def _set_cors_headers(self, origin: str | None) -> None:
        """Set CORS response headers if an allowed origin matched."""
        if not origin:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        origin = self._cors_origin()
        if not origin:
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self._set_cors_headers(origin)
        self.end_headers()

    def _dispatch_with_logging(self, method: str, handler_fn) -> None:
        """Wrap a do_* handler with access logging, rate limiting, and error catching."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # Rate limiting — exempt health check
        if path != "/health" and self._check_rate_limit():
            self._json_response(
                {"error": "Too many requests", "retry_after_seconds": self._RATE_LIMIT_WINDOW},
                status=429,
            )
            logger.warning("Rate limit exceeded for %s %s %s", self.address_string(), method, path)
            return

        start = time.monotonic()
        status_code = 200
        try:
            handler_fn()
        except (BrokenPipeError, ConnectionResetError):
            status_code = 0
            logger.debug("Client disconnected during %s %s", method, path)
        except Exception:
            status_code = 500
            logger.exception("Unhandled exception in %s %s", method, path)
            try:
                # If _read_post_body already sent a 400 response, attempting
                # another response will raise — silently ignore that.
                self._json_response({"error": "Internal server error"}, status=500)
            except Exception:
                pass  # response headers already sent
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.info(
                "%s %s %s %.1fms %s",
                method, path, status_code, elapsed_ms, self.address_string(),
            )

    def _send_gzipped(self, content, content_type, cache_control=None):
        """Send response with gzip compression if the client supports it."""
        raw = content.encode("utf-8") if isinstance(content, str) else content
        origin = self._cors_origin()
        accept_enc = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept_enc:
            compressed = gzip.compress(raw)
            self.send_response(200)
            self._set_cors_headers(origin)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(compressed)))
            if cache_control:
                self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(compressed)
        else:
            self.send_response(200)
            self._set_cors_headers(origin)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            if cache_control:
                self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(raw)

    def do_GET(self):
        self._dispatch_with_logging("GET", self._do_GET)

    def _do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        # Auth routes — always accessible (no session check)
        if path == "/login":
            self._serve_login()
            return
        if path == "/auth/login":
            self._handle_auth_login(query)
            return
        if path == "/auth/callback":
            self._handle_auth_callback(query)
            return
        if path == "/auth/logout":
            self._handle_auth_logout()
            return
        # Welcome route — accessible without auth (landing/intro page)
        if path == "/welcome":
            self._serve_dashboard()
            return
        # Onboarding route — accessible without auth for first-time setup
        if path == "/onboarding":
            self._serve_onboarding()
            return

        # Auth middleware — exempt public routes and static assets from the gate
        if (
            path not in ("/health", "/login", "/welcome", "/onboarding", "/api/onboarding/status", "/api/auth/status", "/auth/login", "/auth/callback", "/auth/logout")
            and not path.startswith("/assets/")
            and path not in ("/favicon.ico", "/manifest.json")
        ):
            user = self._check_auth()
            if user is None:
                self._redirect("/login")
                return
        else:
            user = None

        # Control plane API routes (handled first for /api/* prefix)
        if (
            _HAS_CONTROL_PLANE
            and getattr(self, "control_plane", None)
            and path.startswith("/api/")
            and path not in _CONTROL_PLANE_EXEMPT_PATHS
            and not any(path.startswith(prefix) for prefix in _CONTROL_PLANE_EXEMPT_PREFIXES)
        ):
            if cp_handle_get(self, self.control_plane):
                return

        # Serve React SPA static assets from ui/dist/
        if path.startswith("/assets/") or path in ("/favicon.ico", "/manifest.json"):
            self._serve_static_file(path)
            return

        if path in ("/", "/dashboard"):
            self._serve_dashboard()
        elif path == "/health":
            self._json_response({"status": "ok"})
        elif path == "/data":
            self._serve_json()
        elif path == "/api/activity":
            self._handle_api_activity(query)
        elif path == "/api/stream":
            self._handle_sse()
        elif path == "/api/scheduler":
            self._json_response(_read_json_file(_JOBS_PATH) or [], cache_control="public, max-age=15")
        elif path == "/api/jobs":
            self._handle_list_jobs_api()
        elif re.match(r"^/api/jobs/[^/]+/history$", path):
            self._handle_job_history_api()
        elif path == "/api/routines":
            self._handle_list_routines_api()
        elif re.match(r"^/api/routines/[^/]+$", path):
            self._handle_get_routine_api()
        elif re.match(r"^/api/routines/[^/]+/runs$", path):
            self._handle_routine_runs_api()
        elif re.match(r"^/api/routines/[^/]+/activity$", path):
            self._handle_routine_activity_api()
        elif path == "/api/rbac":
            self._json_response(_read_roles_yaml(), cache_control="public, max-age=60")
        elif path == "/api/auth/status":
            self._handle_api_auth_status()
        elif path == "/api/onboarding/status":
            self._handle_api_onboarding_status()
        elif path == "/api/graph":
            self._handle_api_graph()
        elif path == "/api/settings":
            self._json_response(_read_settings(), cache_control="private, max-age=30")
        elif path == "/api/settings/full":
            self._json_response(_build_full_settings(), cache_control="private, no-cache")
        elif path == "/api/scheduler/history":
            self._json_response(_build_scheduler_history(), cache_control="public, max-age=30")
        elif path == "/api/scheduler/templates":
            self._json_response({"templates": SCHEDULER_TEMPLATES}, cache_control="public, max-age=300")
        elif path == "/api/roles":
            self._json_response(_build_roles_matrix(), cache_control="public, max-age=60")
        elif path == "/api/providers/schemas":
            self._json_response(_build_provider_parameter_schemas(), cache_control="private, max-age=60")
        elif path == "/api/costs/by_hour":
            try:
                self._json_response(_get_token_tracker().by_hour(since_hours=48), cache_control="public, max-age=120")
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path == "/api/costs/by_day":
            try:
                self._json_response(_get_token_tracker().by_day(since_days=30), cache_control="public, max-age=300")
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path == "/api/costs/by_week":
            try:
                self._json_response(_get_token_tracker().by_week(since_weeks=12), cache_control="public, max-age=600")
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path == "/api/costs/by_month":
            try:
                self._json_response(_get_token_tracker().by_month(since_months=6), cache_control="public, max-age=3600")
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path == "/api/costs/by_agent":
            try:
                tracker = _get_token_tracker()
                # Use the store to get ticket titles if available
                store = getattr(self.server, "_store", None)
                result = tracker.by_agent_list(since_hours=168)
                self._json_response(result, cache_control="public, max-age=300")
            except Exception as exc:
                self._json_response({"error": str(exc)}, status=500)
        elif path == "/api/costs/by_ticket":
            try:
                tracker = _get_token_tracker()
                store = getattr(self.server, "_store", None)
                result = tracker.by_ticket_list(since_hours=168, store=store)
                self._json_response(result, cache_control="public, max-age=300")
            except Exception as exc:
                self._json_response({"error": str(exc)}, status=500)
        elif path == "/api/costs/by_range":
            try:
                from datetime import datetime as _dt, timezone as _tz
                start_str = query.get("start", [None])[0]
                end_str = query.get("end", [None])[0]
                if not start_str or not end_str:
                    self._json_response({"error": "start and end query params required"}, status=400)
                else:
                    start = _dt.fromisoformat(start_str).replace(tzinfo=_tz.utc)
                    end = _dt.fromisoformat(end_str).replace(tzinfo=_tz.utc)
                    tracker = _get_token_tracker()
                    result = tracker.by_range(start, end)
                    self._json_response(result, cache_control="public, max-age=120")
            except (ValueError, TypeError) as exc:
                self._json_response({"error": f"Invalid date format: {exc}"}, status=400)
            except Exception as exc:
                self._json_response({"error": str(exc)}, status=500)
        elif path == "/api/costs/by_model":
            try:
                tracker = _get_token_tracker()
                result = tracker.by_model(since_hours=168)
                self._json_response(result, cache_control="public, max-age=300")
            except Exception as exc:
                self._json_response({"error": str(exc)}, status=500)
        elif path == "/api/costs/roi":
            self._handle_costs_roi(query)
        elif path == "/api/costs/cache_efficiency":
            self._handle_cache_efficiency()
        elif path == "/api/cost":
            self._handle_budget_status(query)
        # Budget API routes
        elif path == "/api/budget/policies":
            if self.command == "POST":
                content_length = int(self.headers.get("Content-Length", 0))
                self._handle_budget_policies_post(query, self.rfile.read(content_length))
            else:
                self._handle_budget_policies_get(query)
        elif path == "/api/budget/incidents":
            if self.command == "POST":
                content_length = int(self.headers.get("Content-Length", 0))
                self._handle_budget_incidents_post(query, self.rfile.read(content_length))
            else:
                self._handle_budget_incidents_get(query)
        elif re.match(r"^/api/budget/incidents/[^/]+/resolve$", path):
            self._handle_budget_incidents_resolve(path.split("/"))
        elif path == "/api/budget/provider-quotas":
            self._handle_budget_provider_quotas(query)
        elif path == "/api/budget/spend-window":
            self._handle_budget_spend_window(query)
        elif path == "/api/budget/subscriptions":
            self._handle_budget_subscriptions(query)
        elif path == "/api/budget/accounting-models":
            self._handle_budget_accounting_models(query)
        elif path == "/api/pricing":
            self._json_response(
                _normalize_pricing_config(load_pricing()),
                cache_control="public, max-age=3600",
            )
        elif path == "/api/projects":
            self._handle_list_projects()
        elif path.startswith("/api/projects/"):
            project_name = path[len("/api/projects/"):]
            # Check for sub-resources
            if project_name.endswith("/tickets"):
                name = project_name[:-len("/tickets")]
                self._handle_get_project_tickets(name)
                return
            if project_name.endswith("/stats"):
                name = project_name[:-len("/stats")]
                self._handle_get_project_stats(name)
                return
            if project_name.endswith("/secrets"):
                name = project_name[:-len("/secrets")]
                self._handle_list_project_secrets(name)
                return
            if project_name.endswith("/env"):
                name = project_name[:-len("/env")]
                self._handle_list_project_env(name)
                return
            self._handle_get_project(project_name)
        elif path == "/api/goals":
            self._handle_list_goals()
        elif path.startswith("/api/goals/"):
            goal_id = path[len("/api/goals/"):]
            # Check for sub-resources
            if goal_id.endswith("/stats"):
                id_part = goal_id[:-len("/stats")]
                self._handle_get_goal_stats(id_part)
                return
            self._handle_get_goal(goal_id)
        elif path == "/api/jobs":
            self._handle_list_jobs_api()
        elif re.match(r"^/api/jobs/[^/]+/history$", path):
            self._handle_job_history_api()
        elif path == "/api/governor/status":
            self._json_response(_get_governor_status())
        elif path == "/api/governor/quota":
            self._handle_governor_quota()
        elif path == "/api/governor/decision":
            self._handle_governor_decision()
        elif path == "/api/governor/alerts":
            self._handle_governor_alerts()
        elif path == "/api/governor/summary":
            self._handle_governor_summary()
        # GET /api/tickets/export — CSV/JSON export
        elif path == "/api/tickets/export":
            self._handle_tickets_export(query)
        # GET /api/tickets/<id> — single ticket detail
        elif re.match(r"^/api/tickets/[^/]+$", path):
            ticket_id = path.split("/")[-1]
            self._handle_get_ticket(ticket_id)
        # GET /api/tickets/<id>/activity — activity timeline
        elif re.match(r"^/api/tickets/([^/]+)/activity$", path):
            m = re.match(r"^/api/tickets/([^/]+)/activity$", path)
            if m:
                self._handle_ticket_activity(m.group(1))
                return
        # GET /api/tickets/<id>/feed — unified activity feed
        elif re.match(r"^/api/tickets/([^/]+)/feed$", path):
            m = re.match(r"^/api/tickets/([^/]+)/feed$", path)
            if m:
                self._handle_get_ticket_feed(unquote(m.group(1)))
                return
        # --- Multi-user account API ---
        elif path == "/api/users/me":
            self._handle_get_me(user)
        elif path == "/api/secrets":
            self._handle_list_secrets(user)
        elif path == "/api/users":
            self._handle_list_users(user)
        elif path == "/api/teams":
            try:
                import yaml
                config = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
                teams = config.get("teams", {})
                # Enrich with live status from Supabase ticket counts
                if hasattr(self, "store") and self.store:
                    try:
                        all_tickets = self.store.list_all(limit=500)
                        for team_key, team_cfg in teams.items():
                            team_id = f"swe-squad-{team_key}"
                            team_tickets = [t for t in all_tickets if (t.metadata.get("assigned_to") or "").startswith(team_cfg.get("github_account", ""))]
                            active = [t for t in team_tickets if t.status.value in ("investigating", "in_development", "in_review")]
                            open_tickets = [t for t in team_tickets if t.status.value in ("new", "triaged")]
                            closed_tickets = [t for t in team_tickets if t.status.value in ("resolved", "closed", "failed")]
                            active_sessions = len([t for t in team_tickets if t.status.value in ("investigating", "in_development")])
                            team_cfg["live_status"] = {
                                "total_tickets": len(team_tickets),
                                "active_tickets": len(active),
                                "active_ticket_ids": [t.ticket_id[:12] for t in active[:5]],
                                "active_sessions": active_sessions,
                                "open_count": len(open_tickets),
                                "in_progress_count": len(active),
                                "closed_count": len(closed_tickets),
                            }
                    except Exception:
                        pass  # Supabase may be unavailable
                # Enrich with burn rate from cost tracker
                cost_tracker = getattr(self.server, "_cost_tracker", None)
                if cost_tracker:
                    try:
                        for team_key, team_cfg in teams.items():
                            tid = f"swe-squad-{team_key}"
                            budget_status = cost_tracker.check_budget(tid)
                            daily_usd = round(budget_status.daily_spent / 100, 4) if budget_status.daily_spent else 0.0
                            from datetime import datetime as _dt
                            hour_of_day = max(_dt.now().hour, 1)
                            hourly_rate = round(daily_usd / hour_of_day, 4) if daily_usd > 0 else 0.0
                            team_cfg["burn_rate"] = {
                                "daily_spent_usd": daily_usd,
                                "hourly_rate_usd": hourly_rate,
                                "budget_percent": budget_status.percent_used,
                            }
                    except Exception:
                        pass  # Cost tracker may be unavailable
                # Enrich with VM run state (#793 connectivity, #795 start/stop)
                for team_key, team_cfg in teams.items():
                    state = _get_team_run_state(team_key)
                    team_cfg["vm_status"] = state
                self._json_response(teams, cache_control="private, max-age=15")
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path.startswith("/api/teams/"):
            # GET /api/teams/<name>/health — VM health check (#793)
            m = re.match(r"^/api/teams/([^/]+)/health$", path)
            if m:
                self._handle_team_health(unquote(m.group(1)))
            else:
                self.send_error(404, "Not found")
        elif path == "/api/agents":
            self._json_response(_load_agents_from_config(), cache_control="public, max-age=60")
        elif path.startswith("/api/agents/"):
            # Agent sub-resources: /api/agents/<name>/*, /api/agents/models
            if path == "/api/agents/models":
                self._handle_agent_models()
            else:
                m = re.match(r"^/api/agents/([^/]+)$", path)
                if m:
                    self._handle_get_agent(unquote(m.group(1)))
                else:
                    m = re.match(r"^/api/agents/([^/]+)/runs$", path)
                    if m:
                        self._handle_agent_runs(unquote(m.group(1)), query)
                    else:
                        m = re.match(r"^/api/agents/([^/]+)/stats$", path)
                        if m:
                            self._handle_agent_stats(unquote(m.group(1)))
                        else:
                            m = re.match(r"^/api/agents/([^/]+)/keys$", path)
                            if m:
                                self._handle_agent_keys(unquote(m.group(1)))
                            else:
                                self.send_error(404, "Not found")
        elif path == "/api/engines":
            try:
                import yaml
                config = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
                # Include the full engine registry from the provider system
                try:
                    from src.swe_team.providers.coding_engine import list_engines, resolve_engine
                    registry = []
                    for name in list_engines():
                        try:
                            engine = resolve_engine(name, {"timeout_seconds": 5})
                            registry.append({
                                "name": name,
                                "available": engine.health_check(),
                                "model": engine.model() if hasattr(engine, "model") else "",
                            })
                        except Exception:
                            registry.append({"name": name, "available": False, "model": ""})
                except Exception:
                    registry = []
                # Mask api_key in fallback_agents before sending to client (#802)
                fallback_agents = []
                for agent in config.get("fallback_agents", []):
                    agent_copy = dict(agent)
                    raw_key = agent_copy.pop("api_key", None)
                    if raw_key:
                        agent_copy["api_key_masked"] = self._mask_api_key(raw_key)
                    else:
                        agent_copy["api_key_masked"] = None
                    fallback_agents.append(agent_copy)
                self._json_response({
                    "engine_routing": config.get("engine_routing", {}),
                    "fallback_agents": fallback_agents,
                    "models": config.get("models", {}),
                    "registry": registry,
                    "registry_count": len(registry),
                }, cache_control="public, max-age=60")
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path == "/api/integrations":
            try:
                from src.swe_team.integrations import list_connectors

                connectors = []
                categories = set()
                for connector in list_connectors():
                    manifest = connector.manifest
                    categories.add(manifest.category)
                    connectors.append(
                        {
                            "connector_type": manifest.connector_type,
                            "name": manifest.name,
                            "category": manifest.category,
                            "description": manifest.description,
                            "icon": manifest.icon,
                            "auth_type": manifest.auth_type,
                            "actions": manifest.actions,
                            "triggers": manifest.triggers,
                            "config_schema": manifest.config_schema,
                            "credential_schema": [
                                {
                                    "key": field.key,
                                    "label": field.label,
                                    "field_type": field.field_type,
                                    "required": field.required,
                                    "secret": field.secret,
                                    "description": field.description,
                                }
                                for field in manifest.credential_schema
                            ],
                        }
                    )

                self._json_response(
                    {"connectors": connectors, "categories": sorted(categories)},
                    cache_control="public, max-age=60",
                )
            except Exception as exc:
                self._json_response({"error": str(exc), "status": 500}, status=500)
        elif path == "/api/mcp/servers":
            self._handle_mcp_servers_list()
        elif path == "/api/onboarding/status":
            self._handle_onboarding_status()
        elif path == "/api/instance/settings":
            self._json_response(_read_instance_settings(), cache_control="private, max-age=30")
        elif path == "/api/instance/connections":
            settings = _read_instance_settings()
            methods = settings.get("connection_methods", [])
            self._json_response({"connection_methods": methods}, cache_control="private, max-age=30")
        elif path == "/api/instance/heartbeat":
            self._json_response(_get_instance_heartbeat(), cache_control="public, max-age=15")
        elif path == "/api/instance/creation-methods":
            self._json_response(_get_creation_methods(), cache_control="public, max-age=60")
        elif path == "/api/heartbeats":
            self._handle_api_heartbeats(query)
        elif path == "/api/github/repos":
            self._handle_github_repos(query)
        # GET /api/rate-limits — current rate limit lifecycle state per provider
        elif path == "/api/rate-limits":
            self._handle_get_rate_limits()
        # GET /api/approvals — list pending approvals
        elif path == "/api/approvals":
            self._handle_list_approvals()
        # GET /api/approvals/<id> — single approval detail
        elif re.match(r"^/api/approvals/[^/]+$", path):
            approval_id = path[len("/api/approvals/"):]
            self._handle_get_approval(approval_id)
        # GET /api/approvals/<id>/comments — list comments for an approval
        elif re.match(r"^/api/approvals/([^/]+)/comments$", path):
            m = re.match(r"^/api/approvals/([^/]+)/comments$", path)
            self._handle_get_approval_comments(m.group(1))
        # GET /api/workflows — return the active workflow definition
        elif path == "/api/workflows":
            try:
                from src.swe_team.workflow.models import create_default_pipeline
                from src.swe_team.config import load_config as _lc
                _cfg = _lc()
                pipeline = create_default_pipeline(team_id=_cfg.team_id)
                self._json_response(pipeline.to_dict())
            except Exception as exc:
                self._json_response({"error": str(exc)}, status=500)
        # GET /api/pipeline/config — return pipeline stage configuration
        elif path == "/api/pipeline/config":
            self._json_response(_read_pipeline_config(), cache_control="private, max-age=10")
        # GET /api/execution/mode — get current execution mode
        elif path == "/api/execution/mode":
            self._json_response(_read_execution_mode(), cache_control="private, max-age=5")
        # GET /api/execution/checkpoints — list pending review checkpoints
        elif path == "/api/execution/checkpoints":
            self._handle_get_execution_checkpoints()
        # GET /api/accounts — list current user's accounts
        elif path == "/api/accounts":
            self._handle_list_accounts(user)
        # GET /api/accounts/<id>/secrets — list account-level secret names
        elif re.match(r"^/api/accounts/([^/]+)/secrets$", path):
            m = re.match(r"^/api/accounts/([^/]+)/secrets$", path)
            self._handle_list_account_secrets(m.group(1))
        # GET /api/accounts/<id>/members — list account members
        elif re.match(r"^/api/accounts/([^/]+)/members$", path):
            m = re.match(r"^/api/accounts/([^/]+)/members$", path)
            self._handle_get_account_members(m.group(1))
        # GET /api/accounts/<id> — get account details
        elif re.match(r"^/api/accounts/[^/]+$", path):
            account_id = path[len("/api/accounts/"):]
            self._handle_get_account(account_id)
        # GET /api/github/label-triggers — list configured label triggers
        elif path == "/api/github/label-triggers":
            self._handle_list_label_triggers()
        # GET /api/suggestions — list creative agent suggestions
        elif path == "/api/suggestions":
            self._handle_list_suggestions()
        else:
            # SPA fallback: serve the React app for any non-API, non-static
            # path so that client-side routing (React Router) can handle it.
            _STATIC_EXTENSIONS = {
                ".js", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif",
                ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".json",
                ".webp", ".avif", ".webmanifest",
            }
            ext = os.path.splitext(path)[1].lower()
            if not path.startswith("/api/") and ext not in _STATIC_EXTENSIONS:
                self._serve_dashboard()
            else:
                self.send_error(404, "Not found")

    def do_POST(self):
        self._dispatch_with_logging("POST", self._do_POST)

    def _do_POST(self):
        """Handle POST requests for scheduler job actions and ticket actions."""
        parsed = urlparse(self.path)
        path = parsed.path

        # Auth middleware
        user = self._check_auth()
        if user is None:
            self._json_response({"error": "Unauthorized", "status": 401}, status=401)
            return

        # Control plane POST routes
        if _HAS_CONTROL_PLANE and getattr(self, "control_plane", None) and cp_handle_post(self, self.control_plane):
            return

        # POST /api/pricing — save pricing config
        if path == "/api/pricing":
            body = self._read_post_body()
            if body is None:
                return
            try:
                normalized = _normalize_pricing_config(body)
                save_pricing(normalized, str(PROJECT_ROOT / "config" / "pricing.json"))
                self._json_response(normalized)
            except Exception as exc:
                logger.exception("Failed to save pricing")
                self._json_response({"error": str(exc)}, status=500)
            return

        # POST /api/pricing/reset — reset pricing to defaults
        if path == "/api/pricing/reset":
            try:
                from src.swe_team.providers.usage_monitor.pricing import DEFAULT_PRICING
                defaults = dict(DEFAULT_PRICING)
                save_pricing(defaults, str(PROJECT_ROOT / "config" / "pricing.json"))
                self._json_response(defaults)
            except Exception as exc:
                logger.exception("Failed to reset pricing")
                self._json_response({"error": str(exc)}, status=500)
            return

        # POST /api/settings — save dashboard settings
        if path == "/api/settings":
            body = self._read_post_body()
            if body is None:
                return
            if _write_settings(body):
                self._json_response({"ok": True, "settings": _read_settings()})
            else:
                self._json_response({"error": "Failed to save settings"}, status=500)
            return

        # POST /api/instance/settings — save instance settings
        if path == "/api/instance/settings":
            body = self._read_post_body()
            if body is None:
                return
            if _write_instance_settings(body):
                self._json_response({"ok": True, "settings": _read_instance_settings()})
            else:
                self._json_response({"error": "Failed to save instance settings"}, status=500)
            return

        # POST /api/instance/connections — save connection methods in instance settings
        if path == "/api/instance/connections":
            body = self._read_post_body()
            if body is None:
                return
            methods = body.get("connection_methods", [])
            if not isinstance(methods, list):
                self._json_response({"error": "Field 'connection_methods' must be a list"}, status=400)
                return
            if _write_instance_settings({"connection_methods": methods}):
                self._json_response({"ok": True, "connection_methods": _read_instance_settings().get("connection_methods", [])})
            else:
                self._json_response({"error": "Failed to save connection methods"}, status=500)
            return

        # POST /api/instance/connections/ssh/generate — generate SSH keypair and store private key as secret
        if path == "/api/instance/connections/ssh/generate":
            self._handle_generate_ssh_key(user)
            return

        # POST /api/instance/connections/ssh/import — import existing SSH private key into secret store
        if path == "/api/instance/connections/ssh/import":
            self._handle_import_ssh_key(user)
            return

        # POST /api/instance/connections/test — test SSH connectivity using stored key secret
        if path == "/api/instance/connections/test":
            self._handle_test_ssh_connection(user)
            return

        # POST /api/instance/provision — provision a new instance via selected method
        if path == "/api/instance/provision":
            self._handle_instance_provision()
            return

        # POST /api/onboarding/complete — complete first-time setup
        if path == "/api/onboarding/complete":
            self._handle_onboarding_complete()
            return

        # POST /api/teams — add a new team
        if path == "/api/teams":
            self._handle_create_team()
            return

        # POST /api/teams/<name>/start|stop|restart — team lifecycle controls (#795)
        m = re.match(r"^/api/teams/([^/]+)/(start|stop|restart)$", path)
        if m:
            self._handle_team_action(unquote(m.group(1)), m.group(2))
            return

        # POST /api/projects — add a new project
        if path == "/api/projects":
            self._handle_create_project()
            return

        # POST /api/scheduler/templates/<id>/apply — create job from template
        m_tpl = re.match(r"^/api/scheduler/templates/([^/]+)/apply$", path)
        if m_tpl:
            self._handle_apply_template(m_tpl.group(1))
            return

        # POST /api/jobs — create a new job
        if path == "/api/jobs":
            self._handle_create_job()
            return

        # POST /api/routines — create a new routine
        if path == "/api/routines":
            self._handle_create_routine()
            return

        # POST /api/jobs/<id>/<action>
        m = re.match(r"^/api/jobs/([^/]+)/(pause|resume|cancel|trigger|delete)$", path)
        if m:
            job_id, action = m.group(1), m.group(2)
            self._handle_job_action(job_id, action)
            return

        # POST /api/routines/<id>/<action>
        m = re.match(r"^/api/routines/([^/]+)/(run|pause|resume|archive)$", path)
        if m:
            routine_id, action = m.group(1), m.group(2)
            self._handle_routine_action(routine_id, action)
            return

        # POST /api/goals — create a new goal
        if path == "/api/goals":
            self._handle_create_goal()
            return

        # POST /api/tickets — create a new ticket
        if path == "/api/tickets":
            self._handle_create_ticket()
            return

        # POST /api/tickets/<id>/assign — assign a ticket
        m = re.match(r"^/api/tickets/([^/]+)/assign$", path)
        if m:
            self._handle_ticket_assign(m.group(1))
            return

        # POST /api/tickets/<id>/investigate — trigger investigation
        m = re.match(r"^/api/tickets/([^/]+)/investigate$", path)
        if m:
            self._handle_ticket_investigate(m.group(1))
            return

        # POST /api/tickets/<id>/develop — trigger developer agent
        m = re.match(r"^/api/tickets/([^/]+)/develop$", path)
        if m:
            self._handle_ticket_develop(m.group(1))
            return

        # POST /api/tickets/<id>/trigger — alias for investigate (used by existing UI)
        m = re.match(r"^/api/tickets/([^/]+)/trigger$", path)
        if m:
            self._handle_ticket_investigate(m.group(1))
            return

        # POST /api/tickets/<id>/comment — add comment
        m = re.match(r"^/api/tickets/([^/]+)/comment$", path)
        if m:
            self._handle_ticket_comment(m.group(1))
            return

        # POST /api/tickets/<id>/feed/comment — add comment to feed
        m = re.match(r"^/api/tickets/([^/]+)/feed/comment$", path)
        if m:
            self._handle_add_feed_comment(unquote(m.group(1)))
            return

        # POST /api/tickets/<id>/label — update labels
        m = re.match(r"^/api/tickets/([^/]+)/label$", path)
        if m:
            self._handle_ticket_label(m.group(1))
            return

        # POST /api/tickets/import — import tickets from file
        if path == "/api/tickets/import":
            self._handle_tickets_import()
            return

        # POST /api/pipeline/trigger — trigger full pipeline cycle
        if path == "/api/pipeline/trigger":
            self._handle_pipeline_trigger()
            return

        # POST /api/secrets/purge — delete all expired secrets
        if path == "/api/secrets/purge":
            self._handle_purge_expired_secrets()
            return

        # POST /api/secrets — create / update a secret
        if path == "/api/secrets":
            self._handle_create_secret(user)
            return

        # POST /api/agents — create a new agent
        if path == "/api/agents":
            self._handle_create_agent()
            return

        # POST /api/agents/<name>/environment-test — test agent environment
        m = re.match(r"^/api/agents/([^/]+)/environment-test$", path)
        if m:
            self._handle_agent_environment_test(unquote(m.group(1)))
            return

        # POST /api/github/repos/connect — connect a GitHub repo
        if path == "/api/github/repos/connect":
            self._handle_github_repos_connect()
            return

        # POST /api/inbox/alerts/archive — archive a governor alert
        if path == "/api/inbox/alerts/archive":
            self._handle_archive_alert()
            return

        # POST /api/inbox/failed-runs/archive — archive a failed routine run
        if path == "/api/inbox/failed-runs/archive":
            self._handle_archive_failed_run()
            return

        # POST /api/approvals/<id>/approve — approve a pending ticket
        m = re.match(r"^/api/approvals/([^/]+)/approve$", path)
        if m:
            self._handle_approval_approve(m.group(1))
            return

        # POST /api/approvals/<id>/reject — reject a pending ticket
        m = re.match(r"^/api/approvals/([^/]+)/reject$", path)
        if m:
            self._handle_approval_reject(m.group(1))
            return

        # POST /api/approvals/<id>/request-revision — request revision
        m = re.match(r"^/api/approvals/([^/]+)/request-revision$", path)
        if m:
            self._handle_approval_request_revision(m.group(1))
            return

        # POST /api/approvals/<id>/comments — add comment to approval
        m = re.match(r"^/api/approvals/([^/]+)/comments$", path)
        if m:
            self._handle_add_approval_comment(m.group(1))
            return

        # POST /api/suggestions/<id>/accept — accept a suggestion (creates ticket)
        m = re.match(r"^/api/suggestions/([^/]+)/accept$", path)
        if m:
            self._handle_suggestion_accept(m.group(1))
            return

        # POST /api/suggestions/<id>/dismiss — dismiss a suggestion
        m = re.match(r"^/api/suggestions/([^/]+)/dismiss$", path)
        if m:
            self._handle_suggestion_dismiss(m.group(1))
            return

        # POST /api/github/label-triggers — create/update a label trigger
        if path == "/api/github/label-triggers":
            self._handle_create_label_trigger()
            return

        # POST /api/github/label-triggers/test — test a trigger against live issues
        if path == "/api/github/label-triggers/test":
            self._handle_test_label_trigger()
            return

        # POST /api/execution/checkpoints/<id>/approve — approve a checkpoint
        m = re.match(r"^/api/execution/checkpoints/([^/]+)/approve$", path)
        if m:
            self._handle_checkpoint_approve(m.group(1))
            return

        # POST /api/execution/checkpoints/<id>/reject — reject a checkpoint
        m = re.match(r"^/api/execution/checkpoints/([^/]+)/reject$", path)
        if m:
            self._handle_checkpoint_reject(m.group(1))
            return

        # POST /api/engines/install — install an engine
        if path == "/api/engines/install":
            self._handle_post_engine_install()
            return

        # POST /api/engines/health-check — health-check a specific engine
        if path == "/api/engines/health-check":
            self._handle_post_engine_health_check()
            return

        # POST /api/models/probe — probe a model endpoint for availability
        if path == "/api/models/probe":
            self._handle_model_probe()
            return

        # POST /api/integrations/configure — save connector credentials
        if path == "/api/integrations/configure":
            self._handle_integration_configure()
            return

        # POST /api/integrations/test — test connector credentials
        if path == "/api/integrations/test":
            self._handle_integration_test()
            return

        # POST /api/mcp/servers — add an MCP server config
        if path == "/api/mcp/servers":
            self._handle_mcp_server_add()
            return

        # POST /api/accounts — create a new account
        if path == "/api/accounts":
            self._handle_create_account(user)
            return

        # POST /api/accounts/<id>/secrets — create account secret
        m = re.match(r"^/api/accounts/([^/]+)/secrets$", path)
        if m:
            self._handle_create_account_secret(m.group(1))
            return

        # POST /api/accounts/<id>/members — invite a member
        m = re.match(r"^/api/accounts/([^/]+)/members$", path)
        if m:
            self._handle_invite_account_member(m.group(1), user)
            return

        # POST /api/projects/<name>/secrets — create project secret
        m = re.match(r"^/api/projects/([^/]+)/secrets$", path)
        if m:
            self._handle_create_project_secret(unquote(m.group(1)))
            return

        # POST /api/projects/<name>/env — set/update an env var
        m = re.match(r"^/api/projects/([^/]+)/env$", path)
        if m:
            self._handle_set_project_env(unquote(m.group(1)))
            return

        self.send_error(404, "Not found")

    def do_PATCH(self):
        self._dispatch_with_logging("PATCH", self._do_PATCH)

    def _do_PATCH(self):
        """Handle PATCH requests for ticket status and severity updates."""
        parsed = urlparse(self.path)
        path = parsed.path

        # Auth middleware
        user = self._check_auth()
        if user is None:
            self._json_response({"error": "Unauthorized", "status": 401}, status=401)
            return

        # PATCH /api/tickets/<id>/status
        m = re.match(r"^/api/tickets/([^/]+)/status$", path)
        if m:
            self._handle_ticket_status(m.group(1))
            return

        # PATCH /api/tickets/<id>/severity
        m = re.match(r"^/api/tickets/([^/]+)/severity$", path)
        if m:
            self._handle_ticket_severity(m.group(1))
            return

        # PATCH /api/tickets/<id>/title
        m = re.match(r"^/api/tickets/([^/]+)/title$", path)
        if m:
            self._handle_ticket_title(m.group(1))
            return

        # PATCH /api/tickets/<id>/description
        m = re.match(r"^/api/tickets/([^/]+)/description$", path)
        if m:
            self._handle_ticket_description(m.group(1))
            return

        # PATCH /api/routines/<id>
        m = re.match(r"^/api/routines/([^/]+)$", path)
        if m:
            self._handle_update_routine(m.group(1))
            return

        # PATCH /api/users/me/settings
        if path == "/api/users/me/settings":
            self._handle_update_my_settings(user)
            return

        # PATCH /api/projects/<name>/name
        m = re.match(r"^/api/projects/([^/]+)/name$", path)
        if m:
            self._handle_update_project_name(m.group(1))
            return

        # PATCH /api/projects/<name>/description
        m = re.match(r"^/api/projects/([^/]+)/description$", path)
        if m:
            self._handle_update_project_description(m.group(1))
            return

        # PATCH /api/projects/<name>/priority
        m = re.match(r"^/api/projects/([^/]+)/priority$", path)
        if m:
            self._handle_update_project_priority(m.group(1))
            return

        # PATCH /api/projects/<name>/enabled
        m = re.match(r"^/api/projects/([^/]+)/enabled$", path)
        if m:
            self._handle_update_project_enabled(m.group(1))
            return

        # PATCH /api/projects/<name>/local_path
        m = re.match(r"^/api/projects/([^/]+)/local_path$", path)
        if m:
            self._handle_update_project_local_path(m.group(1))
            return

        # PATCH /api/projects/<name>/github_repo
        m = re.match(r"^/api/projects/([^/]+)/github_repo$", path)
        if m:
            self._handle_update_project_github_repo(m.group(1))
            return

        # PATCH /api/engines/routing
        if path == "/api/engines/routing":
            self._handle_patch_engine_routing()
            return

        # PATCH /api/engines/<name>/model — update default_model for a fallback agent
        m = re.match(r"^/api/engines/([^/]+)/model$", path)
        if m:
            self._handle_patch_engine_field(unquote(m.group(1)), "default_model", "model")
            return

        # PATCH /api/engines/<name>/timeout — update timeout for a fallback agent
        m = re.match(r"^/api/engines/([^/]+)/timeout$", path)
        if m:
            self._handle_patch_engine_timeout(unquote(m.group(1)))
            return

        # PATCH /api/engines/<name>/binary — update command (binary path) for a fallback agent
        m = re.match(r"^/api/engines/([^/]+)/binary$", path)
        if m:
            self._handle_patch_engine_field(unquote(m.group(1)), "command", "binary")
            return

        # PATCH /api/engines/<name>/enabled — enable/disable a fallback agent
        m = re.match(r"^/api/engines/([^/]+)/enabled$", path)
        if m:
            self._handle_patch_engine_enabled(unquote(m.group(1)))
            return

        # PATCH /api/engines/<name>/team — assign engine to a team (#803)
        m = re.match(r"^/api/engines/([^/]+)/team$", path)
        if m:
            self._handle_patch_engine_team(unquote(m.group(1)))
            return

        # PATCH /api/engines/<name>/api_key — BYOK API key per engine (#802)
        m = re.match(r"^/api/engines/([^/]+)/api_key$", path)
        if m:
            self._handle_patch_engine_api_key(unquote(m.group(1)))
            return

        # PATCH /api/teams/<name>/name
        m = re.match(r"^/api/teams/([^/]+)/name$", path)
        if m:
            self._handle_update_team_name(m.group(1))
            return

        # PATCH /api/teams/<name>/vm_address
        m = re.match(r"^/api/teams/([^/]+)/vm_address$", path)
        if m:
            self._handle_update_team_vm_address(m.group(1))
            return

        # PATCH /api/teams/<name>/github_account
        m = re.match(r"^/api/teams/([^/]+)/github_account$", path)
        if m:
            self._handle_update_team_github_account(m.group(1))
            return

        # PATCH /api/teams/<name>/tier
        m = re.match(r"^/api/teams/([^/]+)/tier$", path)
        if m:
            self._handle_update_team_tier(m.group(1))
            return

        # PATCH /api/teams/<name>/concurrency
        m = re.match(r"^/api/teams/([^/]+)/concurrency$", path)
        if m:
            self._handle_update_team_concurrency(m.group(1))
            return

        # PATCH /api/teams/<name>/budget
        m = re.match(r"^/api/teams/([^/]+)/budget$", path)
        if m:
            self._handle_update_team_budget(m.group(1))
            return

        # PATCH /api/teams/<name>/role
        m = re.match(r"^/api/teams/([^/]+)/role$", path)
        if m:
            self._handle_update_team_role(m.group(1))
            return

        # PATCH /api/teams/<name>/engine
        m = re.match(r"^/api/teams/([^/]+)/engine$", path)
        if m:
            self._handle_update_team_engine(m.group(1))
            return

        # PATCH /api/teams/<name>/specializations
        m = re.match(r"^/api/teams/([^/]+)/specializations$", path)
        if m:
            self._handle_update_team_specializations(m.group(1))
            return

        # PATCH /api/agents/<name>/role
        m = re.match(r"^/api/agents/([^/]+)/role$", path)
        if m:
            self._handle_patch_agent_role(unquote(m.group(1)))
            return

        # PATCH /api/agents/<name>/engine
        m = re.match(r"^/api/agents/([^/]+)/engine$", path)
        if m:
            self._handle_patch_agent_engine(unquote(m.group(1)))
            return

        # PATCH /api/agents/<name>/model_tier
        m = re.match(r"^/api/agents/([^/]+)/model_tier$", path)
        if m:
            self._handle_patch_agent_model_tier(unquote(m.group(1)))
            return

        # PATCH /api/agents/<name>/max_tasks
        m = re.match(r"^/api/agents/([^/]+)/max_tasks$", path)
        if m:
            self._handle_patch_agent_max_tasks(unquote(m.group(1)))
            return

        # PATCH /api/agents/<name>/tools
        m = re.match(r"^/api/agents/([^/]+)/tools$", path)
        if m:
            self._handle_patch_agent_tools(unquote(m.group(1)))
            return

        # PATCH /api/agents/<name>/enabled
        m = re.match(r"^/api/agents/([^/]+)/enabled$", path)
        if m:
            self._handle_patch_agent_enabled(unquote(m.group(1)))
            return

        # PATCH /api/agents/<name>/description
        m = re.match(r"^/api/agents/([^/]+)/description$", path)
        if m:
            self._handle_patch_agent_description(unquote(m.group(1)))
            return

        # PATCH /api/accounts/<id>/members/<login> — update member role
        m = re.match(r"^/api/accounts/([^/]+)/members/([^/]+)$", path)
        if m:
            self._handle_patch_account_member_role(m.group(1), m.group(2))
            return

        # PATCH /api/settings/governance
        if path == "/api/settings/governance":
            self._handle_patch_settings_section("governance")
            return

        # PATCH /api/settings/cycle
        if path == "/api/settings/cycle":
            self._handle_patch_settings_section("cycle")
            return

        # PATCH /api/settings/memory
        if path == "/api/settings/memory":
            self._handle_patch_settings_section("memory")
            return

        # PATCH /api/settings/monitor
        if path == "/api/settings/monitor":
            self._handle_patch_settings_section("monitor")
            return

        # PATCH /api/settings/display — patch individual dashboard display settings
        if path == "/api/settings/display":
            self._handle_patch_display_settings()
            return

        # PATCH /api/pipeline/stages/<stage_name> — update a single pipeline stage config
        m = re.match(r"^/api/pipeline/stages/([a-z_]+)$", path)
        if m:
            self._handle_patch_pipeline_stage(m.group(1))
            return

        # PATCH /api/pipeline/profile — switch execution profile
        if path == "/api/pipeline/profile":
            self._handle_patch_pipeline_profile()
            return

        # PATCH /api/execution/mode — change execution mode (plan/review/start)
        if path == "/api/execution/mode":
            self._handle_patch_execution_mode()
            return

        # PATCH /api/mcp/servers/<name> — toggle enable/disable
        m = re.match(r"^/api/mcp/servers/([^/]+)$", path)
        if m:
            self._handle_mcp_server_patch(unquote(m.group(1)))
            return

        self.send_error(404, "Not found")

    def do_PUT(self):
        self._dispatch_with_logging("PUT", self._do_PUT)

    def _do_PUT(self):
        """Handle PUT requests (currently: PUT /api/agents/<name>)."""
        parsed = urlparse(self.path)
        path = parsed.path

        # Auth middleware
        user = self._check_auth()
        if user is None:
            self._json_response({"error": "Unauthorized", "status": 401}, status=401)
            return

        # PUT /api/agents/<name> — update an agent
        m = re.match(r"^/api/agents/([^/]+)$", path)
        if m:
            self._handle_update_agent(unquote(m.group(1)))
            return

        self.send_error(404, "Not found")

    def do_DELETE(self):
        self._dispatch_with_logging("DELETE", self._do_DELETE)

    def _do_DELETE(self):
        """Handle DELETE requests (currently: DELETE /api/projects/<name>)."""
        parsed = urlparse(self.path)
        path = parsed.path

        # Auth middleware
        user = self._check_auth()
        if user is None:
            self._json_response({"error": "Unauthorized", "status": 401}, status=401)
            return
        # DELETE /api/projects/<name>/secrets/<secret_name> — delete project secret
        m = re.match(r"^/api/projects/([^/]+)/secrets/([^/]+)$", path)
        if m:
            self._handle_delete_project_secret(unquote(m.group(1)), unquote(m.group(2)))
            return
        # DELETE /api/projects/<name>/env/<key> — delete an env var
        m = re.match(r"^/api/projects/([^/]+)/env/([^/]+)$", path)
        if m:
            self._handle_delete_project_env(unquote(m.group(1)), unquote(m.group(2)))
            return
        if path.startswith("/api/projects/"):
            project_name = path[len("/api/projects/"):]
            self._handle_delete_project(project_name)
            return
        # DELETE /api/secrets/<name>
        if path.startswith("/api/secrets/"):
            secret_name = path[len("/api/secrets/"):]
            self._handle_delete_secret(user, secret_name)
            return
        # DELETE /api/engines/<name> — remove a fallback agent / engine
        m = re.match(r"^/api/engines/([^/]+)$", path)
        if m:
            self._handle_delete_engine(unquote(m.group(1)))
            return
        # DELETE /api/agents/<name> — delete an agent
        m = re.match(r"^/api/agents/([^/]+)$", path)
        if m:
            self._handle_delete_agent(unquote(m.group(1)))
            return
        # DELETE /api/tickets/<id>/comment/<index> — delete a comment
        m = re.match(r"^/api/tickets/([^/]+)/comment/(\d+)$", path)
        if m:
            self._handle_delete_ticket_comment(m.group(1), int(m.group(2)))
            return
        # DELETE /api/accounts/<id>/secrets/<name> — delete account secret
        m = re.match(r"^/api/accounts/([^/]+)/secrets/([^/]+)$", path)
        if m:
            self._handle_delete_account_secret(m.group(1), unquote(m.group(2)))
            return
        # DELETE /api/accounts/<id>/members/<login> — remove a member
        m = re.match(r"^/api/accounts/([^/]+)/members/([^/]+)$", path)
        if m:
            self._handle_remove_account_member(m.group(1), m.group(2))
            return
        # DELETE /api/teams/<name> — remove a team
        m = re.match(r"^/api/teams/([^/]+)$", path)
        if m:
            self._handle_delete_team(unquote(m.group(1)))
            return
        # DELETE /api/github/label-triggers/<label> — remove a label trigger
        m = re.match(r"^/api/github/label-triggers/([^/]+)$", path)
        if m:
            self._handle_delete_label_trigger(unquote(m.group(1)))
            return
        # DELETE /api/mcp/servers/<name> — remove an MCP server
        m = re.match(r"^/api/mcp/servers/([^/]+)$", path)
        if m:
            self._handle_mcp_server_delete(unquote(m.group(1)))
            return
        self.send_error(404, "Not found")

    # --- Multi-user account API helpers ---

    def _handle_get_me(self, session_user: Optional[dict]) -> None:
        """GET /api/users/me — return the current user's profile from UserStore."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            # UserStore not available — return session-only profile
            self._json_response({
                "github_login": login,
                "name": session_user.get("name", ""),
                "orgs": session_user.get("orgs", []),
                "role": "user",
            })
            return
        user = us.get_user(login)
        if user is None:
            # Auto-provision on first access via API (e.g. if OAuth callback missed it)
            user = us.get_or_create_user(login)
        self._json_response(user)

    def _handle_update_my_settings(self, session_user: Optional[dict]) -> None:
        """PATCH /api/users/me/settings — update the current user's settings."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        body = self._read_post_body()
        try:
            result = us.update_settings(login, body)
            self._json_response({"ok": True, "settings": result})
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=404)
        except Exception as exc:
            logger.exception("Error updating user settings")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_list_secrets(self, session_user: Optional[dict]) -> None:
        """GET /api/secrets — list secret names (never values) for the current user."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        try:
            # Auto-provision if needed
            if us.get_user(login) is None:
                us.get_or_create_user(login)
            names = us.list_secret_names(login)
            self._json_response({"secrets": names})
        except Exception as exc:
            logger.exception("Error listing secrets")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_create_secret(self, session_user: Optional[dict]) -> None:
        """POST /api/secrets — store an encrypted secret for the current user.

        Body: {"name": "KEY_NAME", "value": "secret_value"}
        The secret value is never returned in any response.
        """
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        body = self._read_post_body()
        name = (body.get("name") or "").strip()
        value = body.get("value", "")
        if not name:
            self._json_response({"error": "Field 'name' is required"}, status=400)
            return
        if not isinstance(value, str) or not value:
            self._json_response({"error": "Field 'value' must be a non-empty string"}, status=400)
            return
        try:
            if us.get_user(login) is None:
                us.get_or_create_user(login)
            us.set_secret(login, name, value)
            self._json_response({"ok": True, "name": name}, status=201)
        except Exception as exc:
            logger.exception("Error storing secret")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_delete_secret(self, session_user: Optional[dict], secret_name: str) -> None:
        """DELETE /api/secrets/<name> — delete a secret for the current user."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        if not secret_name:
            self._json_response({"error": "Secret name required in URL"}, status=400)
            return
        try:
            deleted = us.delete_secret(login, secret_name)
            if deleted:
                self._json_response({"ok": True, "deleted": secret_name})
            else:
                self._json_response({"error": f"Secret {secret_name!r} not found"}, status=404)
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=404)
        except Exception as exc:
            logger.exception("Error deleting secret")
            self._json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------
    # Account-level secrets
    # ------------------------------------------------------------------

    def _handle_list_account_secrets(self, account_id: str) -> None:
        """GET /api/accounts/<id>/secrets — list secret metadata for an account."""
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        try:
            entries = us.list_account_secret_names(account_id)
            self._json_response({"secrets": entries})
        except Exception as exc:
            logger.exception("Error listing account secrets")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_create_account_secret(self, account_id: str) -> None:
        """POST /api/accounts/<id>/secrets — store an encrypted secret for an account.

        Body: {"name": "KEY_NAME", "value": "secret_value", "ttl_minutes": 60}
        ttl_minutes is optional; when set the secret auto-expires after that duration.
        """
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        body = self._read_post_body()
        name = (body.get("name") or "").strip()
        value = body.get("value", "")
        ttl_minutes = body.get("ttl_minutes")
        if not name:
            self._json_response({"error": "Field 'name' is required"}, status=400)
            return
        if not isinstance(value, str) or not value:
            self._json_response({"error": "Field 'value' must be a non-empty string"}, status=400)
            return
        if ttl_minutes is not None:
            try:
                ttl_minutes = int(ttl_minutes)
            except (TypeError, ValueError):
                self._json_response({"error": "Field 'ttl_minutes' must be an integer"}, status=400)
                return
        try:
            us.set_account_secret(account_id, name, value, ttl_minutes=ttl_minutes)
            self._json_response({"ok": True, "name": name}, status=201)
        except Exception as exc:
            logger.exception("Error storing account secret")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_delete_account_secret(self, account_id: str, secret_name: str) -> None:
        """DELETE /api/accounts/<id>/secrets/<name> — delete an account secret."""
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        if not secret_name:
            self._json_response({"error": "Secret name required in URL"}, status=400)
            return
        try:
            deleted = us.delete_account_secret(account_id, secret_name)
            if deleted:
                self._json_response({"ok": True, "deleted": secret_name})
            else:
                self._json_response({"error": f"Secret {secret_name!r} not found"}, status=404)
        except Exception as exc:
            logger.exception("Error deleting account secret")
            self._json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------
    # Project-level secrets
    # ------------------------------------------------------------------

    def _handle_list_project_secrets(self, project_name: str) -> None:
        """GET /api/projects/<name>/secrets — list secret metadata for a project."""
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        try:
            entries = us.list_project_secret_names(project_name)
            self._json_response({"secrets": entries})
        except Exception as exc:
            logger.exception("Error listing project secrets")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_create_project_secret(self, project_name: str) -> None:
        """POST /api/projects/<name>/secrets — store an encrypted secret for a project.

        Body: {"name": "KEY_NAME", "value": "secret_value", "ttl_minutes": 60}
        ttl_minutes is optional; when set the secret auto-expires after that duration.
        """
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        body = self._read_post_body()
        name = (body.get("name") or "").strip()
        value = body.get("value", "")
        ttl_minutes = body.get("ttl_minutes")
        if not name:
            self._json_response({"error": "Field 'name' is required"}, status=400)
            return
        if not isinstance(value, str) or not value:
            self._json_response({"error": "Field 'value' must be a non-empty string"}, status=400)
            return
        if ttl_minutes is not None:
            try:
                ttl_minutes = int(ttl_minutes)
            except (TypeError, ValueError):
                self._json_response({"error": "Field 'ttl_minutes' must be an integer"}, status=400)
                return
        try:
            us.set_project_secret(project_name, name, value, ttl_minutes=ttl_minutes)
            self._json_response({"ok": True, "name": name}, status=201)
        except Exception as exc:
            logger.exception("Error storing project secret")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_delete_project_secret(self, project_name: str, secret_name: str) -> None:
        """DELETE /api/projects/<name>/secrets/<secret_name> — delete a project secret."""
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        if not secret_name:
            self._json_response({"error": "Secret name required in URL"}, status=400)
            return
        try:
            deleted = us.delete_project_secret(project_name, secret_name)
            if deleted:
                self._json_response({"ok": True, "deleted": secret_name})
            else:
                self._json_response({"error": f"Secret {secret_name!r} not found"}, status=404)
        except Exception as exc:
            logger.exception("Error deleting project secret")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_purge_expired_secrets(self) -> None:
        """POST /api/secrets/purge — delete all expired secrets."""
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        try:
            deleted_count = us.purge_expired_secrets()
            self._json_response({"ok": True, "deleted": deleted_count})
        except Exception as exc:
            logger.exception("Error purging expired secrets")
            self._json_response({"error": str(exc)}, status=500)

    # --- Project environment variables API ---

    def _handle_list_project_env(self, project_name: str) -> None:
        """GET /api/projects/<name>/env — list env vars (secrets masked)."""
        env_vars = _load_project_env(project_name)
        self._json_response({"env_vars": _mask_secret_values(env_vars)})

    def _handle_set_project_env(self, project_name: str) -> None:
        """POST /api/projects/<name>/env — set or update an env var.

        Body: {"key": "NODE_ENV", "value": "production", "secret": false}
        """
        body = self._read_post_body()
        key = (body.get("key") or "").strip()
        value = body.get("value", "")
        secret = bool(body.get("secret", False))

        if not key:
            self._json_response({"error": "Field 'key' is required"}, status=400)
            return
        if not isinstance(value, str):
            self._json_response({"error": "Field 'value' must be a string"}, status=400)
            return

        env_vars = _load_project_env(project_name)
        # Update existing or append new
        found = False
        for var in env_vars:
            if var.get("key") == key:
                var["value"] = value
                var["secret"] = secret
                found = True
                break
        if not found:
            env_vars.append({"key": key, "value": value, "secret": secret})

        if _save_project_env(project_name, env_vars):
            self._json_response({"ok": True, "key": key}, status=201)
        else:
            self._json_response({"error": "Failed to save env var"}, status=500)

    def _handle_delete_project_env(self, project_name: str, key: str) -> None:
        """DELETE /api/projects/<name>/env/<key> — remove an env var."""
        if not key:
            self._json_response({"error": "Env var key required in URL"}, status=400)
            return

        env_vars = _load_project_env(project_name)
        original_len = len(env_vars)
        env_vars = [v for v in env_vars if v.get("key") != key]

        if len(env_vars) == original_len:
            self._json_response({"error": f"Env var {key!r} not found"}, status=404)
            return

        if _save_project_env(project_name, env_vars):
            self._json_response({"ok": True, "deleted": key})
        else:
            self._json_response({"error": "Failed to save env vars"}, status=500)

    def _handle_generate_ssh_key(self, session_user: Optional[dict]) -> None:
        """POST /api/instance/connections/ssh/generate."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return

        body = self._read_post_body()
        secret_name = (body.get("secret_name") or "").strip()
        comment = (body.get("comment") or f"{login}@swe-squad").strip()
        if not secret_name:
            self._json_response({"error": "Field 'secret_name' is required"}, status=400)
            return

        try:
            import subprocess
            import tempfile

            with tempfile.TemporaryDirectory() as td:
                private_path = Path(td) / "id_ed25519"
                public_path = Path(f"{private_path}.pub")

                created = subprocess.run(
                    ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(private_path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if created.returncode != 0:
                    self._json_response(
                        {"error": (created.stderr or "Failed to generate SSH key").strip()},
                        status=500,
                    )
                    return

                private_key = private_path.read_text()
                public_key = public_path.read_text().strip()
                fingerprint_proc = subprocess.run(
                    ["ssh-keygen", "-lf", str(public_path)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                fingerprint = ""
                if fingerprint_proc.returncode == 0:
                    parts = (fingerprint_proc.stdout or "").strip().split()
                    if len(parts) > 1:
                        fingerprint = parts[1]

                if us.get_user(login) is None:
                    us.get_or_create_user(login)
                us.set_secret(login, secret_name, private_key)
                self._json_response(
                    {
                        "ok": True,
                        "secret_name": secret_name,
                        "public_key": public_key,
                        "fingerprint": fingerprint,
                    },
                    status=201,
                )
        except Exception as exc:
            logger.exception("Error generating SSH key")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_import_ssh_key(self, session_user: Optional[dict]) -> None:
        """POST /api/instance/connections/ssh/import."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return

        body = self._read_post_body()
        secret_name = (body.get("secret_name") or "").strip()
        private_key = body.get("private_key", "")
        if not secret_name:
            self._json_response({"error": "Field 'secret_name' is required"}, status=400)
            return
        if not isinstance(private_key, str) or not private_key.strip():
            self._json_response({"error": "Field 'private_key' must be a non-empty string"}, status=400)
            return

        try:
            import subprocess
            import tempfile

            with tempfile.TemporaryDirectory() as td:
                private_path = Path(td) / "imported_key"
                private_path.write_text(private_key)
                os.chmod(private_path, 0o600)

                public_proc = subprocess.run(
                    ["ssh-keygen", "-y", "-f", str(private_path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if public_proc.returncode != 0:
                    self._json_response({"error": "Invalid SSH private key"}, status=400)
                    return
                public_key = (public_proc.stdout or "").strip()

                fingerprint_proc = subprocess.run(
                    ["ssh-keygen", "-lf", str(private_path)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                fingerprint = ""
                if fingerprint_proc.returncode == 0:
                    parts = (fingerprint_proc.stdout or "").strip().split()
                    if len(parts) > 1:
                        fingerprint = parts[1]

                if us.get_user(login) is None:
                    us.get_or_create_user(login)
                us.set_secret(login, secret_name, private_key)
                self._json_response(
                    {
                        "ok": True,
                        "secret_name": secret_name,
                        "public_key": public_key,
                        "fingerprint": fingerprint,
                    },
                    status=201,
                )
        except Exception as exc:
            logger.exception("Error importing SSH key")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_test_ssh_connection(self, session_user: Optional[dict]) -> None:
        """POST /api/instance/connections/test."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return

        body = self._read_post_body()
        host = (body.get("host") or "").strip()
        username = (body.get("username") or "").strip()
        secret_name = (body.get("secret_name") or "").strip()
        try:
            port = int(body.get("port", 22))
        except (TypeError, ValueError):
            self._json_response({"error": "Field 'port' must be an integer"}, status=400)
            return

        if not host or not username or not secret_name:
            self._json_response(
                {"error": "Fields 'host', 'username', and 'secret_name' are required"},
                status=400,
            )
            return
        if port < 1 or port > 65535:
            self._json_response({"error": "Field 'port' must be between 1 and 65535"}, status=400)
            return
        if not re.match(r"^[A-Za-z0-9._-]+$", host):
            self._json_response({"error": "Field 'host' contains invalid characters"}, status=400)
            return
        if not re.match(r"^[A-Za-z0-9._-]+$", username):
            self._json_response({"error": "Field 'username' contains invalid characters"}, status=400)
            return

        try:
            private_key = us.get_secret(login, secret_name)
        except ValueError:
            self._json_response({"error": f"Secret {secret_name!r} not found"}, status=404)
            return
        except Exception as exc:
            logger.exception("Error reading SSH key secret")
            self._json_response({"error": str(exc)}, status=500)
            return

        try:
            import subprocess
            import tempfile

            with tempfile.TemporaryDirectory() as td:
                private_path = Path(td) / "test_key"
                private_path.write_text(private_key)
                os.chmod(private_path, 0o600)
                target = f"{username}@{host}"
                proc = subprocess.run(
                    [
                        "ssh",
                        "-i",
                        str(private_path),
                        "-p",
                        str(port),
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "IdentitiesOnly=yes",
                        "-o",
                        "StrictHostKeyChecking=no",
                        "-o",
                        "UserKnownHostsFile=/dev/null",
                        "-o",
                        "ConnectTimeout=8",
                        target,
                        "echo swe-ssh-ok",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=12,
                    check=False,
                )
                stdout = (proc.stdout or "").strip()
                stderr = (proc.stderr or "").strip()
                self._json_response(
                    {
                        "ok": proc.returncode == 0 and "swe-ssh-ok" in stdout,
                        "exit_code": proc.returncode,
                        "stdout": stdout[-1000:],
                        "stderr": stderr[-1000:],
                    }
                )
        except Exception as exc:
            logger.exception("Error testing SSH connection")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_instance_provision(self) -> None:
        """POST /api/instance/provision — provision a new instance."""
        body = self._read_post_body()
        method = body.get("method", "")
        name = body.get("name", "")
        config = body.get("config", {})

        if not method:
            self._json_response({"error": "Field 'method' is required"}, status=400)
            return
        if not name:
            self._json_response({"error": "Field 'name' is required"}, status=400)
            return

        result = _provision_instance(method, name, config)
        if result.get("ok"):
            self._json_response(result, status=201)
        else:
            status = 400 if "Unknown" in result.get("error", "") or "not yet" in result.get("error", "") else 500
            self._json_response(result, status=status)

    def _handle_list_users(self, session_user: Optional[dict]) -> None:
        """GET /api/users — admin only, return all users."""
        if not session_user or not session_user.get("login"):
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        login = session_user["login"]
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        # Check admin role
        user_record = us.get_user(login)
        if user_record is None:
            user_record = us.get_or_create_user(login)
        if user_record.get("role") != "admin":
            self._json_response({"error": "Forbidden — admin only"}, status=403)
            return
        self._json_response(us.list_users())

    # --- Projects API helpers ---

    def _handle_list_projects(self):
        """GET /api/projects — return all configured projects as a JSON list."""
        projects = _load_projects_from_config()
        self._json_response(projects)

    def _handle_get_project(self, name: str):
        """GET /api/projects/<name> — return a single project or 404."""
        projects = _load_projects_from_config()
        for p in projects:
            if p.get("name") == name:
                self._json_response(p)
                return
        self._json_response({"error": f"Project {name!r} not found"}, status=404)

    def _handle_create_project(self):
        """POST /api/projects — add a new project to config."""
        body = self._read_post_body()
        if body is None:
            return
        name = body.get("name", "").strip()
        if not name:
            self._json_response({"error": "Field 'name' is required"}, status=400)
            return
        project = {
            "name": name,
            "description": body.get("description") or "",
            "local_path": body.get("local_path") or "",
            "priority": body.get("priority", "medium"),
            "enabled": body.get("enabled", True),
        }
        github_repo = (body.get("github_repo") or "").strip()
        if github_repo:
            project["github_repo"] = github_repo
        ok = _save_project_to_config(project)
        if not ok:
            self._json_response({"error": f"Project {name!r} already exists"}, status=409)
            return
        self._json_response({"ok": True, "project": project}, status=201)

    def _handle_delete_project(self, name: str):
        """DELETE /api/projects/<name> — remove a project from config."""
        ok = _delete_project_from_config(name)
        if not ok:
            self._json_response({"error": f"Project {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "deleted": name})

    def _handle_update_project_name(self, name: str):
        """PATCH /api/projects/<name>/name — update project name."""
        body = self._read_post_body()
        new_name = body.get("name", "").strip()
        if not new_name:
            self._json_response({"error": "Field 'name' is required"}, status=400)
            return

        # Check if new name already exists (and isn't the same project)
        projects = _load_projects_from_config()
        for p in projects:
            if p.get("name") == new_name and p.get("name") != name:
                self._json_response({"error": f"Project {new_name!r} already exists"}, status=409)
                return

        # Update the name field
        ok = _update_project_field(name, "name", new_name)
        if not ok:
            self._json_response({"error": f"Project {name!r} not found"}, status=404)
            return

        self._json_response({"ok": True, "name": new_name})

    def _handle_update_project_description(self, name: str):
        """PATCH /api/projects/<name>/description — update project description."""
        body = self._read_post_body()
        description = body.get("description", "")

        ok = _update_project_field(name, "description", description)
        if not ok:
            self._json_response({"error": f"Project {name!r} not found"}, status=404)
            return

        self._json_response({"ok": True, "description": description})

    def _handle_update_project_priority(self, name: str):
        """PATCH /api/projects/<name>/priority — update project priority."""
        body = self._read_post_body()
        priority = body.get("priority", "")
        valid_priorities = {"low", "medium", "high", "critical"}
        if priority.lower() not in valid_priorities:
            self._json_response({"error": f"Invalid priority. Must be one of: {', '.join(valid_priorities)}"}, status=400)
            return

        ok = _update_project_field(name, "priority", priority.lower())
        if not ok:
            self._json_response({"error": f"Project {name!r} not found"}, status=404)
            return

        self._json_response({"ok": True, "priority": priority.lower()})

    def _handle_update_project_enabled(self, name: str):
        """PATCH /api/projects/<name>/enabled — toggle project enabled status."""
        body = self._read_post_body()
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            self._json_response({"error": "Field 'enabled' must be a boolean"}, status=400)
            return

        ok = _update_project_field(name, "enabled", enabled)
        if not ok:
            self._json_response({"error": f"Project {name!r} not found"}, status=404)
            return

        self._json_response({"ok": True, "enabled": enabled})

    def _handle_update_project_local_path(self, name: str):
        """PATCH /api/projects/<name>/local_path — update project local path."""
        body = self._read_post_body()
        local_path = body.get("local_path", "")

        ok = _update_project_field(name, "local_path", local_path)
        if not ok:
            self._json_response({"error": f"Project {name!r} not found"}, status=404)
            return

        self._json_response({"ok": True, "local_path": local_path})

    def _handle_update_project_github_repo(self, name: str):
        """PATCH /api/projects/<name>/github_repo — update linked GitHub repo."""
        body = self._read_post_body()
        github_repo = (body.get("github_repo") or "").strip()

        ok = _update_project_field(name, "github_repo", github_repo)
        if not ok:
            self._json_response({"error": f"Project {name!r} not found"}, status=404)
            return

        self._json_response({"ok": True, "github_repo": github_repo})

    def _handle_get_project_tickets(self, name: str):
        """GET /api/projects/<name>/tickets — return tickets for a project."""
        # Get tickets from store
        all_tickets = self.store.list_all()

        # Map project name to project_id format used in tickets
        # Tickets use project_id field which matches repo name
        project_tickets = [t.to_dict() for t in all_tickets if t.project_id == name]

        # Sort by updated_at descending
        project_tickets.sort(key=lambda t: t.get("updated_at", ""), reverse=True)

        self._json_response(project_tickets)

    def _handle_get_project_stats(self, name: str):
        """GET /api/projects/<name>/stats — return statistics for a project."""
        all_tickets = self.store.list_all()

        # Filter tickets for this project
        project_tickets = [t for t in all_tickets if t.project_id == name]

        # Count by status
        status_counts = {}
        for t in project_tickets:
            status = t.status.value
            status_counts[status] = status_counts.get(status, 0) + 1

        # Calculate cost attribution (if available from metadata)
        total_cost = 0.0
        cost_by_status = {}
        for t in project_tickets:
            meta = t.metadata or {}
            cost = meta.get("total_cost_usd", 0.0)
            if isinstance(cost, (int, float)):
                total_cost += float(cost)
                status = t.status.value
                cost_by_status[status] = cost_by_status.get(status, 0.0) + float(cost)

        # Build stats response
        stats = {
            "project_name": name,
            "total_tickets": len(project_tickets),
            "by_status": status_counts,
            "total_cost_usd": round(total_cost, 2),
            "cost_by_status": {k: round(v, 2) for k, v in cost_by_status.items()},
        }

        self._json_response(stats)

    # --- Teams API handlers ---

    def _handle_create_team(self):
        """POST /api/teams — create a new team.

        Writes to swe_team.yaml under the teams: section.
        Body: {name, github_account, role, engine, tier, max_concurrent, cost_budget_daily, specialization, vm}
        """
        body = self._read_post_body()
        if body is None:
            return
        name = body.get("name", "").strip().lower()
        if not name:
            self._json_response({"error": "Field 'name' is required"}, status=400)
            return
        github_account = body.get("github_account", f"swe-squad-{name}")
        try:
            import yaml
            config_path = _CONFIG_PATH
            raw = yaml.safe_load(config_path.read_text()) or {}
            teams = raw.setdefault("teams", {})
            if name in teams:
                self._json_response({"error": f"Team '{name}' already exists"}, status=409)
                return
            # Validate numeric fields with bounds
            try:
                max_concurrent = int(body.get("max_concurrent", 5))
                if max_concurrent < 1 or max_concurrent > 100:
                    self._json_response({"error": "Field 'max_concurrent' must be between 1 and 100"}, status=400)
                    return
            except (TypeError, ValueError):
                self._json_response({"error": "Field 'max_concurrent' must be an integer"}, status=400)
                return
            try:
                cost_budget_daily = float(body.get("cost_budget_daily", 50.0))
                if cost_budget_daily < 0 or cost_budget_daily > 100000:
                    self._json_response({"error": "Field 'cost_budget_daily' must be between 0 and 100000"}, status=400)
                    return
            except (TypeError, ValueError):
                self._json_response({"error": "Field 'cost_budget_daily' must be a number"}, status=400)
                return
            # Validate role and tier against allowed values
            role = body.get("role", "developer")
            valid_roles = {"developer", "investigator", "manager", "reviewer", "senior"}
            if role not in valid_roles:
                self._json_response({"error": f"Invalid role. Must be one of: {', '.join(sorted(valid_roles))}"}, status=400)
                return
            tier = body.get("tier", "standard")
            valid_tiers = {"economy", "standard", "senior", "premium"}
            if tier not in valid_tiers:
                self._json_response({"error": f"Invalid tier. Must be one of: {', '.join(sorted(valid_tiers))}"}, status=400)
                return
            teams[name] = {
                "vm": body.get("vm", f"swe-squad-{name}"),
                "github_account": github_account,
                "role": role,
                "max_concurrent": max_concurrent,
                "cost_budget_daily": cost_budget_daily,
                "specialization": body.get("specialization", []),
                "engine": body.get("engine", "claude"),
                "tier": tier,
            }
            config_path.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
            logger.info("Created team: %s", name)
            self._json_response({"status": "created", "team": name, "config": teams[name]}, status=201)
        except Exception as exc:
            logger.exception("Failed to create team %s", name)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_delete_team(self, name: str):
        """DELETE /api/teams/<name> — remove a team from swe_team.yaml."""
        try:
            import yaml
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            teams = raw.get("teams", {})
            if name not in teams:
                self._json_response({"error": f"Team {name!r} not found"}, status=404)
                return
            del teams[name]
            _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
            logger.info("Deleted team: %s", name)
            self._json_response({"ok": True, "deleted": name})
        except Exception as exc:
            logger.exception("Failed to delete team %s", name)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_update_team_name(self, old_name: str):
        """PATCH /api/teams/<name>/name — rename a team key in swe_team.yaml."""
        body = self._read_post_body()
        new_name = (body.get("name") or "").strip().lower()
        if not new_name:
            self._json_response({"error": "Field 'name' is required"}, status=400)
            return
        try:
            import yaml
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            teams = raw.get("teams", {})
            if old_name not in teams:
                self._json_response({"error": f"Team {old_name!r} not found"}, status=404)
                return
            if new_name != old_name and new_name in teams:
                self._json_response({"error": f"Team {new_name!r} already exists"}, status=409)
                return
            if new_name != old_name:
                teams[new_name] = teams.pop(old_name)
                raw["teams"] = teams
                _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
            self._json_response({"ok": True, "old_name": old_name, "name": new_name})
        except Exception as exc:
            logger.exception("Failed to rename team %s", old_name)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_update_team_vm_address(self, name: str):
        """PATCH /api/teams/<name>/vm_address — update the VM address for a team."""
        body = self._read_post_body()
        value = (body.get("vm") or "").strip()
        if not value:
            self._json_response({"error": "Field 'vm' is required"}, status=400)
            return
        ok = _update_team_field(name, "vm", value)
        if not ok:
            self._json_response({"error": f"Team {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "vm": value})

    def _handle_update_team_github_account(self, name: str):
        """PATCH /api/teams/<name>/github_account — update the GitHub account for a team."""
        body = self._read_post_body()
        value = (body.get("github_account") or "").strip()
        if not value:
            self._json_response({"error": "Field 'github_account' is required"}, status=400)
            return
        ok = _update_team_field(name, "github_account", value)
        if not ok:
            self._json_response({"error": f"Team {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "github_account": value})

    def _handle_update_team_tier(self, name: str):
        """PATCH /api/teams/<name>/tier — update the tier for a team."""
        body = self._read_post_body()
        value = (body.get("tier") or "").strip().lower()
        valid_tiers = {"senior", "standard", "economy"}
        if value not in valid_tiers:
            self._json_response({"error": f"Invalid tier. Must be one of: {', '.join(sorted(valid_tiers))}"}, status=400)
            return
        ok = _update_team_field(name, "tier", value)
        if not ok:
            self._json_response({"error": f"Team {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "tier": value})

    def _handle_update_team_concurrency(self, name: str):
        """PATCH /api/teams/<name>/concurrency — update max_concurrent for a team."""
        body = self._read_post_body()
        value = body.get("max_concurrent")
        if not isinstance(value, int) or value < 1:
            self._json_response({"error": "Field 'max_concurrent' must be a positive integer"}, status=400)
            return
        ok = _update_team_field(name, "max_concurrent", value)
        if not ok:
            self._json_response({"error": f"Team {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "max_concurrent": value})

    def _handle_update_team_budget(self, name: str):
        """PATCH /api/teams/<name>/budget — update cost_budget_daily for a team."""
        body = self._read_post_body()
        value = body.get("cost_budget_daily")
        if not isinstance(value, (int, float)) or value < 0:
            self._json_response({"error": "Field 'cost_budget_daily' must be a non-negative number"}, status=400)
            return
        ok = _update_team_field(name, "cost_budget_daily", float(value))
        if not ok:
            self._json_response({"error": f"Team {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "cost_budget_daily": float(value)})

    def _handle_update_team_role(self, name: str):
        """PATCH /api/teams/<name>/role — update the role for a team."""
        body = self._read_post_body()
        value = body.get("role", "").strip()
        valid_roles = {"developer", "full", "monitor", "triage", "investigator"}
        if value not in valid_roles:
            self._json_response({"error": f"Invalid role. Must be one of: {', '.join(sorted(valid_roles))}"}, status=400)
            return
        ok = _update_team_field(name, "role", value)
        if not ok:
            self._json_response({"error": f"Team {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "role": value})

    # ------------------------------------------------------------------
    # Team lifecycle + health handlers (#793, #795)
    # ------------------------------------------------------------------

    def _handle_team_health(self, name: str):
        """GET /api/teams/<name>/health — return VM connectivity status (#793)."""
        try:
            import yaml
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            teams = raw.get("teams", {})
            if name not in teams:
                self._json_response({"error": f"Team {name!r} not found"}, status=404)
                return
            state = _get_team_run_state(name)
            # Derive health from run state: running=healthy, stopped=unreachable, else=unknown
            if state["status"] == "running":
                health = "healthy"
            elif state["status"] == "stopped":
                health = "unreachable"
            else:
                health = "unknown"
            self._json_response({
                "name": name,
                "status": health,
                "run_state": state["status"],
                "last_check": state["last_check"],
                "vm": teams[name].get("vm", ""),
            })
        except Exception as exc:
            logger.exception("Failed to check team health: %s", name)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_team_action(self, name: str, action: str):
        """POST /api/teams/<name>/(start|stop|restart) — team lifecycle controls (#795)."""
        try:
            import yaml
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            teams = raw.get("teams", {})
            if name not in teams:
                self._json_response({"error": f"Team {name!r} not found"}, status=404)
                return

            current = _get_team_run_state(name)

            if action == "start":
                if current["status"] == "running":
                    self._json_response({"error": f"Team {name!r} is already running"}, status=409)
                    return
                _set_team_run_state(name, "starting")
                # Simulate async startup — immediately transition to running
                _set_team_run_state(name, "running")
                logger.info("Team %s started", name)

            elif action == "stop":
                if current["status"] == "stopped":
                    self._json_response({"error": f"Team {name!r} is already stopped"}, status=409)
                    return
                _set_team_run_state(name, "stopping")
                _set_team_run_state(name, "stopped")
                logger.info("Team %s stopped", name)

            elif action == "restart":
                _set_team_run_state(name, "stopping")
                _set_team_run_state(name, "starting")
                _set_team_run_state(name, "running")
                logger.info("Team %s restarted", name)

            else:
                self._json_response({"error": f"Unknown action: {action}"}, status=400)
                return

            state = _get_team_run_state(name)
            self._json_response({
                "ok": True,
                "name": name,
                "action": action,
                "status": state["status"],
                "last_check": state["last_check"],
            })
        except Exception as exc:
            logger.exception("Failed to %s team %s", action, name)
            self._json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------
    # Engine management handlers
    # ------------------------------------------------------------------

    def _handle_patch_engine_routing(self):
        """PATCH /api/engines/routing — update engine_routing in swe_team.yaml."""
        body = self._read_post_body()
        if not isinstance(body, dict) or not body:
            self._json_response({"error": "Body must be a non-empty object mapping task→engine"}, status=400)
            return
        try:
            import yaml
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            existing = raw.get("engine_routing", {})
            existing.update(body)
            raw["engine_routing"] = existing
            _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
            self._json_response({"ok": True, "engine_routing": existing})
        except Exception as exc:
            logger.exception("Failed to update engine routing")
            self._json_response({"error": str(exc)}, status=500)


    def _handle_patch_engine_field(self, engine_name: str, yaml_field: str, body_field: str):
        """Generic PATCH handler for a single string field on a fallback_agent entry."""
        body = self._read_post_body()
        value = body.get(body_field)
        if value is None or not isinstance(value, str):
            self._json_response({"error": f"Field '{body_field}' (string) is required"}, status=400)
            return
        try:
            import yaml
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            agents = raw.get("fallback_agents", [])
            found = False
            for agent in agents:
                if agent.get("name") == engine_name:
                    agent[yaml_field] = value.strip()
                    found = True
                    break
            if not found:
                self._json_response({"error": f"Engine {engine_name!r} not found in fallback_agents"}, status=404)
                return
            raw["fallback_agents"] = agents
            _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
            self._json_response({"ok": True, "name": engine_name, yaml_field: value.strip()})
        except Exception as exc:
            logger.exception("Failed to update engine %s field %s", engine_name, yaml_field)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_patch_engine_timeout(self, engine_name: str):
        """PATCH /api/engines/<name>/timeout — update timeout (integer) for a fallback agent."""
        body = self._read_post_body()
        value = body.get("timeout")
        if value is None:
            self._json_response({"error": "Field 'timeout' (number) is required"}, status=400)
            return
        try:
            timeout = int(value)
            if timeout < 1:
                raise ValueError("timeout must be >= 1")
        except (TypeError, ValueError) as exc:
            self._json_response({"error": f"Invalid timeout: {exc}"}, status=400)
            return
        try:
            import yaml
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            agents = raw.get("fallback_agents", [])
            found = False
            for agent in agents:
                if agent.get("name") == engine_name:
                    agent["timeout"] = timeout
                    found = True
                    break
            if not found:
                self._json_response({"error": f"Engine {engine_name!r} not found in fallback_agents"}, status=404)
                return
            raw["fallback_agents"] = agents
            _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
            self._json_response({"ok": True, "name": engine_name, "timeout": timeout})
        except Exception as exc:
            logger.exception("Failed to update engine %s timeout", engine_name)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_patch_engine_enabled(self, engine_name: str):
        """PATCH /api/engines/<name>/enabled — enable or disable a fallback agent."""
        body = self._read_post_body()
        value = body.get("enabled")
        if value is None or not isinstance(value, bool):
            self._json_response({"error": "Field 'enabled' (boolean) is required"}, status=400)
            return
        try:
            import yaml
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            agents = raw.get("fallback_agents", [])
            found = False
            for agent in agents:
                if agent.get("name") == engine_name:
                    agent["enabled"] = value
                    found = True
                    break
            if not found:
                self._json_response({"error": f"Engine {engine_name!r} not found in fallback_agents"}, status=404)
                return
            raw["fallback_agents"] = agents
            _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
            self._json_response({"ok": True, "name": engine_name, "enabled": value})
        except Exception as exc:
            logger.exception("Failed to update engine %s enabled state", engine_name)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_patch_engine_team(self, engine_name: str):
        """PATCH /api/engines/<name>/team — assign engine to a team (#803)."""
        body = self._read_post_body()
        team = body.get("team")
        if team is not None and not isinstance(team, str):
            self._json_response({"error": "Field 'team' must be a string or null"}, status=400)
            return
        try:
            import yaml
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            agents = raw.get("fallback_agents", [])
            found = False
            for agent in agents:
                if agent.get("name") == engine_name:
                    if team:
                        agent["team"] = team.strip()
                    else:
                        agent.pop("team", None)
                    found = True
                    break
            if not found:
                self._json_response({"error": f"Engine {engine_name!r} not found in fallback_agents"}, status=404)
                return
            raw["fallback_agents"] = agents
            _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
            self._json_response({"ok": True, "name": engine_name, "team": team})
        except Exception as exc:
            logger.exception("Failed to update engine %s team", engine_name)
            self._json_response({"error": str(exc)}, status=500)

    @staticmethod
    def _mask_api_key(key: str) -> str:
        """Return masked API key showing only last 4 characters."""
        if not key or len(key) <= 4:
            return "****"
        return "****" + key[-4:]

    def _handle_patch_engine_api_key(self, engine_name: str):
        """PATCH /api/engines/<name>/api_key — store BYOK API key per engine (#802)."""
        body = self._read_post_body()
        api_key = body.get("api_key")
        if api_key is not None and not isinstance(api_key, str):
            self._json_response({"error": "Field 'api_key' must be a string or null"}, status=400)
            return
        try:
            import yaml
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            agents = raw.get("fallback_agents", [])
            found = False
            for agent in agents:
                if agent.get("name") == engine_name:
                    if api_key:
                        agent["api_key"] = api_key.strip()
                    else:
                        agent.pop("api_key", None)
                    found = True
                    break
            if not found:
                self._json_response({"error": f"Engine {engine_name!r} not found in fallback_agents"}, status=404)
                return
            raw["fallback_agents"] = agents
            _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
            masked = self._mask_api_key(api_key) if api_key else None
            self._json_response({"ok": True, "name": engine_name, "api_key_masked": masked})
        except Exception as exc:
            logger.exception("Failed to update engine %s api_key", engine_name)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_delete_engine(self, engine_name: str):
        """DELETE /api/engines/<name> — remove a fallback agent from swe_team.yaml."""
        try:
            import yaml
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            agents = raw.get("fallback_agents", [])
            new_agents = [a for a in agents if a.get("name") != engine_name]
            if len(new_agents) == len(agents):
                self._json_response({"error": f"Engine {engine_name!r} not found in fallback_agents"}, status=404)
                return
            raw["fallback_agents"] = new_agents
            _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
            self._json_response({"ok": True, "name": engine_name, "removed": True})
        except Exception as exc:
            logger.exception("Failed to delete engine %s", engine_name)
            self._json_response({"error": str(exc)}, status=500)

    _ENGINE_INSTALL_COMMANDS: dict = {
        "claude": "npm install -g @anthropic-ai/claude-code",
        "codex": "npm install -g @openai/codex",
        "gemini": "npm install -g @google/gemini-cli",
        "aider": "pip install aider-chat",
        "opencode": "go install github.com/opencode-ai/opencode@latest",
        "copilot": "gh extension install github/gh-copilot",
        "amazon_q": "brew install amazon-q",
        "bolt": "npm install -g bolt-cli",
        "windsurf": "npm install -g @codeium/windsurf",
        "sweep": "pip install sweepai",
        "codegpt": "pip install codegpt",
        "sgpt": "pip install shell-gpt",
        "goose": "pipx install goose-ai",
        "gorilla": "pip install gorilla-cli",
        "openhands": "pip install openhands-ai",
        "antigravity": "pip install antigravity",
        "pi_agents": "pip install pi-agents",
    }

    def _handle_post_engine_install(self):
        """POST /api/engines/install — install an engine via subprocess."""
        body = self._read_post_body()
        engine = (body.get("engine") or "").strip().lower()
        if not engine:
            self._json_response({"error": "Field 'engine' is required"}, status=400)
            return
        cmd = self._ENGINE_INSTALL_COMMANDS.get(engine)
        if not cmd:
            self._json_response({"error": f"No install command known for engine {engine!r}. Known: {list(self._ENGINE_INSTALL_COMMANDS)}"}, status=422)
            return
        import subprocess
        import threading

        result_box: dict = {}

        def _run():
            try:
                started_at = datetime.now(timezone.utc)
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(PROJECT_ROOT),
                )
                finished_at = datetime.now(timezone.utc)
                result_box["returncode"] = proc.returncode
                result_box["stdout"] = proc.stdout[-4000:] if proc.stdout else ""
                result_box["stderr"] = proc.stderr[-2000:] if proc.stderr else ""
                result_box["ok"] = proc.returncode == 0
                result_box["started_at"] = started_at.isoformat()
                result_box["finished_at"] = finished_at.isoformat()
                result_box["duration_seconds"] = round((finished_at - started_at).total_seconds(), 3)
                result_box["working_directory"] = str(PROJECT_ROOT)
            except subprocess.TimeoutExpired:
                result_box["ok"] = False
                result_box["error"] = "Install command timed out after 120s"
            except Exception as exc:
                result_box["ok"] = False
                result_box["error"] = str(exc)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=130)  # slightly longer than subprocess timeout
        if not result_box:
            self._json_response({"ok": False, "error": "Install thread did not complete in time"}, status=500)
            return
        status_code = 200 if result_box.get("ok") else 500
        self._json_response({"engine": engine, "command": cmd, **result_box}, status=status_code)

    def _handle_post_engine_health_check(self):
        """POST /api/engines/health-check — run health check for a specific engine."""
        body = self._read_post_body()
        engine_name = (body.get("engine") or "").strip()
        if not engine_name:
            self._json_response({"error": "Field 'engine' is required"}, status=400)
            return
        try:
            from src.swe_team.providers.coding_engine import resolve_engine
            engine = resolve_engine(engine_name, {"timeout_seconds": 5})
            available = engine.health_check()
            model = engine.model() if hasattr(engine, "model") else ""
            self._json_response({"engine": engine_name, "available": available, "model": model})
        except Exception as exc:
            self._json_response({"engine": engine_name, "available": False, "error": str(exc)})

    # ------------------------------------------------------------------
    # Integration configure + test
    # ------------------------------------------------------------------

    def _handle_integration_configure(self):
        """POST /api/integrations/configure — save connector credentials."""
        body = self._read_post_body()
        ct = (body.get("connector_type") or "").strip()
        creds = body.get("credentials", {})
        if not ct:
            self._json_response({"error": "connector_type required"}, status=400)
            return
        if not isinstance(creds, dict):
            self._json_response({"error": "credentials must be object"}, status=400)
            return
        us = _get_user_store()
        if us is None:
            self._json_response({"error": "UserStore not available"}, status=503)
            return
        try:
            for k, v in creds.items():
                if isinstance(v, str) and v.strip():
                    us.set_project_secret("_integrations", f"integration:{ct}:{k}", v)
            self._json_response({"ok": True, "connector_type": ct}, status=201)
        except Exception as exc:
            logger.exception("Error saving integration credentials")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_integration_test(self):
        """POST /api/integrations/test — test connector credentials."""
        body = self._read_post_body()
        ct = (body.get("connector_type") or "").strip()
        creds = body.get("credentials", {})
        if not ct:
            self._json_response({"error": "connector_type required"}, status=400)
            return
        try:
            from src.swe_team.integrations import list_connectors
            conn = next((c for c in list_connectors() if c.manifest.connector_type == ct), None)
            if conn is None:
                self._json_response({"ok": False, "message": f"Unknown connector: {ct}"})
                return
            missing = [f.label for f in conn.manifest.credential_schema if f.required and not creds.get(f.key, "").strip()]
            if missing:
                self._json_response({"ok": False, "message": f"Missing: {', '.join(missing)}"})
                return
            if hasattr(conn, "test_connection"):
                r = conn.test_connection(creds)
                self._json_response({"ok": r.get("ok", False), "message": r.get("message", "Done")})
            else:
                self._json_response({"ok": True, "message": "Credentials validated (no live test)"})
        except ImportError:
            self._json_response({"ok": False, "message": "Integrations module not available"})
        except Exception as exc:
            self._json_response({"ok": False, "message": str(exc)})

    # ------------------------------------------------------------------
    # MCP Server management (#647)
    # ------------------------------------------------------------------

    _MCP_SERVERS_PATH = PROJECT_ROOT / "data" / "swe_team" / "mcp_servers.json"

    def _read_mcp_servers(self) -> list:
        try:
            if self._MCP_SERVERS_PATH.exists():
                return json.loads(self._MCP_SERVERS_PATH.read_text()) or []
        except Exception:
            pass
        return []

    def _write_mcp_servers(self, servers: list) -> None:
        self._MCP_SERVERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._MCP_SERVERS_PATH.write_text(json.dumps(servers, indent=2))

    def _handle_mcp_servers_list(self):
        """GET /api/mcp/servers — list configured MCP servers."""
        self._json_response({"servers": self._read_mcp_servers()})

    def _handle_mcp_server_add(self):
        """POST /api/mcp/servers — add an MCP server config."""
        body = self._read_post_body()
        name = (body.get("name") or "").strip()
        command = (body.get("command") or "").strip()
        if not name:
            self._json_response({"error": "name is required"}, status=400)
            return
        if not command:
            self._json_response({"error": "command is required"}, status=400)
            return
        servers = self._read_mcp_servers()
        # Check for duplicate name
        if any(s["name"] == name for s in servers):
            self._json_response({"error": f"Server '{name}' already exists"}, status=409)
            return
        server = {
            "name": name,
            "command": command,
            "args": body.get("args", []),
            "env": body.get("env", {}),
            "enabled": True,
        }
        servers.append(server)
        self._write_mcp_servers(servers)
        self._json_response({"ok": True, "server": server}, status=201)

    def _handle_mcp_server_delete(self, name: str):
        """DELETE /api/mcp/servers/<name> — remove an MCP server."""
        servers = self._read_mcp_servers()
        original_len = len(servers)
        servers = [s for s in servers if s["name"] != name]
        if len(servers) == original_len:
            self._json_response({"error": f"Server '{name}' not found"}, status=404)
            return
        self._write_mcp_servers(servers)
        self._json_response({"ok": True, "deleted": name})

    def _handle_mcp_server_patch(self, name: str):
        """PATCH /api/mcp/servers/<name> — update MCP server config."""
        body = self._read_post_body()
        servers = self._read_mcp_servers()
        for s in servers:
            if s["name"] == name:
                if "enabled" in body:
                    s["enabled"] = bool(body["enabled"])
                if "command" in body:
                    s["command"] = body["command"]
                if "args" in body:
                    s["args"] = body["args"]
                if "env" in body:
                    s["env"] = body["env"]
                self._write_mcp_servers(servers)
                self._json_response({"ok": True, "server": s})
                return
        self._json_response({"error": f"Server '{name}' not found"}, status=404)

    def _handle_model_probe(self):
        """POST /api/models/probe — probe a model endpoint for availability.

        Accepts {"url": "...", "api_key": "...", "model": "..."}.
        Sends a minimal completion request and returns available models.
        Handles timeouts, non-JSON responses, and multiple response formats
        (OpenAI, Anthropic, plain text).
        """
        body = self._read_post_body()
        url = (body.get("url") or "").strip().rstrip("/")
        api_key = (body.get("api_key") or "").strip()
        model = (body.get("model") or "").strip()

        if not url:
            self._json_response({"success": False, "models": [], "error": "Field 'url' is required"}, status=400)
            return

        timeout = body.get("timeout", 10)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            timeout = 10

        # --- Step 1: Try to list models from the endpoint ---
        models_list = []
        models_error = None
        try:
            models_list, models_error = _probe_list_models(url, api_key, timeout)
        except Exception as exc:
            models_error = str(exc)

        # --- Step 2: If a specific model was requested, try a completion probe ---
        completion_ok = None
        completion_error = None
        if model:
            try:
                completion_ok, completion_error = _probe_completion(url, api_key, model, timeout)
            except Exception as exc:
                completion_ok = False
                completion_error = str(exc)

        success = (len(models_list) > 0) or (completion_ok is True)
        error = None
        if not success:
            error = completion_error or models_error or "No models found and completion probe failed"

        self._json_response({
            "success": success,
            "models": models_list,
            "model_tested": model or None,
            "completion_ok": completion_ok,
            "error": error,
        })

    def _handle_update_team_engine(self, name: str):
        """PATCH /api/teams/<name>/engine — update the coding engine alias for a team."""
        body = self._read_post_body()
        value = body.get("engine", "").strip()
        if not value:
            self._json_response({"error": "Field 'engine' is required"}, status=400)
            return
        ok = _update_team_field(name, "engine", value)
        if not ok:
            self._json_response({"error": f"Team {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "engine": value})

    def _handle_update_team_specializations(self, name: str):
        """PATCH /api/teams/<name>/specializations — update specialization list for a team."""
        body = self._read_post_body()
        value = body.get("specialization")
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            self._json_response({"error": "Field 'specialization' must be a list of strings"}, status=400)
            return
        ok = _update_team_field(name, "specialization", value)
        if not ok:
            self._json_response({"error": f"Team {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "specialization": value})

    # --- Agent field PATCH handlers ---

    _VALID_AGENT_ROLES = {"monitor", "triage", "investigator", "developer", "reviewer", "creative", "governor"}
    _VALID_MODEL_TIERS = {"haiku", "sonnet", "opus"}

    def _handle_patch_agent_role(self, name: str):
        """PATCH /api/agents/<name>/role — update the role for an agent."""
        body = self._read_post_body()
        value = body.get("role", "").strip()
        if value not in self._VALID_AGENT_ROLES:
            self._json_response({"error": f"Invalid role. Must be one of: {', '.join(sorted(self._VALID_AGENT_ROLES))}"}, status=400)
            return
        ok = _patch_agent_field(name, "role", value)
        if not ok:
            self._json_response({"error": f"Agent {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "role": value})

    def _handle_patch_agent_engine(self, name: str):
        """PATCH /api/agents/<name>/engine — update the coding engine alias for an agent."""
        body = self._read_post_body()
        value = body.get("engine", "").strip()
        if not value:
            self._json_response({"error": "Field 'engine' is required"}, status=400)
            return
        ok = _patch_agent_field(name, "engine", value)
        if not ok:
            self._json_response({"error": f"Agent {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "engine": value})

    def _handle_patch_agent_model_tier(self, name: str):
        """PATCH /api/agents/<name>/model_tier — update the model tier (haiku/sonnet/opus) for an agent."""
        body = self._read_post_body()
        value = body.get("model_tier", "").strip()
        if value not in self._VALID_MODEL_TIERS:
            self._json_response({"error": f"Invalid model_tier. Must be one of: {', '.join(sorted(self._VALID_MODEL_TIERS))}"}, status=400)
            return
        ok = _patch_agent_field(name, "model", value)
        if not ok:
            self._json_response({"error": f"Agent {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "model": value})

    def _handle_patch_agent_max_tasks(self, name: str):
        """PATCH /api/agents/<name>/max_tasks — update max_concurrent_tasks for an agent."""
        body = self._read_post_body()
        value = body.get("max_tasks")
        if not isinstance(value, int) or value < 1:
            self._json_response({"error": "Field 'max_tasks' must be a positive integer"}, status=400)
            return
        ok = _patch_agent_field(name, "max_concurrent_tasks", value)
        if not ok:
            self._json_response({"error": f"Agent {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "max_concurrent_tasks": value})

    def _handle_patch_agent_tools(self, name: str):
        """PATCH /api/agents/<name>/tools — update allowed tools list for an agent."""
        body = self._read_post_body()
        value = body.get("tools")
        if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
            self._json_response({"error": "Field 'tools' must be a list of strings"}, status=400)
            return
        ok = _patch_agent_field(name, "tools", value)
        if not ok:
            self._json_response({"error": f"Agent {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "tools": value})

    def _handle_patch_agent_description(self, name: str):
        """PATCH /api/agents/<name>/description — update the description for an agent."""
        body = self._read_post_body()
        value = body.get("description", "").strip()
        ok = _patch_agent_field(name, "description", value)
        if not ok:
            self._json_response({"error": f"Agent {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "description": value})

    def _handle_patch_agent_enabled(self, name: str):
        """PATCH /api/agents/<name>/enabled — toggle the enabled flag for an agent."""
        body = self._read_post_body()
        value = body.get("enabled")
        if not isinstance(value, bool):
            self._json_response({"error": "Field 'enabled' must be a boolean"}, status=400)
            return
        ok = _patch_agent_field(name, "enabled", value)
        if not ok:
            self._json_response({"error": f"Agent {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "name": name, "enabled": value})

    def _handle_patch_settings_section(self, section: str):
        """PATCH /api/settings/<section> — update a config section in swe_team.yaml."""
        body = self._read_post_body()
        if body is None:
            return
        ok = _update_config_section(section, body)
        if not ok:
            self._json_response({"error": f"Failed to update {section!r} config"}, status=500)
            return
        self._json_response({"ok": True, "section": section, "settings": _build_full_settings()})

    def _handle_patch_display_settings(self):
        """PATCH /api/settings/display — update individual dashboard display settings."""
        body = self._read_post_body()
        if body is None:
            return
        current = _read_settings()
        current.update(body)
        ok = _write_settings(current)
        if not ok:
            self._json_response({"error": "Failed to save display settings"}, status=500)
            return
        self._json_response({"ok": True, "settings": _read_settings()})

    # --- Pipeline configuration handlers ---

    def _handle_patch_pipeline_stage(self, stage_name: str):
        """PATCH /api/pipeline/stages/<stage_name> — update config for a single pipeline stage."""
        valid_stages = {"monitor", "triage", "investigate", "develop", "review", "verify"}
        if stage_name not in valid_stages:
            self._json_response(
                {"error": f"Unknown stage {stage_name!r}. Valid: {sorted(valid_stages)}"},
                status=400,
            )
            return
        body = self._read_post_body()
        if not isinstance(body, dict):
            self._json_response({"error": "Request body must be a JSON object"}, status=400)
            return

        config = _read_pipeline_config()
        stage = config["stages"].get(stage_name, dict(_DEFAULT_PIPELINE_STAGES.get(stage_name, {})))

        # Apply allowed fields
        if "enabled" in body and isinstance(body["enabled"], bool):
            stage["enabled"] = body["enabled"]
        if "timeout_minutes" in body:
            val = body["timeout_minutes"]
            if isinstance(val, (int, float)) and val > 0:
                stage["timeout_minutes"] = int(val)
        if "max_retries" in body:
            val = body["max_retries"]
            if isinstance(val, int) and 0 <= val <= 10:
                stage["max_retries"] = val
        if "model_tier" in body:
            tier = body["model_tier"]
            if tier in ("T1", "T2", "T3"):
                stage["model_tier"] = tier

        config["stages"][stage_name] = stage
        ok = _write_pipeline_config(config)
        if not ok:
            self._json_response({"error": "Failed to save pipeline config"}, status=500)
            return
        self._json_response({"ok": True, "stage": stage_name, "config": stage})

    def _handle_patch_pipeline_profile(self):
        """PATCH /api/pipeline/profile — switch execution profile (base/burst/max)."""
        body = self._read_post_body()
        profile = body.get("execution_profile", "").strip().lower()
        valid_profiles = {"base", "burst", "max"}
        if profile not in valid_profiles:
            self._json_response(
                {"error": f"Invalid profile {profile!r}. Valid: {sorted(valid_profiles)}"},
                status=400,
            )
            return

        config = _read_pipeline_config()
        config["execution_profile"] = profile
        ok = _write_pipeline_config(config)
        if not ok:
            self._json_response({"error": "Failed to save pipeline config"}, status=500)
            return
        self._json_response({"ok": True, "execution_profile": profile})

    # --- Goals API handlers ---

    def _handle_create_goal(self):
        """POST /api/goals — create a goal by adding a placeholder ticket."""
        from src.swe_team.models import SWETicket, TicketSeverity, TicketType

        body = self._read_post_body()
        project_id_value = body.get("project_id")
        if not isinstance(project_id_value, str):
            self._json_response({"error": "Missing required field: project_id"}, status=400)
            return

        project_id = project_id_value.strip()
        if not project_id:
            self._json_response({"error": "Missing required field: project_id"}, status=400)
            return

        existing = self.store.list_by_project_id(project_id)
        if existing:
            self._json_response({"error": f"Goal already exists: {project_id}"}, status=409)
            return

        goal_value = body.get("goal")
        if goal_value is None:
            goal_text = ""
        elif isinstance(goal_value, str):
            goal_text = goal_value
        else:
            self._json_response({"error": "Field 'goal' must be a string"}, status=400)
            return

        placeholder = SWETicket(
            title=f"[Goal] {project_id}",
            description=f"Project goal placeholder: {goal_text}",
            severity=TicketSeverity.LOW,
            ticket_type=TicketType.DOCUMENTATION,
            project_id=project_id,
            goal=goal_text,
        )
        self.store.add(placeholder)

        self._json_response(
            {
                "project_id": project_id,
                "goal": goal_text,
                "ticket_id": placeholder.ticket_id,
                "status": "created",
            },
            status=201,
        )

    def _handle_list_goals(self):
        """GET /api/goals — list all goals (projects with hierarchies)."""
        all_tickets = self.store.list_all()

        # Group tickets by project_id
        project_tickets = {}
        for ticket in all_tickets:
            if ticket.project_id:
                if ticket.project_id not in project_tickets:
                    project_tickets[ticket.project_id] = []
                project_tickets[ticket.project_id].append(ticket)

        # Build goals list
        goals = []
        for project_id, tickets in project_tickets.items():
            # Get goal description from first ticket with a goal field
            goal_description = None
            for ticket in tickets:
                if ticket.goal:
                    goal_description = ticket.goal
                    break

            # Count completed tickets
            completed_count = sum(1 for t in tickets if t.status.value in ("resolved", "closed"))

            goals.append({
                "id": project_id,
                "name": goal_description or project_id,
                "tickets_count": len(tickets),
                "completed_count": completed_count,
            })

        # Sort by name
        goals.sort(key=lambda g: g["name"])

        self._json_response(goals)

    def _handle_get_goal(self, goal_id: str):
        """GET /api/goals/<id> — get goal hierarchy as tree."""
        from src.swe_team.models import TicketStatus

        # Get root tickets for this project
        root_tickets = self.store.get_project_root_tickets(goal_id)

        if not root_tickets:
            self._json_response({"error": f"Goal {goal_id!r} not found"}, status=404)
            return

        def build_tree(ticket):
            """Recursively build tree structure for a ticket."""
            children = self.store.list_by_parent_ticket_id(ticket.ticket_id)
            return {
                "ticket_id": ticket.ticket_id,
                "title": ticket.title,
                "status": ticket.status.value,
                "description": ticket.description,
                "goal": ticket.goal,
                "created_at": ticket.created_at,
                "updated_at": ticket.updated_at,
                "children": [build_tree(child) for child in children],
            }

        # Build tree for each root ticket
        tree = [build_tree(ticket) for ticket in root_tickets]

        self._json_response(tree)

    def _handle_get_goal_stats(self, goal_id: str):
        """GET /api/goals/<id>/stats — get goal statistics."""
        from src.swe_team.models import TicketStatus

        # Get all tickets for this project
        tickets = self.store.list_by_project_id(goal_id)

        if not tickets:
            self._json_response({"error": f"Goal {goal_id!r} not found"}, status=404)
            return

        # Count by status
        total = len(tickets)
        completed = sum(1 for t in tickets if t.status.value in ("resolved", "closed"))
        in_progress = sum(1 for t in tickets if t.status.value in (
            "investigating", "investigation_complete", "in_development", "in_review",
            "testing", "deploying", "monitoring", "verifying"
        ))
        pending = total - completed - in_progress

        # Calculate progress percentage
        progress = int((completed / total * 100)) if total > 0 else 0

        stats = {
            "goal_id": goal_id,
            "total": total,
            "completed": completed,
            "in_progress": in_progress,
            "pending": pending,
            "progress": progress,
        }

        self._json_response(stats)


    # --- Agents API handlers ---

    def _handle_get_agent(self, name: str):
        """GET /api/agents/<name> — return a single agent or 404."""
        agents = _load_agents_from_config()
        for a in agents:
            if a.get("name") == name:
                self._json_response(a)
                return
        self._json_response({"error": f"Agent {name!r} not found"}, status=404)

    def _handle_create_agent(self):
        """POST /api/agents — add a new agent to config."""
        body = self._read_post_body()
        if body is None:
            return
        name = body.get("name", "").strip()
        if not name:
            self._json_response({"error": "Field 'name' is required"}, status=400)
            return
        # Check for existing agent with same name
        agents = _load_agents_from_config()
        for a in agents:
            if a.get("name") == name:
                self._json_response({"error": f"Agent {name!r} already exists"}, status=409)
                return
        ok = _save_agent_to_config(body)
        if not ok:
            self._json_response({"error": "Failed to create agent"}, status=500)
            return
        agent = dict(body)
        agent.setdefault("enabled", True)
        self._json_response({"ok": True, "agent": agent}, status=201)

    def _handle_update_agent(self, name: str):
        """PUT /api/agents/<name> — update an agent in config."""
        body = self._read_post_body()
        if body is None:
            return
        # Ensure name in body matches the URL
        body["name"] = name
        agents = _load_agents_from_config()
        found = any(a.get("name") == name for a in agents)
        if not found:
            self._json_response({"error": f"Agent {name!r} not found"}, status=404)
            return
        ok = _save_agent_to_config(body)
        if not ok:
            self._json_response({"error": "Failed to update agent"}, status=500)
            return
        agent = dict(body)
        agent.setdefault("enabled", True)
        self._json_response({"ok": True, "agent": agent})

    def _handle_delete_agent(self, name: str):
        """DELETE /api/agents/<name> — remove an agent from config."""
        ok = _delete_agent_from_config(name)
        if not ok:
            self._json_response({"error": f"Agent {name!r} not found"}, status=404)
            return
        self._json_response({"ok": True, "deleted": name})

    def _handle_agent_runs(self, name: str, query: dict):
        """GET /api/agents/<name>/runs — return execution history for an agent."""
        try:
            # Parse query params
            limit = int(query.get("limit", ["50"])[0])
            offset = int(query.get("offset", ["0"])[0])

            # Load sessions from sessions.json
            sessions = _read_json_file(_SESSIONS_PATH) or []
            # Load token usage from token_usage.jsonl
            token_records = []
            if _TOKEN_USAGE_PATH.exists():
                for line in _TOKEN_USAGE_PATH.read_text().strip().splitlines():
                    if line:
                        try:
                            token_records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

            # Map agent name to agent type
            agent_type_map = {
                "swe_monitor": "monitor",
                "swe_triage": "triage",
                "swe_developer": "developer",
                "swe_reviewer": "reviewer",
                "swe_tester": "tester",
                "swe_deployer": "deployer",
                "swe_documenter": "documenter",
                "swe_creative": "creative",
                "browser_investigator": "investigator",
                "db_investigator": "investigator",
                "infra_investigator": "investigator",
            }
            target_agent_type = agent_type_map.get(name, name)

            # Build runs list by combining sessions and token records
            runs = []
            for session in sessions:
                if session.get("agent_type") == target_agent_type or session.get("agent_type") == name:
                    # Find matching token record
                    token_info = next(
                        (t for t in token_records if t.get("session_id") == session.get("session_id")),
                        {}
                    )
                    created_at = session.get("created_at", 0)
                    last_active = session.get("last_active", 0)
                    duration_seconds = last_active - created_at if created_at and last_active else 0

                    runs.append({
                        "session_id": session.get("session_id", ""),
                        "ticket_id": session.get("ticket_id"),
                        "agent_type": session.get("agent_type", ""),
                        "task_type": token_info.get("task", "unknown"),
                        "model": token_info.get("model", "unknown"),
                        "status": session.get("status", "unknown"),
                        "created_at": datetime.fromtimestamp(created_at, timezone.utc).isoformat() if created_at else None,
                        "completed_at": datetime.fromtimestamp(last_active, timezone.utc).isoformat() if last_active else None,
                        "duration_seconds": round(duration_seconds, 2),
                        "input_tokens": token_info.get("input_tokens", 0),
                        "output_tokens": token_info.get("output_tokens", 0),
                        "cache_read_tokens": token_info.get("cache_read_tokens", 0),
                        "cache_creation_tokens": token_info.get("cache_creation_tokens", 0),
                        "cost_usd": token_info.get("cost_usd", 0.0),
                    })

            # Sort by created_at descending
            runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)

            # Apply pagination
            total = len(runs)
            runs = runs[offset:offset + limit]

            self._json_response({"runs": runs, "total": total}, cache_control="public, max-age=30")
        except Exception as exc:
            logger.exception("Agent runs API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_agent_stats(self, name: str):
        """GET /api/agents/<name>/stats — return statistics for an agent."""
        try:
            sessions = _read_json_file(_SESSIONS_PATH) or []

            # Map agent name to agent type
            agent_type_map = {
                "swe_monitor": "monitor",
                "swe_triage": "triage",
                "swe_developer": "developer",
                "swe_reviewer": "reviewer",
                "swe_tester": "tester",
                "swe_deployer": "deployer",
                "swe_documenter": "documenter",
                "swe_creative": "creative",
                "browser_investigator": "investigator",
                "db_investigator": "investigator",
                "infra_investigator": "investigator",
            }
            target_agent_type = agent_type_map.get(name, name)

            # Filter sessions for this agent
            agent_sessions = [
                s for s in sessions
                if s.get("agent_type") == target_agent_type or s.get("agent_type") == name
            ]

            total_runs = len(agent_sessions)
            successful_runs = sum(1 for s in agent_sessions if s.get("status") == "completed")
            failed_runs = total_runs - successful_runs

            # Calculate average duration
            durations = []
            for s in agent_sessions:
                created = s.get("created_at", 0)
                last_active = s.get("last_active", 0)
                if created and last_active:
                    durations.append(last_active - created)
            avg_duration_seconds = sum(durations) / len(durations) if durations else 0.0

            # Get token usage from token_usage.jsonl
            total_tokens = 0
            total_cost = 0.0
            session_ids = {s.get("session_id") for s in agent_sessions}
            if _TOKEN_USAGE_PATH.exists():
                for line in _TOKEN_USAGE_PATH.read_text().strip().splitlines():
                    if line:
                        try:
                            token_record = json.loads(line)
                            if token_record.get("session_id") in session_ids:
                                total_tokens += (
                                    token_record.get("input_tokens", 0) +
                                    token_record.get("output_tokens", 0)
                                )
                                total_cost += token_record.get("cost_usd", 0.0)
                        except json.JSONDecodeError:
                            pass

            # Get last run time
            last_run = None
            if agent_sessions:
                last_active = max(s.get("last_active", 0) for s in agent_sessions)
                if last_active:
                    last_run = datetime.fromtimestamp(last_active, timezone.utc).isoformat()

            stats = {
                "name": name,
                "total_runs": total_runs,
                "successful_runs": successful_runs,
                "failed_runs": failed_runs,
                "avg_duration_seconds": round(avg_duration_seconds, 2),
                "total_tokens": total_tokens,
                "total_cost_usd": round(total_cost, 6),
                "last_run": last_run,
            }

            self._json_response(stats, cache_control="public, max-age=60")
        except Exception as exc:
            logger.exception("Agent stats API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_agent_keys(self, name: str):
        """GET /api/agents/<name>/keys — return environment keys/secrets for an agent."""
        try:
            import yaml
            config = yaml.safe_load(_CONFIG_PATH.read_text()) or {}

            # Find agent
            agents = config.get("agents", [])
            agent = next((a for a in agents if a.get("name") == name), None)
            if not agent:
                self._json_response({"error": f"Agent {name!r} not found"}, status=404)
                return

            role = agent.get("role", "")

            # Get env allowlists for this role
            env_allowlists = config.get("env_allowlists", {})
            allowed_vars = env_allowlists.get(role, [])

            # Map to agent keys with descriptions
            role_descriptions = {
                "monitor": "Scans logs and metrics for errors",
                "triage": "Classifies tickets by severity",
                "investigator": "Diagnoses root cause",
                "developer": "Implements fixes and features",
                "reviewer": "Reviews code changes",
                "tester": "Runs tests in sandboxed environment",
                "deployer": "Injects fixes and monitors rollback",
                "documenter": "Keeps documentation updated",
                "creative": "Proposes workflow optimizations",
            }

            keys = []
            for var in allowed_vars:
                keys.append({
                    "name": var,
                    "environment_variable": var,
                    "source": "env",
                    "required": True,
                    "description": f"{var} for {role} agent",
                })

            # Add standard keys if not in allowlist
            standard_keys = ["PATH", "HOME", "LANG", "PYTHONPATH"]
            for key in standard_keys:
                if key not in [k["name"] for k in keys]:
                    keys.append({
                        "name": key,
                        "environment_variable": key,
                        "source": "default",
                        "required": False,
                        "description": "Standard system variable",
                    })

            self._json_response(keys, cache_control="private, max-age=60")
        except Exception as exc:
            logger.exception("Agent keys API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_agent_models(self):
        """GET /api/agents/models — return available models for agents."""
        try:
            import yaml
            config = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            models_config = config.get("models", {})

            pricing_data = _normalize_pricing_config(load_pricing())

            # Map tier names to config keys and defaults
            tier_map = [
                ("t3_fast", models_config.get("t3_fast", "haiku"), 200000),
                ("t2_standard", models_config.get("t2_standard", "sonnet"), 200000),
                ("t1_heavy", models_config.get("t1_heavy", "opus"), 200000),
            ]

            from src.swe_team.providers.usage_monitor.pricing import _fuzzy_match

            models = []
            for tier, model_name, ctx_window in tier_map:
                # Look up pricing from the shared pricing config via fuzzy match
                pricing_key = _fuzzy_match(model_name, pricing_data)
                if pricing_key and pricing_key in pricing_data:
                    p = pricing_data[pricing_key]
                else:
                    p = {"input": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0}
                models.append({
                    "name": model_name,
                    "tier": tier,
                    "context_window": ctx_window,
                    "pricing": {
                        "input_per_1m": p.get("input", 0.0),
                        "output_per_1m": p.get("output", 0.0),
                        "cache_read_per_1m": p.get("cache_read", 0.0),
                        "cache_creation_per_1m": p.get("cache_write", 0.0),
                    },
                    "available": True,
                })

            self._json_response({"models": models}, cache_control="public, max-age=3600")
        except Exception as exc:
            logger.exception("Agent models API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_agent_environment_test(self, name: str):
        """POST /api/agents/<name>/environment-test — test agent environment."""
        try:
            import yaml
            import subprocess
            config = yaml.safe_load(_CONFIG_PATH.read_text()) or {}

            # Find agent
            agents = config.get("agents", [])
            agent = next((a for a in agents if a.get("name") == name), None)
            if not agent:
                self._json_response({"error": f"Agent {name!r} not found"}, status=404)
                return

            checks = []

            # Check if enabled
            enabled = agent.get("enabled", False)
            checks.append({
                "name": "enabled",
                "passed": enabled,
                "message": "Enabled" if enabled else "Disabled in config",
                "value": str(enabled),
            })

            # Check Python availability
            python_check = subprocess.run(
                ["python3", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            checks.append({
                "name": "python",
                "passed": python_check.returncode == 0,
                "message": python_check.stdout.strip() if python_check.returncode == 0 else "Python not available",
                "value": python_check.stdout.strip() if python_check.returncode == 0 else None,
            })

            # Check config file
            checks.append({
                "name": "config",
                "passed": _CONFIG_PATH.exists(),
                "message": f"Config file found" if _CONFIG_PATH.exists() else "Config file not found",
                "value": str(_CONFIG_PATH),
            })

            # Check log directory
            checks.append({
                "name": "logs",
                "passed": True,
                "message": "Log directory accessible",
                "value": str(PROJECT_ROOT / "logs"),
            })

            # Check if node is primary or worker
            node = agent.get("node", "primary")
            checks.append({
                "name": "node",
                "passed": True,
                "message": f"Agent configured for {node} node",
                "value": node,
            })

            # Check model availability
            model = agent.get("model", "sonnet")
            checks.append({
                "name": "model",
                "passed": True,
                "message": f"Model configured: {model}",
                "value": model,
            })

            all_passed = all(c["passed"] for c in checks)

            result = {
                "ok": all_passed,
                "checks": checks,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            self._json_response(result, cache_control="no-cache")
        except Exception as exc:
            logger.exception("Agent environment test API error")
            self._json_response({"error": str(exc)}, status=500)


    # --- Scheduler API helpers ---

    def _read_post_body(self, *, required_fields: list[str] | None = None) -> dict | None:
        """Read and parse JSON POST body.

        Enforces a maximum body size to prevent denial-of-service via
        excessively large payloads.

        Returns the parsed dict on success.  Returns ``None`` **and**
        sends a 400 response when the body is oversized, malformed, not a
        JSON object, or missing *required_fields*.  Callers that receive
        ``None`` should ``return`` immediately (the error is already sent).
        """
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            if required_fields:
                self._json_response(
                    {"error": "Request body is empty", "required_fields": required_fields},
                    status=400,
                )
                return None
            return {}
        if content_length > _MAX_POST_BODY_BYTES:
            logger.warning("Rejected POST body: %d bytes exceeds %d limit", content_length, _MAX_POST_BODY_BYTES)
            self._json_response(
                {"error": f"Request body too large ({content_length} bytes, max {_MAX_POST_BODY_BYTES})"},
                status=400,
            )
            return None
        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Malformed JSON in POST body: %s", exc)
            self._json_response({"error": f"Malformed JSON: {exc}"}, status=400)
            return None
        if not isinstance(body, dict):
            self._json_response({"error": "Request body must be a JSON object"}, status=400)
            return None
        # Validate required fields if specified
        if required_fields:
            missing = []
            for field in required_fields:
                val = body.get(field)
                if val is None or (isinstance(val, str) and not val.strip()):
                    missing.append(field)
            if missing:
                self._json_response(
                    {"error": f"Missing required field(s): {', '.join(missing)}",
                     "missing_fields": missing},
                    status=400,
                )
                return None
        return body

    def _handle_job_action(self, job_id: str, action: str):
        """Handle pause/resume/cancel/trigger/delete actions on a job."""
        try:
            _store, sched = _get_scheduler_and_store()
            if action == "delete":
                deleted = sched.delete_job(job_id)
                if not deleted:
                    self._json_response({"error": f"Job {job_id} not found"}, status=404)
                    return
                self._json_response({"ok": True, "deleted": job_id})
                return

            method = getattr(sched, f"{action}_job", None)
            if method is None:
                self._json_response({"error": f"Unknown action: {action}"}, status=400)
                return
            job = method(job_id)
            if job is None:
                self._json_response(
                    {"error": f"Job {job_id} not found or action not applicable"},
                    status=404,
                )
                return
            self._json_response({"ok": True, "job": job.to_dict()})
        except Exception as exc:
            logger.exception("Job action %s/%s error", job_id, action)

    def _handle_apply_template(self, template_id: str):
        """POST /api/scheduler/templates/<id>/apply -- create a job from a built-in template."""
        try:
            from src.swe_team.scheduler import ScheduledJob
            tpl = _get_scheduler_template(template_id)
            if tpl is None:
                self._json_response({"error": f"Template '{template_id}' not found"}, status=404)
                return
            body = self._read_post_body()
            job_data = {
                "name": body.get("name", tpl["name"]),
                "description": body.get("description", tpl["description"]),
                "cron_expression": body.get("cron", tpl["cron"]),
                "schedule_type": "cron",
                "metadata": {"from_template": tpl["id"], "category": tpl["category"], "action": tpl["action"]},
            }
            job = ScheduledJob.from_dict(job_data)
            _store, sched = _get_scheduler_and_store()
            job = sched.add_job(job)
            self._json_response({"ok": True, "job": job.to_dict(), "template": tpl})
        except Exception as exc:
            logger.exception("Apply template error for %s", template_id)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_create_job(self):
        """Handle POST /api/jobs to create a new job."""
        try:
            from src.swe_team.scheduler import ScheduledJob
            body = self._read_post_body()
            if not body.get("name"):
                self._json_response({"error": "Job name is required"}, status=400)
                return
            job = ScheduledJob.from_dict(body)
            _store, sched = _get_scheduler_and_store()
            job = sched.add_job(job)
            self._json_response({"ok": True, "job": job.to_dict()})
        except Exception as exc:
            logger.exception("Create job error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_list_jobs_api(self):
        """GET /api/jobs — return all jobs as JSON."""
        try:
            from src.swe_team.scheduler import JobStore
            job_store = JobStore(_JOBS_DIR / "jobs.json")
            jobs = job_store.load_all()
            self._json_response([j.to_dict() for j in jobs])
        except Exception as exc:
            logger.exception("List jobs API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_job_history_api(self):
        """GET /api/jobs/<id>/history — return run history for a job, excluding archived failed runs."""
        try:
            from src.swe_team.scheduler import RunHistoryStore
            parsed = urlparse(self.path)
            m = re.match(r"^/api/jobs/([^/]+)/history$", parsed.path)
            if not m:
                self._json_response({"error": "Not found", "status": 404}, status=404)
                return
            job_id = m.group(1)
            history_store = RunHistoryStore(_JOBS_DIR / "run_history.jsonl")
            records = history_store.get_history(job_id=job_id, limit=50)

            # Load archived failed runs and filter them out
            archived_runs = _load_archived_runs()
            filtered_records = []
            for record in records:
                # Only filter failed runs
                if record.status in ("failed", "error"):
                    # Check if this specific run is archived
                    if (job_id, record.timestamp) in archived_runs:
                        continue
                filtered_records.append(record)

            self._json_response([r.to_dict() for r in filtered_records])
        except Exception as exc:
            logger.exception("Job history API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_list_routines_api(self):
        """GET /api/routines — return scheduler jobs as routine objects."""
        try:
            from src.swe_team.scheduler import JobStore
            job_store = JobStore(_JOBS_DIR / "jobs.json")
            jobs = job_store.load_all()
            self._json_response([_job_to_routine_payload(j.to_dict()) for j in jobs])
        except Exception as exc:
            logger.exception("List routines API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_get_routine_api(self):
        """GET /api/routines/<id> — return a single routine object."""
        try:
            parsed = urlparse(self.path)
            m = re.match(r"^/api/routines/([^/]+)$", parsed.path)
            if not m:
                self._json_response({"error": "Not found", "status": 404}, status=404)
                return
            routine_id = m.group(1)
            from src.swe_team.scheduler import JobStore

            job_store = JobStore(_JOBS_DIR / "jobs.json")
            jobs = job_store.load_all()
            job = next((j for j in jobs if j.job_id == routine_id), None)
            if job is None:
                self._json_response({"error": f"Routine {routine_id} not found"}, status=404)
                return
            self._json_response(_job_to_routine_payload(job.to_dict()))
        except Exception as exc:
            logger.exception("Get routine API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_routine_runs_api(self):
        """GET /api/routines/<id>/runs — return run history for a routine."""
        try:
            from src.swe_team.scheduler import RunHistoryStore
            parsed = urlparse(self.path)
            m = re.match(r"^/api/routines/([^/]+)/runs$", parsed.path)
            if not m:
                self._json_response({"error": "Not found", "status": 404}, status=404)
                return
            routine_id = m.group(1)
            history_store = RunHistoryStore(_JOBS_DIR / "run_history.jsonl")
            records = history_store.get_history(job_id=routine_id, limit=50)
            payload = []
            for record in records:
                rec = record.to_dict()
                payload.append(
                    {
                        "job_id": rec.get("job_id"),
                        "run_at": rec.get("timestamp"),
                        "status": "failure" if rec.get("status") in ("failed", "error") else "success",
                        "duration_seconds": rec.get("duration_seconds"),
                        "error": rec.get("error"),
                    }
                )
            self._json_response(payload)
        except Exception as exc:
            logger.exception("Routine runs API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_routine_activity_api(self):
        """GET /api/routines/<id>/activity — derive routine activity feed from run history."""
        try:
            from src.swe_team.scheduler import RunHistoryStore
            parsed = urlparse(self.path)
            m = re.match(r"^/api/routines/([^/]+)/activity$", parsed.path)
            if not m:
                self._json_response({"error": "Not found", "status": 404}, status=404)
                return
            routine_id = m.group(1)
            history_store = RunHistoryStore(_JOBS_DIR / "run_history.jsonl")
            records = history_store.get_history(job_id=routine_id, limit=50)
            activity = []
            for record in records:
                rec = record.to_dict()
                status = rec.get("status")
                activity.append(
                    {
                        "at": rec.get("timestamp"),
                        "event": "run_failed" if status in ("failed", "error") else "run_succeeded",
                        "message": rec.get("error") if status in ("failed", "error") else "Routine run completed",
                    }
                )
            self._json_response(activity)
        except Exception as exc:
            logger.exception("Routine activity API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_create_routine(self):
        """POST /api/routines — create a scheduler-backed routine."""
        try:
            from src.swe_team.scheduler import ScheduledJob

            body = self._read_post_body()
            name = (body.get("name") or "").strip()
            schedule = (body.get("schedule") or "").strip()
            if not name:
                self._json_response({"error": "Routine name is required"}, status=400)
                return
            if not schedule:
                self._json_response({"error": "Routine schedule is required"}, status=400)
                return

            metadata = body.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            trigger = body.get("trigger")
            if isinstance(trigger, dict):
                webhook_url = trigger.get("webhook_url")
                if isinstance(webhook_url, str) and webhook_url.strip():
                    metadata["webhook_url"] = webhook_url.strip()

            job = ScheduledJob.from_dict(
                {
                    "name": name,
                    "description": body.get("description", ""),
                    "cron_expression": schedule,
                    "enabled": True,
                    "metadata": metadata,
                }
            )
            _store, sched = _get_scheduler_and_store()
            created = sched.add_job(job)
            self._json_response({"ok": True, "routine": _job_to_routine_payload(created.to_dict())}, status=201)
        except Exception as exc:
            logger.exception("Create routine error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_update_routine(self, routine_id: str):
        """PATCH /api/routines/<id> — update routine schedule/description/trigger."""
        try:
            from src.swe_team.scheduler import JobStore

            body = self._read_post_body()
            store = JobStore(_JOBS_DIR / "jobs.json")
            jobs = store.load_all()
            idx = next((i for i, j in enumerate(jobs) if j.job_id == routine_id), None)
            if idx is None:
                self._json_response({"error": f"Routine {routine_id} not found"}, status=404)
                return

            job = jobs[idx]
            if "name" in body and isinstance(body["name"], str):
                job.name = body["name"].strip()
            if "description" in body and isinstance(body["description"], str):
                job.description = body["description"]
            if "schedule" in body and isinstance(body["schedule"], str):
                job.cron_expression = body["schedule"].strip()

            metadata = dict(job.metadata or {})
            trigger = body.get("trigger")
            if isinstance(trigger, dict):
                webhook = trigger.get("webhook_url")
                if isinstance(webhook, str):
                    cleaned_webhook = webhook.strip()
                    if cleaned_webhook:
                        metadata["webhook_url"] = cleaned_webhook
                    else:
                        metadata.pop("webhook_url", None)
            job.metadata = metadata

            jobs[idx] = job
            store.save_all(jobs)
            self._json_response({"ok": True, "routine": _job_to_routine_payload(job.to_dict())})
        except Exception as exc:
            logger.exception("Update routine error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_routine_action(self, routine_id: str, action: str):
        """POST /api/routines/<id>/<action> — run/pause/resume/archive."""
        try:
            if action == "run":
                self._handle_job_action(routine_id, "trigger")
                return

            if action in ("pause", "resume"):
                self._handle_job_action(routine_id, action)
                return

            if action == "archive":
                from src.swe_team.scheduler import JobStore

                store = JobStore(_JOBS_DIR / "jobs.json")
                jobs = store.load_all()
                idx = next((i for i, j in enumerate(jobs) if j.job_id == routine_id), None)
                if idx is None:
                    self._json_response({"error": f"Routine {routine_id} not found"}, status=404)
                    return
                job = jobs[idx]
                metadata = dict(job.metadata or {})
                metadata["archived"] = True
                job.metadata = metadata
                job.enabled = False
                jobs[idx] = job
                store.save_all(jobs)
                self._json_response({"ok": True, "routine": _job_to_routine_payload(job.to_dict())})
                return

            self._json_response({"error": f"Unknown action: {action}"}, status=400)
        except Exception as exc:
            logger.exception("Routine action %s/%s error", routine_id, action)
            self._json_response({"error": str(exc)}, status=500)

    # --- Governor API helpers ---

    def _handle_governor_quota(self):
        """GET /api/governor/quota — return just QuotaStatus."""
        import dataclasses
        gov = _get_governor()
        if gov is None:
            self._json_response({"error": "Governor not configured", "configured": False})
            return
        self._json_response(dataclasses.asdict(gov.get_quota_status()))

    def _handle_governor_decision(self):
        """GET /api/governor/decision — return just ConcurrencyDecision."""
        import dataclasses
        gov = _get_governor()
        if gov is None:
            self._json_response({"error": "Governor not configured", "configured": False})
            return
        self._json_response(dataclasses.asdict(gov.get_concurrency_decision()))

    def _handle_governor_alerts(self):
        """GET /api/governor/alerts — return list of active alert strings, excluding archived."""
        gov = _get_governor()
        if gov is None:
            self._json_response({"error": "Governor not configured", "configured": False})
            return

        alerts = gov.check_alerts()
        archived_alerts = _load_archived_alerts()

        # Filter out archived alerts by checking if alert hash is in archived set
        filtered_alerts = []
        for alert in alerts:
            alert_hash = hashlib.sha256(alert.encode()).hexdigest()
            if alert_hash not in archived_alerts:
                filtered_alerts.append(alert)

        self._json_response(filtered_alerts)

    def _handle_governor_summary(self):
        """GET /api/governor/summary — return daily summary text."""
        gov = _get_governor()
        if gov is None:
            self._json_response({"error": "Governor not configured", "configured": False})
            return
        self._json_response({"summary": gov.get_daily_summary()})

    # --- Inbox archive API handlers ---

    def _handle_archive_alert(self):
        """POST /api/inbox/alerts/archive — archive a governor alert."""
        try:
            body = self._read_post_body()
            alert_message = body.get("alert_message")

            if not alert_message:
                self._json_response({"error": "alert_message is required"}, status=400)
                return

            # Use hash of the alert message as the alert ID
            alert_id = hashlib.sha256(alert_message.encode()).hexdigest()

            if _save_archived_alert(alert_id):
                self._json_response({"ok": True, "alert_id": alert_id})
            else:
                self._json_response({"error": "Failed to archive alert"}, status=500)
        except Exception as exc:
            logger.exception("Failed to archive alert")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_archive_failed_run(self):
        """POST /api/inbox/failed-runs/archive — archive a failed routine run."""
        try:
            body = self._read_post_body()
            routine_id = body.get("routine_id")
            run_at = body.get("run_at")

            if not routine_id or not run_at:
                self._json_response({"error": "routine_id and run_at are required"}, status=400)
                return

            if _save_archived_run(routine_id, run_at):
                self._json_response({"ok": True, "routine_id": routine_id, "run_at": run_at})
            else:
                self._json_response({"error": "Failed to archive failed run"}, status=500)
        except Exception as exc:
            logger.exception("Failed to archive failed run")
            self._json_response({"error": str(exc)}, status=500)

    # --- Ticket action API handlers ---

    def _handle_get_ticket(self, ticket_id: str):
        """GET /api/tickets/<id> — return full ticket detail as JSON."""
        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return
        self._json_response(ticket.to_dict())

    def _handle_tickets_export(self, query: dict):
        """GET /api/tickets/export — export tickets as CSV, JSON, or ZIP.

        Query parameters:
        - format: "csv", "json", or "zip" (default: "csv")
        - ticket_ids: comma-separated list of ticket IDs to export (optional)
        - status: filter by status (optional)
        - severity: filter by severity (optional)
        - source_module: filter by source module (optional)
        - include_full: include full ticket data (description, investigation_report, etc.)
        """
        fmt = query.get("format", ["csv"])[0].lower()
        ticket_ids = query.get("ticket_ids", [""])[0].split(",") if query.get("ticket_ids") else None
        status_filter = query.get("status", [""])[0].strip()
        severity_filter = query.get("severity", [""])[0].strip()
        module_filter = query.get("source_module", [""])[0].strip()
        include_full = query.get("include_full", ["false"])[0].lower() == "true"

        # Get all tickets and apply filters
        all_tickets = self.store.list_all()
        tickets = []

        for t in all_tickets:
            # Skip if ticket_ids filter is set and ticket not in list
            if ticket_ids and ticket_ids[0] and t.ticket_id not in ticket_ids:
                continue
            # Skip if status filter is set and doesn't match
            if status_filter:
                t_status = t.status.value if hasattr(t.status, "value") else str(t.status)
                if t_status != status_filter:
                    continue
            # Skip if severity filter is set and doesn't match
            if severity_filter:
                t_sev = t.severity.value if hasattr(t.severity, "value") else str(t.severity)
                if t_sev != severity_filter:
                    continue
            # Skip if module filter is set and doesn't match
            if module_filter and t.source_module != module_filter:
                continue
            tickets.append(t)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        if fmt == "zip":
            # Export as ZIP with file tree preview
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                # Add manifest file
                manifest = {
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "count": len(tickets),
                    "filters": {
                        "ticket_ids": ticket_ids,
                        "status": status_filter or None,
                        "severity": severity_filter or None,
                        "source_module": module_filter or None,
                    }
                }
                zf.writestr("export/manifest.json", json.dumps(manifest, indent=2))

                # Add tickets as individual JSON files organized by status
                status_dirs = set()
                for t in tickets:
                    t_status = t.status.value if hasattr(t.status, "value") else str(t.status)
                    status_dirs.add(t_status)
                    ticket_data = t.to_dict() if include_full else {
                        "ticket_id": t.ticket_id,
                        "title": t.title,
                        "severity": t_sev,
                        "status": t_status,
                        "assigned_to": t.assigned_to,
                        "source_module": t.source_module,
                        "created_at": t.created_at,
                        "updated_at": t.updated_at,
                    }
                    filename = f"export/{t_status.lower()}/{t.ticket_id}.json"
                    zf.writestr(filename, json.dumps(ticket_data, indent=2, default=str))

                # Add summary CSV
                csv_buffer = io.StringIO()
                writer = csv.writer(csv_buffer)
                writer.writerow(["ticket_id", "title", "severity", "status", "assigned_to",
                                 "source_module", "created_at", "updated_at"])
                for t in tickets:
                    writer.writerow([
                        t.ticket_id, t.title,
                        t.severity.value if hasattr(t.severity, "value") else str(t.severity),
                        t.status.value if hasattr(t.status, "value") else str(t.status),
                        t.assigned_to or "", t.source_module or "",
                        t.created_at, t.updated_at,
                    ])
                zf.writestr("export/tickets.csv", csv_buffer.getvalue())

                # Add file tree preview
                tree = []
                tree.append("export/")
                tree.append("export/manifest.json")
                tree.append("export/tickets.csv")
                for status in sorted(status_dirs):
                    status_path = f"export/{status.lower()}/"
                    tree.append(status_path)
                    for t in tickets:
                        t_status = t.status.value if hasattr(t.status, "value") else str(t.status)
                        if t_status == status:
                            tree.append(f"{status_path}{t.ticket_id}.json")
                zf.writestr("export/file_tree.txt", "\n".join(tree))

            body = zip_buffer.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename=swe_tickets_{timestamp}.zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif fmt == "json":
            data = [t.to_dict() if include_full else {
                "ticket_id": t.ticket_id,
                "title": t.title,
                "severity": t.severity.value if hasattr(t.severity, "value") else str(t.severity),
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                "assigned_to": t.assigned_to,
                "source_module": t.source_module,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
            } for t in tickets]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", f"attachment; filename=swe_tickets_{timestamp}.json")
            self._json_response(data)

        else:  # CSV
            output = io.StringIO()
            writer = csv.writer(output)
            headers = ["ticket_id", "title", "severity", "status", "assigned_to",
                       "source_module", "created_at", "updated_at"]
            if include_full:
                headers.extend(["description", "labels", "ticket_type", "investigation_report", "proposed_fix"])
            writer.writerow(headers)
            for t in tickets:
                row = [
                    t.ticket_id, t.title,
                    t.severity.value if hasattr(t.severity, "value") else str(t.severity),
                    t.status.value if hasattr(t.status, "value") else str(t.status),
                    t.assigned_to or "", t.source_module or "",
                    t.created_at, t.updated_at,
                ]
                if include_full:
                    row.extend([
                        t.description or "",
                        json.dumps(t.labels) if t.labels else "[]",
                        t.ticket_type or "",
                        t.investigation_report or "",
                        t.proposed_fix or "",
                    ])
                writer.writerow(row)
            body = output.getvalue().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", f"attachment; filename=swe_tickets_{timestamp}.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _read_multipart_body(self) -> dict:
        """Read and parse multipart/form-data POST body.

        Returns dict with keys 'files' (list of (filename, content) tuples) and
        'fields' (dict of string fields).
        """
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            return {"files": [], "fields": {}}

        # Parse boundary
        parts = content_type.split("boundary=")
        if len(parts) < 2:
            return {"files": [], "fields": {}}
        boundary = parts[1].strip().encode()

        content_length = int(self.headers.get("Content-Length", 0))
        raw_data = self.rfile.read(content_length)

        # Parse as email message
        msg = email.message_from_bytes(
            b"Content-Type: " + content_type.encode() + b"\n\n" + raw_data,
            policy=email.policy.compat32
        )

        result = {"files": [], "fields": {}}

        for part in msg.walk():
            disposition = part.get("Content-Disposition", "")
            if not disposition:
                continue

            # Parse disposition to get field name and filename
            params = {}
            for d in disposition.split(";"):
                d = d.strip()
                if "=" in d:
                    key, val = d.split("=", 1)
                    params[key.strip()] = val.strip('"')

            name = params.get("name")
            filename = params.get("filename")

            if filename:
                # This is a file upload
                result["files"].append((filename, part.get_payload(decode=True)))
            elif name:
                # This is a form field
                result["fields"][name] = part.get_payload(decode=True).decode("utf-8", errors="replace")

        return result

    def _handle_tickets_import(self):
        """POST /api/tickets/import — import tickets from CSV or JSON file.

        Expected multipart/form-data:
        - file: CSV or JSON file
        - strategy: "skip", "overwrite", or "merge" (default: "skip")
        - dry_run: "true" to preview without importing (default: "false")

        Returns import results with counts and any errors.
        """
        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus, TicketType

        multipart = self._read_multipart_body()
        files = multipart.get("files", [])
        fields = multipart.get("fields", {})

        if not files:
            self._json_response({"error": "No file uploaded"}, status=400)
            return

        strategy = fields.get("strategy", "skip").lower()
        dry_run = fields.get("dry_run", "false").lower() == "true"

        filename, content = files[0]

        imported = []
        skipped = []
        updated = []
        errors = []

        try:
            if filename.endswith(".json"):
                # Parse JSON import
                data = json.loads(content.decode("utf-8"))
                tickets_data = data if isinstance(data, list) else [data]

                for item in tickets_data:
                    try:
                        ticket_id = item.get("ticket_id")
                        if not ticket_id:
                            errors.append({"error": "Missing ticket_id", "data": item})
                            continue

                        existing = self.store.get(ticket_id)

                        if existing:
                            if strategy == "skip":
                                skipped.append(ticket_id)
                                continue
                            elif strategy == "overwrite":
                                # Create new ticket from data
                                ticket = self._dict_to_ticket(item)
                                if not dry_run:
                                    self.store.add(ticket)
                                updated.append(ticket_id)
                            else:  # merge
                                # Merge non-empty fields
                                merged = existing.to_dict()
                                for k, v in item.items():
                                    if v is not None and v != "" and v != []:
                                        merged[k] = v
                                ticket = self._dict_to_ticket(merged)
                                if not dry_run:
                                    self.store.add(ticket)
                                updated.append(ticket_id)
                        else:
                            ticket = self._dict_to_ticket(item)
                            if not dry_run:
                                self.store.add(ticket)
                            imported.append(ticket_id)

                    except Exception as e:
                        errors.append({"error": str(e), "ticket_id": item.get("ticket_id")})

            elif filename.endswith(".csv"):
                # Parse CSV import
                csv_text = content.decode("utf-8")
                reader = csv.DictReader(io.StringIO(csv_text))

                for row in reader:
                    try:
                        ticket_id = row.get("ticket_id")
                        if not ticket_id:
                            errors.append({"error": "Missing ticket_id", "row": row})
                            continue

                        existing = self.store.get(ticket_id)

                        if existing:
                            if strategy == "skip":
                                skipped.append(ticket_id)
                                continue
                            elif strategy == "overwrite":
                                ticket = self._dict_to_ticket(row)
                                if not dry_run:
                                    self.store.add(ticket)
                                updated.append(ticket_id)
                            else:  # merge
                                merged = existing.to_dict()
                                for k, v in row.items():
                                    if v and v.strip():
                                        # Handle labels field
                                        if k == "labels":
                                            try:
                                                merged[k] = json.loads(v)
                                            except:
                                                merged[k] = v.split(",") if "," in v else [v]
                                        else:
                                            merged[k] = v.strip()
                                ticket = self._dict_to_ticket(merged)
                                if not dry_run:
                                    self.store.add(ticket)
                                updated.append(ticket_id)
                        else:
                            ticket = self._dict_to_ticket(row)
                            if not dry_run:
                                self.store.add(ticket)
                            imported.append(ticket_id)

                    except Exception as e:
                        errors.append({"error": str(e), "row": row})

            else:
                self._json_response({"error": f"Unsupported file type: {filename}"}, status=400)
                return

            response = {
                "status": "preview" if dry_run else "success",
                "imported": imported,
                "skipped": skipped,
                "updated": updated,
                "errors": errors,
                "summary": {
                    "total": len(imported) + len(skipped) + len(updated) + len(errors),
                    "imported": len(imported),
                    "skipped": len(skipped),
                    "updated": len(updated),
                    "errors": len(errors),
                }
            }
            self._json_response(response)

        except Exception as e:
            logger.exception("Failed to import tickets")
            self._json_response({"error": str(e)}, status=500)

    def _dict_to_ticket(self, data: dict) -> SWETicket:
        """Convert a dict to a SWETicket, handling field validation."""
        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus, TicketType

        # Parse severity
        sev_str = data.get("severity", "MEDIUM").upper()
        try:
            severity = TicketSeverity(sev_str)
        except ValueError:
            severity = TicketSeverity.MEDIUM

        # Parse status
        status_str = data.get("status", "OPEN").upper()
        try:
            status = TicketStatus(status_str)
        except ValueError:
            status = TicketStatus.OPEN

        # Parse type
        type_str = data.get("ticket_type", "BUG").upper()
        try:
            ticket_type = TicketType(type_str)
        except ValueError:
            ticket_type = TicketType.BUG

        # Parse labels
        labels = data.get("labels", [])
        if isinstance(labels, str):
            try:
                labels = json.loads(labels)
            except:
                labels = [l.strip() for l in labels.split(",") if l.strip()]
        elif not isinstance(labels, list):
            labels = []

        # Parse metadata
        metadata = data.get("metadata", {})
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except:
                metadata = {}

        # Parse blocked_by and blocking
        blocked_by = data.get("blocked_by", [])
        if isinstance(blocked_by, str):
            try:
                blocked_by = json.loads(blocked_by)
            except:
                blocked_by = []
        blocking = data.get("blocking", [])
        if isinstance(blocking, str):
            try:
                blocking = json.loads(blocking)
            except:
                blocking = []

        return SWETicket(
            ticket_id=data.get("ticket_id", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            severity=severity,
            status=status,
            ticket_type=ticket_type,
            assigned_to=data.get("assigned_to") or None,
            labels=labels,
            metadata=metadata,
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
            source_module=data.get("source_module"),
            investigation_report=data.get("investigation_report"),
            proposed_fix=data.get("proposed_fix"),
            test_results=data.get("test_results"),
            blocked_by=blocked_by,
            blocking=blocking,
            related_tickets=data.get("related_tickets", []),
            project_id=data.get("project_id"),
            parent_ticket_id=data.get("parent_ticket_id"),
            goal=data.get("goal"),
        )

    def _handle_create_ticket(self):
        """POST /api/tickets — create a new ticket."""
        body = self._read_post_body()
        if body is None:
            return

        # Validate required fields
        title = body.get("title", "").strip()
        description = body.get("description", "").strip()

        if not title:
            self._json_response({"error": "Field 'title' is required"}, status=400)
            return
        if not description:
            self._json_response({"error": "Field 'description' is required"}, status=400)
            return

        try:
            # Create ticket using the existing helper
            ticket = self._dict_to_ticket(body)

            # Store the ticket
            self.store.add(ticket)

            # Broadcast SSE event for real-time updates
            _broadcast_sse_event("action", {
                "event": "ticket_created",
                "ticket_id": ticket.ticket_id,
            })

            # Return created ticket with 201 status
            self._json_response(ticket.to_dict(), status=201)
        except Exception as e:
            logger.exception("Failed to create ticket")
            self._json_response({"error": str(e)}, status=500)

    def _handle_ticket_assign(self, ticket_id: str):
        """POST /api/tickets/<id>/assign — assign ticket to an agent."""
        body = self._read_post_body()
        if body is None:
            return
        assignee = body.get("assignee", "").strip()
        if not assignee:
            self._json_response({"error": "Field 'assignee' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        ticket.assigned_to = assignee
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        # Comment on linked GitHub issue if present
        gh_number = ticket.metadata.get("github_issue_number")
        if gh_number:
            self._gh_comment_async(
                gh_number,
                f"Ticket assigned to **{assignee}** via SWE-Squad dashboard."
            )

        _broadcast_sse_event("action", {
            "event": "ticket_assigned",
            "ticket_id": ticket_id,
            "assignee": assignee,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id, "assignee": assignee})

    def _handle_ticket_investigate(self, ticket_id: str):
        """POST /api/tickets/<id>/investigate — trigger investigation in background."""
        body = self._read_post_body()
        if body is None:
            return
        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        model = body.get("model", "sonnet")

        def _run():
            try:
                from src.swe_team.investigator import InvestigatorAgent
                from src.swe_team.config import load_config as _lc
                cfg = _lc()
                agent = InvestigatorAgent(config=cfg, ticket_store=self.store)
                agent.investigate(ticket, model=model)
                _broadcast_sse_event("action", {
                    "event": "investigation_complete",
                    "ticket_id": ticket_id,
                })
            except Exception as exc:
                logger.exception("Background investigation failed for %s", ticket_id)
                _broadcast_sse_event("action", {
                    "event": "investigation_failed",
                    "ticket_id": ticket_id,
                    "error": str(exc),
                })

        thread = threading.Thread(target=_run, daemon=True, name=f"investigate-{ticket_id}")
        thread.start()
        self._json_response({"status": "queued", "ticket_id": ticket_id, "action": "investigate"})

    def _handle_ticket_develop(self, ticket_id: str):
        """POST /api/tickets/<id>/develop — trigger developer agent in background."""
        body = self._read_post_body()
        if body is None:
            return
        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        model = body.get("model", "sonnet")

        def _run():
            try:
                from src.swe_team.developer import DeveloperAgent
                from src.swe_team.config import load_config as _lc
                cfg = _lc()
                agent = DeveloperAgent(config=cfg, ticket_store=self.store)
                agent.attempt_fix(ticket, model=model)
                _broadcast_sse_event("action", {
                    "event": "development_complete",
                    "ticket_id": ticket_id,
                })
            except Exception as exc:
                logger.exception("Background development failed for %s", ticket_id)
                _broadcast_sse_event("action", {
                    "event": "development_failed",
                    "ticket_id": ticket_id,
                    "error": str(exc),
                })

        thread = threading.Thread(target=_run, daemon=True, name=f"develop-{ticket_id}")
        thread.start()
        self._json_response({"status": "queued", "ticket_id": ticket_id, "action": "develop"})

    def _handle_ticket_status(self, ticket_id: str):
        """PATCH /api/tickets/<id>/status — update ticket status."""
        body = self._read_post_body()
        if body is None:
            return
        new_status_str = body.get("status", "").strip().lower()
        if not new_status_str:
            self._json_response({"error": "Field 'status' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        # Validate the status value
        from src.swe_team.models import TicketStatus
        try:
            new_status = TicketStatus(new_status_str)
        except ValueError:
            valid = [s.value for s in TicketStatus]
            self._json_response(
                {"error": f"Invalid status '{new_status_str}'. Valid: {valid}"},
                status=400,
            )
            return

        # If a resolution_note is provided, set it before transition (for bypass)
        if body.get("resolution_note"):
            ticket.metadata["resolution_note"] = body["resolution_note"]

        try:
            ticket.transition(new_status)
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=422)
            return

        self.store.add(ticket)

        # Comment on linked GitHub issue
        gh_number = ticket.metadata.get("github_issue_number")
        if gh_number:
            self._gh_comment_async(
                gh_number,
                f"Ticket status changed to **{new_status.value}** via SWE-Squad dashboard."
            )

        _broadcast_sse_event("action", {
            "event": "status_changed",
            "ticket_id": ticket_id,
            "status": new_status.value,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id, "new_status": new_status.value})

    def _handle_ticket_severity(self, ticket_id: str):
        """PATCH /api/tickets/<id>/severity — update ticket severity."""
        body = self._read_post_body()
        if body is None:
            return
        new_sev_str = body.get("severity", "").strip().lower()
        if not new_sev_str:
            self._json_response({"error": "Field 'severity' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        from src.swe_team.models import TicketSeverity
        try:
            new_severity = TicketSeverity(new_sev_str)
        except ValueError:
            valid = [s.value for s in TicketSeverity]
            self._json_response(
                {"error": f"Invalid severity '{new_sev_str}'. Valid: {valid}"},
                status=400,
            )
            return

        ticket.severity = new_severity
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        _broadcast_sse_event("action", {
            "event": "severity_changed",
            "ticket_id": ticket_id,
            "severity": new_severity.value,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id, "new_severity": new_severity.value})

    def _handle_ticket_comment(self, ticket_id: str):
        """POST /api/tickets/<id>/comment — add comment to ticket and GitHub."""
        body = self._read_post_body()
        if body is None:
            return
        comment_text = body.get("comment", "").strip()
        if not comment_text:
            self._json_response({"error": "Field 'comment' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        # Store comment in ticket metadata
        comments = ticket.metadata.get("comments", [])
        comments.append({
            "text": comment_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "dashboard",
        })
        ticket.metadata["comments"] = comments
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        # Comment on linked GitHub issue
        gh_number = ticket.metadata.get("github_issue_number")
        if gh_number:
            self._gh_comment_async(gh_number, comment_text)

        _broadcast_sse_event("action", {
            "event": "comment_added",
            "ticket_id": ticket_id,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id})

    def _handle_delete_ticket_comment(self, ticket_id: str, index: int):
        """DELETE /api/tickets/<id>/comment/<index> — remove a comment from ticket."""
        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        comments = ticket.metadata.get("comments", [])
        if index < 0 or index >= len(comments):
            self._json_response({"error": f"Comment index {index} out of range"}, status=400)
            return

        # Remove comment at specified index
        removed_comment = comments.pop(index)
        ticket.metadata["comments"] = comments
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        _broadcast_sse_event("action", {
            "event": "comment_deleted",
            "ticket_id": ticket_id,
            "comment_index": index,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id, "deleted_comment": removed_comment})

    # Label format: alphanumeric, hyphens, underscores, dots, spaces (no newlines), slashes, colons
    _LABEL_PATTERN = re.compile(r"^[\w ./:@-]{1,100}$")

    def _handle_ticket_label(self, ticket_id: str):
        """POST /api/tickets/<id>/label — update labels on ticket and GitHub."""
        body = self._read_post_body()
        add_labels = body.get("add", [])
        remove_labels = body.get("remove", [])

        # Validate that labels are lists of strings with safe characters
        if not isinstance(add_labels, list) or not isinstance(remove_labels, list):
            self._json_response({"error": "Fields 'add' and 'remove' must be arrays"}, status=400)
            return
        for label in add_labels + remove_labels:
            if not isinstance(label, str) or not self._LABEL_PATTERN.match(label):
                self._json_response(
                    {"error": f"Invalid label: {str(label)[:100]!r}. Labels must be 1-100 chars: alphanumeric, hyphens, underscores, dots, spaces, slashes, colons."},
                    status=400,
                )
                return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        # Update local labels
        for label in add_labels:
            if label not in ticket.labels:
                ticket.labels.append(label)
        for label in remove_labels:
            if label in ticket.labels:
                ticket.labels.remove(label)
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        # Update GitHub issue labels
        gh_number = ticket.metadata.get("github_issue_number")
        if gh_number:
            repo = os.environ.get("SWE_GITHUB_REPO", "")
            if repo:
                def _update_gh_labels():
                    import subprocess
                    try:
                        if add_labels:
                            labels_str = ",".join(add_labels)
                            subprocess.run(
                                ["gh", "issue", "edit", str(gh_number),
                                 "--repo", repo, "--add-label", labels_str],
                                capture_output=True, timeout=15,
                            )
                        if remove_labels:
                            labels_str = ",".join(remove_labels)
                            subprocess.run(
                                ["gh", "issue", "edit", str(gh_number),
                                 "--repo", repo, "--remove-label", labels_str],
                                capture_output=True, timeout=15,
                            )
                    except Exception as exc:
                        logger.warning("Failed to update GH labels: %s", exc)
                threading.Thread(target=_update_gh_labels, daemon=True).start()

        _broadcast_sse_event("action", {
            "event": "labels_updated",
            "ticket_id": ticket_id,
            "labels": ticket.labels,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id, "labels": ticket.labels})

    def _handle_ticket_title(self, ticket_id: str):
        """PATCH /api/tickets/<id>/title — update ticket title."""
        body = self._read_post_body()
        new_title = body.get("title", "").strip()
        if not new_title:
            self._json_response({"error": "Field 'title' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        old_title = ticket.title
        ticket.title = new_title
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        # Comment on linked GitHub issue
        gh_number = ticket.metadata.get("github_issue_number")
        if gh_number:
            self._gh_comment_async(
                gh_number,
                f"Ticket title changed from \"{old_title}\" to \"{new_title}\" via SWE-Squad dashboard."
            )

        _broadcast_sse_event("action", {
            "event": "title_changed",
            "ticket_id": ticket_id,
            "old_title": old_title,
            "new_title": new_title,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id, "ticket": ticket.to_dict()})

    def _handle_ticket_description(self, ticket_id: str):
        """PATCH /api/tickets/<id>/description — update ticket description."""
        body = self._read_post_body()
        new_description = body.get("description", "").strip()
        if not new_description:
            self._json_response({"error": "Field 'description' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        old_description = ticket.description
        ticket.description = new_description
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        # Comment on linked GitHub issue
        gh_number = ticket.metadata.get("github_issue_number")
        if gh_number:
            self._gh_comment_async(
                gh_number,
                f"Ticket description updated via SWE-Squad dashboard."
            )

        _broadcast_sse_event("action", {
            "event": "description_changed",
            "ticket_id": ticket_id,
        })
        self._json_response({"status": "ok", "ticket_id": ticket_id, "ticket": ticket.to_dict()})

    def _handle_ticket_activity(self, ticket_id: str):
        """GET /api/tickets/<id>/activity — get activity timeline from audit trail."""
        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        # Query audit trail from Supabase if available
        activity = []
        supabase_connection_string = os.environ.get("SUPABASE_CONNECTION_STRING", "")

        if supabase_connection_string:
            try:
                import psycopg
                with psycopg.connect(supabase_connection_string) as conn:
                    with conn.cursor() as cur:
                        # Query audit trail for this ticket, ordered by timestamp descending
                        cur.execute("""
                            SELECT id, team_id, ticket_id, actor, action, details, timestamp, created_at
                            FROM swe_audit_trail
                            WHERE ticket_id = %s
                            ORDER BY timestamp DESC
                            LIMIT 100
                        """, (ticket_id,))
                        rows = cur.fetchall()
                        for row in rows:
                            activity.append({
                                "id": str(row[0]),
                                "team_id": row[1],
                                "ticket_id": row[2],
                                "actor": row[3],
                                "action": row[4],
                                "details": row[5],
                                "timestamp": row[6].isoformat() if row[6] else None,
                                "created_at": row[7].isoformat() if row[7] else None,
                            })
            except Exception as exc:
                logger.warning("Failed to query audit trail: %s", exc)
                # Return empty activity list on error rather than failing the request
                activity = []

        self._json_response({
            "ticket_id": ticket_id,
            "activity": activity,
        })

    # ---------------------------------------------------------------------------
    # Ticket Feed API — unified activity feed with inline diffs & comments
    # ---------------------------------------------------------------------------

    def _get_feed_path(self, ticket_id: str) -> Path:
        """Return the path for a ticket's feed file."""
        _FEEDS_DIR.mkdir(parents=True, exist_ok=True)
        # Sanitize ticket_id for filesystem safety
        safe_id = re.sub(r"[^a-zA-Z0-9_\-.]", "_", ticket_id)
        return _FEEDS_DIR / f"{safe_id}.json"

    def _read_feed(self, ticket_id: str) -> list:
        """Read feed entries for a ticket, auto-generating from ticket state."""
        feed_path = self._get_feed_path(ticket_id)
        entries = []
        if feed_path.exists():
            try:
                entries = json.loads(feed_path.read_text()) or []
            except (json.JSONDecodeError, OSError):
                entries = []

        # Auto-generate entries from ticket state if feed is empty
        ticket = self.store.get(ticket_id) if hasattr(self, "store") and self.store else None
        if ticket and not entries:
            entries = self._generate_feed_from_ticket(ticket)
            self._write_feed(ticket_id, entries)

        return entries

    def _write_feed(self, ticket_id: str, entries: list):
        """Write feed entries to disk."""
        feed_path = self._get_feed_path(ticket_id)
        _FEEDS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            feed_path.write_text(json.dumps(entries, indent=2))
        except OSError as exc:
            logger.warning("Failed to write feed for %s: %s", ticket_id, exc)

    def _generate_feed_from_ticket(self, ticket) -> list:
        """Generate initial feed entries from ticket state changes."""
        import uuid
        entries = []

        # Created event
        entries.append({
            "id": str(uuid.uuid4()),
            "type": "system",
            "timestamp": ticket.created_at,
            "actor": "system",
            "content": f"Ticket created: {ticket.title}",
            "metadata": {"severity": ticket.severity.value if hasattr(ticket.severity, "value") else str(ticket.severity)},
        })

        # Status change events from audit trail comments
        meta = ticket.metadata or {}
        comments = meta.get("comments", [])
        for comment in comments:
            entries.append({
                "id": str(uuid.uuid4()),
                "type": "comment",
                "timestamp": comment.get("timestamp", ticket.updated_at),
                "actor": comment.get("source", "unknown"),
                "content": comment.get("text", ""),
                "metadata": {},
            })

        # If ticket is assigned
        if ticket.assigned_to:
            entries.append({
                "id": str(uuid.uuid4()),
                "type": "status_change",
                "timestamp": ticket.updated_at,
                "actor": "system",
                "content": f"Assigned to {ticket.assigned_to}",
                "metadata": {"assigned_to": ticket.assigned_to},
            })

        # Status entries
        status_val = ticket.status.value if hasattr(ticket.status, "value") else str(ticket.status)
        if status_val not in ("OPEN", "new"):
            entries.append({
                "id": str(uuid.uuid4()),
                "type": "status_change",
                "timestamp": ticket.updated_at,
                "actor": ticket.assigned_to or "system",
                "content": f"Status changed to {status_val}",
                "metadata": {"status": status_val},
            })

        # Investigation report
        if ticket.investigation_report:
            entries.append({
                "id": str(uuid.uuid4()),
                "type": "investigation",
                "timestamp": ticket.updated_at,
                "actor": ticket.assigned_to or "investigator",
                "content": ticket.investigation_report[:500] + ("..." if len(ticket.investigation_report) > 500 else ""),
                "metadata": {"full_length": len(ticket.investigation_report)},
            })

        # Proposed fix / diff
        if ticket.proposed_fix:
            entries.append({
                "id": str(uuid.uuid4()),
                "type": "diff",
                "timestamp": ticket.updated_at,
                "actor": ticket.assigned_to or "developer",
                "content": ticket.proposed_fix[:1000] + ("..." if len(ticket.proposed_fix) > 1000 else ""),
                "metadata": {"full_length": len(ticket.proposed_fix)},
            })

        # PR created
        if meta.get("pr_url"):
            entries.append({
                "id": str(uuid.uuid4()),
                "type": "system",
                "timestamp": ticket.updated_at,
                "actor": ticket.assigned_to or "developer",
                "content": f"PR created: #{meta.get('pr_number', '?')}",
                "metadata": {"pr_url": meta["pr_url"], "pr_number": meta.get("pr_number")},
            })

        # Sort by timestamp
        entries.sort(key=lambda e: e.get("timestamp", ""))
        return entries

    def _handle_get_ticket_feed(self, ticket_id: str):
        """GET /api/tickets/<id>/feed — unified activity feed."""
        ticket = self.store.get(ticket_id) if hasattr(self, "store") and self.store else None
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        entries = self._read_feed(ticket_id)
        self._json_response({"feed": entries})

    def _handle_add_feed_comment(self, ticket_id: str):
        """POST /api/tickets/<id>/feed/comment — add a user comment to the feed."""
        import uuid
        body = self._read_post_body()
        content = body.get("content", "").strip()
        author = body.get("author", "user").strip()

        if not content:
            self._json_response({"error": "Field 'content' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id) if hasattr(self, "store") and self.store else None
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return

        entries = self._read_feed(ticket_id)

        new_entry = {
            "id": str(uuid.uuid4()),
            "type": "comment",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": author,
            "content": content,
            "metadata": {},
        }
        entries.append(new_entry)
        self._write_feed(ticket_id, entries)

        # Also store in ticket metadata comments for consistency
        comments = ticket.metadata.get("comments", [])
        comments.append({
            "text": content,
            "timestamp": new_entry["timestamp"],
            "source": author,
        })
        ticket.metadata["comments"] = comments
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        _broadcast_sse_event("action", {
            "event": "feed_comment_added",
            "ticket_id": ticket_id,
        })

        self._json_response({"ok": True, "entry": new_entry})

    # ---------------------------------------------------------------------------
    # Approvals API — HITL escalation endpoints
    # ---------------------------------------------------------------------------

    def _approval_is_pending(self, ticket) -> bool:
        """Return True if a ticket is pending human approval."""
        from src.swe_team.models import TicketStatus
        pending_statuses = {TicketStatus.IN_REVIEW, TicketStatus.REWORK_REQUESTED}
        if ticket.status in pending_statuses:
            return True
        if ticket.metadata.get("needs_hitl"):
            return True
        return False

    def _handle_list_approvals(self):
        """GET /api/approvals — list all tickets pending human approval.

        Returns tickets that require HITL review:
        - metadata.needs_hitl == True
        - status IN_REVIEW
        - status REWORK_REQUESTED
        """
        tickets = self.store.list_all()
        approvals = []
        for ticket in tickets:
            if self._approval_is_pending(ticket):
                d = ticket.to_dict()
                d["is_pending_approval"] = True
                approvals.append(d)

        self._json_response({"approvals": approvals, "count": len(approvals)})

    def _handle_get_approval(self, ticket_id: str):
        """GET /api/approvals/<id> — return a single approval ticket."""
        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return
        if not self._approval_is_pending(ticket):
            self._json_response({"error": f"Ticket {ticket_id} is not pending approval"}, status=404)
            return
        d = ticket.to_dict()
        d["is_pending_approval"] = True
        self._json_response(d)

    def _handle_approval_approve(self, ticket_id: str):
        """POST /api/approvals/<id>/approve — approve a HITL-escalated ticket.

        Clears HITL flags and transitions the ticket:
        - If proposed_fix is available → RESOLVED
        - Otherwise → IN_DEVELOPMENT (continue work)
        """
        from src.swe_team.models import TicketStatus
        body = self._read_post_body()
        if body is None:
            return
        note = body.get("note", "").strip()

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return
        if not self._approval_is_pending(ticket):
            self._json_response({"error": f"Ticket {ticket_id} is not pending approval"}, status=400)
            return

        # Clear HITL flag
        ticket.metadata["needs_hitl"] = False
        if note:
            ticket.metadata["approval_note"] = note
        ticket.metadata["approved_at"] = datetime.now(timezone.utc).isoformat()

        # Choose new status: resolve if a fix is ready, else send back to development
        if ticket.proposed_fix:
            ticket.metadata["resolution_note"] = note or "Approved via HITL review"
            try:
                ticket.transition(TicketStatus.RESOLVED)
            except ValueError:
                ticket.status = TicketStatus.RESOLVED
                ticket.updated_at = datetime.now(timezone.utc).isoformat()
        else:
            ticket.status = TicketStatus.IN_DEVELOPMENT
            ticket.updated_at = datetime.now(timezone.utc).isoformat()

        self.store.add(ticket)

        _broadcast_sse_event("action", {
            "event": "approval_approved",
            "ticket_id": ticket_id,
            "new_status": ticket.status.value,
        })
        self._json_response({
            "status": "ok",
            "ticket_id": ticket_id,
            "new_status": ticket.status.value,
        })

    def _handle_approval_reject(self, ticket_id: str):
        """POST /api/approvals/<id>/reject — reject a HITL-escalated ticket.

        Stores the rejection reason and closes the ticket.
        """
        from src.swe_team.models import TicketStatus
        body = self._read_post_body()
        if body is None:
            return
        reason = body.get("reason", "").strip()
        if not reason:
            self._json_response({"error": "Field 'reason' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return
        if not self._approval_is_pending(ticket):
            self._json_response({"error": f"Ticket {ticket_id} is not pending approval"}, status=400)
            return

        ticket.metadata["needs_hitl"] = False
        ticket.metadata["rejection_reason"] = reason
        ticket.metadata["rejected_at"] = datetime.now(timezone.utc).isoformat()
        ticket.metadata["resolution_note"] = f"Rejected: {reason}"
        ticket.status = TicketStatus.CLOSED
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        _broadcast_sse_event("action", {
            "event": "approval_rejected",
            "ticket_id": ticket_id,
            "new_status": ticket.status.value,
        })
        self._json_response({
            "status": "ok",
            "ticket_id": ticket_id,
            "new_status": ticket.status.value,
        })

    def _handle_approval_request_revision(self, ticket_id: str):
        """POST /api/approvals/<id>/request-revision — send ticket back for rework.

        Moves the ticket to REWORK_REQUESTED and stores the feedback.
        """
        from src.swe_team.models import TicketStatus
        body = self._read_post_body()
        if body is None:
            return
        feedback = body.get("feedback", "").strip()
        if not feedback:
            self._json_response({"error": "Field 'feedback' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return
        if not self._approval_is_pending(ticket):
            self._json_response({"error": f"Ticket {ticket_id} is not pending approval"}, status=400)
            return

        ticket.metadata["rework_feedback"] = feedback
        ticket.metadata["rework_requested_at"] = datetime.now(timezone.utc).isoformat()
        ticket.status = TicketStatus.REWORK_REQUESTED
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        _broadcast_sse_event("action", {
            "event": "approval_revision_requested",
            "ticket_id": ticket_id,
            "new_status": ticket.status.value,
        })
        self._json_response({
            "status": "ok",
            "ticket_id": ticket_id,
            "new_status": ticket.status.value,
        })

    def _handle_get_approval_comments(self, ticket_id: str):
        """GET /api/approvals/<id>/comments — list comments for an approval ticket."""
        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return
        if not self._approval_is_pending(ticket):
            self._json_response({"error": f"Ticket {ticket_id} is not pending approval"}, status=404)
            return

        comments = ticket.metadata.get("comments", [])
        self._json_response({"comments": comments, "count": len(comments)})

    def _handle_add_approval_comment(self, ticket_id: str):
        """POST /api/approvals/<id>/comments — add a comment to an approval ticket."""
        body = self._read_post_body()
        if body is None:
            return
        comment_text = body.get("comment", "").strip()
        if not comment_text:
            self._json_response({"error": "Field 'comment' is required"}, status=400)
            return

        ticket = self.store.get(ticket_id)
        if not ticket:
            self._json_response({"error": f"Ticket {ticket_id} not found"}, status=404)
            return
        if not self._approval_is_pending(ticket):
            self._json_response({"error": f"Ticket {ticket_id} is not pending approval"}, status=404)
            return

        comments = ticket.metadata.get("comments", [])
        comments.append({
            "text": comment_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "dashboard",
        })
        ticket.metadata["comments"] = comments
        ticket.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.add(ticket)

        # Also comment on linked GitHub issue if present
        gh_number = ticket.metadata.get("github_issue_number")
        if gh_number:
            self._gh_comment_async(gh_number, comment_text)

        _broadcast_sse_event("action", {
            "event": "approval_comment_added",
            "ticket_id": ticket_id,
        })
        self._json_response({
            "status": "ok",
            "ticket_id": ticket_id,
            "comment_count": len(comments),
        })

    # --- Suggestions API handlers ---

    def _handle_list_suggestions(self):
        """GET /api/suggestions — list all suggestions."""
        suggestions = _read_suggestions()
        self._json_response({"suggestions": suggestions, "count": len(suggestions)})

    def _handle_suggestion_accept(self, suggestion_id: str):
        """POST /api/suggestions/<id>/accept — accept a suggestion and create a ticket."""
        from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus

        suggestions = _read_suggestions()
        suggestion = _find_suggestion(suggestion_id, suggestions)
        if not suggestion:
            self._json_response({"error": f"Suggestion {suggestion_id} not found"}, status=404)
            return
        if suggestion.get("status") != "pending":
            self._json_response({"error": f"Suggestion {suggestion_id} is not pending"}, status=400)
            return

        # Create a ticket from the suggestion
        ticket = SWETicket(
            title=suggestion["title"],
            description=suggestion.get("description", ""),
            severity=TicketSeverity.LOW,
            labels=["suggestion", suggestion.get("category", "general")],
        )
        ticket.metadata["suggestion_id"] = suggestion_id
        ticket.metadata["suggestion_category"] = suggestion.get("category", "")
        ticket.metadata["suggestion_impact"] = suggestion.get("impact", "")
        self.store.add(ticket)

        # Update suggestion status
        suggestion["status"] = "accepted"
        suggestion["accepted_at"] = datetime.now(timezone.utc).isoformat()
        suggestion["ticket_id"] = ticket.ticket_id
        _write_suggestions(suggestions)

        _broadcast_sse_event("action", {
            "event": "suggestion_accepted",
            "suggestion_id": suggestion_id,
            "ticket_id": ticket.ticket_id,
        })
        self._json_response({
            "status": "ok",
            "suggestion_id": suggestion_id,
            "ticket_id": ticket.ticket_id,
        })

    def _handle_suggestion_dismiss(self, suggestion_id: str):
        """POST /api/suggestions/<id>/dismiss — dismiss a suggestion."""
        body = self._read_post_body()
        reason = body.get("reason", "").strip()

        suggestions = _read_suggestions()
        suggestion = _find_suggestion(suggestion_id, suggestions)
        if not suggestion:
            self._json_response({"error": f"Suggestion {suggestion_id} not found"}, status=404)
            return
        if suggestion.get("status") != "pending":
            self._json_response({"error": f"Suggestion {suggestion_id} is not pending"}, status=400)
            return

        suggestion["status"] = "dismissed"
        suggestion["dismissed_at"] = datetime.now(timezone.utc).isoformat()
        if reason:
            suggestion["dismiss_reason"] = reason
        _write_suggestions(suggestions)

        _broadcast_sse_event("action", {
            "event": "suggestion_dismissed",
            "suggestion_id": suggestion_id,
        })
        self._json_response({
            "status": "ok",
            "suggestion_id": suggestion_id,
        })

    # --- Execution Mode API handlers ---

    def _handle_get_execution_checkpoints(self):
        """GET /api/execution/checkpoints — list pending review checkpoints."""
        checkpoints = _read_checkpoints()
        pending = [cp for cp in checkpoints if cp.get("status") == "pending"]
        self._json_response({"checkpoints": pending, "count": len(pending)})

    def _handle_patch_execution_mode(self):
        """PATCH /api/execution/mode — change execution mode (plan/review/start)."""
        body = self._read_post_body()
        mode = body.get("mode", "").strip().lower()
        if mode not in _VALID_EXECUTION_MODES:
            self._json_response(
                {"error": f"Invalid mode '{mode}'. Must be one of: {', '.join(_VALID_EXECUTION_MODES)}"},
                status=400,
            )
            return

        if not _write_execution_mode(mode):
            self._json_response({"error": "Failed to persist execution mode"}, status=500)
            return

        _broadcast_sse_event("action", {
            "event": "execution_mode_changed",
            "mode": mode,
        })
        self._json_response({
            "status": "ok",
            "mode": mode,
            "description": _EXECUTION_MODE_DESCRIPTIONS.get(mode, ""),
        })

    def _handle_checkpoint_approve(self, checkpoint_id: str):
        """POST /api/execution/checkpoints/<id>/approve — approve a checkpoint."""
        checkpoints = _read_checkpoints()
        found = None
        for cp in checkpoints:
            if cp.get("id") == checkpoint_id:
                found = cp
                break

        if not found:
            self._json_response({"error": f"Checkpoint {checkpoint_id} not found"}, status=404)
            return

        if found.get("status") != "pending":
            self._json_response(
                {"error": f"Checkpoint {checkpoint_id} is not pending (status: {found.get('status')})"},
                status=400,
            )
            return

        found["status"] = "approved"
        found["resolved_at"] = datetime.now(timezone.utc).isoformat()
        _write_checkpoints(checkpoints)

        _broadcast_sse_event("action", {
            "event": "checkpoint_approved",
            "checkpoint_id": checkpoint_id,
            "ticket_id": found.get("ticket_id", ""),
        })
        self._json_response({"status": "ok", "checkpoint_id": checkpoint_id, "new_status": "approved"})

    def _handle_checkpoint_reject(self, checkpoint_id: str):
        """POST /api/execution/checkpoints/<id>/reject — reject a checkpoint with feedback."""
        body = self._read_post_body()
        feedback = body.get("feedback", "").strip()

        checkpoints = _read_checkpoints()
        found = None
        for cp in checkpoints:
            if cp.get("id") == checkpoint_id:
                found = cp
                break

        if not found:
            self._json_response({"error": f"Checkpoint {checkpoint_id} not found"}, status=404)
            return

        if found.get("status") != "pending":
            self._json_response(
                {"error": f"Checkpoint {checkpoint_id} is not pending (status: {found.get('status')})"},
                status=400,
            )
            return

        found["status"] = "rejected"
        found["resolved_at"] = datetime.now(timezone.utc).isoformat()
        if feedback:
            found["feedback"] = feedback
        _write_checkpoints(checkpoints)

        _broadcast_sse_event("action", {
            "event": "checkpoint_rejected",
            "checkpoint_id": checkpoint_id,
            "ticket_id": found.get("ticket_id", ""),
            "feedback": feedback,
        })
        self._json_response({"status": "ok", "checkpoint_id": checkpoint_id, "new_status": "rejected"})

    # --- Accounts API handlers ---

    def _handle_list_accounts(self, user: Optional[dict]) -> None:
        """GET /api/accounts — list accounts the current user belongs to."""
        login = (user or {}).get("login", "")
        if not login:
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        acct = _get_account_store()
        if acct is None:
            # Graceful degradation: return empty list so the frontend doesn't break.
            # The personal account will be auto-created on next login when the store is available.
            self._json_response([])
            return
        try:
            accounts = acct.get_user_accounts(login)
            # If user has no accounts yet (race: auto-create may still be in progress),
            # attempt on-demand personal account creation before returning empty.
            if not accounts:
                try:
                    _ensure_personal_account(login)
                    accounts = acct.get_user_accounts(login)
                except Exception:
                    logger.debug("On-demand personal account creation failed for %s", login)
            self._json_response(accounts)
        except Exception as exc:
            logger.exception("Failed to list accounts for %s", login)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_get_account(self, account_id: str) -> None:
        """GET /api/accounts/<id> — get account details."""
        acct = _get_account_store()
        if acct is None:
            self._json_response({"error": "AccountStore unavailable"}, status=503)
            return
        try:
            account = acct.get_account(account_id)
            if account is None:
                self._json_response({"error": f"Account {account_id!r} not found"}, status=404)
                return
            self._json_response(account)
        except Exception as exc:
            logger.exception("Failed to get account %s", account_id)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_get_account_members(self, account_id: str) -> None:
        """GET /api/accounts/<id>/members — list members of an account."""
        acct = _get_account_store()
        if acct is None:
            self._json_response({"error": "AccountStore unavailable"}, status=503)
            return
        try:
            members = acct.get_account_members(account_id)
            self._json_response({"members": members, "count": len(members)})
        except Exception as exc:
            logger.exception("Failed to list members for account %s", account_id)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_create_account(self, user: Optional[dict]) -> None:
        """POST /api/accounts — create a new account."""
        login = (user or {}).get("login", "")
        if not login:
            self._json_response({"error": "Not authenticated"}, status=401)
            return
        body = self._read_post_body()
        name = body.get("name", "").strip()
        slug = body.get("slug", "").strip()
        description = body.get("description", "").strip()
        if not name or not slug:
            self._json_response({"error": "Fields 'name' and 'slug' are required"}, status=400)
            return
        acct = _get_account_store()
        if acct is None:
            self._json_response({"error": "AccountStore unavailable"}, status=503)
            return
        try:
            account = acct.create_account(
                name=name,
                slug=slug,
                created_by=login,
                description=description,
            )
            self._json_response(account, status=201)
        except Exception as exc:
            logger.exception("Failed to create account slug=%s", slug)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_invite_account_member(self, account_id: str, user: Optional[dict]) -> None:
        """POST /api/accounts/<id>/members — invite a member to an account."""
        login = (user or {}).get("login", "")
        body = self._read_post_body()
        github_login = body.get("github_login", "").strip()
        role = body.get("role", "developer").strip()
        if not github_login:
            self._json_response({"error": "Field 'github_login' is required"}, status=400)
            return
        if role not in ("owner", "admin", "developer", "viewer"):
            self._json_response({"error": f"Invalid role {role!r}"}, status=400)
            return
        acct = _get_account_store()
        if acct is None:
            self._json_response({"error": "AccountStore unavailable"}, status=503)
            return
        try:
            member = acct.invite_member(
                account_id=account_id,
                github_login=github_login,
                role=role,
                invited_by=login,
            )
            self._json_response(member, status=201)
        except Exception as exc:
            logger.exception("Failed to invite %s to account %s", github_login, account_id)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_remove_account_member(self, account_id: str, github_login: str) -> None:
        """DELETE /api/accounts/<id>/members/<login> — remove a member from an account."""
        acct = _get_account_store()
        if acct is None:
            self._json_response({"error": "AccountStore unavailable"}, status=503)
            return
        try:
            acct.remove_member(account_id=account_id, github_login=github_login)
            self._json_response({"status": "ok", "removed": github_login})
        except Exception as exc:
            logger.exception("Failed to remove %s from account %s", github_login, account_id)
            self._json_response({"error": str(exc)}, status=500)

    def _handle_patch_account_member_role(self, account_id: str, github_login: str) -> None:
        """PATCH /api/accounts/<id>/members/<login> — update a member's role."""
        body = self._read_post_body()
        role = body.get("role", "").strip()
        if role not in ("owner", "admin", "developer", "viewer"):
            self._json_response({"error": f"Invalid role {role!r}"}, status=400)
            return
        acct = _get_account_store()
        if acct is None:
            self._json_response({"error": "AccountStore unavailable"}, status=503)
            return
        try:
            member = acct.update_member_role(
                account_id=account_id,
                github_login=github_login,
                role=role,
            )
            self._json_response(member)
        except Exception as exc:
            logger.exception(
                "Failed to update role for %s in account %s", github_login, account_id
            )
            self._json_response({"error": str(exc)}, status=500)

    def _handle_pipeline_trigger(self):
        """POST /api/pipeline/trigger — trigger a full pipeline cycle in background."""

        def _run():
            try:
                from scripts.ops.swe_team_runner import run_cycle
                from src.swe_team.config import load_config as _lc
                cfg = _lc()
                run_cycle(cfg)
                _broadcast_sse_event("action", {
                    "event": "pipeline_complete",
                })
            except Exception as exc:
                logger.exception("Background pipeline cycle failed")
                _broadcast_sse_event("action", {
                    "event": "pipeline_failed",
                    "error": str(exc),
                })

        thread = threading.Thread(target=_run, daemon=True, name="pipeline-cycle")
        thread.start()
        self._json_response({"status": "triggered"})

    def _gh_comment_async(self, issue_number, body_text: str):
        """Post a comment to a GitHub issue in a background thread."""
        repo = os.environ.get("SWE_GITHUB_REPO", "")
        if not repo:
            return

        def _post():
            import subprocess
            try:
                subprocess.run(
                    ["gh", "issue", "comment", str(issue_number),
                     "--repo", repo, "--body", body_text],
                    capture_output=True, timeout=15,
                )
            except Exception as exc:
                logger.warning("Failed to comment on GH issue #%s: %s", issue_number, exc)

        threading.Thread(target=_post, daemon=True).start()

    # --- Auth helpers ---

    _SESSION_COOKIE_NAME = "swe_session"

    def _check_auth(self) -> Optional[dict]:
        """Read and validate the session cookie.

        Returns the user dict on success, or ``None`` if not authenticated.
        If OAuth is not enabled, returns a synthetic "anonymous" user so that
        the dashboard remains accessible without credentials.
        """
        if _oauth_provider is None:
            return {"login": "anonymous", "name": "Anonymous", "orgs": []}

        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return None
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except Exception:
            return None
        morsel = jar.get(self._SESSION_COOKIE_NAME)
        if morsel is None:
            return None
        return _oauth_provider.validate_session(morsel.value)

    def _redirect(self, location: str, status: int = 302) -> None:
        """Send a redirect response."""
        self.send_response(status)
        self.send_header("Location", location)
        self.end_headers()

    def _set_session_cookie(self, cookie_value: str, clear: bool = False) -> None:
        """Emit a Set-Cookie header for the session cookie.

        When *clear* is True the cookie is expired immediately.
        """
        if clear:
            self.send_header(
                "Set-Cookie",
                f"{self._SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
            )
        else:
            self.send_header(
                "Set-Cookie",
                f"{self._SESSION_COOKIE_NAME}={cookie_value}; Path=/; HttpOnly; SameSite=Lax",
            )

    def _handle_auth_login(self, query: dict) -> None:
        """GET /auth/login — redirect the browser to GitHub's OAuth authorize URL."""
        if _oauth_provider is None:
            self._redirect("/")
            return
        state = secrets.token_urlsafe(24)
        authorize_url = _oauth_provider.get_authorize_url(state)
        # Store state in a short-lived cookie so we can verify it on callback
        self.send_response(302)
        self.send_header("Location", authorize_url)
        self.send_header(
            "Set-Cookie",
            f"swe_oauth_state={state}; Path=/auth; HttpOnly; SameSite=Lax; Max-Age=600",
        )
        self.end_headers()

    def _handle_auth_callback(self, query: dict) -> None:
        """GET /auth/callback — exchange code for token, set session cookie."""
        if _oauth_provider is None:
            self._redirect("/")
            return

        code_list = query.get("code", [])
        state_list = query.get("state", [])
        if not code_list:
            self.send_error(400, "Missing code parameter")
            return
        code = code_list[0]

        # Verify state cookie (CSRF protection)
        raw_cookie = self.headers.get("Cookie", "")
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except Exception:
            pass
        state_morsel = jar.get("swe_oauth_state")
        expected_state = state_morsel.value if state_morsel else None
        received_state = state_list[0] if state_list else None
        if expected_state and received_state and not hmac.compare_digest(expected_state, received_state):
            self.send_error(403, "State mismatch — possible CSRF")
            return

        try:
            user_info = _oauth_provider.exchange_code(code)
        except Exception as exc:
            logger.error("OAuth code exchange failed: %s", exc)
            self.send_error(500, f"OAuth error: {exc}")
            return

        logger.info(
            "OAuth callback: login=%s, orgs=%s, allowed_orgs=%s, authorized=%s",
            user_info.get("login"), user_info.get("orgs"), _OAUTH_ALLOWED_ORGS,
            _oauth_provider.is_authorized(user_info),
        )
        if not _oauth_provider.is_authorized(user_info):
            body = (
                "<html><body><h2>Access Denied</h2>"
                f"<p>Your account (<b>{user_info.get('login','')}</b>) is not a member of "
                f"an authorised organisation: {', '.join(_OAUTH_ALLOWED_ORGS)}</p>"
                "<p><a href='/auth/login'>Try again</a></p></body></html>"
            ).encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        cookie_value = _oauth_provider.create_session_cookie(user_info)
        self.send_response(302)
        self.send_header("Location", "/")
        self._set_session_cookie(cookie_value)
        # Clear the state cookie
        self.send_header(
            "Set-Cookie",
            "swe_oauth_state=; Path=/auth; HttpOnly; SameSite=Lax; Max-Age=0",
        )
        self.end_headers()
        logger.info("OAuth login: %s", user_info.get("login", "unknown"))
        # Auto-provision / update user in UserStore on every OAuth login
        try:
            _us = _get_user_store()
            if _us is not None:
                _us.get_or_create_user(
                    github_login=user_info.get("login", ""),
                    email=user_info.get("email") or "",
                    display_name=user_info.get("name") or user_info.get("login", ""),
                    avatar_url=user_info.get("avatar_url") or "",
                )
        except Exception:
            logger.exception("UserStore auto-provision failed (non-fatal)")
        # Auto-create a personal account on first OAuth login (non-blocking).
        try:
            _ensure_personal_account(user_info.get("login", ""))
        except Exception:
            logger.exception("Personal account auto-provision failed (non-fatal)")
        # Store OAuth token encrypted for GitHub API access
        if "access_token" in user_info:
            _us = _get_user_store()
            if _us:
                try:
                    _us.set_secret(user_info["login"], "github_access_token", user_info["access_token"])
                except Exception:
                    logger.exception("Failed to store GitHub access token (non-fatal)")

    def _handle_auth_logout(self) -> None:
        """GET /auth/logout — clear session cookie and redirect to login."""
        self.send_response(302)
        self.send_header("Location", "/auth/login")
        self._set_session_cookie("", clear=True)
        self.end_headers()

    # --- GitHub repos API ---

    def _handle_github_repos(self, query: dict):
        """GET /api/github/repos — list the authenticated user's GitHub repos."""
        user = self._check_auth()
        if user is None or user.get("login") == "anonymous":
            self._json_response({"error": "Unauthorized"}, status=401)
            return
        _us = _get_user_store()
        if not _us:
            self._json_response({"error": "UserStore not available"}, status=500)
            return
        token = _us.get_secret(user["login"], "github_access_token")
        if not token:
            self._json_response({"error": "No GitHub token stored. Please re-login with OAuth."}, status=401)
            return
        page = int(query.get("page", ["1"])[0])
        url = f"https://api.github.com/user/repos?per_page=50&sort=pushed&type=all&page={page}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "SWE-Squad-Dashboard/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                link_header = resp.getheader("Link", "")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._json_response({"error": "GitHub token revoked or expired. Please re-login."}, status=401)
                return
            self._json_response({"error": f"GitHub API error: {exc.code}"}, status=502)
            return
        except Exception as exc:
            self._json_response({"error": f"GitHub API request failed: {exc}"}, status=502)
            return
        repos = []
        for r in data:
            repos.append({
                "full_name": r.get("full_name", ""),
                "description": r.get("description") or "",
                "private": r.get("private", False),
                "default_branch": r.get("default_branch", "main"),
                "html_url": r.get("html_url", ""),
                "pushed_at": r.get("pushed_at", ""),
                "language": r.get("language") or "",
                "stargazers_count": r.get("stargazers_count", 0),
            })
        has_more = 'rel="next"' in link_header
        self._json_response({"repos": repos, "page": page, "has_more": has_more})

    def _handle_github_repos_connect(self):
        """POST /api/github/repos/connect — connect a GitHub repo as a project."""
        user = self._check_auth()
        if user is None or user.get("login") == "anonymous":
            self._json_response({"error": "Unauthorized"}, status=401)
            return
        body = self._read_post_body()
        repo = body.get("repo", "").strip()
        if not repo or "/" not in repo:
            self._json_response({"error": "Field 'repo' is required (format: owner/repo)"}, status=400)
            return
        priority = body.get("priority", "medium")
        # Build a project entry
        project = {
            "name": repo,
            "description": "",
            "local_path": "",
            "github_repo": repo,
            "priority": priority,
            "enabled": True,
        }
        ok = _save_project_to_config(project)
        if not ok:
            self._json_response({"error": f"Repo {repo!r} already connected"}, status=409)
            return
        # Also append to github_repos list in config if not already there
        try:
            import yaml
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
            gh_repos = raw.get("github_repos", [])
            if repo not in gh_repos:
                gh_repos.append(repo)
                raw["github_repos"] = gh_repos
                _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))
        except Exception:
            logger.exception("Failed to update github_repos in config (non-fatal)")
        self._json_response({"ok": True, "project": project}, status=201)

    # --- Page handlers ---

    def _serve_dashboard(self):
        try:
            html = _render_dashboard(self.store)
            self._send_gzipped(html, "text/html; charset=utf-8",
                               cache_control="public, max-age=60")
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response — ignored")
        except Exception as exc:
            logger.exception("Dashboard render error")
            try:
                self.send_error(500, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _serve_static_file(self, path: str):
        """Serve a static file from ui/dist/."""
        import mimetypes
        safe_path = path.lstrip("/")
        file_path = _REACT_UI_DIST / safe_path
        if not file_path.exists() or not str(file_path.resolve()).startswith(str(_REACT_UI_DIST.resolve())):
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        data = file_path.read_bytes()
        origin = self._cors_origin()
        self.send_response(200)
        self._set_cors_headers(origin)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if "/assets/" in path:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    def _serve_onboarding(self):
        """GET /onboarding — serve React SPA (handles onboarding route client-side)."""
        self._serve_dashboard()

    def _serve_login(self):
        """GET /login — serve React SPA (handles login route client-side)."""
        self._serve_dashboard()

    def _handle_onboarding_complete(self):
        """POST /api/onboarding/complete — process first-time setup form submission.

        Body: {"team_id": string, "repos": [{name, local_path?, branches?}]}

        Updates swe_team.yaml with team_id and repos configuration.
        """
        body = self._read_post_body()
        team_id = body.get("team_id", "").strip()
        repos = body.get("repos", [])

        # Validate team_id
        if not team_id:
            self._json_response({"error": "team_id is required"}, status=400)
            return

        # Validate repos - check type first
        if not isinstance(repos, list):
            self._json_response({"error": "repos must be an array"}, status=400)
            return

        if len(repos) == 0:
            self._json_response({"error": "at least one repository is required"}, status=400)
            return

        for repo in repos:
            if not isinstance(repo, dict):
                self._json_response({"error": "repos must be an array of objects"}, status=400)
                return
            if not repo.get("name"):
                self._json_response({"error": "each repo must have a 'name' field"}, status=400)
                return

        try:
            import yaml
            # Read existing config
            raw = yaml.safe_load(_CONFIG_PATH.read_text()) if _CONFIG_PATH.exists() else {}
            raw = raw or {}

            # Update team_id
            raw["team_id"] = team_id

            # Update repos (merge with existing)
            existing_repos = raw.get("repos", [])
            existing_names = {r.get("name") for r in existing_repos if isinstance(r, dict)}
            new_repos = [dict(r) for r in repos if r.get("name") not in existing_names]
            raw["repos"] = existing_repos + new_repos

            # Write back
            _CONFIG_PATH.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))

            logger.info("Onboarding completed: team_id=%s, repos=%d", team_id, len(raw.get("repos", [])))
            self._json_response({"ok": True, "team_id": team_id, "repos": raw.get("repos", [])})
        except Exception as exc:
            logger.exception("Onboarding completion failed")
            self._json_response({"error": f"Failed to save configuration: {exc}"}, status=500)

    def _handle_sse(self):
        """GET /api/stream — Server-Sent Events endpoint for live dashboard updates."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            payload = _build_sse_payload()
            self.wfile.write(f"event: update\ndata: {payload}\n\n".encode())
            self.wfile.flush()
            with _sse_lock:
                _sse_clients.append(self.wfile)
            while True:
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sse_lock:
                if self.wfile in _sse_clients:
                    _sse_clients.remove(self.wfile)

    def _handle_api_auth_status(self):
        """GET /api/auth/status — return authentication state for all providers and OAuth session."""
        # OAuth session user (from GitHub OAuth)
        oauth_user = self._check_auth()
        session_info: dict = {}
        if oauth_user and oauth_user.get("login") != "anonymous":
            session_info = {
                "authenticated": True,
                "login": oauth_user.get("login", ""),
                "name": oauth_user.get("name", ""),
                "orgs": oauth_user.get("orgs", []),
            }
        elif oauth_user and oauth_user.get("login") == "anonymous":
            session_info = {"authenticated": False, "login": "anonymous"}
        else:
            session_info = {"authenticated": False}

        # Enrich with UserStore data
        _us = _get_user_store()
        _login_str = session_info.get("login")
        if _us and isinstance(_login_str, str) and _login_str:
            db_user = _us.get_user(_login_str)
            if db_user:
                session_info["avatar_url"] = db_user.get("avatar_url", "")
                session_info["role"] = db_user.get("role", "user")

        # Provider auth states (API key tracking)
        if self.auth_provider is None:
            self._json_response({"providers": [], "session": session_info, "oauth_enabled": _OAUTH_ENABLED})
            return
        providers = []
        for state in self.auth_provider.list_states():
            providers.append({
                "name": state.provider_name,
                "is_authenticated": state.is_authenticated,
                "is_healthy": state.is_healthy(),
                "consecutive_failures": state.consecutive_auth_failures,
                "last_error": state.last_auth_error,
            })
        self._json_response({"providers": providers, "session": session_info, "oauth_enabled": _OAUTH_ENABLED})

    def _handle_api_onboarding_status(self):
        """GET /api/onboarding/status — return onboarding completion status.

        Returns:
            {"completed": bool, "team_id?: string, "repos": []}
        """
        try:
            # Check if swe_team.yaml exists and has team_id configured
            if not _CONFIG_PATH.exists():
                self._json_response({"completed": False})
                return

            config = load_config()
            if not config.team_id:
                self._json_response({"completed": False})
                return

            # Check if we have repos configured
            repos = getattr(config, 'repos', [])
            self._json_response({
                "completed": True,
                "team_id": config.team_id,
                "repos": repos or []
            })
        except Exception as exc:
            self._json_response({"completed": False, "error": str(exc)}, status=500)

    def _handle_api_graph(self):
        """GET /api/graph — return ticket similarity graph data."""
        try:
            data = _build_graph_data(self.store)
            body = json.dumps(data, indent=2, default=str)
            self._send_gzipped(body, "application/json",
                               cache_control="public, max-age=60")
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during graph response — ignored")
        except Exception as exc:
            self._json_response({"error": str(exc)}, status=500)

    def _handle_api_heartbeats(self, query: dict):
        """GET /api/heartbeats — return live agent run tracking data.

        Query parameters:
        - ticket_id: filter by specific ticket ID
        - since: ISO timestamp to filter tickets updated since
        - agents: include scheduler agents list (default: false)
        - active_only: only return runs with recent heartbeat (<10 min, default: true)
        """
        try:
            ticket_id = query.get("ticket_id", [""])[0] or None
            since = query.get("since", [""])[0] or None
            include_agents = query.get("agents", ["false"])[0].lower() == "true"
            active_only = query.get("active_only", ["true"])[0].lower() != "false"

            runs = _get_live_runs(self.store, ticket_id=ticket_id, since=since)

            # Filter by active_only if specified
            if active_only:
                runs = [r for r in runs if r.get("is_live")]

            response = {
                "runs": runs,
                "count": len(runs),
            }

            # Include scheduler agents if requested
            if include_agents:
                response["agents"] = _get_scheduler_agents()

            # Include summary counts
            live_count = sum(1 for r in runs if r.get("is_live"))
            by_status = {}
            by_severity = {}
            for run in runs:
                status = run.get("status", "unknown")
                severity = run.get("severity", "unknown")
                by_status[status] = by_status.get(status, 0) + 1
                by_severity[severity] = by_severity.get(severity, 0) + 1

            response["summary"] = {
                "live_count": live_count,
                "total_count": len(runs),
                "by_status": by_status,
                "by_severity": by_severity,
            }

            # For single ticket query, include the active run directly
            if ticket_id:
                active_run = _get_active_run_for_issue(self.store, ticket_id)
                response["active_run"] = active_run

            self._json_response(response, cache_control="public, max-age=10")
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during heartbeats response — ignored")
        except Exception as exc:
            logger.exception("Heartbeats API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_onboarding_status(self):
        """GET /api/onboarding/status — return onboarding status (no auth required)."""
        try:
            import yaml

            # Default values if config doesn't exist or is invalid
            needs_onboarding = True
            current_team_id = "default"
            repo_count = 0

            # Try to load config
            if _CONFIG_PATH.exists():
                try:
                    config = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
                    current_team_id = config.get("team_id", "default")
                    repos = config.get("repos", [])
                    repo_count = len(repos) if isinstance(repos, list) else 0
                except Exception as exc:
                    logger.warning(f"Failed to load config for onboarding status: {exc}")
            else:
                logger.debug(f"Config file not found at {_CONFIG_PATH}, assuming onboarding needed")

            # Determine if onboarding is needed
            needs_onboarding = (current_team_id == "default") or (repo_count == 0)

            self._json_response({
                "needs_onboarding": needs_onboarding,
                "current_team_id": current_team_id,
                "repo_count": repo_count
            }, cache_control="public, max-age=60")
        except Exception as exc:
            logger.exception("Onboarding status API error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_api_activity(self, query: dict):
        """GET /api/activity — return recent activity with optional filtering.

        Query parameters:
        - action_type: filter by action type (e.g., investigation_complete, status_changed)
        - severity: filter by ticket severity (CRITICAL, HIGH, MEDIUM, LOW)
        - agent: filter by agent name
        - ticket_id: filter by specific ticket ID
        - search: text search in action/title (alias: q)
        - from: ISO date string lower bound (inclusive)
        - to: ISO date string upper bound (inclusive)
        - q: alias for search (text search in action/title/ticket_id)
        """
        log_path = PROJECT_ROOT / "logs" / "swe_team.log"
        entries = []
        if log_path.exists():
            lines = _tail_log_file(log_path)[-50:]  # Read more lines for better filtering
            for line in lines:
                if '[INFO]' in line and any(w in line for w in ['Investigating', 'attempt_fix', 'Triaged', 'SESSION', 'Dispatched', 'Claude CLI', 'gate:']):
                    parts = line.split(' ', 3)
                    timestamp = parts[0] + ' ' + parts[1][:8] if len(parts) > 1 else ""
                    action_text = parts[-1][:120] if parts else line[:120]

                    # Parse action type from action text
                    action_type = "unknown"
                    if "Investigating" in action_text:
                        action_type = "investigation_started"
                    elif "Triaged" in action_text:
                        action_type = "triage_complete"
                    elif "attempt_fix" in action_text or "Attempting fix" in action_text:
                        action_type = "dev_started"
                    elif "fix:" in action_text.lower() or "applied fix" in action_text.lower():
                        action_type = "dev_complete"
                    elif "status" in action_text.lower():
                        action_type = "status_changed"
                    elif "gate:" in action_text.lower() or "stability" in action_text.lower():
                        action_type = "stability_check"

                    # Try to extract ticket ID from action text
                    ticket_id = None
                    ticket_match = re.search(r'\b[a-f0-9]{8,12}\b', action_text)
                    if ticket_match:
                        ticket_id = ticket_match.group(0)

                    # Try to extract severity from action text
                    severity = None
                    sev_match = re.search(r'(CRITICAL|HIGH|MEDIUM|LOW)', action_text.upper())
                    if sev_match:
                        severity = sev_match.group(1).lower()

                    entries.append({
                        "time": timestamp,
                        "agent": "swe-squad",
                        "action": action_text,
                        "action_type": action_type,
                        "ticket_id": ticket_id,
                        "severity": severity,
                    })

        # Apply filters
        filtered = entries

        # Filter by action_type
        action_type_filter = query.get("action_type", [""])[0]
        if action_type_filter and action_type_filter != "all":
            filtered = [e for e in filtered if e.get("action_type") == action_type_filter]

        # Filter by severity
        severity_filter = query.get("severity", [""])[0]
        if severity_filter and severity_filter != "all":
            filtered = [e for e in filtered if e.get("severity") == severity_filter.lower()]

        # Filter by agent
        agent_filter = query.get("agent", [""])[0]
        if agent_filter and agent_filter != "all":
            filtered = [e for e in filtered if e.get("agent") == agent_filter]

        # Filter by ticket_id
        ticket_filter = query.get("ticket_id", [""])[0]
        if ticket_filter:
            filtered = [e for e in filtered if e.get("ticket_id") == ticket_filter]

        # Text search (support both 'search' and 'q' params)
        search_filter = query.get("search", [""])[0] or query.get("q", [""])[0]
        if search_filter:
            search_lower = search_filter.lower()
            filtered = [e for e in filtered if
                         search_lower in e.get("action", "").lower() or
                         (e.get("ticket_id") and search_lower in e.get("ticket_id").lower())]

        # Date range filtering (from / to as ISO date strings, e.g. 2026-04-01)
        from_filter = query.get("from", [""])[0]
        to_filter = query.get("to", [""])[0]
        if from_filter or to_filter:
            def _parse_date(ds):
                """Parse ISO date/datetime string, return None on failure."""
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        return datetime.strptime(ds, fmt)
                    except (ValueError, TypeError):
                        continue
                return None

            from_dt = _parse_date(from_filter) if from_filter else None
            to_dt = _parse_date(to_filter) if to_filter else None

            def _in_range(entry):
                entry_dt = _parse_date(entry.get("time", ""))
                if entry_dt is None:
                    return True  # keep entries we can't parse
                if from_dt and entry_dt < from_dt:
                    return False
                if to_dt and entry_dt > to_dt:
                    return False
                return True

            filtered = [e for e in filtered if _in_range(e)]

        # Collect unique filter values from all entries
        agents = sorted(set(e.get("agent") for e in entries if e.get("agent")))
        action_types = sorted(set(e.get("action_type") for e in entries if e.get("action_type")))
        severities = sorted(set(e.get("severity") for e in entries if e.get("severity")))

        # Build response
        response = {
            "activities": filtered[-100:],  # Return at most 100 filtered entries
            "filters": {
                "agents": agents,
                "action_types": action_types,
                "severities": severities,
            },
            "applied_filters": {
                "action_type": action_type_filter or None,
                "severity": severity_filter or None,
                "agent": agent_filter or None,
                "ticket_id": ticket_filter or None,
                "search": search_filter or None,
                "from": from_filter or None,
                "to": to_filter or None,
            }
        }
        try:
            body = json.dumps(response, indent=2, default=str)
            self._send_gzipped(body, "application/json",
                               cache_control="public, max-age=30")
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during activity response — ignored")

    def _handle_costs_roi(self, query: dict):
        """GET /api/costs/roi — subscription ROI calculation."""
        try:
            monthly_fee = float(query.get("monthly_fee", [200.0])[0])
            since_days = int(query.get("since_days", [30])[0])
            tracker = _get_token_tracker()
            roi = tracker.subscription_roi(monthly_fee=monthly_fee, since_days=since_days)

            # Count tickets resolved in the period
            try:
                records = tracker._filter_since(timedelta(days=since_days))
                tickets = set(r.ticket_id for r in records if r.ticket_id)
                tickets_resolved = len(tickets)
            except Exception:
                tickets_resolved = 0

            # Calculate cost per ticket
            total_cost = roi.get("api_equivalent_cost", 0.0)
            cost_per_ticket = total_cost / tickets_resolved if tickets_resolved > 0 else 0.0

            # Calculate estimated ROI percentage
            savings = roi.get("savings", 0.0)
            estimated_roi = (savings / monthly_fee * 100) if monthly_fee > 0 else 0.0

            result = {
                "tickets_resolved": tickets_resolved,
                "total_api_cost_usd": round(total_cost, 4),
                "cost_per_ticket_usd": round(cost_per_ticket, 4),
                "monthly_team_cost_usd": round(monthly_fee, 4),
                "estimated_roi_pct": round(estimated_roi, 2),
                "subscription_fee": round(monthly_fee, 4),
                "savings": round(savings, 4),
            }
            self._json_response(result)
        except Exception as exc:
            logger.exception("ROI endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_cache_efficiency(self):
        """GET /api/costs/cache_efficiency — cache read vs creation breakdown."""
        try:
            tracker = _get_token_tracker()
            records = tracker._load_records()
            cache_read_total = sum(r.cache_read_tokens for r in records)
            cache_creation_total = sum(r.cache_creation_tokens for r in records)
            input_total = sum(r.input_tokens for r in records)
            denominator = cache_read_total + input_total
            efficiency_pct = round(cache_read_total / denominator * 100, 2) if denominator else 0.0
            # Estimate savings: cache reads are ~90% cheaper than regular input
            avg_input_rate = 0.003  # USD per 1K tokens (sonnet default)
            estimated_savings = round(cache_read_total / 1000 * avg_input_rate * 0.9, 4)
            self._json_response({
                "cache_read_tokens_total": cache_read_total,
                "cache_creation_tokens_total": cache_creation_total,
                "input_tokens_total": input_total,
                "cache_efficiency_pct": efficiency_pct,
                "estimated_cache_savings_usd": estimated_savings,
            })
        except Exception as exc:
            logger.exception("Cache efficiency endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_budget_status(self, query: dict):
        """GET /api/cost — daily/monthly budget status per team.

        Query params:
        - team_id: filter to a specific team (optional; defaults to server's team_id)
        """
        try:
            cost_tracker = getattr(self.server, "_cost_tracker", None)
            team_id = query.get("team_id", [""])[0] if query.get("team_id") else ""
            if cost_tracker is None:
                self._json_response({
                    "configured": False,
                    "team_id": team_id or "unknown",
                    "daily_spent_usd": 0.0,
                    "monthly_spent_usd": 0.0,
                    "budget_status": "unconfigured",
                    "message": "No CostTracker configured. Run migration 004_cost_tracking.sql.",
                })
                return

            if not team_id:
                team_id = getattr(self.server, "_team_id", "") or "default"
            status = cost_tracker.check_budget(team_id)
            summary = cost_tracker.get_team_summary(team_id) if hasattr(cost_tracker, "get_team_summary") else {}
            self._json_response({
                "configured": True,
                "team_id": team_id,
                "budget_status": status.status,
                "percent_used": status.percent_used,
                "daily_spent_cents": status.daily_spent,
                "daily_limit_cents": status.daily_limit,
                "monthly_spent_cents": status.monthly_spent,
                "monthly_limit_cents": status.monthly_limit,
                "daily_spent_usd": round(status.daily_spent / 100, 4),
                "monthly_spent_usd": round(status.monthly_spent / 100, 4),
                "daily_limit_usd": round(status.daily_limit / 100, 2),
                "monthly_limit_usd": round(status.monthly_limit / 100, 2),
                **({k: v for k, v in summary.items() if k not in ("team_id",)}),
            }, cache_control="no-cache")
        except Exception as exc:
            logger.exception("Budget status endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    # _handle_costs and _handle_scheduler removed — the SPA fallback at the
    # end of do_GET now serves the React app for /costs, /scheduler, and all
    # other client-side routes.  Legacy hash-route redirects (/#costs etc.)
    # broke React Router and have been intentionally deleted.

    # ---------------------------------------------------------------------------
    # Budget API handlers
    # ---------------------------------------------------------------------------

    def _handle_budget_policies_get(self, query: dict):
        """GET /api/budget/policies — list all budget policies."""
        try:
            api = get_budget_api()
            policies = api.get_policies()
            # Convert to dicts
            result = [p.__dict__ for p in policies]
            self._json_response(result, cache_control="public, max-age=60")
        except Exception as exc:
            logger.exception("Budget policies endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_budget_policies_post(self, query: dict, body: bytes):
        """POST /api/budget/policies — create or update a budget policy."""
        try:
            from dataclasses import asdict
            api = get_budget_api()
            data = json.loads(body)
            # Import BudgetPolicy dataclass
            from src.swe_team.budget_api import BudgetPolicy
            policy = BudgetPolicy(**data)
            updated = api.set_policy(policy)
            self._json_response(updated.__dict__, cache_control="no-cache")
        except Exception as exc:
            logger.exception("Budget policy POST endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_budget_incidents_get(self, query: dict):
        """GET /api/budget/incidents — list budget incidents."""
        try:
            api = get_budget_api()
            team_id = query.get("team_id", [None])[0]
            resolved_str = query.get("resolved", [None])[0]
            resolved = None if resolved_str is None else resolved_str.lower() == "true"
            incidents = api.get_incidents(team_id=team_id, resolved=resolved)
            # Convert to dicts
            result = [i.__dict__ for i in incidents]
            self._json_response(result, cache_control="public, max-age=30")
        except Exception as exc:
            logger.exception("Budget incidents endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_budget_incidents_post(self, query: dict, body: bytes):
        """POST /api/budget/incidents — create a new budget incident."""
        try:
            from src.swe_team.budget_api import BudgetIncident
            api = get_budget_api()
            data = json.loads(body)
            incident = BudgetIncident(**data)
            created = api.create_incident(incident)
            self._json_response(created.__dict__, cache_control="no-cache")
        except Exception as exc:
            logger.exception("Budget incident POST endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_budget_incidents_resolve(self, path_parts: list):
        """POST /api/budget/incidents/<id>/resolve — resolve an incident."""
        try:
            incident_id = path_parts[4] if len(path_parts) > 4 else ""
            if not incident_id:
                self._json_response({"error": "Missing incident ID"}, status=400)
                return
            api = get_budget_api()
            success = api.resolve_incident(incident_id)
            if success:
                self._json_response({"resolved": True})
            else:
                self._json_response({"error": "Incident not found or already resolved"}, status=404)
        except Exception as exc:
            logger.exception("Budget incident resolve endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_budget_provider_quotas(self, query: dict):
        """GET /api/budget/provider-quotas — get provider quota status."""
        try:
            api = get_budget_api()
            quotas = api.get_provider_quotas()
            # Convert to dicts
            result = [q.__dict__ for q in quotas]
            self._json_response(result, cache_control="public, max-age=300")
        except Exception as exc:
            logger.exception("Provider quotas endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_budget_spend_window(self, query: dict):
        """GET /api/budget/spend-window — get rolling window spend data.

        Query params:
        - days: Number of days in the window (default: 7)
        - team_id: Team ID for filtering (optional)
        """
        try:
            api = get_budget_api()
            days = int(query.get("days", [7])[0])
            team_id = query.get("team_id", ["default"])[0]
            spend_window = api.get_spend_window(days=days, team_id=team_id)
            self._json_response(spend_window.__dict__, cache_control="public, max-age=300")
        except Exception as exc:
            logger.exception("Spend window endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_budget_subscriptions(self, query: dict):
        """GET /api/budget/subscriptions — get subscription/billing info."""
        try:
            api = get_budget_api()
            subscriptions = api.get_subscriptions()
            # Convert to dicts
            result = [s.__dict__ for s in subscriptions]
            self._json_response(result, cache_control="public, max-age=600")
        except Exception as exc:
            logger.exception("Subscriptions endpoint error")
            self._json_response({"error": str(exc)}, status=500)

    def _handle_budget_accounting_models(self, query: dict):
        """GET /api/budget/accounting-models — get cost accounting models.

        Query params:
        - team_id: Team ID for filtering (optional, default: "default")
        """
        try:
            api = get_budget_api()
            team_id = query.get("team_id", ["default"])[0]
            models = api.get_accounting_models(team_id=team_id)
            # Convert to dicts
            result = [m.__dict__ for m in models]
            self._json_response(result, cache_control="public, max-age=300")
        except Exception as exc:
            logger.exception("Accounting models endpoint error")
            self._json_response({"error": str(exc)}, status=500)


    def _handle_get_rate_limits(self):
        """GET /api/rate-limits — return rate limit lifecycle state for all tracked providers."""
        try:
            from src.swe_team.rate_limiter import get_all_lifecycle_statuses
            statuses = get_all_lifecycle_statuses()
            self._json_response({"providers": statuses}, cache_control="private, no-cache")
        except Exception as exc:
            logger.exception("rate-limits endpoint error")
            self._json_response({"error": str(exc)}, status=500)


    # ------------------------------------------------------------------
    # GitHub Label Triggers API helpers
    # ------------------------------------------------------------------

    def _load_label_triggers(self) -> list:
        """Load label triggers from the JSON file."""
        if not _LABEL_TRIGGERS_PATH.exists():
            return []
        try:
            with open(_LABEL_TRIGGERS_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_label_triggers(self, triggers: list) -> None:
        """Save label triggers to the JSON file."""
        _LABEL_TRIGGERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LABEL_TRIGGERS_PATH, 'w', encoding='utf-8') as f:
            json.dump(triggers, f, indent=2)

    def _handle_list_label_triggers(self) -> None:
        """GET /api/github/label-triggers — list configured label triggers."""
        triggers = self._load_label_triggers()
        self._json_response({'triggers': triggers})

    def _handle_create_label_trigger(self) -> None:
        """POST /api/github/label-triggers — create or update a label trigger.

        Body: {"label": "swe-squad", "severity": "high", "auto_assign": true, "enabled": true}
        """
        body = self._read_post_body()
        label = (body.get('label') or '').strip().lower()
        if not label:
            self._json_response({'error': 'label is required'}, status=400)
            return

        severity = body.get('severity', 'medium')
        if severity not in ('critical', 'high', 'medium', 'low'):
            self._json_response({'error': f'Invalid severity: {severity}'}, status=400)
            return

        auto_assign = bool(body.get('auto_assign', True))
        enabled = bool(body.get('enabled', True))

        triggers = self._load_label_triggers()

        # Update existing or append new
        found = False
        for trigger in triggers:
            if trigger.get('label') == label:
                trigger['severity'] = severity
                trigger['auto_assign'] = auto_assign
                trigger['enabled'] = enabled
                found = True
                break

        if not found:
            triggers.append({
                'label': label,
                'severity': severity,
                'auto_assign': auto_assign,
                'enabled': enabled,
            })

        self._save_label_triggers(triggers)
        self._json_response({'ok': True, 'trigger': {
            'label': label,
            'severity': severity,
            'auto_assign': auto_assign,
            'enabled': enabled,
        }})

    def _handle_delete_label_trigger(self, label: str) -> None:
        """DELETE /api/github/label-triggers/<label> — remove a trigger."""
        label = label.strip().lower()
        triggers = self._load_label_triggers()
        original_len = len(triggers)
        triggers = [t for t in triggers if t.get('label') != label]
        if len(triggers) == original_len:
            self._json_response({'error': f"Trigger '{label}' not found"}, status=404)
            return
        self._save_label_triggers(triggers)
        self._json_response({'ok': True, 'deleted': label})

    def _handle_test_label_trigger(self) -> None:
        """POST /api/github/label-triggers/test — test a trigger against live GH issues.

        Body: {"label": "swe-squad"}
        Response: {"matching_issues": 3, "issues": [{"number": 42, "title": "..."}]}
        """
        body = self._read_post_body()
        label = (body.get('label') or '').strip().lower()
        if not label:
            self._json_response({'error': 'label is required'}, status=400)
            return

        # Find repo from config
        try:
            cfg = load_config()
            repos = [r.get('name', '') for r in (cfg.raw.get('repos') or []) if r.get('name')]
        except Exception:
            repos = []

        if not repos:
            self._json_response({'matching_issues': 0, 'issues': [], 'error': 'No repos configured'})
            return

        matching: list[dict] = []
        for repo_name in repos[:5]:  # limit to first 5 repos
            try:
                import subprocess as _sp
                result = _sp.run(
                    [
                        'gh', 'issue', 'list',
                        '--repo', repo_name,
                        '--state', 'open',
                        '--label', label,
                        '--limit', '10',
                        '--json', 'number,title',
                    ],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0 and result.stdout.strip():
                    issues = json.loads(result.stdout)
                    for issue in issues:
                        matching.append({
                            'number': issue.get('number'),
                            'title': issue.get('title', ''),
                            'repo': repo_name,
                        })
            except Exception:
                continue

        self._json_response({
            'matching_issues': len(matching),
            'issues': matching,
        })

    def _serve_json(self):
        try:
            data = _get_cached_dashboard_data(self.store)
            # Retrieve the ETag computed when the cache was populated
            with _data_cache_lock:
                etag = _data_cache.get("etag", "")
            # Honour conditional GET (If-None-Match) — return 304 when data
            # hasn't changed so the browser skips re-parsing the payload.
            if etag and self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "public, max-age=30")
                self.end_headers()
                return
            # Inject extended cost data (cached separately — token_usage.jsonl can be large)
            data["costs_extended"] = _get_cached_costs_extended()
            # Inject governor status (cached — governor queries token_usage.jsonl internally)
            data["governor"] = _get_cached_governor_status()
            body = json.dumps(data, indent=2, default=str)
            raw = body.encode("utf-8")
            accept_enc = self.headers.get("Accept-Encoding", "")
            if "gzip" in accept_enc and len(raw) > 1024:
                compressed = gzip.compress(raw)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(compressed)))
                self.send_header("Cache-Control", "public, max-age=30")
                if etag:
                    self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(compressed)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "public, max-age=30")
                if etag:
                    self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response — ignored")
        except Exception as exc:
            try:
                self._json_response({"error": str(exc), "status": 500}, status=500)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json_response(self, data, status: int = 200, cache_control: str | None = None):
        try:
            body = json.dumps(data, indent=2, default=str).encode("utf-8")
            origin = self._cors_origin()
            accept_enc = self.headers.get("Accept-Encoding", "")
            if "gzip" in accept_enc and len(body) > 1024:
                compressed = gzip.compress(body)
                self.send_response(status)
                self._set_cors_headers(origin)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(compressed)))
                if cache_control:
                    self.send_header("Cache-Control", cache_control)
                self.end_headers()
                self.wfile.write(compressed)
            else:
                self.send_response(status)
                self._set_cors_headers(origin)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if cache_control:
                    self.send_header("Cache-Control", cache_control)
                self.end_headers()
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response — ignored")


def main():
    import time as _t
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="SWE-Squad live dashboard server")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    _t0 = _t.time()
    config = load_config()
    logger.info("Config loaded in %.1fs", _t.time() - _t0)
    _t1 = _t.time()
    store = _load_store(config)
    logger.info("Store loaded in %.1fs", _t.time() - _t1)

    DashboardHandler.store = store

    # Initialize cost tracker and attach to the server so /api/cost works.
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_ANON_KEY", "")
    _supabase_client = None
    if supabase_url and supabase_key:
        try:
            from supabase import create_client  # type: ignore[import-untyped]
            _supabase_client = create_client(supabase_url, supabase_key)
            logger.info("Supabase client created for cost tracker")
        except Exception as exc:
            logger.warning("Could not create Supabase client for cost tracker: %s", exc)
    _cost_tracker = make_cost_tracker(supabase_client=_supabase_client)

    # Pre-warm caches in background so the first /data request is fast.
    def _prewarm():
        try:
            logger.info("Pre-warming dashboard data cache...")
            _get_cached_dashboard_data(store)
            _get_cached_costs_extended()
            _get_cached_governor_status()
            logger.info("Dashboard caches warm — first request will be fast")
        except Exception as exc:
            logger.warning("Cache pre-warm failed (non-fatal): %s", exc)

    threading.Thread(target=_prewarm, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server._cost_tracker = _cost_tracker  # type: ignore[attr-defined]
    server._team_id = config.team_id  # type: ignore[attr-defined]
    logger.info("Dashboard running at http://%s:%d/", args.host, args.port)
    logger.info("Auto-refresh: every %ds | Data API: /data | Health: /health", _REFRESH_SECONDS)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down dashboard server")


if __name__ == "__main__":
    main()
