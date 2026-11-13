import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.analysis.analyzer import TaskAnalyzer
from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.models import (
    AgentDescriptor,
    AgentStatus,
    ProjectProfile,
    ProviderDescriptor,
    ProviderStatus,
    RoutingOutcome,
    TaskRequest,
    TaskStatus,
)
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.errors import NotFoundError, OrchestratorError
from universal_orchestrator.execution.adapter import (
    ExecutionAdapter,
    ExecutionOutcome,
    ExecutionResult,
    VerificationStatus,
)
from universal_orchestrator.execution.registry import AdapterRegistry
from universal_orchestrator.registry.agents import AgentRegistry
from universal_orchestrator.registry.projects import ProjectRegistry
from universal_orchestrator.registry.providers import ProviderRegistry
from universal_orchestrator.routing.router import AgentRouter


class _FakeExecutionAdapter(ExecutionAdapter):
    """Deterministic test double — never a real Claude CLI call."""

    def __init__(self, outcome: ExecutionOutcome = ExecutionOutcome.COMPLETED):
        self._outcome = outcome
        self.received_requests = []

    def verify(self):
        return VerificationStatus.AVAILABLE

    def execute(self, request):
        self.received_requests.append(request)
        return ExecutionResult(
            execution_id=request.execution_id,
            task_id=request.task_id,
            outcome=self._outcome,
            exit_code=0 if self._outcome is ExecutionOutcome.COMPLETED else 1,
        )


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


class RoutingIntegrationTests(EngineTestCase):
    """Project Registry -> Analyzer -> (Manager) -> Router -> Manager,
    exactly the Milestone 3 flow: a profile looked up from a registry
    feeds the Analyzer and, separately, the Router."""

    def setUp(self):
        super().setUp()
        self.projects = ProjectRegistry()
        self.agents = AgentRegistry()
        self.providers = ProviderRegistry()

    def _registered_profile(self, **overrides):
        return self.projects.register(ProjectProfile(**{**overrides, **{
            "name": overrides.get("name", "demo"),
            "repository_path": overrides.get("repository_path", "/workspace/demo"),
        }}))

    def _available_agent(self, **overrides):
        defaults = {"name": "coder-a", "role": "coder", "capabilities": ["python"]}
        defaults.update(overrides)
        agent = self.agents.register(AgentDescriptor(**defaults))
        self.agents.set_status(agent.agent_id, AgentStatus.AVAILABLE)
        return agent

    def _verified_provider(self, **overrides):
        defaults = {"name": "anthropic", "kind": "api"}
        defaults.update(overrides)
        provider = self.providers.register(ProviderDescriptor(**defaults))
        self.providers.set_verified(provider.provider_id, True)
        self.providers.set_status(provider.provider_id, ProviderStatus.AVAILABLE)
        return provider

    def test_profile_from_registry_feeds_the_analyzer(self):
        profile = self._registered_profile(name="demo")
        engine = OrchestrationEngine.bootstrap(self.state)

        task = engine.submit(self._request(), self.projects.get(profile.project_id))

        self.assertEqual(task.project_id, profile.project_id)
        self.assertIsNotNone(task.specification)

    def test_submit_then_route_selects_an_eligible_agent_and_persists_the_decision(self):
        profile = self._registered_profile()
        provider = self._verified_provider()
        agent = self._available_agent(provider_id=provider.provider_id)
        router = AgentRouter(self.agents, self.providers)
        engine = OrchestrationEngine.bootstrap(self.state, router=router)

        task = engine.submit(self._request(), profile)
        decision = engine.route(task.task_id, profile)

        self.assertIs(decision.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(decision.selected_agent_id, agent.agent_id)

        persisted = engine.manager.get_routing_decisions(task.task_id)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].selected_agent_id, agent.agent_id)

    def test_routing_does_not_change_task_status(self):
        profile = self._registered_profile()
        provider = self._verified_provider()
        self._available_agent(provider_id=provider.provider_id)
        router = AgentRouter(self.agents, self.providers)
        engine = OrchestrationEngine.bootstrap(self.state, router=router)

        task = engine.submit(self._request(), profile)
        status_before = task.status
        engine.route(task.task_id, profile)

        self.assertIs(engine.manager.get(task.task_id).status, status_before)

    def test_no_eligible_agent_still_persists_an_explainable_decision(self):
        profile = self._registered_profile()
        router = AgentRouter(self.agents, self.providers)
        engine = OrchestrationEngine.bootstrap(self.state, router=router)

        task = engine.submit(self._request(), profile)
        decision = engine.route(task.task_id, profile)

        self.assertIs(decision.outcome, RoutingOutcome.NO_ELIGIBLE_AGENT)
        self.assertEqual(engine.manager.get_routing_decisions(task.task_id)[0].outcome, decision.outcome)

    def test_routing_without_a_configured_router_raises(self):
        profile = self._registered_profile()
        engine = OrchestrationEngine.bootstrap(self.state)  # no router
        task = engine.submit(self._request(), profile)

        with self.assertRaises(RuntimeError):
            engine.route(task.task_id, profile)

    def test_routing_an_unknown_task_propagates_not_found(self):
        profile = self._registered_profile()
        router = AgentRouter(self.agents, self.providers)
        engine = OrchestrationEngine.bootstrap(self.state, router=router)

        with self.assertRaises(NotFoundError):
            engine.route("task_missing", profile)


class ExecutionIntegrationTests(EngineTestCase):
    """Task Manager -> Agent Router's decision -> Execution Adapter ->
    Task Manager, exactly the Milestone 4 flow, using a fake adapter —
    no real Claude CLI call belongs in this test module."""

    def setUp(self):
        super().setUp()
        self.projects = ProjectRegistry()
        self.agents = AgentRegistry()
        self.providers = ProviderRegistry()
        self._sandbox = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._sandbox.cleanup()
        super().tearDown()

    def _registered_profile(self, **overrides):
        defaults = {"name": "demo", "repository_path": "/workspace/demo"}
        defaults.update(overrides)
        return self.projects.register(ProjectProfile(**defaults))

    def _available_agent(self, **overrides):
        defaults = {"name": "coder-a", "role": "coder", "capabilities": ["python"]}
        defaults.update(overrides)
        agent = self.agents.register(AgentDescriptor(**defaults))
        self.agents.set_status(agent.agent_id, AgentStatus.AVAILABLE)
        return agent

    def _verified_provider(self, **overrides):
        defaults = {"name": "anthropic", "kind": "api"}
        defaults.update(overrides)
        provider = self.providers.register(ProviderDescriptor(**defaults))
        self.providers.set_verified(provider.provider_id, True)
        self.providers.set_status(provider.provider_id, ProviderStatus.AVAILABLE)
        return provider

    def _wired_engine(self, fake_adapter: _FakeExecutionAdapter):
        router = AgentRouter(self.agents, self.providers)
        adapters = AdapterRegistry()
        provider = self._verified_provider()
        self._available_agent(provider_id=provider.provider_id)
        adapters.register(provider.provider_id, fake_adapter)
        engine = OrchestrationEngine.bootstrap(
            self.state, router=router, adapters=adapters, agents=self.agents
        )
        return engine, provider

    def test_route_then_execute_persists_a_completed_result(self):
        fake_adapter = _FakeExecutionAdapter(ExecutionOutcome.COMPLETED)
        engine, _provider = self._wired_engine(fake_adapter)
        profile = self._registered_profile()

        task = engine.submit(self._request(), profile)
        engine.route(task.task_id, profile)
        result = engine.execute(task.task_id, working_directory=self._sandbox.name)

        self.assertIs(result.outcome, ExecutionOutcome.COMPLETED)
        persisted = engine.manager.get_execution_results(task.task_id)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].execution_id, result.execution_id)

    def test_execute_without_routing_first_raises(self):
        fake_adapter = _FakeExecutionAdapter()
        engine, _provider = self._wired_engine(fake_adapter)
        profile = self._registered_profile()
        task = engine.submit(self._request(), profile)

        with self.assertRaises(OrchestratorError):
            engine.execute(task.task_id, working_directory=self._sandbox.name)

    def test_execute_after_no_eligible_agent_raises(self):
        fake_adapter = _FakeExecutionAdapter()
        router = AgentRouter(self.agents, self.providers)  # no agents registered
        adapters = AdapterRegistry()
        engine = OrchestrationEngine.bootstrap(self.state, router=router, adapters=adapters)
        profile = self._registered_profile()
        task = engine.submit(self._request(), profile)
        engine.route(task.task_id, profile)

        with self.assertRaises(OrchestratorError):
            engine.execute(task.task_id, working_directory=self._sandbox.name)

    def test_execute_without_adapter_registry_raises(self):
        router = AgentRouter(self.agents, self.providers)
        engine = OrchestrationEngine.bootstrap(self.state, router=router)  # no adapters
        profile = self._registered_profile()
        task = engine.submit(self._request(), profile)

        with self.assertRaises(RuntimeError):
            engine.execute(task.task_id, working_directory=self._sandbox.name)

    def test_execute_passes_the_agent_role_and_correlation_id_through(self):
        fake_adapter = _FakeExecutionAdapter()
        engine, provider = self._wired_engine(fake_adapter)
        profile = self._registered_profile()
        task = engine.submit(self._request(), profile)
        decision = engine.route(task.task_id, profile)

        engine.execute(task.task_id, working_directory=self._sandbox.name)

        sent = fake_adapter.received_requests[0]
        self.assertEqual(sent.agent_role, "coder")
        self.assertEqual(sent.correlation_id, decision.decision_id)
        self.assertEqual(sent.provider_id, provider.provider_id)

    def test_adapter_error_propagates_but_still_records_nothing_as_task_status(self):
        fake_adapter = _FakeExecutionAdapter(ExecutionOutcome.FAILED)
        engine, _provider = self._wired_engine(fake_adapter)
        profile = self._registered_profile()
        task = engine.submit(self._request(), profile)
        engine.route(task.task_id, profile)

        status_before = engine.manager.get(task.task_id).status
        result = engine.execute(task.task_id, working_directory=self._sandbox.name)

        self.assertIs(result.outcome, ExecutionOutcome.FAILED)
        self.assertIs(engine.manager.get(task.task_id).status, status_before)


if __name__ == "__main__":
    unittest.main()
