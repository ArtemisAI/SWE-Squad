"""Stub A2A models for standalone SWE-Squad deployment."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

class TaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class TaskStatus:
    state: TaskState = TaskState.SUBMITTED
    message: Optional[str] = None

@dataclass
class DataPart:
    data: Any = None

@dataclass
class Message:
    parts: List[Any] = field(default_factory=list)

@dataclass
class Artifact:
    parts: List[Any] = field(default_factory=list)

@dataclass
class Task:
    session_id: Optional[str] = None
    status: TaskStatus = field(default_factory=TaskStatus)
    history: List[Message] = field(default_factory=list)
    artifacts: List[Artifact] = field(default_factory=list)

@dataclass
class AgentSkill:
    id: str = ""
    name: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)

@dataclass
class AgentCard:
    name: str = ""
    description: str = ""
    url: str = ""
    version: str = "0.1.0"
    skills: List[AgentSkill] = field(default_factory=list)
    provider: Dict[str, str] = field(default_factory=dict)
