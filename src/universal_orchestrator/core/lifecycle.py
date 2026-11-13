"""The Task lifecycle state machine. See docs/TASK_LIFECYCLE.md for the
diagram and rationale; this module is its enforced, testable form.

Every status change in `TaskManager` goes through `validate_transition`
first — there is no other path to mutate `Task.status`.
"""

from __future__ import annotations

from universal_orchestrator.core.models import TaskStatus
from universal_orchestrator.errors import InvalidStateTransitionError

TERMINAL_STATUSES = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

# Explicit allow-list: no transition is valid unless listed here.
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.ANALYZED, TaskStatus.CANCELLED}),
    TaskStatus.ANALYZED: frozenset(
        {TaskStatus.PLANNED, TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
    ),
    TaskStatus.PLANNED: frozenset(
        {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
    ),
    TaskStatus.BLOCKED: frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.READY: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
            TaskStatus.CANCELLED,
        }
    ),
    # Recovery is a separate decision from execution: INTERRUPTED only ever
    # moves to READY (retry, picked up by a future dispatcher — never
    # straight back to RUNNING, which would risk duplicating an execution
    # whose outcome is uncertain), or to a terminal state via an explicit,
    # evidenced TaskManager.resolve_interrupted() call.
    TaskStatus.INTERRUPTED: frozenset(
        {TaskStatus.READY, TaskStatus.FAILED, TaskStatus.SUCCEEDED}
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATUSES


def validate_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidStateTransitionError("Task", current.value, target.value)
