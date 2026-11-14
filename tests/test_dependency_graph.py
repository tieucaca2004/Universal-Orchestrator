import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.core.dependency_graph import (
    diagnose_blocked,
    find_cycles,
    would_create_cycle,
)
from universal_orchestrator.core.models import Task, TaskSpecification, TaskStatus
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.core.task_manager import TaskManager
from universal_orchestrator.errors import DependencyCycleError


def _task(task_id: str, *, depends_on=(), status=TaskStatus.CREATED) -> Task:
    return Task(
        title=task_id, project_id="proj", task_id=task_id, depends_on=tuple(depends_on), status=status
    )


def _graph(*tasks: Task) -> dict[str, Task]:
    return {t.task_id: t for t in tasks}


class WouldCreateCycleTests(unittest.TestCase):
    def test_unrelated_tasks_are_safe(self):
        tasks = _graph(_task("a"), _task("b"))
        self.assertIsNone(would_create_cycle(tasks, "a", "b"))

    def test_self_dependency_is_a_cycle(self):
        tasks = _graph(_task("a"))
        self.assertEqual(would_create_cycle(tasks, "a", "a"), ("a", "a"))

    def test_direct_cycle_is_detected(self):
        # b already depends on a; adding a -> b would close the loop.
        tasks = _graph(_task("a"), _task("b", depends_on=["a"]))
        cycle = would_create_cycle(tasks, "a", "b")
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle[0], "a")
        self.assertEqual(cycle[-1], "a")

    def test_multi_node_transitive_cycle_is_detected(self):
        # c -> b -> a (depends_on edges); adding a -> c would close a 3-cycle.
        tasks = _graph(
            _task("a"), _task("b", depends_on=["a"]), _task("c", depends_on=["b"])
        )
        cycle = would_create_cycle(tasks, "a", "c")
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle[0], "a")
        self.assertEqual(cycle[-1], "a")

    def test_diamond_shaped_graph_is_safe(self):
        # a depends on b and c; b and c both depend on d. No cycle.
        tasks = _graph(
            _task("d"),
            _task("b", depends_on=["d"]),
            _task("c", depends_on=["d"]),
            _task("a", depends_on=["b", "c"]),
        )
        self.assertIsNone(would_create_cycle(tasks, "a", "b"))
        self.assertIsNone(would_create_cycle(tasks, "a", "c"))

    def test_dangling_dependency_id_is_not_a_cycle(self):
        tasks = _graph(_task("a"))
        self.assertIsNone(would_create_cycle(tasks, "a", "does_not_exist"))


class FindCyclesTests(unittest.TestCase):
    def test_acyclic_graph_has_no_cycles(self):
        tasks = _graph(
            _task("d"), _task("b", depends_on=["d"]), _task("c", depends_on=["d"]),
            _task("a", depends_on=["b", "c"]),
        )
        self.assertEqual(find_cycles(tasks), [])

    def test_empty_graph_has_no_cycles(self):
        self.assertEqual(find_cycles({}), [])

    def test_finds_a_direct_two_node_cycle(self):
        tasks = _graph(_task("a", depends_on=["b"]), _task("b", depends_on=["a"]))
        cycles = find_cycles(tasks)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {"a", "b"})

    def test_finds_a_multi_node_cycle_among_unrelated_tasks(self):
        tasks = _graph(
            _task("x"),  # unrelated, acyclic
            _task("a", depends_on=["b"]),
            _task("b", depends_on=["c"]),
            _task("c", depends_on=["a"]),
        )
        cycles = find_cycles(tasks)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {"a", "b", "c"})

    def test_self_dependency_is_reported_as_a_cycle(self):
        tasks = _graph(_task("a", depends_on=["a"]))
        cycles = find_cycles(tasks)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {"a"})

    def test_rotations_of_the_same_cycle_are_not_duplicated(self):
        tasks = _graph(
            _task("a", depends_on=["b"]), _task("b", depends_on=["c"]), _task("c", depends_on=["a"]),
        )
        self.assertEqual(len(find_cycles(tasks)), 1)

    def test_running_and_succeeded_tasks_are_never_flagged(self):
        tasks = _graph(
            _task("a", depends_on=["b"], status=TaskStatus.RUNNING),
            _task("b", status=TaskStatus.SUCCEEDED),
        )
        self.assertEqual(find_cycles(tasks), [])


class DiagnoseBlockedTests(unittest.TestCase):
    def test_task_with_no_dependencies_has_empty_diagnosis(self):
        tasks = _graph(_task("a"))
        diagnosis = diagnose_blocked(tasks, "a")
        self.assertEqual(diagnosis.unmet_direct, ())
        self.assertEqual(diagnosis.unmet_chain, ())
        self.assertFalse(diagnosis.deadlocked)

    def test_direct_unmet_dependency_is_reported(self):
        tasks = _graph(_task("a", depends_on=["b"]), _task("b"))
        diagnosis = diagnose_blocked(tasks, "a")
        self.assertEqual(diagnosis.unmet_direct, ("b",))
        self.assertIn("b", diagnosis.unmet_chain)
        self.assertFalse(diagnosis.deadlocked)

    def test_already_satisfied_dependency_is_not_reported(self):
        tasks = _graph(_task("a", depends_on=["b"]), _task("b", status=TaskStatus.SUCCEEDED))
        diagnosis = diagnose_blocked(tasks, "a")
        self.assertEqual(diagnosis.unmet_direct, ())
        self.assertFalse(diagnosis.deadlocked)

    def test_transitive_chain_is_fully_reported(self):
        # a -> b -> c, none succeeded yet.
        tasks = _graph(_task("a", depends_on=["b"]), _task("b", depends_on=["c"]), _task("c"))
        diagnosis = diagnose_blocked(tasks, "a")
        self.assertEqual(set(diagnosis.unmet_chain), {"b", "c"})
        self.assertFalse(diagnosis.deadlocked)

    def test_partially_satisfied_transitive_chain_stops_at_succeeded_dependency(self):
        # a -> b -> c, c already SUCCEEDED, so only b is actually unmet.
        tasks = _graph(
            _task("a", depends_on=["b"]), _task("b", depends_on=["c"]), _task("c", status=TaskStatus.SUCCEEDED)
        )
        diagnosis = diagnose_blocked(tasks, "a")
        self.assertEqual(diagnosis.unmet_chain, ("b",))

    def test_deadlock_is_reported_when_chain_contains_a_cycle(self):
        # This graph could never be built through TaskManager.add_dependency
        # (it refuses to create cycles) — diagnose_blocked must still report
        # the truth about whatever graph it is actually given.
        tasks = _graph(_task("a", depends_on=["b"]), _task("b", depends_on=["a"]))
        diagnosis = diagnose_blocked(tasks, "a")
        self.assertTrue(diagnosis.deadlocked)
        self.assertEqual(set(diagnosis.cycle), {"a", "b"})

    def test_missing_dependency_id_is_reported_but_not_followed(self):
        tasks = _graph(_task("a", depends_on=["ghost"]))
        diagnosis = diagnose_blocked(tasks, "a")
        self.assertEqual(diagnosis.unmet_direct, ("ghost",))
        self.assertEqual(diagnosis.unmet_chain, ("ghost",))
        self.assertFalse(diagnosis.deadlocked)

    def test_unknown_task_id_returns_empty_diagnosis(self):
        diagnosis = diagnose_blocked({}, "does_not_exist")
        self.assertEqual(diagnosis.unmet_chain, ())
        self.assertFalse(diagnosis.deadlocked)


class TaskManagerIntegrationTests(unittest.TestCase):
    """Cycle prevention wired into the one API that actually creates
    `depends_on` edges — never a cycle existing only in a separate
    detector module nobody calls."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state = StateManager(Path(self._tmpdir.name) / "orchestrator.sqlite3")
        self.manager = TaskManager(self.state)

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def test_direct_cycle_is_rejected_and_not_persisted(self):
        a = self.manager.create_task("a", "proj")
        b = self.manager.create_task("b", "proj")
        self.manager.add_dependency(b.task_id, a.task_id)  # b depends on a: fine

        with self.assertRaises(DependencyCycleError):
            self.manager.add_dependency(a.task_id, b.task_id)  # would close a<->b

        # Rejected edge must never have been added.
        self.assertEqual(self.manager.get(a.task_id).depends_on, ())

    def test_self_dependency_is_rejected(self):
        a = self.manager.create_task("a", "proj")
        with self.assertRaises(DependencyCycleError):
            self.manager.add_dependency(a.task_id, a.task_id)

    def test_multi_node_transitive_cycle_is_rejected(self):
        a = self.manager.create_task("a", "proj")
        b = self.manager.create_task("b", "proj")
        c = self.manager.create_task("c", "proj")
        self.manager.add_dependency(b.task_id, a.task_id)  # b -> a
        self.manager.add_dependency(c.task_id, b.task_id)  # c -> b

        with self.assertRaises(DependencyCycleError):
            self.manager.add_dependency(a.task_id, c.task_id)  # would close a->c->b->a

        self.assertEqual(self.manager.detect_cycles(), [])

    def test_valid_diamond_dependency_graph_is_accepted(self):
        d = self.manager.create_task("d", "proj")
        b = self.manager.create_task("b", "proj")
        c = self.manager.create_task("c", "proj")
        a = self.manager.create_task("a", "proj")
        self.manager.add_dependency(b.task_id, d.task_id)
        self.manager.add_dependency(c.task_id, d.task_id)
        self.manager.add_dependency(a.task_id, b.task_id)
        self.manager.add_dependency(a.task_id, c.task_id)

        self.assertEqual(self.manager.detect_cycles(), [])
        self.assertEqual(set(self.manager.get(a.task_id).depends_on), {b.task_id, c.task_id})

    def test_detect_cycles_is_empty_for_a_graph_built_only_through_add_dependency(self):
        a = self.manager.create_task("a", "proj")
        b = self.manager.create_task("b", "proj")
        self.manager.add_dependency(b.task_id, a.task_id)
        self.assertEqual(self.manager.detect_cycles(), [])

    def test_diagnose_blocked_reflects_real_readiness_evaluation(self):
        a = self.manager.create_task("a", "proj")
        b = self.manager.create_task("b", "proj")
        self.manager.add_dependency(a.task_id, b.task_id)
        self.manager.attach_specification(
            a.task_id, TaskSpecification(objective="o", scope="s")
        )
        diagnosis = self.manager.diagnose_blocked(a.task_id)
        self.assertEqual(diagnosis.unmet_direct, (b.task_id,))
        self.assertFalse(diagnosis.deadlocked)
        self.assertIs(self.manager.get(a.task_id).status, TaskStatus.BLOCKED)


if __name__ == "__main__":
    unittest.main()
