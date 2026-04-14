"""Shared fixtures for SWE-Squad unit tests."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Provide a stub 'openai' module so tests can patch("openai.OpenAI", ...)
# even when the openai package is not installed.
# ---------------------------------------------------------------------------
if "openai" not in sys.modules:
    _openai_stub = ModuleType("openai")
    _openai_stub.OpenAI = MagicMock  # type: ignore[attr-defined]
    sys.modules["openai"] = _openai_stub
