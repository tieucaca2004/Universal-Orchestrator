"""Git Worktree data model and state machine — Milestone 6.1
(ROADMAP.md §10.1).

Kept deliberately free of any dependency on `core.state.StateManager`
(the concrete `WorktreeManager`, in `execution.worktree_manager`,
needs that; this module does not) so that `core.state` can import
`WorktreeHandle`/`WorktreeStatus` from here the same way it already
imports `ExecutionResult`/`VerificationStatus` from
`execution.adapter` — without a circular import.

State machine mirrors `core.lifecycle`'s pattern for `Task`: an
explicit allow-list, `errors.InvalidStateTransitionError` on anything
not listed, never a silent no-op:

    ACQUIRED -> EXECUTING -> QUALITY_CHECK -> AWAITING_APPROVAL
             -> INTEGRATING -> INTEGRATED -> RELEASED

Any of the first five states may instead move to NEEDS_REVIEW
(interruption, a failed quality check, a detected conflict, a denied
approval, a failed integration) — NEEDS_REVIEW never auto-advances; it
leaves only through an explicit, later decision ("retry" back to a
fresh EXECUTING, or "discard" to RELEASED — the latter requires an
explicit, authorized confirmation at the `WorktreeManager` layer,
since data safety is not guaranteed on that path the way it is from
INTEGRATED).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone

from universal_orchestrator.errors import InvalidStateTransitionError, OrchestratorError


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorktreeStatus(enum.Enum):
    ACQUIRED = "acquired"
    EXECUTING = "executing"
    QUALITY_CHECK = "quality_check"
    AWAITING_APPROVAL = "awaiting_approval"
    INTEGRATING = "integrating"
    INTEGRATED = "integrated"
    RELEASED = "released"
    NEEDS_REVIEW = "needs_review"


TERMINAL_STATUSES = frozenset({WorktreeStatus.RELEASED})

# Explicit allow-list: no transition is valid unless listed here. See
# ROADMAP.md §10.1 / the M6 Final Consolidated Implementation Blueprint
# for the rationale behind every edge.
ALLOWED_TRANSITIONS: dict[WorktreeStatus, frozenset[WorktreeStatus]] = {
    WorktreeStatus.ACQUIRED: frozenset(
        {WorktreeStatus.EXECUTING, WorktreeStatus.NEEDS_REVIEW}
    ),
    WorktreeStatus.EXECUTING: frozenset(
        {WorktreeStatus.QUALITY_CHECK, WorktreeStatus.NEEDS_REVIEW}
    ),
    WorktreeStatus.QUALITY_CHECK: frozenset(
        {WorktreeStatus.AWAITING_APPROVAL, WorktreeStatus.NEEDS_REVIEW}
    ),
    WorktreeStatus.AWAITING_APPROVAL: frozenset(
        {WorktreeStatus.INTEGRATING, WorktreeStatus.NEEDS_REVIEW}
    ),
    WorktreeStatus.INTEGRATING: frozenset(
        {WorktreeStatus.INTEGRATED, WorktreeStatus.NEEDS_REVIEW}
    ),
    WorktreeStatus.INTEGRATED: frozenset({WorktreeStatus.RELEASED}),
    WorktreeStatus.RELEASED: frozenset(),
    WorktreeStatus.NEEDS_REVIEW: frozenset(
        {WorktreeStatus.EXECUTING, WorktreeStatus.RELEASED}
    ),
}


def is_terminal(status: WorktreeStatus) -> bool:
    return status in TERMINAL_STATUSES


def validate_transition(current: WorktreeStatus, target: WorktreeStatus) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidStateTransitionError("Worktree", current.value, target.value)


@dataclass(slots=True)
class WorktreeHandle:
    task_id: str
    project_id: str
    path: str
    branch: str
    base_sha: str
    status: WorktreeStatus = WorktreeStatus.ACQUIRED
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


class WorktreeOperationError(OrchestratorError):
    """Raised for any Worktree Manager failure that is not already a
    typed error (`DuplicateError`, `NotFoundError`,
    `InvalidStateTransitionError`) — a failed `git` command, an
    invalid `repository_path`, or a refused destructive release."""

    def __init__(self, task_id: str, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"Worktree operation failed for task '{task_id}': {reason}")
