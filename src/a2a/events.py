"""Stub A2A events for standalone SWE-Squad deployment."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

@dataclass
class PipelineEvent:
    event: str = ""
    source_stage: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
