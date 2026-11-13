import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.analysis.analyzer import TaskAnalyzer
from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.models import ProjectProfile, TaskRequest, TaskStatus
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.errors import NotFoundError


class _BrokenAnalyzer(TaskAnalyzer):
    def analyze(self, request, project_profile, constitution=None):
        raise RuntimeError("analyzer exploded")


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state = StateManager(Path(self._tmpdir.name) / "orchestrator.sqlite3")
        self.profile = ProjectProfile(name="demo", repository_path="/workspace/demo")

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def _request(self, title="Add health check"):
        return TaskRequest(
            title=title,
            description="Expose /health",
            metadata={"acceptance_criteria": ["returns 200"], "expected_deliverables": ["endpoint"]},
        )


class SubmitIntegrationTests(EngineTestCase):
    def test_submit_runs_analyzer_then_manager_then_persists_via_state(self):
        engine = OrchestrationEngine.bootstrap(self.state)

        task = engine.submit(self._request(), self.profile)

        self.assertIs(task.status, TaskStatus.READY)
        self.assertIsNotNone(task.specification)
        self.assertEqual(task.specification.objective, "Add health check")

        persisted = self.state.get(task.task_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, TaskStatus.READY)

    def test_submit_with_unmet_dependency_lands_blocked(self):
        engine = OrchestrationEngine.bootstrap(self.state)
        upstream = engine.submit(self._request("upstream"), self.profile)

        downstream = engine.submit(
            self._request("downstream"), self.profile, depends_on=(upstream.task_id,)
        )

        self.assertIs(downstream.status, TaskStatus.BLOCKED)

    def test_submit_records_missing_information_without_blocking_creation(self):
        engine = OrchestrationEngine.bootstrap(self.state)
        bare_request = TaskRequest(title="Underspecified task")

        task = engine.submit(bare_request, self.profile)

        self.assertIn(
            "acceptance_criteria: none specified", task.specification.missing_information
        )


class RecoveryOnBootstrapTests(EngineTestCase):
    def test_bootstrap_recovers_interrupted_tasks_from_a_prior_run(self):
        first_engine = OrchestrationEngine.bootstrap(self.state)
        task = first_engine.submit(self._request(), self.profile)
        first_engine.manager.start(task.task_id)

        second_engine = OrchestrationEngine.bootstrap(self.state)

        self.assertIs(second_engine.manager.get(task.task_id).status, TaskStatus.INTERRUPTED)


class FailurePropagationTests(EngineTestCase):
    def test_analyzer_failure_propagates_and_leaves_the_task_in_created(self):
        manager = OrchestrationEngine.bootstrap(self.state).manager
        engine = OrchestrationEngine(analyzer=_BrokenAnalyzer(), manager=manager)

        with self.assertRaises(RuntimeError):
            engine.submit(self._request(), self.profile)

        created_tasks = self.state.list(status=TaskStatus.CREATED)
        self.assertEqual(len(created_tasks), 1)
        self.assertEqual(created_tasks[0].title, "Add health check")

    def test_manager_failure_on_unknown_dependency_propagates(self):
        engine = OrchestrationEngine.bootstrap(self.state)
        with self.assertRaises(NotFoundError):
            engine.submit(self._request(), self.profile, depends_on=("task_missing",))


if __name__ == "__main__":
    unittest.main()
