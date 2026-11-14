"""State-aware Stall Detector — Milestone 7, Component B.

Reads `Task.status`/`Task.updated_at` (already persisted by every
`TaskManager` transition — Milestone 2) and `WorktreeHandle.status`/
`.updated_at` (Milestone 6.1). Never invents a new status value on
either enum, and never mutates a `Task` or `WorktreeHandle` row itself
— this module only ever reads them and writes its own
`scheduler_alerts` bookkeeping. `ApprovalGate` and `GovernedPipeline`
never consult `scheduler_alerts`; an alert can inform a human, never
silently block or approve an integration.

Six detection rules, each tied to a concrete, observed condition in
this exact codebase (not a hypothetical):

- `RUNNING_TOO_LONG` (soft / `recovery_suspected`): a task has been
  RUNNING longer than `running_stall_threshold_seconds`. This may
  still resolve on its own (a real Agent call can legitimately take a
  while) — it is a suspicion, not a confirmed failure.
- `CONFIRMED_INTERRUPTED` (confirmed): `TaskManager.recover()` has
  already flipped the task to INTERRUPTED after an Orchestrator
  restart found it RUNNING with no observed outcome — a *fact*, not a
  suspicion, surfaced here only so a human notices it needs
  `resolve_interrupted()`.
- `DEADLOCKED` (confirmed, immediate): the task's BLOCKED chain
  contains a structural dependency cycle (`TaskManager.diagnose_blocked`,
  Component A) — it will never resolve on its own, so this is raised
  regardless of how long it has been BLOCKED.
- `BLOCKED_CHAIN` (soft): BLOCKED, not deadlocked, but unmet longer
  than `blocked_stall_threshold_seconds` — still routine while a
  dependency is in progress; only flagged once it has gone on a while.
- `STUCK_QUALITY_CHECK` (soft): a Worktree has sat in QUALITY_CHECK
  longer than `quality_check_stall_threshold_seconds`. Sitting in
  QUALITY_CHECK briefly is *normal* — `GovernedPipeline.dispatch()`
  and `.evaluate_and_propose()` are two separate calls by design — so
  this is duration-gated, never immediate.
- `INCONSISTENT_STATE` (confirmed, immediate): `Task.status is RUNNING`
  while its Worktree is `NEEDS_REVIEW`. This is a real, reachable gap
  in `GovernedPipeline.dispatch()`'s own exception path: a failure
  during `engine.execute()` transitions the Worktree to NEEDS_REVIEW
  but never calls `TaskManager.fail()`, leaving the Task itself stuck
  RUNNING. Flagged immediately — the combination is never valid no
  matter how briefly it has existed.

`WorktreeStatus.AWAITING_APPROVAL` is never treated as a problem at
any age — explicitly informational (Founder review is an expected,
unbounded wait), never an execution failure.

Every alert is deduplicated against the task's own currently-open
(unacknowledged) alert of the same kind (`StateManager.get_open_alert`)
— a persisting condition is reported once, not on every scan.
"""

from __future__ import annotations

from datetime import datetime, timezone

from universal_orchestrator.config.scheduler_settings import (
    SchedulerSettings,
    load_scheduler_settings,
)
from universal_orchestrator.core.models import Task, TaskStatus
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.core.task_manager import TaskManager
from universal_orchestrator.execution.scheduler import AlertKind, AlertSeverity, SchedulerAlert
from universal_orchestrator.execution.worktree import WorktreeHandle, WorktreeStatus
from universal_orchestrator.execution.worktree_manager import WorktreeManager


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StallDetector:
    def __init__(
        self,
        state: StateManager,
        task_manager: TaskManager,
        worktrees: WorktreeManager,
        settings: SchedulerSettings | None = None,
    ) -> None:
        self._state = state
        self._tasks = task_manager
        self._worktrees = worktrees
        self._settings = settings or load_scheduler_settings()

    def scan(self, *, now: datetime | None = None) -> list[SchedulerAlert]:
        """Runs every detection rule once and returns only the *newly*
        raised alerts this call — a condition with an already-open
        alert produces nothing new. Safe to call repeatedly (a
        Scheduler's own periodic loop, a cron job, a manual CLI
        invocation) without accumulating duplicate rows."""
        now = now or _utcnow()
        tasks = self._tasks.all_tasks()
        task_by_id = {t.task_id: t for t in tasks}

        new_alerts: list[SchedulerAlert] = []
        for task in tasks:
            new_alerts.extend(self._check_task(task, now))
        for handle in self._worktrees.list_active():
            new_alerts.extend(self._check_worktree(handle, task_by_id.get(handle.task_id), now))
        return new_alerts

    # -- internals -----------------------------------------------------------

    def _raise_if_new(
        self, task_id: str, kind: AlertKind, severity: AlertSeverity, detail: str, now: datetime
    ) -> SchedulerAlert | None:
        if self._state.get_open_alert(task_id, kind) is not None:
            return None
        alert = SchedulerAlert(
            task_id=task_id, kind=kind, severity=severity, detail=detail, detected_at=now
        )
        self._state.save_scheduler_alert(alert)
        return alert

    def _check_task(self, task: Task, now: datetime) -> list[SchedulerAlert]:
        alerts: list[SchedulerAlert] = []

        if task.status is TaskStatus.RUNNING:
            age = (now - task.updated_at).total_seconds()
            if age > self._settings.running_stall_threshold_seconds:
                alert = self._raise_if_new(
                    task.task_id,
                    AlertKind.RUNNING_TOO_LONG,
                    AlertSeverity.WARNING,
                    f"RUNNING for {age:.0f}s (threshold "
                    f"{self._settings.running_stall_threshold_seconds:.0f}s) — "
                    "recovery_suspected: true; not yet a confirmed failure",
                    now,
                )
                if alert is not None:
                    alerts.append(alert)

        elif task.status is TaskStatus.INTERRUPTED:
            alert = self._raise_if_new(
                task.task_id,
                AlertKind.CONFIRMED_INTERRUPTED,
                AlertSeverity.CRITICAL,
                "confirmed: TaskManager.recover() found this task RUNNING after a "
                "restart with no observed outcome; needs resolve_interrupted()",
                now,
            )
            if alert is not None:
                alerts.append(alert)

        elif task.status is TaskStatus.BLOCKED:
            diagnosis = self._tasks.diagnose_blocked(task.task_id)
            if diagnosis.deadlocked:
                alert = self._raise_if_new(
                    task.task_id,
                    AlertKind.DEADLOCKED,
                    AlertSeverity.CRITICAL,
                    f"confirmed: dependency cycle {' -> '.join(diagnosis.cycle)} — "
                    "will never resolve on its own",
                    now,
                )
                if alert is not None:
                    alerts.append(alert)
            elif diagnosis.unmet_chain:
                age = (now - task.updated_at).total_seconds()
                if age > self._settings.blocked_stall_threshold_seconds:
                    alert = self._raise_if_new(
                        task.task_id,
                        AlertKind.BLOCKED_CHAIN,
                        AlertSeverity.WARNING,
                        f"BLOCKED for {age:.0f}s on unmet dependencies: "
                        f"{', '.join(diagnosis.unmet_chain)}",
                        now,
                    )
                    if alert is not None:
                        alerts.append(alert)

        return alerts

    def _check_worktree(
        self, handle: WorktreeHandle, task: Task | None, now: datetime
    ) -> list[SchedulerAlert]:
        alerts: list[SchedulerAlert] = []

        if handle.status is WorktreeStatus.AWAITING_APPROVAL:
            return alerts

        if handle.status is WorktreeStatus.QUALITY_CHECK:
            age = (now - handle.updated_at).total_seconds()
            if age > self._settings.quality_check_stall_threshold_seconds:
                alert = self._raise_if_new(
                    handle.task_id,
                    AlertKind.STUCK_QUALITY_CHECK,
                    AlertSeverity.WARNING,
                    f"Worktree stuck in QUALITY_CHECK for {age:.0f}s (threshold "
                    f"{self._settings.quality_check_stall_threshold_seconds:.0f}s)",
                    now,
                )
                if alert is not None:
                    alerts.append(alert)

        if (
            task is not None
            and task.status is TaskStatus.RUNNING
            and handle.status is WorktreeStatus.NEEDS_REVIEW
        ):
            alert = self._raise_if_new(
                handle.task_id,
                AlertKind.INCONSISTENT_STATE,
                AlertSeverity.CRITICAL,
                "confirmed: Task is RUNNING but its Worktree is NEEDS_REVIEW — "
                "never a valid pairing, flagged immediately regardless of age",
                now,
            )
            if alert is not None:
                alerts.append(alert)

        return alerts
