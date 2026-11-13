"""Core data models shared across all Universal-Orchestrator components."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TaskStatus(enum.Enum):
    PENDING = "pending"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


@dataclass(slots=True)
class ProjectProfile:
    name: str
    repository_path: str
    project_id: str = field(default_factory=lambda: _new_id("proj"))
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentDescriptor:
    name: str
    role: str
    agent_id: str = field(default_factory=lambda: _new_id("agent"))
    provider_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderDescriptor:
    name: str
    kind: str  # e.g. "cli", "api", "runtime-adapter"
    provider_id: str = field(default_factory=lambda: _new_id("prov"))
    # Must stay False until an actual execution + authentication check
    # against the live provider has succeeded. Nothing may dispatch to a
    # provider while this is False (see errors.ProviderNotVerifiedError).
    verified: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Task:
    title: str
    project_id: str
    task_id: str = field(default_factory=lambda: _new_id("task"))
    status: TaskStatus = TaskStatus.PENDING
    agent_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    history: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.history:
            self.history.append((self.status.value, self.updated_at.isoformat()))
