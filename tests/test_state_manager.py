import sqlite3
import tempfile
import unittest
from pathlib import Path

from datetime import datetime, timezone

from universal_orchestrator.core.models import (
    RejectedCandidate,
    RoutingDecision,
    RoutingOutcome,
    Task,
    TaskError,
    TaskSpecification,
    TaskStatus,
)
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.errors import DuplicateError, NotFoundError
from universal_orchestrator.execution.adapter import ExecutionOutcome, ExecutionResult, VerificationStatus


class StateManagerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "orchestrator.sqlite3"
        self.state = StateManager(self.db_path)

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()


class PersistenceTests(StateManagerTestCase):
    def test_create_then_get_round_trips_all_fields(self):
        task = Task(
            title="scaffold core",
            project_id="proj_a",
            parent_id=None,
            depends_on=("task_dep1", "task_dep2"),
            priority=5,
            owner="founder",
            payload={"k": "v"},
            specification=TaskSpecification(
                objective="o", scope="s", constraints=["c1"], missing_information=["m1"]
            ),
        )
        self.state.create(task)

        loaded = self.state.get(task.task_id)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.task_id, task.task_id)
        self.assertEqual(loaded.status, TaskStatus.CREATED)
        self.assertEqual(loaded.depends_on, ("task_dep1", "task_dep2"))
        self.assertEqual(loaded.priority, 5)
        self.assertEqual(loaded.owner, "founder")
        self.assertEqual(loaded.payload, {"k": "v"})
        self.assertEqual(loaded.specification.objective, "o")
        self.assertEqual(loaded.specification.constraints, ["c1"])
        self.assertEqual(loaded.history, task.history)

    def test_get_missing_task_returns_none(self):
        self.assertIsNone(self.state.get("task_does_not_exist"))

    def test_update_persists_status_and_error(self):
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)

        task.status = TaskStatus.FAILED
        task.error = TaskError(code="boom", message="it broke", detail="stacktrace")
        self.state.update(task)

        loaded = self.state.get(task.task_id)
        self.assertEqual(loaded.status, TaskStatus.FAILED)
        self.assertEqual(loaded.error.code, "boom")
        self.assertEqual(loaded.error.message, "it broke")

    def test_update_missing_task_raises_not_found(self):
        task = Task(title="t", project_id="proj_a")
        with self.assertRaises(NotFoundError):
            self.state.update(task)

    def test_list_filters_by_project_and_status(self):
        t1 = Task(title="t1", project_id="proj_a")
        t2 = Task(title="t2", project_id="proj_b")
        self.state.create(t1)
        self.state.create(t2)

        self.assertEqual({t.task_id for t in self.state.list()}, {t1.task_id, t2.task_id})
        self.assertEqual([t.task_id for t in self.state.list(project_id="proj_a")], [t1.task_id])
        self.assertEqual(
            [t.task_id for t in self.state.list(status=TaskStatus.CREATED)],
            [t1.task_id, t2.task_id],
        )


class DuplicatePreventionTests(StateManagerTestCase):
    def test_creating_the_same_task_id_twice_raises_duplicate_error(self):
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)
        with self.assertRaises(DuplicateError):
            self.state.create(task)

    def test_duplicate_create_does_not_corrupt_the_existing_row(self):
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)
        try:
            self.state.create(task)
        except DuplicateError:
            pass

        self.assertEqual(len(self.state.list()), 1)


class TransactionSafetyTests(StateManagerTestCase):
    def test_a_failed_write_does_not_leave_a_partial_row(self):
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)

        with self.assertRaises(DuplicateError):
            self.state.create(task)

        # The failed second create() must not have touched the first row.
        loaded = self.state.get(task.task_id)
        self.assertEqual(loaded.title, "t")


class RestartBehaviorTests(StateManagerTestCase):
    def test_tasks_survive_closing_and_reopening_the_same_database_file(self):
        task = Task(title="survives restart", project_id="proj_a")
        self.state.create(task)
        self.state.close()

        reopened = StateManager(self.db_path)
        try:
            loaded = reopened.get(task.task_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.title, "survives restart")
        finally:
            reopened.close()

    def test_schema_initialization_is_idempotent(self):
        # Opening the same file again must not fail or wipe existing data.
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)

        second_handle = StateManager(self.db_path)
        try:
            self.assertEqual(len(second_handle.list()), 1)
        finally:
            second_handle.close()


class DataIntegrityTests(StateManagerTestCase):
    def test_tasks_table_enforces_a_primary_key_on_task_id(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute("PRAGMA table_info(tasks)")
            pk_columns = [row[1] for row in cursor.fetchall() if row[5] == 1]
            self.assertEqual(pk_columns, ["task_id"])
        finally:
            conn.close()

    def test_result_and_error_round_trip_through_json(self):
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)

        task.result = {"exit_code": 0, "output": "ok"}
        task.error = TaskError(code="e", message="m")
        self.state.update(task)

        loaded = self.state.get(task.task_id)
        self.assertEqual(loaded.result, {"exit_code": 0, "output": "ok"})
        self.assertEqual(loaded.error.code, "e")


class RoutingDecisionPersistenceTests(StateManagerTestCase):
    def test_save_then_get_round_trips_a_selected_decision(self):
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)
        decision = RoutingDecision(
            task_id=task.task_id,
            project_id="proj_a",
            outcome=RoutingOutcome.SELECTED,
            explanation="selected agent_x",
            selected_agent_id="agent_x",
            selected_provider_id="prov_y",
            matched_capabilities=["python"],
            rejected=[RejectedCandidate("agent_z", ["offline"])],
            score=1.0,
        )

        self.state.save_routing_decision(decision)
        loaded = self.state.get_routing_decisions(task.task_id)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].selected_agent_id, "agent_x")
        self.assertEqual(loaded[0].rejected, [RejectedCandidate("agent_z", ["offline"])])
        self.assertEqual(loaded[0].score, 1.0)

    def test_saving_a_routing_decision_does_not_change_the_task_row(self):
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)
        before = self.state.get(task.task_id)

        self.state.save_routing_decision(
            RoutingDecision(
                task_id=task.task_id,
                project_id="proj_a",
                outcome=RoutingOutcome.NO_ELIGIBLE_AGENT,
                explanation="no eligible agent",
            )
        )

        after = self.state.get(task.task_id)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.history, before.history)
        self.assertEqual(after.updated_at, before.updated_at)

    def test_multiple_decisions_for_the_same_task_are_all_kept(self):
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)
        for i in range(2):
            self.state.save_routing_decision(
                RoutingDecision(
                    task_id=task.task_id,
                    project_id="proj_a",
                    outcome=RoutingOutcome.NO_ELIGIBLE_AGENT,
                    explanation=f"attempt {i}",
                )
            )

        self.assertEqual(len(self.state.get_routing_decisions(task.task_id)), 2)

    def test_no_decisions_returns_empty_list(self):
        self.assertEqual(self.state.get_routing_decisions("task_missing"), [])


class ExecutionResultPersistenceTests(StateManagerTestCase):
    def test_save_then_get_round_trips_a_completed_result(self):
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)
        started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ended = datetime(2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc)
        result = ExecutionResult(
            execution_id="exec_1",
            task_id=task.task_id,
            outcome=ExecutionOutcome.COMPLETED,
            exit_code=0,
            stdout="hello",
            stderr="",
            started_at=started,
            ended_at=ended,
            duration_seconds=3.0,
            cli_version="2.1.221 (Claude Code)",
            output_format="json",
            structured_output={"result": "hello"},
            structured_output_valid=True,
            verification_status=VerificationStatus.AVAILABLE,
        )

        self.state.save_execution_result(result)
        loaded = self.state.get_execution_results(task.task_id)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].execution_id, "exec_1")
        self.assertIs(loaded[0].outcome, ExecutionOutcome.COMPLETED)
        self.assertEqual(loaded[0].structured_output, {"result": "hello"})
        self.assertTrue(loaded[0].structured_output_valid)
        self.assertIs(loaded[0].verification_status, VerificationStatus.AVAILABLE)
        self.assertEqual(loaded[0].duration_seconds, 3.0)

    def test_saving_an_execution_result_does_not_change_the_task_row(self):
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)
        before = self.state.get(task.task_id)

        self.state.save_execution_result(
            ExecutionResult(execution_id="exec_1", task_id=task.task_id, outcome=ExecutionOutcome.FAILED)
        )

        after = self.state.get(task.task_id)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.history, before.history)

    def test_no_results_returns_empty_list(self):
        self.assertEqual(self.state.get_execution_results("task_missing"), [])

    def test_unverified_result_fields_default_to_none(self):
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)
        self.state.save_execution_result(
            ExecutionResult(execution_id="exec_1", task_id=task.task_id, outcome=ExecutionOutcome.ERROR)
        )

        loaded = self.state.get_execution_results(task.task_id)[0]
        self.assertIsNone(loaded.structured_output)
        self.assertIsNone(loaded.structured_output_valid)
        self.assertIsNone(loaded.verification_status)

    def test_duplicate_execution_id_raises_typed_duplicate_error(self):
        """Audit Finding E-2: a raw sqlite3.IntegrityError must never
        surface here — it is translated the same way tasks.create()
        already translates one."""
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)
        result = ExecutionResult(
            execution_id="exec_dup", task_id=task.task_id, outcome=ExecutionOutcome.COMPLETED
        )
        self.state.save_execution_result(result)

        with self.assertRaises(DuplicateError):
            self.state.save_execution_result(result)

    def test_duplicate_execution_result_does_not_corrupt_the_existing_row(self):
        task = Task(title="t", project_id="proj_a")
        self.state.create(task)
        result = ExecutionResult(
            execution_id="exec_dup",
            task_id=task.task_id,
            outcome=ExecutionOutcome.COMPLETED,
            stdout="first",
        )
        self.state.save_execution_result(result)
        try:
            self.state.save_execution_result(
                ExecutionResult(
                    execution_id="exec_dup",
                    task_id=task.task_id,
                    outcome=ExecutionOutcome.FAILED,
                    stdout="second",
                )
            )
        except DuplicateError:
            pass

        loaded = self.state.get_execution_results(task.task_id)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].stdout, "first")


if __name__ == "__main__":
    unittest.main()
