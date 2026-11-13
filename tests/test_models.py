import unittest

from universal_orchestrator.core.models import (
    TERMINAL_TASK_STATUSES,
    AgentDescriptor,
    ProjectProfile,
    ProviderDescriptor,
    Task,
    TaskStatus,
)


class ProjectProfileTests(unittest.TestCase):
    def test_gets_generated_id(self):
        p1 = ProjectProfile(name="demo-a", repository_path="/workspace/demo-a")
        p2 = ProjectProfile(name="demo-b", repository_path="/workspace/demo-b")
        self.assertTrue(p1.project_id.startswith("proj_"))
        self.assertNotEqual(p1.project_id, p2.project_id)


class AgentDescriptorTests(unittest.TestCase):
    def test_defaults(self):
        agent = AgentDescriptor(name="psh-coder", role="coder")
        self.assertTrue(agent.agent_id.startswith("agent_"))
        self.assertIsNone(agent.provider_id)
        self.assertEqual(agent.metadata, {})


class ProviderDescriptorTests(unittest.TestCase):
    def test_defaults_unverified(self):
        provider = ProviderDescriptor(name="anthropic", kind="api")
        self.assertFalse(provider.verified)
        self.assertTrue(provider.provider_id.startswith("prov_"))


class TaskTests(unittest.TestCase):
    def test_records_initial_history_entry(self):
        task = Task(title="scaffold core", project_id="proj_abc123")
        self.assertIs(task.status, TaskStatus.CREATED)
        self.assertEqual(len(task.history), 1)
        status, _timestamp = task.history[0]
        self.assertEqual(status, TaskStatus.CREATED.value)

    def test_terminal_statuses_exclude_active_ones(self):
        self.assertIn(TaskStatus.SUCCEEDED, TERMINAL_TASK_STATUSES)
        self.assertIn(TaskStatus.FAILED, TERMINAL_TASK_STATUSES)
        self.assertIn(TaskStatus.CANCELLED, TERMINAL_TASK_STATUSES)
        self.assertNotIn(TaskStatus.CREATED, TERMINAL_TASK_STATUSES)
        self.assertNotIn(TaskStatus.RUNNING, TERMINAL_TASK_STATUSES)
        self.assertNotIn(TaskStatus.BLOCKED, TERMINAL_TASK_STATUSES)
        self.assertNotIn(TaskStatus.INTERRUPTED, TERMINAL_TASK_STATUSES)


if __name__ == "__main__":
    unittest.main()
