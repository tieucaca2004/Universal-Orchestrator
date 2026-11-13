import unittest

from universal_orchestrator.errors import (
    ApprovalRequiredError,
    DuplicateError,
    InvalidStateTransitionError,
    NotFoundError,
    OrchestratorError,
    ProviderNotVerifiedError,
)


class ApprovalRequiredErrorTests(unittest.TestCase):
    def test_message_includes_action_and_reason(self):
        err = ApprovalRequiredError("deploy-production", "destructive operation")
        self.assertIn("deploy-production", str(err))
        self.assertIn("destructive operation", str(err))
        self.assertIsInstance(err, OrchestratorError)

    def test_default_reason(self):
        err = ApprovalRequiredError("stop-container")
        self.assertIn("no reason given", str(err))


class ProviderNotVerifiedErrorTests(unittest.TestCase):
    def test_carries_provider_id(self):
        err = ProviderNotVerifiedError("prov_deepseek")
        self.assertEqual(err.provider_id, "prov_deepseek")
        self.assertIn("prov_deepseek", str(err))


class NotFoundErrorTests(unittest.TestCase):
    def test_message(self):
        err = NotFoundError("Task", "task_xyz")
        self.assertIn("Task", str(err))
        self.assertIn("task_xyz", str(err))


class DuplicateErrorTests(unittest.TestCase):
    def test_message(self):
        err = DuplicateError("Project", "proj_psh")
        self.assertIn("already registered", str(err))


class InvalidStateTransitionErrorTests(unittest.TestCase):
    def test_message(self):
        err = InvalidStateTransitionError("Task", "succeeded", "running")
        self.assertIn("succeeded", str(err))
        self.assertIn("running", str(err))


class ErrorHierarchyTests(unittest.TestCase):
    def test_all_errors_are_orchestrator_errors(self):
        errors = [
            ApprovalRequiredError("a"),
            ProviderNotVerifiedError("p"),
            NotFoundError("k", "i"),
            DuplicateError("k", "i"),
            InvalidStateTransitionError("e", "a", "b"),
        ]
        for err in errors:
            with self.subTest(err=type(err).__name__):
                self.assertIsInstance(err, OrchestratorError)


if __name__ == "__main__":
    unittest.main()
