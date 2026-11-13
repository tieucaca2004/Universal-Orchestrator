"""Two things about the components that still aren't implemented: the
explicit interfaces (planner, dispatcher, execution adapter) really are
abstract, and every remaining placeholder module at least imports
cleanly. `core.engine` and `core.state` graduated out of this list in
Milestone 2 — they have their own test modules now.
"""

import importlib
import unittest

from universal_orchestrator.execution.adapter import ExecutionAdapter
from universal_orchestrator.planning.dispatcher import Dispatcher
from universal_orchestrator.planning.planner import TaskPlanner

PLACEHOLDER_MODULES = (
    "universal_orchestrator.registry.projects",
    "universal_orchestrator.registry.agents",
    "universal_orchestrator.registry.providers",
    "universal_orchestrator.approval.gate",
)


class InterfaceAbstractnessTests(unittest.TestCase):
    def test_interfaces_cannot_be_instantiated_directly(self):
        for abstract_cls in (TaskPlanner, Dispatcher, ExecutionAdapter):
            with self.subTest(cls=abstract_cls.__name__):
                with self.assertRaises(TypeError):
                    abstract_cls()


class PlaceholderModuleImportTests(unittest.TestCase):
    def test_placeholder_modules_import_cleanly(self):
        for module_name in PLACEHOLDER_MODULES:
            with self.subTest(module=module_name):
                importlib.import_module(module_name)


if __name__ == "__main__":
    unittest.main()
