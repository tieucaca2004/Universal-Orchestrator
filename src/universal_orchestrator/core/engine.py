"""Orchestration Engine — Milestone 2 scope only: wires the Task Analyzer,
Task Manager and State Manager together and nothing else.

No Agent Router, no Dispatcher, no ExecutionAdapter integration, no Worker
communication — those are later milestones (see ARCHITECTURE.md). This
engine cannot execute a task; it can only analyze, create, gate and
persist one.
"""

from __future__ import annotations

from dataclasses import dataclass

from universal_orchestrator.analysis.analyzer import TaskAnalyzer
from universal_orchestrator.core.models import Constitution, ProjectProfile, Task, TaskRequest
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.core.task_manager import TaskManager


@dataclass
class OrchestrationEngine:
    analyzer: TaskAnalyzer
    manager: TaskManager

    @classmethod
    def bootstrap(
        cls, state: StateManager, analyzer: TaskAnalyzer | None = None
    ) -> "OrchestrationEngine":
        """Build the engine and recover any interrupted tasks from a prior
        run. Interrupted tasks are only flagged, never auto-resolved — see
        `TaskManager.resolve_interrupted`."""
        manager = TaskManager(state)
        manager.recover()
        return cls(analyzer=analyzer or TaskAnalyzer(), manager=manager)

    def submit(
        self,
        request: TaskRequest,
        project_profile: ProjectProfile,
        constitution: Constitution | None = None,
        *,
        parent_id: str | None = None,
        depends_on: tuple[str, ...] = (),
        priority: int = 0,
        owner: str | None = None,
    ) -> Task:
        """Task Analyzer -> Task Manager -> State Manager, in that order:
        create the task, wire its declared dependencies, analyze it, and
        let the Task Manager decide whether it lands READY or BLOCKED."""
        task = self.manager.create_task(
            request.title,
            project_profile.project_id,
            parent_id=parent_id,
            priority=priority,
            owner=owner,
        )
        for dependency_id in depends_on:
            self.manager.add_dependency(task.task_id, dependency_id)

        specification = self.analyzer.analyze(request, project_profile, constitution)
        self.manager.attach_specification(task.task_id, specification)
        return self.manager.get(task.task_id)
