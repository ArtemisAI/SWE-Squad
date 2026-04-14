"""
Tests for the SWE-Squad Memory Python client.

These tests verify the client interface. For integration tests that
require a running worker service, see test_integration.py.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch, MagicMock

import sys
import os

# Add memory/src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.client import MemoryClient, MemoryObservation, MemorySearchResult


class TestMemoryClientInit(unittest.TestCase):
    """Test client initialization and configuration."""

    def test_default_config(self):
        client = MemoryClient()
        assert client._team_id == os.environ.get("SWE_TEAM_ID", "default")
        assert client._base_url == "http://127.0.0.1:37777"

    def test_custom_config(self):
        client = MemoryClient(
            team_id="alpha",
            host="10.0.0.1",
            port=38888,
            api_key="test-key",
        )
        assert client._team_id == "alpha"
        assert client._base_url == "http://10.0.0.1:38888"
        assert client._api_key == "test-key"

    def test_headers_without_api_key(self):
        client = MemoryClient(api_key=None)
        # Clear env to test no-key path
        with patch.dict(os.environ, {}, clear=True):
            client._api_key = None
            headers = client._headers()
            assert "Authorization" not in headers
            assert headers["Content-Type"] == "application/json"

    def test_headers_with_api_key(self):
        client = MemoryClient(api_key="my-secret")
        headers = client._headers()
        assert headers["Authorization"] == "Bearer my-secret"


class TestMemoryClientSearch(unittest.TestCase):
    """Test search result parsing."""

    def test_empty_search_result(self):
        result = MemorySearchResult(query="test", elapsed_ms=10.0)
        assert result.total == 0
        assert result.observations == []
        assert result.query == "test"

    def test_observation_dataclass(self):
        obs = MemoryObservation(
            id=1,
            project="test-project",
            type="bugfix",
            title="Fixed auth bug",
            narrative="Found and fixed authentication issue",
            facts='["auth was broken"]',
            concepts="auth,security",
            files_read="src/auth.py",
            files_modified="src/auth.py",
            created_at_epoch=1700000000000,
            platform_source="swe-investigator",
        )
        assert obs.id == 1
        assert obs.type == "bugfix"
        assert obs.platform_source == "swe-investigator"


class TestMemoryClientConvenience(unittest.TestCase):
    """Test convenience methods for SWE-Squad agents."""

    @patch.object(MemoryClient, 'search')
    @patch.object(MemoryClient, 'get_context')
    def test_get_investigation_context(self, mock_context, mock_search):
        mock_search.return_value = MemorySearchResult(
            observations=[
                MemoryObservation(
                    id=1, project="test", type="bugfix",
                    title="Similar auth fix",
                    narrative="Fixed auth by updating tokens",
                ),
            ],
            total=1, query="auth bug",
        )
        mock_context.return_value = "## Timeline\n- Did stuff"

        client = MemoryClient(team_id="test")
        result = client.get_investigation_context("auth bug", "test")

        assert "Similar auth fix" in result
        assert "Timeline" in result
        mock_search.assert_called_once()
        mock_context.assert_called_once()

    @patch.object(MemoryClient, 'search')
    @patch.object(MemoryClient, 'get_context')
    def test_investigation_context_empty(self, mock_context, mock_search):
        mock_search.return_value = MemorySearchResult(total=0, query="nothing")
        mock_context.return_value = ""

        client = MemoryClient(team_id="test")
        result = client.get_investigation_context("nothing", "test")

        assert result == ""


if __name__ == "__main__":
    unittest.main()
