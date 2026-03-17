"""Stub A2A dispatcher for standalone SWE-Squad deployment."""
from __future__ import annotations
import logging
from typing import Optional
from src.a2a.events import PipelineEvent

logger = logging.getLogger(__name__)

async def dispatch_event(event: PipelineEvent, agent: Optional[str] = None) -> bool:
    logger.debug('A2A stub dispatch: %s (agent=%s)', event.event, agent)
    return True
