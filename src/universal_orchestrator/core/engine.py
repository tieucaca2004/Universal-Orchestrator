"""Orchestration Engine — wires the Task Analyzer, Task Manager, State
Manager, Agent Router (Milestone 3) and now an Execution Adapter
(Milestone 4) together. Nothing else: no Dispatcher, no Worker
communication, no Approval Gate — those are later milestones (see
ARCHITECTURE.md).

`execute()` deliberately will not run anything unless `route()` already
produced a `RoutingOutcome.SELECTED` decision for that task — it never
picks an agent/provider on its own, and it never defaults
`working_directory` to a project's real repository path. The caller
supplies that explicitly every time, so the live repository is never
touched just because an adapter happened to start successfully.
"""

from __future__ import annotations

from dataclasses import dataclass

from universal_orchestrator.analysis.analyzer import TaskAnalyzer
from universal_orchestrator.core.models import (
    Constitution,
    ProjectProfile,
    RoutingDecision,
    RoutingOutcome,
    Task,
    TaskRequest,
)
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.core.task_manager import TaskManager
from universal_orchestrator.errors import OrchestratorError
from universal_orchestrator.execution.adapter import ExecutionPolicy, ExecutionRequest, ExecutionResult
from universal_orchestrator.execution.registry import AdapterRegistry
from universal_orchestrator.registry.agents import AgentRegistry
from universal_orchestrator.routing.router import AgentRouter


@dataclass
class OrchestrationEngine:
    analyzer: TaskAnalyzer
    manager: TaskManager
    router: AgentRouter | None = None
    adapters: AdapterRegistry | None = None
    agents: AgentRegistry | None = None

    @classmethod
    def bootstrap(
        cls,
        state: StateManager,
        analyzer: TaskAnalyzer | None = None,
        router: AgentRouter | None = None,
        adapters: AdapterRegistry | None = None,
        agents: AgentRegistry | None = None,
    ) -> "OrchestrationEngine":
        """Build the engine and recover any interrupted tasks from a prior
        run. Interrupted tasks are only flagged, never auto-resolved — see
        `TaskManager.resolve_interrupted`."""
        manager = TaskManager(state)
        manager.recover()
        return cls(
            analyzer=analyzer or TaskAnalyzer(),
            manager=manager,
            router=router,
            adapters=adapters,
            agents=agents,
        )

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

    def execute(
        self,
        task_id: str,
        working_directory: str,
        *,
        input_context: str = "",
        timeout_seconds: float = 60.0,
        policy: ExecutionPolicy | None = None,
    ) -> ExecutionResult:
        """Task Manager -> Agent Router's last decision -> Execution
        Adapter -> Task Manager. Refuses to run without a prior
        `RoutingOutcome.SELECTED` decision for this task — routing is
        never bypassed. `working_directory` has no default: the caller
        decides where this runs, every time."""
        if self.adapters is None:
            raise RuntimeError("OrchestrationEngine has no adapter registry configured")

        task = self.manager.get(task_id)
        decisions = self.manager.get_routing_decisions(task_id)
        if not decisions:
            raise OrchestratorError(
                f"task '{task_id}' has no routing decision yet; call route() first"
            )
        latest = decisions[-1]
        if latest.outcome is not RoutingOutcome.SELECTED:
            raise OrchestratorError(
                f"task '{task_id}' has no SELECTED routing decision "
                f"(latest outcome: {latest.outcome.value}); refusing to execute"
            )

        agent_role = ""
        if self.agents is not None:
            agent_role = self.agents.get(latest.selected_agent_id).role

        adapter = self.adapters.get(latest.selected_provider_id)
        request = ExecutionRequest(
            task_id=task.task_id,
            project_id=task.project_id,
            agent_id=latest.selected_agent_id,
            provider_id=latest.selected_provider_id,
            agent_role=agent_role,
            working_directory=working_directory,
            specification=task.specification,
            input_context=input_context,
            policy=policy or ExecutionPolicy(),
            timeout_seconds=timeout_seconds,
            correlation_id=latest.decision_id,
        )
        result = adapter.execute(request)
        self.manager.record_execution_result(result)
        return result
