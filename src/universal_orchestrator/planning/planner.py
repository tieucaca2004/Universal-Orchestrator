"""Task Planner interface.

A planner turns a submitted `Task` into an ordered set of steps an agent
can act on. Concrete planners (rule-based, provider-delegated, ...) land
once the orchestration engine (Milestone 2) can drive them.
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
