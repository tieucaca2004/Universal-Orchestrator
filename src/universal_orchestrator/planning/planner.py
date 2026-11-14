"""Task Planner interface — superseded, never implemented.

A planner turns a submitted `Task` into an ordered set of steps an
agent can act on. This docstring originally said a concrete planner
would land once the orchestration engine (Milestone 2) could drive
it — the engine now exists, and real task decomposition instead went
through `analysis.analyzer.TaskAnalyzer` (Milestone 2) and
`core.task_manager.TaskManager.decompose()`, never through this
interface. The `role="planner"` references elsewhere in ROADMAP.md
describe an Agent *role string*, not this class.

Audit Finding C-11/16-B (2026-11-14): confirmed, across all of M1-M7,
that no concrete subclass of `TaskPlanner` exists anywhere in this
codebase or its tests — the only reference anywhere is
`tests/test_interfaces.py`'s check that this ABC cannot be
instantiated directly. Kept rather than deleted so this superseded
interface stays visible rather than silently disappearing; not on
any real execution path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from universal_orchestrator.core.models import Task


@dataclass(slots=True)
class PlanStep:
    description: str
    agent_role: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Plan:
    task_id: str
    steps: list[PlanStep] = field(default_factory=list)


class TaskPlanner(ABC):
    """Contract every concrete planner implementation must satisfy."""

    @abstractmethod
    def plan(self, task: Task) -> Plan:
        """Produce an ordered `Plan` for the given task."""
        raise NotImplementedError
