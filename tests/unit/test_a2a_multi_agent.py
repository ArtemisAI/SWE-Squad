"""
Tests for multi-agent A2A integration (issue #21).

Covers:
  - AgentRegistry: register, discover, select, list, expiry, status
  - GenericCLIAdapter: invocation, agent card, availability, registry card
  - GeminiCLIAdapter: Gemini-specific config, invocation
  - OpenCodeCLIAdapter: CLI mode, server mode, availability
  - InvestigatorAgent: fallback agent chain on rate limit exhaustion
  - DeveloperAgent: fallback agent chain on rate limit exhaustion
  - FallbackAgentConfig: config loading from YAML
  - A2A models: new task states
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from src.swe_team.agent_registry import AgentRegistry, WELL_KNOWN_AGENT_CARD_PATH
from src.a2a.adapters.generic_cli_adapter import GenericCLIAdapter
from src.a2a.adapters.gemini_adapter import GeminiCLIAdapter
from src.a2a.adapters.opencode_adapter import OpenCodeCLIAdapter
from src.a2a.models import TaskState
from src.swe_team.config import (
    FallbackAgentConfig,
    SWETeamConfig,
    load_config,
)
from src.swe_team.models import SWETicket, TicketSeverity, TicketStatus
from src.swe_team.investigator import InvestigatorAgent
from src.swe_team.developer import DeveloperAgent
from src.swe_team.config import RateLimitConfig
from src.swe_team.rate_limiter import RateLimitTracker


logging.logAsyncioTasks = False


# ======================================================================
# AgentRegistry
# ======================================================================


class TestAgentRegistry:
    """AgentRegistry: register, discover, select, list, expiry, status."""

    def test_register_and_get(self):
        registry = AgentRegistry()
        card = {
            "name": "test-agent",
            "url": "http://localhost:9000",
            "skills": [{"id": "investigate", "tags": ["investigate"]}],
            "status": "online",
        }
        registry.register(card)
        assert registry.get("test-agent") is not None
        assert registry.get("test-agent")["url"] == "http://localhost:9000"

    def test_register_requires_name(self):
        registry = AgentRegistry()
        with pytest.raises(ValueError, match="name"):
            registry.register({"url": "http://localhost"})

    def test_register_overwrites_existing(self):
        registry = AgentRegistry()
        registry.register({"name": "agent-a", "version": "1.0"})
        registry.register({"name": "agent-a", "version": "2.0"})
        assert registry.get("agent-a")["version"] == "2.0"

    def test_unregister(self):
        registry = AgentRegistry()
        registry.register({"name": "agent-a"})
        assert registry.unregister("agent-a") is True
        assert registry.get("agent-a") is None

    def test_unregister_nonexistent(self):
        registry = AgentRegistry()
        assert registry.unregister("nope") is False

    def test_list_agents_all(self):
        registry = AgentRegistry()
        registry.register({"name": "a", "status": "online"})
        registry.register({"name": "b", "status": "offline"})
        agents = registry.list_agents()
        assert len(agents) == 2

    def test_list_agents_filter_by_status(self):
        registry = AgentRegistry()
        registry.register({"name": "a", "status": "online"})
        registry.register({"name": "b", "status": "offline"})
        online = registry.list_agents(status="online")
        assert len(online) == 1
        assert online[0]["name"] == "a"

    def test_select_agent_by_skill_id(self):
        registry = AgentRegistry()
        registry.register({
            "name": "gemini",
            "status": "online",
            "skills": [{"id": "investigate", "tags": ["investigate"]}],
            "priority": 50,
        })
        result = registry.select_agent(task_type="investigate")
        assert result is not None
        assert result["name"] == "gemini"

    def test_select_agent_by_skill_tag(self):
        registry = AgentRegistry()
        registry.register({
            "name": "opencode",
            "status": "online",
            "skills": [{"id": "code-fix", "tags": ["fix", "patch"]}],
        })
        result = registry.select_agent(task_type="fix")
        assert result is not None
        assert result["name"] == "opencode"

    def test_select_agent_no_match(self):
        registry = AgentRegistry()
        registry.register({
            "name": "agent-x",
            "status": "online",
            "skills": [{"id": "deploy", "tags": ["deploy"]}],
        })
        result = registry.select_agent(task_type="investigate")
        assert result is None

    def test_select_agent_excludes_offline(self):
        registry = AgentRegistry()
        registry.register({
            "name": "agent-offline",
            "status": "offline",
            "skills": [{"id": "investigate", "tags": ["investigate"]}],
        })
        result = registry.select_agent(task_type="investigate")
        assert result is None

    def test_select_agent_with_exclude_list(self):
        registry = AgentRegistry()
        registry.register({
            "name": "agent-a",
            "status": "online",
            "skills": [{"id": "investigate", "tags": ["investigate"]}],
            "priority": 10,
        })
        registry.register({
            "name": "agent-b",
            "status": "online",
            "skills": [{"id": "investigate", "tags": ["investigate"]}],
            "priority": 20,
        })
        result = registry.select_agent(task_type="investigate", exclude=["agent-a"])
        assert result is not None
        assert result["name"] == "agent-b"

    def test_select_agent_priority_sorting(self):
        registry = AgentRegistry()
        registry.register({
            "name": "low-priority",
            "status": "online",
            "skills": [{"id": "investigate", "tags": []}],
            "priority": 100,
        })
        registry.register({
            "name": "high-priority",
            "status": "online",
            "skills": [{"id": "investigate", "tags": []}],
            "priority": 10,
        })
        result = registry.select_agent(task_type="investigate")
        assert result["name"] == "high-priority"

    def test_select_agent_severity_preference(self):
        registry = AgentRegistry()
        registry.register({
            "name": "standard-agent",
            "status": "online",
            "skills": [{"id": "investigate", "tags": ["investigate", "standard"]}],
            "priority": 10,
        })
        registry.register({
            "name": "heavy-agent",
            "status": "online",
            "skills": [{"id": "investigate", "tags": ["investigate", "heavy"]}],
            "priority": 50,
        })
        # For critical severity, should prefer the "heavy" tagged agent
        result = registry.select_agent(task_type="investigate", severity="critical")
        assert result["name"] == "heavy-agent"

    def test_select_agent_severity_preference_high(self):
        registry = AgentRegistry()
        registry.register({
            "name": "standard-agent",
            "status": "online",
            "skills": [{"id": "investigate", "tags": ["investigate", "standard"]}],
            "priority": 10,
        })
        registry.register({
            "name": "high-agent",
            "status": "online",
            "skills": [{"id": "investigate", "tags": ["investigate", "high"]}],
            "priority": 50,
        })
        result = registry.select_agent(task_type="investigate", severity="high")
        assert result["name"] == "high-agent"

    def test_select_agent_default_severity_uses_priority(self):
        registry = AgentRegistry()
        registry.register({
            "name": "agent-a",
            "status": "online",
            "skills": [{"id": "investigate", "tags": ["investigate"]}],
            "priority": 30,
        })
        registry.register({
            "name": "agent-b",
            "status": "online",
            "skills": [{"id": "investigate", "tags": ["investigate"]}],
            "priority": 10,
        })
        # For medium severity, just use priority ordering
        result = registry.select_agent(task_type="investigate", severity="medium")
        assert result["name"] == "agent-b"

    def test_set_status(self):
        registry = AgentRegistry()
        registry.register({"name": "agent-a", "status": "online"})
        assert registry.set_status("agent-a", "offline") is True
        assert registry.get("agent-a")["status"] == "offline"

    def test_set_status_nonexistent(self):
        registry = AgentRegistry()
        assert registry.set_status("nope", "online") is False

    def test_expire_stale_agents(self):
        registry = AgentRegistry(ttl_seconds=0)  # Immediate expiry
        registry.register({"name": "ephemeral", "status": "online"})
        # Force TTL check by listing agents (which calls _expire_stale)
        time.sleep(0.01)
        agents = registry.list_agents()
        assert len(agents) == 0

    def test_non_expired_agents_persist(self):
        registry = AgentRegistry(ttl_seconds=3600)
        registry.register({"name": "stable", "status": "online"})
        agents = registry.list_agents()
        assert len(agents) == 1

    def test_discover_no_urls(self):
        registry = AgentRegistry(discovery_urls=[])
        result = registry.discover()
        assert result == []

    def test_discover_handles_failed_urls(self):
        registry = AgentRegistry(discovery_urls=["http://nonexistent.invalid:9999"])
        with patch.object(registry, "_fetch_agent_card", return_value=None):
            result = registry.discover()
        assert result == []

    def test_discover_registers_found_agents(self):
        mock_card = {
            "name": "discovered-agent",
            "url": "http://remote:8080",
            "skills": [{"id": "investigate", "tags": []}],
        }
        registry = AgentRegistry(discovery_urls=["http://remote:8080"])
        with patch.object(registry, "_fetch_agent_card", return_value=mock_card):
            result = registry.discover()
        assert len(result) == 1
        assert registry.get("discovered-agent") is not None

    def test_get_nonexistent(self):
        registry = AgentRegistry()
        assert registry.get("nonexistent") is None


# ======================================================================
# GenericCLIAdapter
# ======================================================================


class TestGenericCLIAdapter:
    """GenericCLIAdapter: invocation, agent card, availability, registry card."""

    def test_agent_card(self):
        adapter = GenericCLIAdapter(
            name="test-agent",
            command="/usr/bin/test",
            skills=[
                {"id": "investigate", "name": "Investigate", "description": "desc", "tags": ["investigate"]},
            ],
            version="1.2.3",
            provider={"organization": "TestOrg"},
        )
        card = adapter.agent_card()
        assert card.name == "test-agent"
        assert card.version == "1.2.3"
        assert len(card.skills) == 1
        assert card.skills[0].id == "investigate"
        assert card.provider == {"organization": "TestOrg"}

    def test_invoke_success(self):
        adapter = GenericCLIAdapter(
            name="test-agent",
            command="/usr/bin/echo",
            args_template=["{prompt}"],
            prompt_via_stdin=False,
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "investigation result\n"
        mock_result.stderr = ""

        with patch("src.a2a.adapters.generic_cli_adapter.subprocess.run", return_value=mock_result):
            result = adapter.invoke("test prompt")

        assert result == "investigation result"

    def test_invoke_failure(self):
        adapter = GenericCLIAdapter(
            name="test-agent",
            command="/usr/bin/failing",
        )

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Command failed"

        with patch("src.a2a.adapters.generic_cli_adapter.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="test-agent: Command failed"):
                adapter.invoke("test prompt")

    def test_invoke_timeout(self):
        adapter = GenericCLIAdapter(
            name="test-agent",
            command="/usr/bin/slow",
            timeout=1,
        )

        with patch(
            "src.a2a.adapters.generic_cli_adapter.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="slow", timeout=1),
        ):
            with pytest.raises(subprocess.TimeoutExpired):
                adapter.invoke("test prompt")

    def test_invoke_with_model_override(self):
        adapter = GenericCLIAdapter(
            name="test-agent",
            command="/usr/bin/agent",
            args_template=["--model", "{model}", "-p", "{prompt}"],
            default_model="default-model",
            prompt_via_stdin=False,
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "result\n"
        mock_result.stderr = ""

        with patch("src.a2a.adapters.generic_cli_adapter.subprocess.run", return_value=mock_result) as mock_run:
            adapter.invoke("prompt text", model="custom-model")

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "custom-model"

    def test_invoke_stdin_mode(self):
        adapter = GenericCLIAdapter(
            name="stdin-agent",
            command="/usr/bin/agent",
            args_template=["--run"],
            prompt_via_stdin=True,
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ok\n"
        mock_result.stderr = ""

        with patch("src.a2a.adapters.generic_cli_adapter.subprocess.run", return_value=mock_result) as mock_run:
            adapter.invoke("my prompt")

        call_args = mock_run.call_args
        assert call_args[1]["input"] == "my prompt"

    def test_is_available_true(self, tmp_path):
        # Create a fake executable
        fake_bin = tmp_path / "fake_agent"
        fake_bin.write_text("#!/bin/sh\necho ok")
        fake_bin.chmod(0o755)

        adapter = GenericCLIAdapter(
            name="test",
            command=str(fake_bin),
        )
        assert adapter.is_available() is True

    def test_is_available_false(self):
        adapter = GenericCLIAdapter(
            name="test",
            command="/nonexistent/binary",
        )
        assert adapter.is_available() is False

    def test_to_registry_card(self):
        adapter = GenericCLIAdapter(
            name="test-agent",
            command="/nonexistent/binary",
            skills=[{"id": "investigate", "name": "Investigate", "description": "", "tags": ["investigate"]}],
            priority=42,
            version="1.0.0",
        )
        card = adapter.to_registry_card()
        assert card["name"] == "test-agent"
        assert card["priority"] == 42
        assert card["status"] == "offline"
        assert card["adapter_type"] == "generic_cli"
        assert len(card["skills"]) == 1

    def test_to_registry_card_online(self, tmp_path):
        fake_bin = tmp_path / "agent"
        fake_bin.write_text("#!/bin/sh\necho ok")
        fake_bin.chmod(0o755)

        adapter = GenericCLIAdapter(
            name="test",
            command=str(fake_bin),
        )
        card = adapter.to_registry_card()
        assert card["status"] == "online"

    def test_handle_message_success(self):
        from src.a2a.models import DataPart, Message

        adapter = GenericCLIAdapter(
            name="test-agent",
            command="/usr/bin/test",
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "response text\n"
        mock_result.stderr = ""

        message = Message(parts=[DataPart(data={"prompt": "test prompt"})])

        with patch("src.a2a.adapters.generic_cli_adapter.subprocess.run", return_value=mock_result):
            task = asyncio.run(
                adapter.handle_message(message, session_id="sess-1")
            )

        assert task.status.state == TaskState.COMPLETED
        assert task.session_id == "sess-1"
        assert len(task.artifacts) == 1

    def test_handle_message_no_prompt(self):
        from src.a2a.models import Message

        adapter = GenericCLIAdapter(
            name="test-agent",
            command="/usr/bin/test",
        )
        message = Message(parts=[])

        task = asyncio.run(
            adapter.handle_message(message)
        )
        assert task.status.state == TaskState.FAILED
        assert "No prompt" in (task.status.message or "")

    def test_build_command(self):
        adapter = GenericCLIAdapter(
            name="test",
            command="/usr/bin/agent",
            args_template=["--model", "{model}", "-p", "{prompt}"],
        )
        cmd = adapter._build_command("hello world", "gpt-4")
        assert cmd == ["/usr/bin/agent", "--model", "gpt-4", "-p", "hello world"]

    def test_invoke_with_env(self):
        adapter = GenericCLIAdapter(
            name="test",
            command="/usr/bin/agent",
            env={"CUSTOM_VAR": "value"},
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ok"
        mock_result.stderr = ""

        with patch("src.a2a.adapters.generic_cli_adapter.subprocess.run", return_value=mock_result) as mock_run:
            adapter.invoke("test")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["env"]["CUSTOM_VAR"] == "value"

    def test_invoke_with_cwd(self, tmp_path):
        adapter = GenericCLIAdapter(
            name="test",
            command="/usr/bin/agent",
            cwd=str(tmp_path),
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ok"
        mock_result.stderr = ""

        with patch("src.a2a.adapters.generic_cli_adapter.subprocess.run", return_value=mock_result) as mock_run:
            adapter.invoke("test")

        assert mock_run.call_args[1]["cwd"] == str(tmp_path)


# ======================================================================
# GeminiCLIAdapter
# ======================================================================


class TestGeminiCLIAdapter:
    """GeminiCLIAdapter: Gemini-specific config and invocation."""

    def test_default_config(self):
        adapter = GeminiCLIAdapter()
        assert adapter._name == "gemini-cli"
        assert adapter._command == "/usr/bin/gemini"
        assert adapter._default_model == "gemini-2.5-flash"
        assert adapter._prompt_via_stdin is False

    def test_agent_card(self):
        adapter = GeminiCLIAdapter()
        card = adapter.agent_card()
        assert card.name == "gemini-cli"
        assert card.provider == {"organization": "Google"}
        assert len(card.skills) == 3
        skill_ids = {s.id for s in card.skills}
        assert "investigate" in skill_ids
        assert "fix" in skill_ids
        assert "review" in skill_ids

    def test_custom_model(self):
        adapter = GeminiCLIAdapter(default_model="gemini-2.5-pro")
        assert adapter._default_model == "gemini-2.5-pro"

    def test_sandbox_flag(self):
        adapter = GeminiCLIAdapter(sandbox=True)
        assert "--sandbox" in adapter._args_template

    def test_no_sandbox_by_default(self):
        adapter = GeminiCLIAdapter()
        assert "--sandbox" not in adapter._args_template

    def test_extra_args(self):
        adapter = GeminiCLIAdapter(extra_args=["--verbose", "--json"])
        assert "--verbose" in adapter._args_template
        assert "--json" in adapter._args_template

    def test_invoke_calls_cli(self):
        adapter = GeminiCLIAdapter()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "gemini says hello\n"
        mock_result.stderr = ""

        with patch("src.a2a.adapters.generic_cli_adapter.subprocess.run", return_value=mock_result) as mock_run:
            result = adapter.invoke("test prompt")

        assert result == "gemini says hello"
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/gemini"
        assert "-p" in cmd

    def test_invoke_with_model_override(self):
        adapter = GeminiCLIAdapter()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "result\n"
        mock_result.stderr = ""

        with patch("src.a2a.adapters.generic_cli_adapter.subprocess.run", return_value=mock_result) as mock_run:
            adapter.invoke("test", model="gemini-2.5-pro")

        cmd = mock_run.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "gemini-2.5-pro"

    def test_registry_card(self):
        adapter = GeminiCLIAdapter()
        card = adapter.to_registry_card()
        assert card["name"] == "gemini-cli"
        assert card["priority"] == 50
        assert card["adapter_type"] == "generic_cli"

    def test_priority_override(self):
        adapter = GeminiCLIAdapter(priority=25)
        card = adapter.to_registry_card()
        assert card["priority"] == 25


# ======================================================================
# OpenCodeCLIAdapter
# ======================================================================


class TestOpenCodeCLIAdapter:
    """OpenCodeCLIAdapter: CLI mode, server mode, availability."""

    def test_default_config(self):
        adapter = OpenCodeCLIAdapter()
        assert adapter._name == "opencode"
        assert adapter._command == "/usr/local/bin/opencode"
        assert adapter._mode == "cli"

    def test_agent_card(self):
        adapter = OpenCodeCLIAdapter()
        card = adapter.agent_card()
        assert card.name == "opencode"
        assert card.provider == {"organization": "OpenCode"}
        skill_ids = {s.id for s in card.skills}
        assert "investigate" in skill_ids
        assert "fix" in skill_ids
        assert "refactor" in skill_ids

    def test_cli_mode_invocation(self):
        adapter = OpenCodeCLIAdapter(mode="cli")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "opencode output\n"
        mock_result.stderr = ""

        with patch("src.a2a.adapters.generic_cli_adapter.subprocess.run", return_value=mock_result):
            result = adapter.invoke("test prompt")

        assert result == "opencode output"

    def test_server_mode_invocation(self):
        adapter = OpenCodeCLIAdapter(
            mode="server",
            server_url="http://localhost:8080",
        )

        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = json.dumps({
            "result": {
                "artifacts": [
                    {"parts": [{"kind": "text", "text": "server response"}]}
                ]
            }
        }).encode("utf-8")

        with patch("src.a2a.adapters.opencode_adapter.urllib.request.urlopen", return_value=mock_response):
            result = adapter.invoke("test prompt")

        assert result == "server response"

    def test_server_mode_fallback_to_message(self):
        adapter = OpenCodeCLIAdapter(
            mode="server",
            server_url="http://localhost:8080",
        )

        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = json.dumps({
            "result": {
                "message": {
                    "parts": [{"kind": "text", "text": "message response"}]
                }
            }
        }).encode("utf-8")

        with patch("src.a2a.adapters.opencode_adapter.urllib.request.urlopen", return_value=mock_response):
            result = adapter.invoke("test prompt")

        assert result == "message response"

    def test_server_mode_no_url(self):
        adapter = OpenCodeCLIAdapter(mode="server", server_url=None)
        with pytest.raises(RuntimeError, match="server URL not configured"):
            adapter.invoke("test")

    def test_is_available_cli_mode(self, tmp_path):
        fake_bin = tmp_path / "opencode"
        fake_bin.write_text("#!/bin/sh\necho ok")
        fake_bin.chmod(0o755)

        adapter = OpenCodeCLIAdapter(opencode_path=str(fake_bin), mode="cli")
        assert adapter.is_available() is True

    def test_is_available_server_mode_healthy(self):
        adapter = OpenCodeCLIAdapter(
            mode="server",
            server_url="http://localhost:8080",
        )

        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.status = 200

        with patch("src.a2a.adapters.opencode_adapter.urllib.request.urlopen", return_value=mock_response):
            assert adapter.is_available() is True

    def test_is_available_server_mode_unhealthy(self):
        adapter = OpenCodeCLIAdapter(
            mode="server",
            server_url="http://localhost:8080",
        )
        with patch("src.a2a.adapters.opencode_adapter.urllib.request.urlopen", side_effect=Exception("conn refused")):
            assert adapter.is_available() is False

    def test_is_available_server_mode_no_url(self):
        adapter = OpenCodeCLIAdapter(mode="server", server_url=None)
        assert adapter.is_available() is False

    def test_registry_card_includes_mode(self):
        adapter = OpenCodeCLIAdapter(
            mode="server",
            server_url="http://localhost:8080",
        )
        card = adapter.to_registry_card()
        assert card["mode"] == "server"
        assert card["server_url"] == "http://localhost:8080"
        assert card["adapter_type"] == "opencode"

    def test_extract_response_text_empty(self):
        result = OpenCodeCLIAdapter._extract_response_text({"result": {}})
        assert isinstance(result, str)


# ======================================================================
# A2A Models — Task States
# ======================================================================


class TestA2ATaskStates:
    """A2A models: new task states from the spec."""

    def test_all_task_states_exist(self):
        assert TaskState.SUBMITTED == "submitted"
        assert TaskState.WORKING == "working"
        assert TaskState.INPUT_REQUIRED == "input_required"
        assert TaskState.COMPLETED == "completed"
        assert TaskState.FAILED == "failed"
        assert TaskState.CANCELED == "canceled"
        assert TaskState.REJECTED == "rejected"

    def test_task_state_enum_count(self):
        assert len(TaskState) == 7


# ======================================================================
# FallbackAgentConfig
# ======================================================================


class TestFallbackAgentConfig:
    """FallbackAgentConfig: config loading and serialization."""

    def test_defaults(self):
        cfg = FallbackAgentConfig()
        assert cfg.name == ""
        assert cfg.command == ""
        assert cfg.enabled is False
        assert cfg.priority == 100
        assert cfg.timeout == 120
        assert cfg.skills == []

    def test_from_dict(self):
        cfg = FallbackAgentConfig.from_dict({
            "name": "gemini-cli",
            "command": "/usr/bin/gemini",
            "args_template": ["-p", "{prompt}"],
            "default_model": "gemini-2.5-flash",
            "enabled": True,
            "priority": 50,
            "timeout": 120,
            "prompt_via_stdin": False,
            "skills": ["investigate", "fix"],
        })
        assert cfg.name == "gemini-cli"
        assert cfg.command == "/usr/bin/gemini"
        assert cfg.enabled is True
        assert cfg.priority == 50
        assert cfg.skills == ["investigate", "fix"]

    def test_from_dict_defaults(self):
        cfg = FallbackAgentConfig.from_dict({})
        assert cfg.name == ""
        assert cfg.enabled is False
        assert cfg.priority == 100

    def test_to_dict(self):
        cfg = FallbackAgentConfig(
            name="test",
            command="/bin/test",
            enabled=True,
            priority=42,
            skills=["investigate"],
        )
        d = cfg.to_dict()
        assert d["name"] == "test"
        assert d["command"] == "/bin/test"
        assert d["enabled"] is True
        assert d["priority"] == 42
        assert d["skills"] == ["investigate"]

    def test_roundtrip(self):
        original = FallbackAgentConfig(
            name="gemini",
            command="/usr/bin/gemini",
            args_template=["-p", "{prompt}", "--model", "{model}"],
            default_model="gemini-2.5-flash",
            enabled=True,
            priority=50,
            timeout=120,
            prompt_via_stdin=False,
            skills=["investigate", "fix"],
        )
        d = original.to_dict()
        restored = FallbackAgentConfig.from_dict(d)
        assert restored.name == original.name
        assert restored.command == original.command
        assert restored.args_template == original.args_template
        assert restored.enabled == original.enabled
        assert restored.skills == original.skills


class TestSWETeamConfigFallback:
    """SWETeamConfig with fallback agents."""

    def test_config_includes_fallback_agents(self):
        config = SWETeamConfig()
        assert hasattr(config, "fallback_agents")
        assert config.fallback_agents == []

    def test_config_from_dict_with_fallback_agents(self):
        config = SWETeamConfig.from_dict({
            "fallback_agents": [
                {
                    "name": "gemini-cli",
                    "command": "/usr/bin/gemini",
                    "enabled": True,
                    "priority": 50,
                    "skills": ["investigate", "fix"],
                },
                {
                    "name": "opencode",
                    "command": "/usr/local/bin/opencode",
                    "enabled": False,
                    "priority": 60,
                },
            ],
        })
        assert len(config.fallback_agents) == 2
        assert config.fallback_agents[0].name == "gemini-cli"
        assert config.fallback_agents[0].enabled is True
        assert config.fallback_agents[1].name == "opencode"
        assert config.fallback_agents[1].enabled is False

    def test_config_to_dict_includes_fallback_agents(self):
        config = SWETeamConfig(
            fallback_agents=[
                FallbackAgentConfig(name="test", enabled=True),
            ],
        )
        d = config.to_dict()
        assert "fallback_agents" in d
        assert len(d["fallback_agents"]) == 1
        assert d["fallback_agents"][0]["name"] == "test"

    def test_load_config_with_fallback_agents(self, tmp_path):
        yaml_content = """
enabled: false
fallback_agents:
  - name: "gemini-cli"
    command: "/usr/bin/gemini"
    args_template: ["-p", "{prompt}", "--model", "{model}"]
    default_model: "gemini-2.5-flash"
    enabled: true
    priority: 50
    timeout: 120
    prompt_via_stdin: false
    skills: ["investigate", "fix"]
"""
        cfg_file = tmp_path / "test_config.yaml"
        cfg_file.write_text(yaml_content)
        config = load_config(str(cfg_file))
        assert len(config.fallback_agents) == 1
        assert config.fallback_agents[0].name == "gemini-cli"
        assert config.fallback_agents[0].enabled is True
        assert config.fallback_agents[0].skills == ["investigate", "fix"]


# ======================================================================
# InvestigatorAgent fallback integration
# ======================================================================


class TestInvestigatorFallback:
    """Investigator: fallback agents when Claude is rate-limited."""

    def test_investigator_accepts_fallback_agents(self, tmp_path):
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        mock_agent = MagicMock()
        agent = InvestigatorAgent(
            program_path=program,
            claude_path="/usr/bin/claude",
            fallback_agents=[mock_agent],
        )
        assert len(agent._fallback_agents) == 1

    def test_investigator_default_no_fallback(self, tmp_path):
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        agent = InvestigatorAgent(
            program_path=program,
            claude_path="/usr/bin/claude",
        )
        assert agent._fallback_agents == []

    def test_investigator_fallback_on_rate_limit(self, tmp_path):
        """When Claude hits rate limit, fallback agent should be tried."""
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        ticket = SWETicket(
            title="Test crash",
            description="boom",
            severity=TicketSeverity.HIGH,
            source_module="testing",
            error_log="Traceback: boom",
        )
        ticket.transition(TicketStatus.TRIAGED)

        def always_rate_limited(*args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stderr = "Rate limit hit (429)"
            result.stdout = ""
            return result

        mock_fallback = MagicMock()
        mock_fallback._name = "gemini-cli"
        mock_fallback.is_available.return_value = True
        mock_fallback.invoke.return_value = "Fallback investigation: root cause found"

        rl_config = RateLimitConfig(
            max_retries_on_429=1,
            initial_backoff_seconds=0.01,
            max_backoff_seconds=0.1,
        )

        with (
            patch("src.swe_team.investigator.subprocess.run", side_effect=always_rate_limited),
            patch("src.swe_team.investigator.notify_investigation_summary"),
        ):
            agent = InvestigatorAgent(
                program_path=program,
                claude_path="/usr/bin/claude",
                rate_limit_config=rl_config,
                fallback_agents=[mock_fallback],
            )
            result = agent.investigate(ticket)

        assert result is True
        assert ticket.investigation_report == "Fallback investigation: root cause found"
        assert ticket.metadata.get("fallback_agent_used") == "gemini-cli"
        mock_fallback.invoke.assert_called_once()

    def test_investigator_fallback_all_fail(self, tmp_path):
        """When all fallback agents fail, should still mark rate limited."""
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        ticket = SWETicket(
            title="Test crash",
            description="boom",
            severity=TicketSeverity.HIGH,
            source_module="testing",
            error_log="Traceback: boom",
        )
        ticket.transition(TicketStatus.TRIAGED)

        def always_rate_limited(*args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stderr = "Rate limit hit (429)"
            result.stdout = ""
            return result

        mock_fallback = MagicMock()
        mock_fallback._name = "gemini-cli"
        mock_fallback.is_available.return_value = True
        mock_fallback.invoke.side_effect = RuntimeError("Gemini also failed")

        rl_config = RateLimitConfig(
            max_retries_on_429=1,
            initial_backoff_seconds=0.01,
            max_backoff_seconds=0.1,
        )

        with (
            patch("src.swe_team.investigator.subprocess.run", side_effect=always_rate_limited),
            patch("src.swe_team.investigator.notify_investigation_summary"),
            patch("src.swe_team.telegram.send_message", return_value=True),
        ):
            agent = InvestigatorAgent(
                program_path=program,
                claude_path="/usr/bin/claude",
                rate_limit_config=rl_config,
                fallback_agents=[mock_fallback],
            )
            result = agent.investigate(ticket)

        assert result is False
        assert ticket.metadata.get("rate_limited") is True

    def test_investigator_fallback_skips_unavailable(self, tmp_path):
        """Unavailable fallback agents should be skipped."""
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        ticket = SWETicket(
            title="Test crash",
            description="boom",
            severity=TicketSeverity.HIGH,
            source_module="testing",
            error_log="Traceback: boom",
        )
        ticket.transition(TicketStatus.TRIAGED)

        def always_rate_limited(*args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stderr = "Rate limit hit (429)"
            result.stdout = ""
            return result

        # First agent unavailable, second available
        mock_unavailable = MagicMock()
        mock_unavailable._name = "unavailable-agent"
        mock_unavailable.is_available.return_value = False

        mock_available = MagicMock()
        mock_available._name = "available-agent"
        mock_available.is_available.return_value = True
        mock_available.invoke.return_value = "Found the bug"

        rl_config = RateLimitConfig(
            max_retries_on_429=1,
            initial_backoff_seconds=0.01,
            max_backoff_seconds=0.1,
        )

        with (
            patch("src.swe_team.investigator.subprocess.run", side_effect=always_rate_limited),
            patch("src.swe_team.investigator.notify_investigation_summary"),
        ):
            agent = InvestigatorAgent(
                program_path=program,
                claude_path="/usr/bin/claude",
                rate_limit_config=rl_config,
                fallback_agents=[mock_unavailable, mock_available],
            )
            result = agent.investigate(ticket)

        assert result is True
        assert ticket.metadata.get("fallback_agent_used") == "available-agent"
        mock_unavailable.invoke.assert_not_called()
        mock_available.invoke.assert_called_once()

    def test_investigator_no_fallback_on_success(self, tmp_path):
        """When Claude succeeds, fallback agents should not be invoked."""
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        ticket = SWETicket(
            title="Test crash",
            description="boom",
            severity=TicketSeverity.HIGH,
            source_module="testing",
            error_log="Traceback: boom",
        )
        ticket.transition(TicketStatus.TRIAGED)

        def claude_success(*args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = "Root cause: found\n"
            result.stderr = ""
            return result

        mock_fallback = MagicMock()

        with (
            patch("src.swe_team.investigator.subprocess.run", side_effect=claude_success),
            patch("src.swe_team.investigator.notify_investigation_summary"),
        ):
            agent = InvestigatorAgent(
                program_path=program,
                claude_path="/usr/bin/claude",
                fallback_agents=[mock_fallback],
            )
            result = agent.investigate(ticket)

        assert result is True
        mock_fallback.invoke.assert_not_called()

    def test_try_fallback_agents_empty_list(self, tmp_path):
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        agent = InvestigatorAgent(
            program_path=program,
            claude_path="/usr/bin/claude",
            fallback_agents=[],
        )
        ticket = SWETicket(title="t", description="d")
        result = agent._try_fallback_agents("prompt", ticket)
        assert result is None

    def test_try_fallback_agents_empty_response(self, tmp_path):
        """Fallback agents returning empty strings should be skipped."""
        program = tmp_path / "investigate.md"
        program.write_text("Error: {error_log}\nModule: {source_module}\n")

        mock_fallback = MagicMock()
        mock_fallback._name = "empty-agent"
        mock_fallback.is_available.return_value = True
        mock_fallback.invoke.return_value = "   "  # whitespace only

        agent = InvestigatorAgent(
            program_path=program,
            claude_path="/usr/bin/claude",
            fallback_agents=[mock_fallback],
        )
        ticket = SWETicket(title="t", description="d")
        result = agent._try_fallback_agents("prompt", ticket)
        assert result is None


# ======================================================================
# DeveloperAgent fallback integration
# ======================================================================


class TestDeveloperFallback:
    """Developer: fallback agents when Claude is rate-limited."""

    def test_developer_accepts_fallback_agents(self, tmp_path):
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")

        dev = DeveloperAgent(
            repo_root=tmp_path,
            program_path=program,
            fallback_agents=[MagicMock()],
        )
        assert len(dev._fallback_agents) == 1

    def test_developer_default_no_fallback(self, tmp_path):
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")

        dev = DeveloperAgent(
            repo_root=tmp_path,
            program_path=program,
        )
        assert dev._fallback_agents == []

    def test_try_fallback_agents_empty(self, tmp_path):
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")

        dev = DeveloperAgent(
            repo_root=tmp_path,
            program_path=program,
            fallback_agents=[],
        )
        ticket = SWETicket(title="t", description="d")
        result = dev._try_fallback_agents("prompt", ticket, 120)
        assert result is False

    def test_try_fallback_agents_success(self, tmp_path):
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")

        mock_agent = MagicMock()
        mock_agent._name = "gemini-cli"
        mock_agent.is_available.return_value = True
        mock_agent.invoke.return_value = "fix applied"

        dev = DeveloperAgent(
            repo_root=tmp_path,
            program_path=program,
            fallback_agents=[mock_agent],
        )
        ticket = SWETicket(title="bug", description="fix it")
        result = dev._try_fallback_agents("fix prompt", ticket, 120)
        assert result is True
        assert ticket.metadata.get("fallback_agent_used") == "gemini-cli"

    def test_try_fallback_agents_all_fail(self, tmp_path):
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")

        mock_agent = MagicMock()
        mock_agent._name = "failing-agent"
        mock_agent.is_available.return_value = True
        mock_agent.invoke.side_effect = RuntimeError("nope")

        dev = DeveloperAgent(
            repo_root=tmp_path,
            program_path=program,
            fallback_agents=[mock_agent],
        )
        ticket = SWETicket(title="bug", description="fix it")
        result = dev._try_fallback_agents("fix prompt", ticket, 120)
        assert result is False

    def test_try_fallback_agents_skips_unavailable(self, tmp_path):
        program = tmp_path / "fix.md"
        program.write_text("{ticket_id} {title} {severity} {source_module} {investigation_report}")

        mock_unavailable = MagicMock()
        mock_unavailable._name = "down"
        mock_unavailable.is_available.return_value = False

        mock_available = MagicMock()
        mock_available._name = "up"
        mock_available.is_available.return_value = True
        mock_available.invoke.return_value = "done"

        dev = DeveloperAgent(
            repo_root=tmp_path,
            program_path=program,
            fallback_agents=[mock_unavailable, mock_available],
        )
        ticket = SWETicket(title="bug", description="fix it")
        result = dev._try_fallback_agents("fix prompt", ticket, 120)
        assert result is True
        assert ticket.metadata["fallback_agent_used"] == "up"
        mock_unavailable.invoke.assert_not_called()


# ======================================================================
# Integration: Registry + Adapters
# ======================================================================


class TestRegistryAdapterIntegration:
    """Integration between AgentRegistry and CLI adapters."""

    def test_register_gemini_adapter(self):
        registry = AgentRegistry()
        adapter = GeminiCLIAdapter()
        registry.register(adapter.to_registry_card())
        agents = registry.list_agents()
        assert len(agents) == 1
        assert agents[0]["name"] == "gemini-cli"

    def test_register_opencode_adapter(self):
        registry = AgentRegistry()
        adapter = OpenCodeCLIAdapter()
        registry.register(adapter.to_registry_card())
        agents = registry.list_agents()
        assert len(agents) == 1
        assert agents[0]["name"] == "opencode"

    def test_select_gemini_for_investigation(self):
        registry = AgentRegistry()
        adapter = GeminiCLIAdapter()
        card = adapter.to_registry_card()
        card["status"] = "online"  # Override since binary doesn't exist in test
        registry.register(card)

        selected = registry.select_agent(task_type="investigate")
        assert selected is not None
        assert selected["name"] == "gemini-cli"

    def test_select_from_multiple_adapters(self):
        registry = AgentRegistry()

        gemini = GeminiCLIAdapter(priority=50)
        opencode = OpenCodeCLIAdapter(priority=60)

        gemini_card = gemini.to_registry_card()
        gemini_card["status"] = "online"
        opencode_card = opencode.to_registry_card()
        opencode_card["status"] = "online"

        registry.register(gemini_card)
        registry.register(opencode_card)

        # Both can investigate, Gemini has higher priority (lower number)
        selected = registry.select_agent(task_type="investigate")
        assert selected["name"] == "gemini-cli"

    def test_fallback_when_primary_excluded(self):
        registry = AgentRegistry()

        gemini = GeminiCLIAdapter(priority=50)
        opencode = OpenCodeCLIAdapter(priority=60)

        gemini_card = gemini.to_registry_card()
        gemini_card["status"] = "online"
        opencode_card = opencode.to_registry_card()
        opencode_card["status"] = "online"

        registry.register(gemini_card)
        registry.register(opencode_card)

        # Exclude Gemini, should fall back to OpenCode
        selected = registry.select_agent(task_type="investigate", exclude=["gemini-cli"])
        assert selected is not None
        assert selected["name"] == "opencode"

    def test_register_generic_adapter(self):
        registry = AgentRegistry()
        adapter = GenericCLIAdapter(
            name="custom-agent",
            command="/usr/bin/custom",
            skills=[{"id": "investigate", "name": "Investigate", "description": "", "tags": ["investigate"]}],
            priority=75,
        )
        card = adapter.to_registry_card()
        card["status"] = "online"
        registry.register(card)

        selected = registry.select_agent(task_type="investigate")
        assert selected["name"] == "custom-agent"
