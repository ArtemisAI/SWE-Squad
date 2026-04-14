"""Unit tests for src/swe_team/proxy_model_policy.py."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.swe_team.proxy_model_policy import ProxyModelPolicyResolver


class TestProxyModelPolicyResolver(unittest.TestCase):
    def _write_policy(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        policy = {
            "version": 1,
            "defaults": {"model_provider": "openai"},
            "models": [
                {
                    "short_name": "qwen3",
                    "model_id": "qwen3-coder:480b-cloud",
                    "provider": "openai",
                    "status": "healthy",
                    "role_hint": "primary coder",
                },
                {
                    "short_name": "deepseek-v3",
                    "model_id": "deepseek-v3.1:671b-cloud",
                    "provider": "openai",
                    "status": "healthy",
                    "role_hint": "reasoning and fallback",
                },
                {
                    "short_name": "gemini-pro",
                    "model_id": "gemini-2.5-pro",
                    "provider": "openai",
                    "status": "failing",
                    "failure_mode": "403 token_rejected",
                },
            ],
        }
        Path(path).write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
        return path

    def test_disabled_without_proxy_env(self):
        with patch.dict(os.environ, {}, clear=True):
            r = ProxyModelPolicyResolver(policy_path="/nonexistent.yaml")
            self.assertFalse(r.enabled)
            self.assertEqual(r.resolve("qwen3"), "qwen3")

    def test_alias_resolves_when_enabled(self):
        policy = self._write_policy()
        try:
            with patch.dict(os.environ, {"SWE_PROXY_POLICY_ENABLED": "true"}, clear=False):
                r = ProxyModelPolicyResolver(policy_path=policy)
                self.assertTrue(r.enabled)
                self.assertEqual(r.resolve("qwen3", tier="t2_standard"), "openai/qwen3-coder:480b-cloud")
        finally:
            Path(policy).unlink(missing_ok=True)

    def test_failing_model_falls_back_to_healthy(self):
        policy = self._write_policy()
        try:
            with patch.dict(os.environ, {"SWE_PROXY_POLICY_ENABLED": "true"}, clear=False):
                r = ProxyModelPolicyResolver(policy_path=policy)
                # t1 should prefer reasoning fallback (deepseek)
                self.assertEqual(r.resolve("gemini-pro", tier="t1_heavy"), "openai/deepseek-v3.1:671b-cloud")
                # t2 should prefer coder fallback (qwen)
                self.assertEqual(r.resolve("gemini-pro", tier="t2_standard"), "openai/qwen3-coder:480b-cloud")
        finally:
            Path(policy).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
