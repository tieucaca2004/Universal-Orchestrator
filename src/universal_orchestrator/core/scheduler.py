"""Task Scheduler — Milestone 7, Component C.

Automatically dispatches every eligible READY task, bounded by a
configurable global concurrency limit, with every execution getting
its own isolated Worktree (M6 Option B, unchanged) — never a
Project-wide mutex, never two executions sharing one Worktree
(`WorktreeManager.acquire`'s own `task_id` primary key already
guarantees that at the persistence layer, M6, unchanged).

Built entirely on top of existing M1-M6 components, used exactly as
they already exist: `OrchestrationEngine`, `GovernedPipeline`,
`TaskManager`, `WorktreeManager`, `ApprovalGate`, `ConflictManager`,
`QualityGate`. None of their APIs or behavior are changed here.
Founder approval remains mandatory before integration — this module
never calls `integrate_after_approval`; that stays the Approval CLI's
job, strictly after a human decision.

**A load-bearing, honest limitation, not a bug:** `evaluate_and_propose()`
is called here with no `criteria_evidence` unless a caller explicitly
supplies a `criteria_evidence_provider` (never auto-populated,
never inferred — see Quality Gate's own docstring: "`criteria_evidence`
must be supplied by whoever actually checked the work"). Without one, a
task with declared `acceptance_criteria` will legitimately end FAILED
with its Worktree NEEDS_REVIEW after automatic dispatch — Quality Gate
returns NOT_VERIFIED, exactly as it already does for *any* caller
supplying no evidence, unchanged since Milestone 6.4. This Scheduler
does not bypass that, and never fabricates evidence to make a task
appear to pass.

**Fencing boundary:** `SchedulerLeaseManager` (Phase 2) owns `scheduler_
claims`, never `tasks`/`worktrees`. A worker thread's one fenced
write is `complete_claim()`, checked against the *live* lease epoch in
one atomic statement, immediately before returning. If that fails (a
takeover happened mid-dispatch), this worker's own
`GovernedPipeline` mutations already happened and cannot be undone —
but nothing further is attempted (`evaluate_and_propose` is skipped
entirely if the lease was lost between `dispatch()` and that call),
and the *new* authority's own recovery scan (`_recover()`, below)
will find the stale CLAIMED row and quarantine both the claim and,
if the Worktree is in a non-terminal state, the Worktree itself —
never silently reused.
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, Lock
from typing import Callable

from universal_orchestrator.audit.logger import get_logger
from universal_orchestrator.config.scheduler_settings import (
    SchedulerSettings,
    load_scheduler_settings,
)
from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.governed_pipeline import GovernedPipeline
from universal_orchestrator.core.models import RoutingOutcome, TaskStatus
from universal_orchestrator.core.stall_detector import StallDetector
from universal_orchestrator.errors import DuplicateError, InvalidStateTransitionError, NotFoundError
from universal_orchestrator.execution.scheduler import ClaimStatus
from universal_orchestrator.execution.hermes_transfer import HermesWorkspaceTransfer
from universal_orchestrator.execution.scheduler_manager import (
    LeaseLostError,
    SchedulerLeaseManager,
    new_holder_id,
)
from universal_orchestrator.execution.worktree import WorktreeStatus
from universal_orchestrator.registry.projects import ProjectRegistry

_LOG = get_logger("scheduler")

CriteriaEvidenceProvider = Callable[[str], "dict[str, bool] | None"]
InputContextBuilder = Callable[["object"], str]


def default_input_context(task) -> str:
    """`GovernedPipeline.dispatch()`'s `input_context` has no default
    of its own (Milestone 4: the caller always supplies it explicitly
    — e.g. `orchestrator_cli submit --input-context`). A Scheduler
    dispatching automatically, with no human typing one in per task,
    has no other source for a meaningful prompt than the task's own
    `TaskSpecification` — already built for exactly this by the Task
    Analyzer (Milestone 2) and otherwise unused by any M1-M6 dispatch
    path. Returns "" (never fabricated filler) if there is no
    specification at all."""
    spec = task.specification
    if spec is None:
        return ""
    parts = [f"Objective: {spec.objective}", f"Scope: {spec.scope}"]
    if spec.constraints:
        parts.append("Constraints: " + "; ".join(spec.constraints))
    if spec.expected_deliverables:
        parts.append("Expected deliverables: " + "; ".join(spec.expected_deliverables))
    if spec.acceptance_criteria:
        parts.append("Acceptance criteria: " + "; ".join(spec.acceptance_criteria))
    return "\n".join(parts)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    def __init__(
        self,
        state,
        engine: OrchestrationEngine,
        pipeline: GovernedPipeline,
        projects: ProjectRegistry,
        *,
        settings: SchedulerSettings | None = None,
        holder_id: str | None = None,
        criteria_evidence_provider: CriteriaEvidenceProvider | None = None,
        workspace_transfer: HermesWorkspaceTransfer | None = None,
        input_context_builder: InputContextBuilder = default_input_context,
    ) -> None:
        self._state = state
        self._engine = engine
        self._pipeline = pipeline
        self._projects = projects
        self._settings = settings or load_scheduler_settings()
        self._holder_id = holder_id or new_holder_id()
        self._evidence_provider = criteria_evidence_provider
        self._workspace_transfer = workspace_transfer
        self._input_context_builder = input_context_builder
        self._leases = SchedulerLeaseManager(state)
        # Audit Finding C-5: previously only reachable out-of-band via
        # `alerts_cli scan` -- a live Scheduler never ran it itself.
        # `StallDetector.scan()` only ever reads Task/Worktree state
        # and writes its own `scheduler_alerts` bookkeeping (never a
        # Task/Worktree mutation), and already dedupes against each
        # task's currently-open alert of the same kind, so calling it
        # once per `run_once()` is safe and never produces duplicate
        # alerts for a persisting condition.
        self._stall_detector = StallDetector(state, engine.manager, pipeline.worktrees, self._settings)
        self._epoch: int | None = None
        self._stop_event = Event()
        self._executor: ThreadPoolExecutor | None = None
        self._futures: dict[str, Future] = {}
        self._futures_lock = Lock()
        # Audit Finding C-6: (provider_id, execution_id) for every
        # task currently blocked inside adapter.execute(), recorded
        # the moment it's known (engine.execute()'s on_dispatched
        # hook) -- the only way shutdown() can attempt a real,
        # targeted subprocess termination for a worker stuck past
        # drain_timeout_seconds. This does not and cannot kill the
        # Python *thread* itself; it kills the subprocess that thread
        # is blocked on, which is the actual resource that matters --
        # see `shutdown()`'s own docstring for why the lease is never
        # released based on this alone.
        self._inflight: dict[str, tuple[str, str]] = {}

    @property
    def holder_id(self) -> str:
        return self._holder_id

    @property
    def epoch(self) -> int | None:
        return self._epoch

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Acquire the lease (polling until free or the caller
        stops), then run recovery/reconciliation — in that order,
        exactly as required: normal scheduling never resumes before
        stale claims from any superseded holder have been dealt with."""
        self._epoch = self._acquire_with_retry()
        self._executor = ThreadPoolExecutor(max_workers=max(1, self._settings.max_concurrency))
        self._recover()
        self._reconcile_task_state()
        self._recover_worktrees()

    def _recover_worktrees(self) -> None:
        """Audit Finding C-12: `WorktreeManager.recover()` existed but
        was never called from any real startup path -- a Worktree
        whose persisted state disagreed with the real Git worktree on
        disk (e.g. this Orchestrator crashed between `git worktree
        add` and persisting the row) stayed unreconciled until a human
        happened to invoke it by hand. Runs once per registered
        project, here and only here -- the authoritative moment a new
        Scheduler takes over, after stale claims are already
        quarantined, never on every CLI bootstrap (that would race
        exactly like `TaskManager.recover()` did before the C-1 fix).
        Only ever reclassifies an uncertain Worktree to NEEDS_REVIEW
        for a later human decision -- never deletes anything."""
        for profile in self._projects.list():
            try:
                self._pipeline.worktrees.recover(profile.repository_path)
            except Exception:
                _LOG.exception(
                    "worktree recovery failed for a project -- continuing with the others",
                    extra={"fields": {"project_id": profile.project_id}},
                )

    def _acquire_with_retry(self) -> int:
        while not self._stop_event.is_set():
            epoch = self._leases.try_acquire(
                self._holder_id, lease_seconds=self._settings.lease_seconds
            )
            if epoch is not None:
                _LOG.info(
                    "scheduler lease acquired",
                    extra={"fields": {"holder_id": self._holder_id, "epoch": epoch}},
                )
                return epoch
            self._stop_event.wait(self._settings.poll_seconds)
        raise RuntimeError("stopped before a scheduler lease could be acquired")

    def _recover(self) -> None:
        for claim in self._leases.list_claims(status=ClaimStatus.CLAIMED):
            if claim.holder_id == self._holder_id and claim.epoch == self._epoch:
                continue  # cannot happen right after a fresh acquire; never trust that blindly
            if not self._leases.quarantine_claim(claim.task_id, self._holder_id, self._epoch):
                continue
            self._quarantine_worktree_if_ambiguous(claim.task_id)
            _LOG.warning(
                "quarantined a stale claim on takeover",
                extra={"fields": {
                    "task_id": claim.task_id, "stale_epoch": claim.epoch,
                    "new_holder_id": self._holder_id, "new_epoch": self._epoch,
                }},
            )

    def _reconcile_task_state(self) -> None:
        """Audit remediation (Finding C-1): `_recover()` above only
        ever quarantines stale `scheduler_claims` rows; it never
        touches `TaskManager`'s `tasks` table. A task left RUNNING by
        a now-superseded (crashed) holder was, until this call,
        protected forever from `TaskManager.recover()` by
        `bootstrap()`'s own fencing (its claim still looked CLAIMED
        at bootstrap time, before this lease was even acquired).
        Now that `_recover()` has quarantined every stale claim, this
        re-runs `TaskManager.recover()` with the now-current (and,
        right after a fresh takeover, normally empty) set of still-
        CLAIMED task_ids protected — any task whose claim just got
        quarantined is no longer in that set, so it is finally, and
        only now, flagged INTERRUPTED for a human to resolve via
        `resolve_interrupted`. A task still genuinely claimed under
        any live epoch (including this one) remains untouched."""
        protect = frozenset(
            claim.task_id for claim in self._leases.list_claims(status=ClaimStatus.CLAIMED)
        )
        self._engine.manager.recover(protect=protect)

    def _quarantine_worktree_if_ambiguous(self, task_id: str) -> None:
        try:
            handle = self._pipeline.worktrees.get(task_id)
        except NotFoundError:
            return
        if handle.status in (
            WorktreeStatus.RELEASED, WorktreeStatus.INTEGRATED, WorktreeStatus.NEEDS_REVIEW,
        ):
            return  # already safe/terminal, or already flagged -- never re-flag
        try:
            self._pipeline.worktrees.transition(task_id, WorktreeStatus.NEEDS_REVIEW)
        except InvalidStateTransitionError:  # pragma: no cover - defensive only
            pass

    # -- scheduling loop ---------------------------------------------------------

    def run_once(self) -> list[str]:
        """One scheduling pass: renew the lease, pick up any tasks a
        different process submitted since the cache was last built,
        then claim and dispatch as much eligible READY work as the
        configured concurrency allows. Returns the task_ids dispatched
        this pass. Raises `LeaseLostError` if renewal itself fails —
        the caller must stop scheduling immediately, not retry."""
        self._leases.renew(self._holder_id, self._epoch, lease_seconds=self._settings.lease_seconds)
        self._engine.manager.sync_new_tasks()
        self._stall_detector.scan()

        dispatched: list[str] = []
        for task in self._ready_tasks():
            if self._active_count() >= self._settings.max_concurrency:
                break
            if not self._claim(task.task_id):
                continue
            self._submit_worker(task.task_id)
            dispatched.append(task.task_id)
        return dispatched

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop_event.is_set():
                try:
                    self.run_once()
                except LeaseLostError:
                    _LOG.error("scheduler lease lost during the scheduling loop -- stopping")
                    break
                self._stop_event.wait(self._settings.poll_seconds)
        finally:
            self.shutdown()

    def stop(self) -> None:
        self._stop_event.set()

    def shutdown(self) -> None:
        """Stop accepting new work, wait up to `drain_timeout_seconds`
        for in-flight workers, and release the lease *only if* every
        one of them actually finished.

        Audit Finding C-6: a Python worker *thread* cannot be safely
        force-terminated by this or any code -- that claim is never
        made here, and `cancel()`ing the `Future` this thread runs
        under would only prevent a not-yet-started call from starting,
        never stop one already blocked inside `adapter.execute()`
        (`concurrent.futures.Future.cancel()`'s own documented
        behavior). What genuinely *can* be terminated is the real
        Agent subprocess that thread is blocked on, via each
        adapter's own existing, targeted `cancel(execution_id)` (real
        process-group SIGTERM/SIGKILL for Claude; a second, in-
        container PID-targeted signal for Hermes, never a broad
        pattern-match kill that could hit an unrelated process sharing
        that container). For every worker still active once the drain
        deadline passes, this now makes exactly one best-effort
        attempt at that -- logged either way, and still never
        required to succeed.

        This does **not** change the release decision: whether or not
        the cancel attempt above succeeded, the lease is released
        *only if* `_active_count()` is actually back to zero by the
        time this method returns. A cancelled subprocess usually
        unblocks its worker thread quickly (the thread's own
        `communicate()`/`wait()` call returns once the process exits),
        so this often lets a clean release happen where the old code
        never could -- but if a thread is still blocked for any other
        reason, the lease is still correctly left unreleased, exactly
        as before this fix: a straggler left running keeps the lease
        it still (genuinely) holds, so a future takeover's own
        recovery scan is what eventually quarantines it, never a
        premature reuse of its Worktree."""
        self._stop_event.set()
        if self._executor is None:
            return

        deadline = time.monotonic() + self._settings.drain_timeout_seconds
        while time.monotonic() < deadline and self._active_count() > 0:
            time.sleep(0.05)

        if self._active_count() > 0:
            self._attempt_cancel_stuck_workers()
            # A successful subprocess kill unblocks its thread almost
            # immediately (it is blocked on that subprocess exiting);
            # give it one short, bounded grace window rather than
            # declaring it still-active a moment too early.
            grace_deadline = time.monotonic() + min(5.0, self._settings.drain_timeout_seconds)
            while time.monotonic() < grace_deadline and self._active_count() > 0:
                time.sleep(0.05)

        still_active = self._active_count()
        self._executor.shutdown(wait=False)

        if still_active > 0:
            _LOG.warning(
                "shutdown: worker(s) still active after drain_timeout_seconds (and a "
                "best-effort subprocess cancel attempt) -- NOT releasing the lease; "
                "it will expire naturally",
                extra={"fields": {"still_active": still_active, "holder_id": self._holder_id}},
            )
            return

        released = self._leases.release(self._holder_id, self._epoch)
        _LOG.info(
            "scheduler shutdown complete",
            extra={"fields": {"holder_id": self._holder_id, "lease_released": released}},
        )

    def _attempt_cancel_stuck_workers(self) -> None:
        with self._futures_lock:
            stuck = {task_id: self._inflight[task_id] for task_id in self._futures if task_id in self._inflight}
        for task_id, (provider_id, execution_id) in stuck.items():
            try:
                adapter = self._engine.adapters.get(provider_id) if self._engine.adapters else None
                cancel = getattr(adapter, "cancel", None)
                if cancel is None:
                    _LOG.warning(
                        "shutdown: adapter has no cancel() -- cannot terminate this worker's subprocess",
                        extra={"fields": {"task_id": task_id, "provider_id": provider_id}},
                    )
                    continue
                cancelled = cancel(execution_id)
                _LOG.warning(
                    "shutdown: attempted subprocess cancellation for a worker stuck past drain_timeout_seconds",
                    extra={"fields": {
                        "task_id": task_id, "execution_id": execution_id, "cancel_accepted": cancelled,
                    }},
                )
            except Exception:
                _LOG.exception(
                    "shutdown: cancel attempt itself raised -- worker left as-is, lease still not released",
                    extra={"fields": {"task_id": task_id}},
                )

    # -- internals -----------------------------------------------------------

    def _ready_tasks(self):
        claimed = {c.task_id for c in self._leases.list_claims(status=ClaimStatus.CLAIMED)}
        return [
            t for t in self._engine.manager.all_tasks()
            if t.status is TaskStatus.READY and t.task_id not in claimed
        ]

    def _active_count(self) -> int:
        with self._futures_lock:
            return len(self._futures)

    def _claim(self, task_id: str) -> bool:
        try:
            return self._leases.claim_task(task_id, self._holder_id, self._epoch)
        except DuplicateError:
            return False

    def _submit_worker(self, task_id: str) -> None:
        future = self._executor.submit(self._run_task, task_id)
        with self._futures_lock:
            self._futures[task_id] = future
        future.add_done_callback(lambda _f, tid=task_id: self._on_worker_done(tid))

    def _on_worker_done(self, task_id: str) -> None:
        with self._futures_lock:
            self._futures.pop(task_id, None)

    def _record_inflight(self, task_id: str, request) -> None:
        with self._futures_lock:
            self._inflight[task_id] = (request.provider_id, request.execution_id)

    def _forget_inflight(self, task_id: str) -> None:
        with self._futures_lock:
            self._inflight.pop(task_id, None)

    def _run_task(self, task_id: str) -> None:
        task = self._engine.manager.get(task_id)
        try:
            project = self._projects.get(task.project_id)
        except NotFoundError:
            _LOG.error(
                "no ProjectProfile registered for project; cannot dispatch",
                extra={"fields": {"task_id": task_id, "project_id": task.project_id}},
            )
            self._finish_claim(task_id)
            return

        try:
            self._ensure_routed(task_id, project)
            input_context = self._input_context_builder(task)
            self._pipeline.dispatch(
                task_id, project,
                input_context=input_context, workspace_transfer=self._workspace_transfer,
                timeout_seconds=self._settings.dispatch_timeout_seconds,
                on_dispatched=lambda request: self._record_inflight(task_id, request),
            )
        except Exception:
            _LOG.exception("dispatch failed", extra={"fields": {"task_id": task_id}})
            self._finish_claim(task_id)
            return
        finally:
            self._forget_inflight(task_id)

        if not self._still_authoritative():
            _LOG.warning(
                "scheduler lease lost mid-dispatch -- not calling evaluate_and_propose; "
                "the new authority's recovery scan will quarantine this claim",
                extra={"fields": {"task_id": task_id}},
            )
            return  # deliberately skips _finish_claim: this epoch is no longer authoritative

        try:
            evidence = self._evidence_provider(task_id) if self._evidence_provider else None
            self._pipeline.evaluate_and_propose(task_id, project, criteria_evidence=evidence)
        except Exception:
            _LOG.exception("evaluate_and_propose failed", extra={"fields": {"task_id": task_id}})

        self._finish_claim(task_id)

    def _ensure_routed(self, task_id: str, project) -> None:
        decisions = self._engine.manager.get_routing_decisions(task_id)
        if decisions and decisions[-1].outcome is RoutingOutcome.SELECTED:
            return
        task = self._engine.manager.get(task_id)
        task.payload.setdefault("required_role", "coder")
        self._engine.route(task_id, project)

    def _still_authoritative(self) -> bool:
        current = self._leases.current()
        return current.holder_id == self._holder_id and current.epoch == self._epoch

    def _finish_claim(self, task_id: str) -> None:
        ok = self._leases.complete_claim(task_id, self._holder_id, self._epoch)
        if not ok:
            _LOG.warning(
                "stale completion write rejected -- lease was lost before this claim "
                "could complete",
                extra={"fields": {"task_id": task_id}},
            )
