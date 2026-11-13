import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.core.models import TaskError, TaskSpecification, TaskStatus
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.core.task_manager import InterruptedResolution, TaskManager
from universal_orchestrator.errors import (
    InvalidStateTransitionError,
    NotFoundError,
    OrchestratorError,
    QualityGateRequiredError,
)
from universal_orchestrator.execution.quality import QualityReport, QualityVerdict


def _spec(**overrides):
    defaults = {"objective": "o", "scope": "s"}
    defaults.update(overrides)
    return TaskSpecification(**defaults)


class TaskManagerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state = StateManager(Path(self._tmpdir.name) / "orchestrator.sqlite3")
        self.manager = TaskManager(self.state)

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def _pass_quality_gate(self, task_id: str) -> None:
        """Milestone 6.4: `succeed()` now requires a recorded,
        task-level Quality Gate PASS. These fixtures are about Task
        Manager behavior (dependency cascading, result recording, ...),
        not about Quality Gate evaluation itself — see
        tests/test_quality_gate.py for that — so this helper records
        the precondition directly rather than re-deriving it."""
        self.state.save_quality_report(
            QualityReport(
                task_id=task_id,
                level="task",
                verdict=QualityVerdict.PASS,
                rationale="test fixture",
            )
        )


class CreationTests(TaskManagerTestCase):
    def test_create_task_assigns_a_stable_unique_id(self):
        t1 = self.manager.create_task("t1", "proj_a")
        t2 = self.manager.create_task("t2", "proj_a")
        self.assertNotEqual(t1.task_id, t2.task_id)
        self.assertEqual(self.manager.get(t1.task_id).task_id, t1.task_id)

    def test_new_task_starts_created(self):
        task = self.manager.create_task("t", "proj_a")
        self.assertIs(task.status, TaskStatus.CREATED)

    def test_get_unknown_task_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.manager.get("task_unknown")


class DecompositionTests(TaskManagerTestCase):
    def test_decompose_creates_children_linked_to_parent(self):
        parent = self.manager.create_task("parent", "proj_a")
        children = self.manager.decompose(parent.task_id, ["child-1", "child-2"])

        self.assertEqual(len(children), 2)
        for child in children:
            self.assertEqual(child.parent_id, parent.task_id)
            self.assertEqual(child.project_id, parent.project_id)

        linked = self.manager.list_children(parent.task_id)
        self.assertEqual({c.task_id for c in linked}, {c.task_id for c in children})

    def test_decompose_unknown_parent_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.manager.decompose("task_unknown", ["child"])


class DependencyTests(TaskManagerTestCase):
    def test_task_with_unmet_dependency_becomes_blocked(self):
        a = self.manager.create_task("A", "proj_a")
        b = self.manager.create_task("B", "proj_a")
        self.manager.add_dependency(b.task_id, a.task_id)

        updated = self.manager.attach_specification(b.task_id, _spec())
        self.assertIs(updated.status, TaskStatus.BLOCKED)

    def test_task_with_no_dependencies_becomes_ready(self):
        a = self.manager.create_task("A", "proj_a")
        updated = self.manager.attach_specification(a.task_id, _spec())
        self.assertIs(updated.status, TaskStatus.READY)

    def test_blocked_task_becomes_ready_once_dependency_succeeds(self):
        a = self.manager.create_task("A", "proj_a")
        b = self.manager.create_task("B", "proj_a")
        self.manager.add_dependency(b.task_id, a.task_id)
        self.manager.attach_specification(a.task_id, _spec())
        self.manager.attach_specification(b.task_id, _spec())
        self.assertIs(self.manager.get(b.task_id).status, TaskStatus.BLOCKED)

        self.manager.start(a.task_id)
        self._pass_quality_gate(a.task_id)
        self.manager.succeed(a.task_id, result="done")

        self.assertIs(self.manager.get(b.task_id).status, TaskStatus.READY)

    def test_task_depending_on_two_tasks_waits_for_both(self):
        a = self.manager.create_task("A", "proj_a")
        b = self.manager.create_task("B", "proj_a")
        c = self.manager.create_task("C", "proj_a")
        self.manager.add_dependency(c.task_id, a.task_id)
        self.manager.add_dependency(c.task_id, b.task_id)
        self.manager.attach_specification(a.task_id, _spec())
        self.manager.attach_specification(b.task_id, _spec())
        self.manager.attach_specification(c.task_id, _spec())
        self.assertIs(self.manager.get(c.task_id).status, TaskStatus.BLOCKED)

        self.manager.start(a.task_id)
        self._pass_quality_gate(a.task_id)
        self.manager.succeed(a.task_id)
        self.assertIs(
            self.manager.get(c.task_id).status,
            TaskStatus.BLOCKED,
            "still blocked: B has not succeeded yet",
        )

        self.manager.start(b.task_id)
        self._pass_quality_gate(b.task_id)
        self.manager.succeed(b.task_id)
        self.assertIs(self.manager.get(c.task_id).status, TaskStatus.READY)

    def test_cannot_add_dependency_once_task_is_ready(self):
        a = self.manager.create_task("A", "proj_a")
        b = self.manager.create_task("B", "proj_a")
        self.manager.attach_specification(b.task_id, _spec())  # -> READY

        with self.assertRaises(OrchestratorError):
            self.manager.add_dependency(b.task_id, a.task_id)


class CancellationTests(TaskManagerTestCase):
    def test_cancel_from_created(self):
        task = self.manager.create_task("t", "proj_a")
        cancelled = self.manager.cancel(task.task_id)
        self.assertIs(cancelled.status, TaskStatus.CANCELLED)

    def test_cannot_cancel_a_terminal_task(self):
        task = self.manager.create_task("t", "proj_a")
        self.manager.cancel(task.task_id)
        with self.assertRaises(InvalidStateTransitionError):
            self.manager.cancel(task.task_id)


class InvalidTransitionTests(TaskManagerTestCase):
    def test_cannot_start_a_task_that_was_never_analyzed(self):
        task = self.manager.create_task("t", "proj_a")
        with self.assertRaises(InvalidStateTransitionError):
            self.manager.start(task.task_id)

    def test_cannot_succeed_a_task_that_is_not_running(self):
        task = self.manager.create_task("t", "proj_a")
        self.manager.attach_specification(task.task_id, _spec())  # -> READY
        with self.assertRaises(InvalidStateTransitionError):
            self.manager.succeed(task.task_id)

    def test_terminal_states_accept_no_further_transition(self):
        task = self.manager.create_task("t", "proj_a")
        self.manager.attach_specification(task.task_id, _spec())
        self.manager.start(task.task_id)
        self._pass_quality_gate(task.task_id)
        self.manager.succeed(task.task_id)
        with self.assertRaises(InvalidStateTransitionError):
            self.manager.start(task.task_id)

    def test_cannot_succeed_without_a_recorded_quality_gate_pass(self):
        """Milestone 6.4: the task is otherwise perfectly eligible to
        succeed (RUNNING, valid transition target) — the only thing
        missing is a recorded Quality Gate verdict. A completed
        process is never, by itself, sufficient."""
        task = self.manager.create_task("t", "proj_a")
        self.manager.attach_specification(task.task_id, _spec())
        self.manager.start(task.task_id)
        with self.assertRaises(QualityGateRequiredError):
            self.manager.succeed(task.task_id)
        # Refusal must not have silently advanced the task's status.
        self.assertIs(self.manager.get(task.task_id).status, TaskStatus.RUNNING)

    def test_cannot_succeed_with_a_fail_or_not_verified_quality_gate_result(self):
        task = self.manager.create_task("t", "proj_a")
        self.manager.attach_specification(task.task_id, _spec())
        self.manager.start(task.task_id)
        self.state.save_quality_report(
            QualityReport(
                task_id=task.task_id,
                level="task",
                verdict=QualityVerdict.FAIL,
                rationale="test fixture",
            )
        )
        with self.assertRaises(QualityGateRequiredError):
            self.manager.succeed(task.task_id)


class RecoveryTests(TaskManagerTestCase):
    def test_recover_flags_running_tasks_as_interrupted(self):
        task = self.manager.create_task("t", "proj_a")
        self.manager.attach_specification(task.task_id, _spec())
        self.manager.start(task.task_id)

        fresh_manager = TaskManager(self.state)
        interrupted_ids = fresh_manager.recover()

        self.assertEqual(interrupted_ids, [task.task_id])
        self.assertIs(fresh_manager.get(task.task_id).status, TaskStatus.INTERRUPTED)

    def test_recover_never_marks_a_task_succeeded(self):
        task = self.manager.create_task("t", "proj_a")
        self.manager.attach_specification(task.task_id, _spec())
        self.manager.start(task.task_id)

        fresh_manager = TaskManager(self.state)
        fresh_manager.recover()

        self.assertIsNot(fresh_manager.get(task.task_id).status, TaskStatus.SUCCEEDED)

    def test_resolve_interrupted_retry_goes_to_ready_not_running(self):
        task = self.manager.create_task("t", "proj_a")
        self.manager.attach_specification(task.task_id, _spec())
        self.manager.start(task.task_id)
        self.manager.recover()

        resolved = self.manager.resolve_interrupted(task.task_id, InterruptedResolution.RETRY)
        self.assertIs(resolved.status, TaskStatus.READY)

    def test_resolve_interrupted_confirmed_succeeded_requires_evidence(self):
        task = self.manager.create_task("t", "proj_a")
        self.manager.attach_specification(task.task_id, _spec())
        self.manager.start(task.task_id)
        self.manager.recover()

        with self.assertRaises(ValueError):
            self.manager.resolve_interrupted(
                task.task_id, InterruptedResolution.CONFIRMED_SUCCEEDED, evidence=""
            )

    def test_resolve_interrupted_confirmed_succeeded_with_evidence_cascades_dependents(self):
        a = self.manager.create_task("A", "proj_a")
        b = self.manager.create_task("B", "proj_a")
        self.manager.add_dependency(b.task_id, a.task_id)
        self.manager.attach_specification(a.task_id, _spec())
        self.manager.attach_specification(b.task_id, _spec())
        self.manager.start(a.task_id)
        self.manager.recover()  # a.task_id -> INTERRUPTED

        self.manager.resolve_interrupted(
            a.task_id,
            InterruptedResolution.CONFIRMED_SUCCEEDED,
            evidence="checked external system log: run completed",
        )

        self.assertIs(self.manager.get(a.task_id).status, TaskStatus.SUCCEEDED)
        self.assertIs(self.manager.get(b.task_id).status, TaskStatus.READY)

    def test_cannot_resolve_a_task_that_is_not_interrupted(self):
        task = self.manager.create_task("t", "proj_a")
        with self.assertRaises(OrchestratorError):
            self.manager.resolve_interrupted(task.task_id, InterruptedResolution.RETRY)


class ResultAndErrorRecordingTests(TaskManagerTestCase):
    def test_fail_records_structured_error(self):
        task = self.manager.create_task("t", "proj_a")
        self.manager.attach_specification(task.task_id, _spec())
        self.manager.start(task.task_id)

        failed = self.manager.fail(task.task_id, TaskError(code="boom", message="it broke"))
        self.assertIs(failed.status, TaskStatus.FAILED)
        self.assertEqual(failed.error.code, "boom")

    def test_succeed_records_result(self):
        task = self.manager.create_task("t", "proj_a")
        self.manager.attach_specification(task.task_id, _spec())
        self.manager.start(task.task_id)
        self._pass_quality_gate(task.task_id)

        succeeded = self.manager.succeed(task.task_id, result={"ok": True})
        self.assertEqual(succeeded.result, {"ok": True})


if __name__ == "__main__":
    unittest.main()
