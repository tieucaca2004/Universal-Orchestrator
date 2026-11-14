"""Two things about the components that still aren't implemented: the
explicit interfaces (planner, dispatcher, execution adapter) really are
abstract, and every remaining placeholder module at least imports
cleanly. `core.engine` and `core.state` graduated out of this list in
Milestone 2 — they have their own test modules now.

Audit Finding C-11/F (2026-11-14): `registry.projects`/`.agents`/
`.providers` were removed from `PLACEHOLDER_MODULES` below — all
three have been fully-implemented, heavily-used, separately-tested
modules since Milestone 3 (see test_project_registry.py,
test_agent_registry.py, test_provider_registry.py); listing them here
as "placeholders" was stale documentation debt, not a functional
issue (they still "imported cleanly" either way), but it misdescribed
what they are. `planning.dispatcher`/`.planner` and `approval.gate`
remain here because they genuinely still are unimplemented
placeholders — see their own module docstrings for the explicit
"superseded, never wired to any real path" note added by this same
audit pass.
"""

import importlib
import unittest

from universal_orchestrator.execution.adapter import ExecutionAdapter
from universal_orchestrator.planning.dispatcher import Dispatcher
from universal_orchestrator.planning.planner import TaskPlanner

PLACEHOLDER_MODULES = (
    "universal_orchestrator.planning.dispatcher",
    "universal_orchestrator.planning.planner",
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
