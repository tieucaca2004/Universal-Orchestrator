"""Milestone 7, Component C — Task Scheduler.

Uses a small in-process fake `ExecutionAdapter` (the documented,
pluggable seam every real adapter implements — `ClaudeCodeAdapter`/
`HermesAdapter` are just two concrete instances of it) instead of a
subprocess, so concurrency and lease-takeover timing can be controlled
deterministically with a `threading.Event` rather than real sleeps.
Everything else — `GovernedPipeline`, `TaskManager`, `WorktreeManager`,
`ConflictManager`, `ApprovalGate`, `QualityGate`, `StateManager`/SQLite,
real `git` — is exercised for real, unmocked, same discipline as
tests/test_governed_pipeline.py.
"""

import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from universal_orchestrator.config.scheduler_settings import SchedulerSettings
from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.governed_pipeline import GovernedPipeline
from universal_orchestrator.core.models import (
    AgentDescriptor,
    AgentStatus,
    ProjectProfile,
    ProviderDescriptor,
    ProviderStatus,
    TaskRequest,
)
from universal_orchestrator.core.scheduler import Scheduler, default_input_context
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.execution.adapter import (
    ExecutionAdapter,
    ExecutionOutcome,
    ExecutionResult,
    VerificationStatus,
)
from universal_orchestrator.execution.approval_manager import ApprovalGate
from universal_orchestrator.execution.conflict_manager import ConflictManager
from universal_orchestrator.execution.quality_manager import QualityGate
from universal_orchestrator.execution.registry import AdapterRegistry
from universal_orchestrator.execution.scheduler import ClaimStatus
from universal_orchestrator.execution.scheduler_manager import SchedulerLeaseManager
from universal_orchestrator.execution.worktree import WorktreeStatus
from universal_orchestrator.execution.worktree_manager import WorktreeManager
from universal_orchestrator.registry.agents import AgentRegistry
from universal_orchestrator.registry.projects import ProjectRegistry
from universal_orchestrator.registry.providers import ProviderRegistry
from universal_orchestrator.routing.router import AgentRouter


def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], path)
    _run(["git", "config", "user.email", "test@example.com"], path)
    _run(["git", "config", "user.name", "Test"], path)
    (path / "README.md").write_text("original\n")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-q", "-m", "initial"], path)


class BlockingAdapter(ExecutionAdapter):
    """Blocks inside `execute()` until the test releases it — gives
    deterministic control over exactly when a dispatch "finishes",
    without real sleeps or subprocess timing."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []
        self.input_contexts: dict[str, str] = {}

    def verify(self) -> VerificationStatus:
        return VerificationStatus.AVAILABLE

    def execute(self, request) -> ExecutionResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(request.task_id)
            self.input_contexts[request.task_id] = request.input_context
        self.started.set()
        self.release.wait(timeout=10)
        Path(request.working_directory, "agent_output.txt").write_text("fake output\n")
        with self._lock:
            self.active -= 1
        return ExecutionResult(
            execution_id=f"exec_{request.task_id}",
            task_id=request.task_id,
            outcome=ExecutionOutcome.COMPLETED,
            exit_code=0,
        )


def _build_stack(state: StateManager, adapter: ExecutionAdapter):
    agents = AgentRegistry()
    providers = ProviderRegistry()
    adapters = AdapterRegistry()

    provider = providers.register(ProviderDescriptor(name="fake", kind="cli"))
    providers.set_verified(provider.provider_id, True)
    providers.set_status(provider.provider_id, ProviderStatus.AVAILABLE)
    adapters.register(provider.provider_id, adapter)

    agent = agents.register(AgentDescriptor(name="fake-coder", role="coder", provider_id=provider.provider_id))
    agents.set_status(agent.agent_id, AgentStatus.AVAILABLE)

    router = AgentRouter(agents, providers)
    engine = OrchestrationEngine.bootstrap(
        state, router=router, adapters=adapters, agents=agents, providers=providers
    )
    worktrees = WorktreeManager(state)
    conflicts = ConflictManager(state, worktrees)
    approvals = ApprovalGate(state)
    quality = QualityGate(state)
    pipeline = GovernedPipeline(engine, worktrees, conflicts, approvals, quality)
    return engine, pipeline


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_path = self.tmp_path / "project"
        _init_repo(self.repo_path)
        self.db_path = self.tmp_path / "orchestrator.sqlite3"
        self.state = StateManager(self.db_path)
        self.adapter = BlockingAdapter()
        self.engine, self.pipeline = _build_stack(self.state, self.adapter)
        self.projects = ProjectRegistry()
        self.project = self.projects.register(
            ProjectProfile(name="proj", repository_path=str(self.repo_path), project_id="proj")
        )
        self.settings = SchedulerSettings(max_concurrency=1, lease_seconds=30, poll_seconds=0.05,
                                           drain_timeout_seconds=1.0)

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def _submit(self, title="t") -> str:
        task = self.engine.submit(TaskRequest(title=title), self.project)
        return task.task_id

    def _new_state_connection(self) -> StateManager:
        return StateManager(self.db_path)


class NoEvidenceEndsFailedTests(SchedulerTestCase):
    """The honest, documented limitation: no criteria_evidence_provider
    means every automatic evaluate_and_propose() call sees no
    evidence, so Quality Gate correctly returns NOT_VERIFIED, not PASS
    -- this is unchanged M6.4 behavior, not a Scheduler bug."""

    def test_task_with_no_evidence_ends_failed_and_worktree_needs_review(self):
        task_id = self._submit()
        self.adapter.release.set()  # never actually blocks for this test
        scheduler = Scheduler(self.state, self.engine, self.pipeline, self.projects, settings=self.settings)
        scheduler.start()
        dispatched = scheduler.run_once()
        self.assertEqual(dispatched, [task_id])

        leases = SchedulerLeaseManager(self.state)
        self.assertTrue(_wait_until(lambda: leases.get_claim(task_id).status == ClaimStatus.COMPLETED))

        task = self.engine.manager.get(task_id)
        self.assertEqual(task.status.value, "failed")
        handle = self.pipeline.worktrees.get(task_id)
        self.assertEqual(handle.status, WorktreeStatus.NEEDS_REVIEW)
        scheduler.shutdown()


class SuccessPathWithEvidenceTests(SchedulerTestCase):
    def test_task_with_supplied_evidence_reaches_awaiting_approval_with_a_proposal(self):
        task = self.engine.manager.create_task("t", self.project.project_id)
        from universal_orchestrator.core.models import TaskSpecification
        self.engine.manager.attach_specification(
            task.task_id, TaskSpecification(objective="o", scope="s", acceptance_criteria=["works"])
        )
        self.adapter.release.set()

        def _evidence(task_id):
            return {"works": True}

        scheduler = Scheduler(
            self.state, self.engine, self.pipeline, self.projects,
            settings=self.settings, criteria_evidence_provider=_evidence,
        )
        scheduler.start()
        scheduler.run_once()

        leases = SchedulerLeaseManager(self.state)
        self.assertTrue(_wait_until(lambda: leases.get_claim(task.task_id).status == ClaimStatus.COMPLETED))

        updated = self.engine.manager.get(task.task_id)
        self.assertEqual(updated.status.value, "succeeded")
        handle = self.pipeline.worktrees.get(task.task_id)
        self.assertEqual(handle.status, WorktreeStatus.AWAITING_APPROVAL)
        self.assertIsNotNone(self.pipeline.approvals.get_proposal(task.task_id))
        scheduler.shutdown()


class ConcurrencyGatingTests(SchedulerTestCase):
    def test_run_once_never_claims_more_than_max_concurrency(self):
        ids = [self._submit(f"t{i}") for i in range(3)]
        scheduler = Scheduler(self.state, self.engine, self.pipeline, self.projects, settings=self.settings)
        scheduler.start()

        first = scheduler.run_once()
        self.assertEqual(len(first), 1)
        self.assertTrue(self.adapter.started.wait(timeout=2))
        self.assertEqual(self.adapter.max_active, 1)

        # Still blocked -- a second pass must not claim anything new.
        second = scheduler.run_once()
        self.assertEqual(second, [])

        self.adapter.release.set()
        leases = SchedulerLeaseManager(self.state)
        self.assertTrue(_wait_until(lambda: leases.get_claim(first[0]).status == ClaimStatus.COMPLETED))

        # Reset for the next worker and let the remaining two proceed.
        self.adapter.release.set()
        remaining = set(ids) - {first[0]}
        dispatched_more = set()
        while remaining - dispatched_more:
            got = scheduler.run_once()
            dispatched_more.update(got)
            self.assertTrue(_wait_until(
                lambda: all(leases.get_claim(t) and leases.get_claim(t).status == ClaimStatus.COMPLETED
                            for t in dispatched_more)
            ))
        self.assertEqual(self.adapter.max_active, 1)
        scheduler.shutdown()


class CrossProcessTakeoverTests(SchedulerTestCase):
    def test_stale_lease_is_taken_over_and_stuck_claim_is_quarantined(self):
        task_id = self._submit()

        short_lease_settings = SchedulerSettings(
            max_concurrency=1, lease_seconds=0.05, poll_seconds=0.02, drain_timeout_seconds=1.0,
        )
        scheduler1 = Scheduler(
            self.state, self.engine, self.pipeline, self.projects, settings=short_lease_settings,
        )
        scheduler1.start()
        dispatched = scheduler1.run_once()
        self.assertEqual(dispatched, [task_id])
        self.assertTrue(self.adapter.started.wait(timeout=2))  # worker is now blocked mid-execute()

        handle = self.pipeline.worktrees.get(task_id)
        self.assertEqual(handle.status, WorktreeStatus.EXECUTING)

        time.sleep(0.15)  # scheduler1's lease has now expired

        other_state = self._new_state_connection()
        try:
            other_adapter = BlockingAdapter()
            other_adapter.release.set()
            other_engine, other_pipeline = _build_stack(other_state, other_adapter)
            scheduler2 = Scheduler(
                other_state, other_engine, other_pipeline, self.projects,
                settings=SchedulerSettings(max_concurrency=1, lease_seconds=30, poll_seconds=0.02,
                                            drain_timeout_seconds=1.0),
            )
            scheduler2.start()  # acquires a new epoch and runs recovery
            self.assertEqual(scheduler2.epoch, scheduler1.epoch + 1)

            leases = SchedulerLeaseManager(self.state)
            self.assertEqual(leases.get_claim(task_id).status, ClaimStatus.QUARANTINED)
            self.assertEqual(self.pipeline.worktrees.get(task_id).status, WorktreeStatus.NEEDS_REVIEW)

            # scheduler2 must never pick this task back up -- it is no
            # longer READY (TaskManager.start() already moved it to
            # RUNNING), independent of the claim bookkeeping above.
            other_engine.manager.sync_new_tasks()
            self.assertEqual(scheduler2.run_once(), [])

            # The stale scheduler1 worker finally "finishes" -- its own
            # fenced completion write must be rejected, and its attempt
            # to advance the (already quarantined) Worktree must itself
            # fail against the *unchanged* M6 Worktree state machine
            # (NEEDS_REVIEW -> QUALITY_CHECK is not an allowed edge),
            # so the Worktree is never silently moved out of review.
            self.adapter.release.set()
            self.assertTrue(_wait_until(lambda: scheduler1._active_count() == 0))

            self.assertEqual(leases.get_claim(task_id).status, ClaimStatus.QUARANTINED)
            self.assertEqual(self.pipeline.worktrees.get(task_id).status, WorktreeStatus.NEEDS_REVIEW)
        finally:
            other_state.close()
        scheduler1.shutdown()


class ShutdownDrainTests(SchedulerTestCase):
    def test_shutdown_does_not_release_lease_while_a_worker_is_unconfirmed(self):
        task_id = self._submit()
        scheduler = Scheduler(
            self.state, self.engine, self.pipeline, self.projects,
            settings=SchedulerSettings(max_concurrency=1, lease_seconds=30, poll_seconds=0.02,
                                        drain_timeout_seconds=0.2),
        )
        scheduler.start()
        scheduler.run_once()
        self.assertTrue(self.adapter.started.wait(timeout=2))  # worker blocked, unconfirmed

        scheduler.shutdown()  # drain_timeout_seconds=0.2, worker still blocked -> must not release
        leases = SchedulerLeaseManager(self.state)
        current = leases.current()
        self.assertEqual(current.holder_id, scheduler.holder_id)
        self.assertEqual(current.epoch, scheduler.epoch)

        self.adapter.release.set()
        self.assertTrue(_wait_until(lambda: leases.get_claim(task_id).status == ClaimStatus.COMPLETED))

    def test_shutdown_releases_the_lease_once_the_worker_actually_finishes(self):
        task_id = self._submit()
        self.adapter.release.set()
        scheduler = Scheduler(self.state, self.engine, self.pipeline, self.projects, settings=self.settings)
        scheduler.start()
        scheduler.run_once()

        leases = SchedulerLeaseManager(self.state)
        self.assertTrue(_wait_until(lambda: leases.get_claim(task_id).status == ClaimStatus.COMPLETED))

        scheduler.shutdown()
        self.assertIsNone(leases.current().holder_id)


class InputContextTests(SchedulerTestCase):
    """Regression coverage for the empty-prompt dispatch bug: before
    this fix, `_run_task` called `pipeline.dispatch()` with no
    `input_context` at all, so every Scheduler-initiated Agent call
    silently fell back to `GovernedPipeline.dispatch()`'s own
    `input_context: str = ""` default -- a real Agent run with no
    prompt. `default_input_context()` is the fix: it is the Scheduler's
    only source for a prompt (no human types one in per task), built
    from the task's own `TaskSpecification` (Milestone 2's Task
    Analyzer output), never fabricated."""

    def test_default_input_context_builds_a_real_prompt_from_the_specification(self):
        from universal_orchestrator.core.models import Task, TaskSpecification, TaskStatus

        spec = TaskSpecification(
            objective="Add a health-check endpoint",
            scope="Only the HTTP layer",
            constraints=["No new dependencies"],
            expected_deliverables=["GET /health returns 200"],
            acceptance_criteria=["curl /health exits 0"],
        )
        task = Task(task_id="t1", title="t", project_id="p", status=TaskStatus.ANALYZED, specification=spec)

        context = default_input_context(task)

        self.assertIn("Add a health-check endpoint", context)
        self.assertIn("Only the HTTP layer", context)
        self.assertIn("No new dependencies", context)
        self.assertIn("GET /health returns 200", context)
        self.assertIn("curl /health exits 0", context)

    def test_default_input_context_is_empty_string_never_fabricated_without_a_specification(self):
        from universal_orchestrator.core.models import Task, TaskStatus

        task = Task(task_id="t1", title="t", project_id="p", status=TaskStatus.CREATED)
        self.assertEqual(default_input_context(task), "")

    def test_scheduler_dispatched_task_receives_a_non_empty_input_context(self):
        from universal_orchestrator.core.models import TaskSpecification

        task = self.engine.manager.create_task("t", self.project.project_id)
        self.engine.manager.attach_specification(
            task.task_id,
            TaskSpecification(objective="Do the real work", scope="Everything in scope"),
        )
        self.adapter.release.set()

        scheduler = Scheduler(self.state, self.engine, self.pipeline, self.projects, settings=self.settings)
        scheduler.start()
        scheduler.run_once()

        leases = SchedulerLeaseManager(self.state)
        self.assertTrue(_wait_until(
            lambda: leases.get_claim(task.task_id) and leases.get_claim(task.task_id).status == ClaimStatus.COMPLETED
        ))

        received = self.adapter.input_contexts[task.task_id]
        self.assertNotEqual(received, "")
        self.assertIn("Do the real work", received)
        scheduler.shutdown()


if __name__ == "__main__":
    unittest.main()
