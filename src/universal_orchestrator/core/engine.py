"""Orchestration Engine — wires the Task Analyzer, Task Manager, State
Manager and (as of Milestone 3) the Agent Router together. Nothing else:
no Dispatcher, no ExecutionAdapter integration, no Worker communication —
those are later milestones (see ARCHITECTURE.md). This engine can
analyze, create, gate, persist and now *route* a task; it still cannot
execute one.
"""

from __future__ import annotations

from dataclasses import dataclass

from universal_orchestrator.analysis.analyzer import TaskAnalyzer
from universal_orchestrator.core.models import (
    Constitution,
    ProjectProfile,
    RoutingDecision,
    Task,
    TaskRequest,
)
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.core.task_manager import TaskManager
from universal_orchestrator.routing.router import AgentRouter


@dataclass
class OrchestrationEngine:
    analyzer: TaskAnalyzer
    manager: TaskManager
    router: AgentRouter | None = None

    @classmethod
    def bootstrap(
        cls,
        state: StateManager,
        analyzer: TaskAnalyzer | None = None,
        router: AgentRouter | None = None,
    ) -> "OrchestrationEngine":
        """Build the engine and recover any interrupted tasks from a prior
        run. Interrupted tasks are only flagged, never auto-resolved — see
        `TaskManager.resolve_interrupted`."""
        manager = TaskManager(state)
        manager.recover()
        return cls(analyzer=analyzer or TaskAnalyzer(), manager=manager, router=router)

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

    def route(self, task_id: str, project_profile: ProjectProfile) -> RoutingDecision:
        """Task Manager -> Agent Router -> Task Manager: fetch the task,
        ask the router who should perform it, and persist the decision
        through the manager so the router never touches storage directly.
        Does not change `Task.status` and does not execute anything."""
        if self.router is None:
            raise RuntimeError("OrchestrationEngine has no router configured")
        task = self.manager.get(task_id)
        decision = self.router.route(task, project_profile)
        self.manager.record_routing_decision(decision)
        return decision
