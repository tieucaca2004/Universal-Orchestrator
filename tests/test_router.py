import unittest

from universal_orchestrator.core.models import (
    AgentDescriptor,
    AgentStatus,
    CostConfidence,
    CostInfo,
    ProjectProfile,
    ProviderDescriptor,
    ProviderStatus,
    RoutingOutcome,
    Task,
    TaskSpecification,
)
from universal_orchestrator.registry.agents import AgentRegistry
from universal_orchestrator.registry.providers import ProviderRegistry
from universal_orchestrator.routing.router import AgentRouter


def _profile(**overrides):
    defaults = {"name": "demo", "repository_path": "/workspace/demo"}
    defaults.update(overrides)
    return ProjectProfile(**defaults)


def _task(**overrides):
    defaults = {
        "title": "t",
        "project_id": "proj_demo",
        "specification": TaskSpecification(objective="o", scope="s"),
        "payload": {},
    }
    defaults.update(overrides)
    return Task(**defaults)


class RouterTestCase(unittest.TestCase):
    def setUp(self):
        self.agents = AgentRegistry()
        self.providers = ProviderRegistry()
        self.router = AgentRouter(self.agents, self.providers)
        self.profile = _profile()

    def _make_available_agent(self, **overrides) -> AgentDescriptor:
        defaults = {"name": "a", "role": "coder", "capabilities": ["python"]}
        defaults.update(overrides)
        agent = self.agents.register(AgentDescriptor(**defaults))
        self.agents.set_status(agent.agent_id, AgentStatus.AVAILABLE)
        return agent

    def _make_verified_provider(self, **overrides) -> ProviderDescriptor:
        defaults = {"name": "anthropic", "kind": "api"}
        defaults.update(overrides)
        provider = self.providers.register(ProviderDescriptor(**defaults))
        self.providers.set_verified(provider.provider_id, True)
        self.providers.set_status(provider.provider_id, ProviderStatus.AVAILABLE)
        return provider


class RoleMatchingTests(RouterTestCase):
    def test_required_role_filters_out_mismatched_agents(self):
        self._make_available_agent(name="planner-agent", role="planner")
        coder = self._make_available_agent(name="coder-agent", role="coder")

        decision = self.router.route(
            _task(payload={"required_role": "coder"}), self.profile
        )

        self.assertIs(decision.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(decision.selected_agent_id, coder.agent_id)


class CapabilityMatchingTests(RouterTestCase):
    def test_agent_missing_required_capability_is_rejected(self):
        self._make_available_agent(capabilities=["python"])

        decision = self.router.route(
            _task(specification=TaskSpecification(objective="o", scope="s", required_capabilities=["rust"])),
            self.profile,
        )

        self.assertIs(decision.outcome, RoutingOutcome.NO_ELIGIBLE_AGENT)
        self.assertEqual(len(decision.rejected), 1)
        self.assertTrue(any("missing capabilities" in r for r in decision.rejected[0].reasons))

    def test_agent_with_required_capability_is_selected(self):
        agent = self._make_available_agent(capabilities=["python", "git"])

        decision = self.router.route(
            _task(specification=TaskSpecification(objective="o", scope="s", required_capabilities=["python"])),
            self.profile,
        )

        self.assertIs(decision.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(decision.selected_agent_id, agent.agent_id)
        self.assertEqual(decision.matched_capabilities, ["python"])


class PermissionRestrictionTests(RouterTestCase):
    def test_agent_missing_required_permission_is_rejected(self):
        self._make_available_agent(permissions=["read"])

        decision = self.router.route(
            _task(payload={"required_permissions": ["deploy"]}), self.profile
        )

        self.assertIs(decision.outcome, RoutingOutcome.NO_ELIGIBLE_AGENT)
        self.assertTrue(any("missing permissions" in r for r in decision.rejected[0].reasons))

    def test_project_restricted_operation_rejects_even_a_permitted_agent(self):
        self._make_available_agent(permissions=["force_push"])
        restrictive_profile = _profile(restricted_operations=["force_push"])

        decision = self.router.route(
            _task(payload={"required_permissions": ["force_push"]}), restrictive_profile
        )

        self.assertIs(decision.outcome, RoutingOutcome.NO_ELIGIBLE_AGENT)
        self.assertTrue(
            any("restricted by project" in r for r in decision.rejected[0].reasons)
        )


class ProviderCompatibilityTests(RouterTestCase):
    def test_unregistered_provider_id_is_rejected(self):
        self._make_available_agent(provider_id="prov_missing")

        decision = self.router.route(
            _task(specification=TaskSpecification(objective="o", scope="s", required_capabilities=["python"])),
            self.profile,
        )

        self.assertIs(decision.outcome, RoutingOutcome.NO_ELIGIBLE_AGENT)
        self.assertTrue(any("not registered" in r for r in decision.rejected[0].reasons))

    def test_unverified_provider_is_rejected(self):
        provider = self.providers.register(ProviderDescriptor(name="anthropic", kind="api"))
        self._make_available_agent(provider_id=provider.provider_id)

        decision = self.router.route(
            _task(specification=TaskSpecification(objective="o", scope="s", required_capabilities=["python"])),
            self.profile,
        )

        self.assertIs(decision.outcome, RoutingOutcome.NO_ELIGIBLE_AGENT)
        self.assertTrue(any("not verified" in r for r in decision.rejected[0].reasons))

    def test_verified_and_available_provider_allows_selection(self):
        provider = self._make_verified_provider()
        agent = self._make_available_agent(provider_id=provider.provider_id)

        decision = self.router.route(_task(), self.profile)

        self.assertIs(decision.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(decision.selected_provider_id, provider.provider_id)


class OfflineDisabledUnverifiedAgentRejectionTests(RouterTestCase):
    def test_each_non_available_agent_status_is_rejected(self):
        for status in (
            AgentStatus.REGISTERED,
            AgentStatus.BUSY,
            AgentStatus.OFFLINE,
            AgentStatus.UNVERIFIED,
            AgentStatus.DISABLED,
            AgentStatus.ERROR,
        ):
            with self.subTest(status=status):
                agent = self.agents.register(
                    AgentDescriptor(name=f"a-{status.value}", role="coder")
                )
                self.agents.set_status(agent.agent_id, status)

                decision = self.router.route(_task(), self.profile)

                self.assertIs(decision.outcome, RoutingOutcome.NO_ELIGIBLE_AGENT)
                self.assertTrue(
                    any("not AVAILABLE" in r for r in decision.rejected[-1].reasons)
                )


class ResourceConstraintTests(RouterTestCase):
    def test_required_machine_mismatch_is_rejected(self):
        self._make_available_agent(machine="NUC")

        decision = self.router.route(
            _task(payload={"required_machine": "RTX3070"}), self.profile
        )

        self.assertIs(decision.outcome, RoutingOutcome.NO_ELIGIBLE_AGENT)
        self.assertTrue(any("does not match required machine" in r for r in decision.rejected[0].reasons))

    def test_required_machine_match_allows_selection(self):
        agent = self._make_available_agent(machine="NUC")

        decision = self.router.route(
            _task(payload={"required_machine": "NUC"}), self.profile
        )

        self.assertIs(decision.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(decision.selected_agent_id, agent.agent_id)


class CostConstraintTests(RouterTestCase):
    def test_verified_cost_over_budget_is_rejected(self):
        provider = self.providers.register(
            ProviderDescriptor(
                name="anthropic",
                kind="api",
                cost=CostInfo(confidence=CostConfidence.VERIFIED, input_per_1k_tokens=10.0),
            )
        )
        self.providers.set_verified(provider.provider_id, True)
        self.providers.set_status(provider.provider_id, ProviderStatus.AVAILABLE)
        self._make_available_agent(provider_id=provider.provider_id)

        decision = self.router.route(
            _task(payload={"max_cost_per_1k_tokens": 5.0}), self.profile
        )

        self.assertIs(decision.outcome, RoutingOutcome.NO_ELIGIBLE_AGENT)
        self.assertTrue(any("exceeds constraint" in r for r in decision.rejected[0].reasons))

    def test_estimated_cost_never_blocks_routing(self):
        provider = self.providers.register(
            ProviderDescriptor(
                name="anthropic",
                kind="api",
                cost=CostInfo(confidence=CostConfidence.ESTIMATED, input_per_1k_tokens=999.0),
            )
        )
        self.providers.set_verified(provider.provider_id, True)
        self.providers.set_status(provider.provider_id, ProviderStatus.AVAILABLE)
        agent = self._make_available_agent(provider_id=provider.provider_id)

        decision = self.router.route(
            _task(payload={"max_cost_per_1k_tokens": 5.0}), self.profile
        )

        self.assertIs(decision.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(decision.selected_agent_id, agent.agent_id)


class MultipleCandidateRankingTests(RouterTestCase):
    def test_higher_capability_match_score_wins(self):
        self._make_available_agent(name="partial", capabilities=["python"])
        full = self._make_available_agent(name="full", capabilities=["python", "sqlite"])

        decision = self.router.route(
            _task(
                specification=TaskSpecification(
                    objective="o", scope="s", required_capabilities=["python", "sqlite"]
                )
            ),
            self.profile,
        )

        self.assertEqual(decision.selected_agent_id, full.agent_id)
        self.assertEqual(decision.score, 1.0)

    def test_tie_breaks_deterministically_by_agent_id(self):
        a1 = self._make_available_agent(name="x")
        a2 = self._make_available_agent(name="y")

        decision1 = self.router.route(_task(), self.profile)
        decision2 = self.router.route(_task(), self.profile)

        self.assertEqual(decision1.selected_agent_id, decision2.selected_agent_id)
        self.assertEqual(decision1.selected_agent_id, min(a1.agent_id, a2.agent_id))


class NoEligibleAgentTests(RouterTestCase):
    def test_empty_registry_returns_no_eligible_agent(self):
        decision = self.router.route(_task(), self.profile)
        self.assertIs(decision.outcome, RoutingOutcome.NO_ELIGIBLE_AGENT)
        self.assertEqual(decision.selected_agent_id, None)
        self.assertEqual(decision.rejected, [])


class ExplainableDecisionTests(RouterTestCase):
    def test_selected_decision_has_all_required_fields(self):
        provider = self._make_verified_provider()
        agent = self._make_available_agent(provider_id=provider.provider_id)

        decision = self.router.route(_task(task_id="task_x"), self.profile)

        self.assertEqual(decision.task_id, "task_x")
        self.assertEqual(decision.project_id, "proj_demo")
        self.assertEqual(decision.selected_agent_id, agent.agent_id)
        self.assertEqual(decision.selected_provider_id, provider.provider_id)
        self.assertTrue(decision.explanation)
        self.assertIsNotNone(decision.decided_at)
        self.assertIsNotNone(decision.decision_id)

    def test_rejected_decision_lists_reasons_per_candidate(self):
        self._make_available_agent(name="a")
        bad_agent = self.agents.register(AgentDescriptor(name="b", role="coder"))
        self.agents.set_status(bad_agent.agent_id, AgentStatus.OFFLINE)

        decision = self.router.route(
            _task(
                specification=TaskSpecification(
                    objective="o", scope="s", required_capabilities=["nonexistent"]
                )
            ),
            self.profile,
        )

        self.assertEqual(len(decision.rejected), 2)
        for rejected in decision.rejected:
            self.assertTrue(rejected.reasons)


class DeterministicRoutingTests(RouterTestCase):
    def test_routing_the_same_task_twice_yields_the_same_decision(self):
        provider = self._make_verified_provider()
        self._make_available_agent(provider_id=provider.provider_id)

        d1 = self.router.route(_task(), self.profile)
        d2 = self.router.route(_task(), self.profile)

        self.assertEqual(d1.selected_agent_id, d2.selected_agent_id)
        self.assertEqual(d1.score, d2.score)


if __name__ == "__main__":
    unittest.main()
