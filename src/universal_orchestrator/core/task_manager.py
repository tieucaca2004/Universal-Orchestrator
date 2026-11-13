"""Task Manager: task identity, parent/child decomposition, the
dependency graph, and lifecycle transitions.

Deliberately does not decide which provider or agent executes a task —
that is the Agent Router's job (Milestone 3+). This module only ever
asks "is this task's dependency graph satisfied" and "is this transition
allowed" (via `core.lifecycle`); it never inspects agents or providers.

All state mutation is persisted through a `StateManager` immediately —
in-memory state is a cache rebuilt from the store on `recover()`, never
the source of truth.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from universal_orchestrator.core import lifecycle
from universal_orchestrator.core.models import (
    RoutingDecision,
    Task,
    TaskError,
    TaskSpecification,
    TaskStatus,
)
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.errors import NotFoundError, OrchestratorError, QualityGateRequiredError
from universal_orchestrator.execution.adapter import ExecutionResult
from universal_orchestrator.execution.quality import QualityVerdict


class InterruptedResolution(enum.Enum):
    """How an operator resolves a task found INTERRUPTED on restart.

    RETRY moves the task back to READY so a future dispatcher can pick it
    up fresh — never straight back to RUNNING, and never automatically.
    FAILED and CONFIRMED_SUCCEEDED record an explicit, evidenced decision
    about an execution whose outcome this process could not observe.
    """

    RETRY = "retry"
    FAILED = "failed"
    CONFIRMED_SUCCEEDED = "confirmed_succeeded"


_RESOLUTION_TARGETS = {
    InterruptedResolution.RETRY: TaskStatus.READY,
    InterruptedResolution.FAILED: TaskStatus.FAILED,
    InterruptedResolution.CONFIRMED_SUCCEEDED: TaskStatus.SUCCEEDED,
}


class TaskManager:
    def __init__(self, state: StateManager) -> None:
        self._state = state
        self._tasks: dict[str, Task] = {}

    # -- lookups ----------------------------------------------------------

    def get(self, task_id: str) -> Task:
        return self._require(task_id)

    def list_children(self, parent_id: str) -> list[Task]:
        return [t for t in self._tasks.values() if t.parent_id == parent_id]

    def record_routing_decision(self, decision: RoutingDecision) -> None:
        """The only path a `RoutingDecision` reaches storage through —
        the Agent Router returns a decision but never persists it
        itself, so it can never bypass the Task Manager."""
        self._require(decision.task_id)
        self._state.save_routing_decision(decision)

    def get_routing_decisions(self, task_id: str) -> list[RoutingDecision]:
        self._require(task_id)
        return self._state.get_routing_decisions(task_id)

    def record_execution_result(self, result: ExecutionResult) -> None:
        """Same bypass-prevention shape as `record_routing_decision`: an
        `ExecutionAdapter` returns a result but never persists it
        itself. `Task.status` is deliberately left untouched here — a
        completed process is not proof the task's business objective
        was met (see `ExecutionResult.success`'s docstring); that
        judgement belongs to a later milestone (Quality Gate)."""
        self._require(result.task_id)
        self._state.save_execution_result(result)

    def get_execution_results(self, task_id: str) -> list[ExecutionResult]:
        self._require(task_id)
        return self._state.get_execution_results(task_id)

    def _require(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise NotFoundError("Task", task_id)
        return task

    # -- creation / decomposition ------------------------------------------

    def create_task(
        self,
        title: str,
        project_id: str,
        *,
        parent_id: str | None = None,
        priority: int = 0,
        owner: str | None = None,
    ) -> Task:
        if parent_id is not None:
            self._require(parent_id)

        task = Task(
            title=title,
            project_id=project_id,
            parent_id=parent_id,
            priority=priority,
            owner=owner,
        )
        self._state.create(task)
        self._tasks[task.task_id] = task
        return task

    def decompose(
        self,
        parent_id: str,
        subtask_titles: list[str],
        *,
        priority: int = 0,
        owner: str | None = None,
    ) -> list[Task]:
        parent = self._require(parent_id)
        return [
            self.create_task(
                title, parent.project_id, parent_id=parent_id, priority=priority, owner=owner
            )
            for title in subtask_titles
        ]

    def add_dependency(self, task_id: str, depends_on_id: str) -> Task:
        task = self._require(task_id)
        self._require(depends_on_id)

        if task.status not in (
            TaskStatus.CREATED,
            TaskStatus.ANALYZED,
            TaskStatus.PLANNED,
            TaskStatus.BLOCKED,
        ):
            raise OrchestratorError(
                f"cannot add a dependency to task '{task_id}' once it is "
                f"'{task.status.value}' — dependencies must be declared "
                "before the task becomes READY"
            )

        if depends_on_id not in task.depends_on:
            task.depends_on = (*task.depends_on, depends_on_id)
            self._persist(task)
        return task

    # -- analysis / planning -----------------------------------------------

    def attach_specification(self, task_id: str, specification: TaskSpecification) -> Task:
        task = self._require(task_id)
        self._transition(task, TaskStatus.ANALYZED)
        task.specification = specification
        self._persist(task)
        self._evaluate_readiness(task)
        return task

    def mark_planned(self, task_id: str) -> Task:
        task = self._require(task_id)
        self._transition(task, TaskStatus.PLANNED)
        self._persist(task)
        self._evaluate_readiness(task)
        return task

    def _evaluate_readiness(self, task: Task) -> None:
        if task.status not in (TaskStatus.ANALYZED, TaskStatus.PLANNED, TaskStatus.BLOCKED):
            return

        unmet = [
            dep_id
            for dep_id in task.depends_on
            if dep_id not in self._tasks or self._tasks[dep_id].status != TaskStatus.SUCCEEDED
        ]
        target = TaskStatus.BLOCKED if unmet else TaskStatus.READY
        if target is task.status:
            return
        self._transition(task, target)
        self._persist(task)

    # -- execution bookkeeping (never decides *who* executes) --------------

    def start(self, task_id: str) -> Task:
        task = self._require(task_id)
        self._transition(task, TaskStatus.RUNNING)
        self._persist(task)
        return task

    def succeed(self, task_id: str, result: object | None = None) -> Task:
        """Milestone 6.4: a task may never reach SUCCEEDED without a
        recorded, task-level Quality Gate PASS tied to its own
        acceptance criteria — a completed process alone has never been
        sufficient evidence (see `execution.adapter.ExecutionResult
        .success`'s own docstring, unchanged since Milestone 4).

        The transition's own validity is checked first (via
        `lifecycle.validate_transition`, the same check `_transition`
        performs) so an already-wrong-state call still raises
        `InvalidStateTransitionError`, exactly as before this
        milestone — the Quality Gate requirement is an additional
        precondition, not a replacement for the existing one."""
        task = self._require(task_id)
        lifecycle.validate_transition(task.status, TaskStatus.SUCCEEDED)
        verdict_report = self._state.latest_quality_report(task_id, level="task")
        if verdict_report is None or verdict_report.verdict is not QualityVerdict.PASS:
            raise QualityGateRequiredError(
                task_id, verdict_report.verdict.value if verdict_report else "NOT_VERIFIED"
            )
        self._transition(task, TaskStatus.SUCCEEDED)
        task.result = result
        self._persist(task)
        self._cascade_dependents(task_id)
        return task

    def fail(self, task_id: str, error: TaskError) -> Task:
        task = self._require(task_id)
        self._transition(task, TaskStatus.FAILED)
        task.error = error
        self._persist(task)
        return task

    def cancel(self, task_id: str) -> Task:
        task = self._require(task_id)
        self._transition(task, TaskStatus.CANCELLED)
        self._persist(task)
        return task

    def _cascade_dependents(self, completed_task_id: str) -> None:
        for task in self._tasks.values():
            if completed_task_id in task.depends_on and task.status == TaskStatus.BLOCKED:
                self._evaluate_readiness(task)

    # -- restart / interruption recovery ------------------------------------

    def recover(self) -> list[str]:
        """Reload tasks from the store and flag any left RUNNING as
        INTERRUPTED. Never resolves them automatically — see
        `resolve_interrupted`."""
        self._tasks = {t.task_id: t for t in self._state.list()}

        interrupted: list[str] = []
        for task in self._tasks.values():
            if task.status == TaskStatus.RUNNING:
                self._transition(task, TaskStatus.INTERRUPTED)
                self._persist(task)
                interrupted.append(task.task_id)
        return interrupted

    def resolve_interrupted(
        self, task_id: str, decision: InterruptedResolution, evidence: str = ""
    ) -> Task:
        task = self._require(task_id)
        if task.status != TaskStatus.INTERRUPTED:
            raise OrchestratorError(
                f"task '{task_id}' is '{task.status.value}', not INTERRUPTED"
            )
        if decision is InterruptedResolution.CONFIRMED_SUCCEEDED and not evidence.strip():
            raise ValueError(
                "resolve_interrupted(CONFIRMED_SUCCEEDED) requires non-empty "
                "evidence — recovery must never silently mark a task "
                "successful"
            )

        target = _RESOLUTION_TARGETS[decision]
        self._transition(task, target)

        if decision is InterruptedResolution.FAILED:
            task.error = TaskError(
                code="interrupted_resolved_failed",
                message="Marked failed after interruption",
                detail=evidence,
            )
        elif decision is InterruptedResolution.CONFIRMED_SUCCEEDED:
            task.payload["recovery_evidence"] = evidence

        self._persist(task)
        if target is TaskStatus.SUCCEEDED:
            self._cascade_dependents(task_id)
        return task

    # -- internals -----------------------------------------------------------

    def _transition(self, task: Task, target: TaskStatus) -> None:
        lifecycle.validate_transition(task.status, target)
        task.status = target
        task.updated_at = datetime.now(timezone.utc)
        task.history.append((target.value, task.updated_at.isoformat()))

    def _persist(self, task: Task) -> None:
        self._state.update(task)
