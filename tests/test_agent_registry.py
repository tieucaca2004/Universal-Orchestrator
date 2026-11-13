import unittest

from universal_orchestrator.core.models import AgentDescriptor, AgentStatus
from universal_orchestrator.errors import DuplicateError, NotFoundError
from universal_orchestrator.registry.agents import AgentRegistry


def _agent(**overrides):
    defaults = {"name": "claude-planner", "role": "planner"}
    defaults.update(overrides)
    return AgentDescriptor(**defaults)


class RegistrationTests(unittest.TestCase):
    def test_register_then_get(self):
        registry = AgentRegistry()
        agent = registry.register(_agent())
        self.assertIs(registry.get(agent.agent_id), agent)

    def test_duplicate_agent_id_raises(self):
        registry = AgentRegistry()
        agent = _agent()
        registry.register(agent)
        with self.assertRaises(DuplicateError):
            registry.register(agent)

    def test_rejects_empty_role(self):
        registry = AgentRegistry()
        with self.assertRaises(ValueError):
            registry.register(_agent(role=""))

    def test_registering_forces_status_to_registered_regardless_of_input(self):
        registry = AgentRegistry()
        agent = registry.register(_agent(status=AgentStatus.AVAILABLE))
        self.assertIs(agent.status, AgentStatus.REGISTERED)


class RoleValidationTests(unittest.TestCase):
    def test_list_filters_by_role(self):
        registry = AgentRegistry()
        planner = registry.register(_agent(role="planner"))
        registry.register(_agent(role="coder"))

        self.assertEqual([a.agent_id for a in registry.list(role="planner")], [planner.agent_id])


class CapabilityMatchingTests(unittest.TestCase):
    def test_is_eligible_requires_capability_subset(self):
        registry = AgentRegistry()
        agent = registry.register(_agent(capabilities=["python", "git"]))
        registry.set_status(agent.agent_id, AgentStatus.AVAILABLE)

        self.assertTrue(
            registry.is_eligible(agent.agent_id, required_capabilities=frozenset({"python"}))
        )
        self.assertFalse(
            registry.is_eligible(agent.agent_id, required_capabilities=frozenset({"rust"}))
        )


class PermissionValidationTests(unittest.TestCase):
    def test_is_eligible_requires_permission_subset(self):
        registry = AgentRegistry()
        agent = registry.register(_agent(permissions=["read", "write"]))
        registry.set_status(agent.agent_id, AgentStatus.AVAILABLE)

        self.assertTrue(
            registry.is_eligible(agent.agent_id, required_permissions=frozenset({"read"}))
        )
        self.assertFalse(
            registry.is_eligible(agent.agent_id, required_permissions=frozenset({"deploy"}))
        )


class StatusManagementTests(unittest.TestCase):
    def test_set_status_updates_the_registered_agent(self):
        registry = AgentRegistry()
        agent = registry.register(_agent())
        registry.set_status(agent.agent_id, AgentStatus.BUSY)
        self.assertIs(registry.get(agent.agent_id).status, AgentStatus.BUSY)

    def test_set_status_on_unknown_agent_raises_not_found(self):
        registry = AgentRegistry()
        with self.assertRaises(NotFoundError):
            registry.set_status("agent_missing", AgentStatus.AVAILABLE)


class UnverifiedAgentRejectionTests(unittest.TestCase):
    def test_newly_registered_agent_is_not_eligible(self):
        registry = AgentRegistry()
        agent = registry.register(_agent(capabilities=["python"]))
        self.assertFalse(registry.is_eligible(agent.agent_id))

    def test_each_non_available_status_is_rejected(self):
        registry = AgentRegistry()
        for status in (
            AgentStatus.BUSY,
            AgentStatus.OFFLINE,
            AgentStatus.UNVERIFIED,
            AgentStatus.DISABLED,
            AgentStatus.ERROR,
        ):
            with self.subTest(status=status):
                agent = registry.register(_agent(name=f"a-{status.value}"))
                registry.set_status(agent.agent_id, status)
                self.assertFalse(registry.is_eligible(agent.agent_id))


if __name__ == "__main__":
    unittest.main()
