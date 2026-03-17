"""Stub AgentAdapter base class for standalone SWE-Squad deployment."""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from src.a2a.models import AgentCard, Message, Task

class AgentAdapter(ABC):
    @abstractmethod
    def agent_card(self) -> AgentCard: ...

    @abstractmethod
    async def handle_message(self, message: Message, session_id: Optional[str] = None) -> Task: ...

    def handle_action(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError
