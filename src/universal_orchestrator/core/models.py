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
    """The Milestone 2 task lifecycle. See docs/TASK_LIFECYCLE.md for the
    full transition diagram and `core.lifecycle` for the enforced rules.

    Renamed/changed from Milestone 1's ``PENDING, PLANNED,
    AWAITING_APPROVAL, DISPATCHED, RUNNING, SUCCEEDED, FAILED, CANCELLED``:
    Milestone 2's Founder-specified lifecycle needs ``ANALYZED``, ``READY``,
    ``BLOCKED`` and ``INTERRUPTED``, and names the initial state ``CREATED``
    rather than ``PENDING``. ``AWAITING_APPROVAL`` and ``DISPATCHED`` are
    dropped rather than kept unused — grep confirmed zero references to
    either anywhere in this repository before the change — and will be
    reintroduced, justified on their own terms, when the Approval Gate
    (Milestone 4) and Dispatcher (Milestone 5) actually exist to drive them.
    """

    CREATED = "created"
    ANALYZED = "analyzed"
    PLANNED = "planned"
    READY = "ready"
    BLOCKED = "blocked"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


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
class TaskError:
    """A structured execution error, distinct from a Python exception so it
    can be persisted and round-tripped through SQLite as plain data."""

    code: str
    message: str
    detail: str = ""


@dataclass(slots=True)
class TaskSpecification:
    """The Task Analyzer's output: a structured breakdown of a TaskRequest.

    `dependencies` is the Analyzer's narrative understanding of what the
    request says it depends on (free text) — it is informational only and
    is never wired into the Task Manager's structural dependency graph
    (`Task.depends_on`, set explicitly via `TaskManager.add_dependency`).
    Conflating the two would let unstructured text silently gate execution.
    """

    objective: str
    scope: str
    constraints: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    expected_deliverables: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TaskRequest:
    """Raw Founder-supplied task request, as given to the Task Analyzer."""

    title: str
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Constitution:
    """Explicit governance constraints supplied to the Task Analyzer.

    FOUNDER_CONSTITUTION.md does not exist in this repository (see the
    Milestone 2 repository audit in ARCHITECTURE.md); until it — or a
    loader for it — exists, a Constitution is always supplied directly by
    the caller, never fabricated or read from a file by the Analyzer.
    """

    rules: list[str] = field(default_factory=list)
    source: str = "inline"


@dataclass(slots=True)
class Task:
    title: str
    project_id: str
    task_id: str = field(default_factory=lambda: _new_id("task"))
    status: TaskStatus = TaskStatus.CREATED
    parent_id: str | None = None
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    priority: int = 0
    owner: str | None = None
    agent_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    specification: TaskSpecification | None = None
    result: Any | None = None
    error: TaskError | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    history: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.history:
            self.history.append((self.status.value, self.updated_at.isoformat()))
