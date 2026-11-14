"""Scheduler Lease Manager — Milestone 7, Phase 2.

Turns `core.state.StateManager`'s raw atomic primitives (acquire/renew/
release a lease; claim/complete/quarantine a task) into the typed-error
style every other M6 manager already uses (`ConflictManager`'s
`IntegrationLockHeldError`, `WorktreeManager`'s `WorktreeOperationError`)
— never a bare `bool` left for a caller to forget to check.

This module has no business logic of its own and makes no scheduling
decisions — it is the fencing primitive the real Scheduler (Milestone
7, Component C, Phase 4) is built on top of. Nothing here calls
`TaskManager`, `WorktreeManager`, or `GovernedPipeline`; this is
deliberately scoped to lease/claim bookkeeping only, so that
responsibility boundary stays testable in isolation from real Agent
execution.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from universal_orchestrator.core.state import StateManager
from universal_orchestrator.errors import OrchestratorError
from universal_orchestrator.execution.scheduler import ClaimStatus, SchedulerLease, TaskClaim


def new_holder_id() -> str:
    return f"sched_{uuid.uuid4().hex[:12]}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LeaseNotHeldError(OrchestratorError):
    """Raised by `acquire()` when a live holder already owns the
    lease — never raised by `try_acquire()`, which returns `None`
    instead for callers that intend to poll/retry rather than fail."""

    def __init__(self, current: SchedulerLease) -> None:
        self.current = current
        super().__init__(
            f"scheduler lease is held by '{current.holder_id}' (epoch {current.epoch}); "
            "refusing to acquire"
        )


class LeaseLostError(OrchestratorError):
    """Raised by `renew()`/`require_current()` when the caller's own
    (holder_id, epoch) is no longer the authoritative lease — the
    caller must stop scheduling immediately, not retry."""

    def __init__(self, holder_id: str, epoch: int) -> None:
        self.holder_id = holder_id
        self.epoch = epoch
        super().__init__(
            f"holder '{holder_id}' epoch {epoch} is no longer the authoritative "
            "scheduler lease"
        )


class SchedulerLeaseManager:
    def __init__(self, state: StateManager) -> None:
        self._state = state

    # -- lease lifecycle ---------------------------------------------------

    def try_acquire(self, holder_id: str, *, lease_seconds: float) -> int | None:
        """Returns the new epoch on success, `None` if a live holder
        already owns the lease — the non-raising form, for a Scheduler
        that intends to poll until it can take over."""
        return self._state.acquire_scheduler_lease(holder_id, _utcnow(), lease_seconds)

    def acquire(self, holder_id: str, *, lease_seconds: float) -> int:
        epoch = self.try_acquire(holder_id, lease_seconds=lease_seconds)
        if epoch is None:
            raise LeaseNotHeldError(self.current())
        return epoch

    def renew(self, holder_id: str, epoch: int, *, lease_seconds: float) -> None:
        ok = self._state.renew_scheduler_lease(holder_id, epoch, _utcnow(), lease_seconds)
        if not ok:
            raise LeaseLostError(holder_id, epoch)

    def release(self, holder_id: str, epoch: int) -> bool:
        """Never raises: releasing a lease you no longer hold is a
        no-op, not an error — the caller was going to stop acting as
        the authority either way."""
        return self._state.release_scheduler_lease(holder_id, epoch)

    def current(self) -> SchedulerLease:
        return self._state.current_scheduler_lease()

    def require_current(self, holder_id: str, epoch: int) -> None:
        """Pure fenced read: raises `LeaseLostError` if (holder_id,
        epoch) is not the live lease right now. Used between major
        orchestration steps (e.g. between `dispatch()` and
        `evaluate_and_propose()`) to detect a takeover that happened
        while a worker was busy, before taking the next action."""
        current = self.current()
        if current.holder_id != holder_id or current.epoch != epoch:
            raise LeaseLostError(holder_id, epoch)

    # -- claims --------------------------------------------------------------

    def claim_task(self, task_id: str, holder_id: str, epoch: int) -> bool:
        """Returns `False` (never raises) if the lease has moved on
        since the caller last checked — the caller must stop, not
        retry blindly. Still raises `errors.DuplicateError` if
        `task_id` is already claimed by anyone, at any epoch."""
        return self._state.claim_task(task_id, holder_id, epoch, _utcnow())

    def complete_claim(self, task_id: str, holder_id: str, epoch: int) -> bool:
        """Returns `False` if this (holder_id, epoch) is no longer
        authoritative — the caller must treat its own just-finished
        work as unconfirmed, never as a normal completion."""
        return self._state.complete_claim(task_id, holder_id, epoch, _utcnow())

    def quarantine_claim(self, task_id: str, holder_id: str, epoch: int) -> bool:
        """`holder_id`/`epoch` here are the *caller's own*, current
        lease identity (recovery is always performed by whoever is
        authoritative *now*) — not the stale claim's. Returns `False`
        if the caller is not actually the current authority, or if
        the claim was not in CLAIMED status to begin with."""
        return self._state.quarantine_claim(task_id, holder_id, epoch, _utcnow())

    def get_claim(self, task_id: str) -> TaskClaim | None:
        return self._state.get_claim(task_id)

    def list_claims(self, *, status: ClaimStatus | None = None) -> list[TaskClaim]:
        return self._state.list_claims(status=status)
