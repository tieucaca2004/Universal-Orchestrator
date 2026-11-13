import unittest

from universal_orchestrator.errors import DuplicateError, NotFoundError
from universal_orchestrator.execution.adapter import ExecutionAdapter, ExecutionOutcome, ExecutionResult, VerificationStatus
from universal_orchestrator.execution.registry import AdapterRegistry


class _FakeAdapter(ExecutionAdapter):
    def verify(self):
        return VerificationStatus.AVAILABLE

    def execute(self, request):
        return ExecutionResult(
            execution_id=request.execution_id, task_id=request.task_id, outcome=ExecutionOutcome.COMPLETED
        )


class AdapterRegistryTests(unittest.TestCase):
    def test_register_then_get(self):
        registry = AdapterRegistry()
        adapter = _FakeAdapter()
        registry.register("prov_claude", adapter)
        self.assertIs(registry.get("prov_claude"), adapter)

    def test_duplicate_provider_id_raises(self):
        registry = AdapterRegistry()
        registry.register("prov_claude", _FakeAdapter())
        with self.assertRaises(DuplicateError):
            registry.register("prov_claude", _FakeAdapter())

    def test_unknown_provider_id_raises_not_found(self):
        registry = AdapterRegistry()
        with self.assertRaises(NotFoundError):
            registry.get("prov_missing")

    def test_list_provider_ids(self):
        registry = AdapterRegistry()
        registry.register("prov_claude", _FakeAdapter())
        registry.register("prov_deepseek_future", _FakeAdapter())
        self.assertEqual(set(registry.list_provider_ids()), {"prov_claude", "prov_deepseek_future"})


if __name__ == "__main__":
    unittest.main()
