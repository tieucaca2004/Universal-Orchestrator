"""Milestone 6.4 — Quality Gate.

Task-level and integration-level are evaluated independently
throughout — no test here lets one stand in for the other, matching
the explicit rule that two individually-passing tasks are never
assumed to imply an integrated PASS.
"""

import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.core.models import TaskSpecification
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.execution.adapter import ExecutionOutcome, ExecutionResult
from universal_orchestrator.execution.quality import QualityVerdict
from universal_orchestrator.execution.quality_manager import QualityGate


def _spec(**overrides) -> TaskSpecification:
    defaults = {"objective": "o", "scope": "s"}
    defaults.update(overrides)
    return TaskSpecification(**defaults)


def _result(task_id: str = "task_a", outcome: ExecutionOutcome = ExecutionOutcome.COMPLETED) -> ExecutionResult:
    return ExecutionResult(execution_id="exec_1", task_id=task_id, outcome=outcome)


class QualityGateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.state = StateManager(self.tmp_path / "orchestrator.sqlite3")
        self.gate = QualityGate(self.state)

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()


class TaskLevelTests(QualityGateTestCase):
    def test_outcome_not_completed_is_fail_regardless_of_criteria(self):
        spec = _spec(acceptance_criteria=["x happens"])
        result = _result(outcome=ExecutionOutcome.FAILED)

        report = self.gate.evaluate_task(spec, result, criteria_evidence={"x happens": True})

        self.assertIs(report.verdict, QualityVerdict.FAIL)

    def test_exit_code_0_alone_is_insufficient_no_criteria_declared(self):
        # ExecutionOutcome.COMPLETED is the "exit code 0" case. With no
        # acceptance criteria to check, this must never default to PASS.
        spec = _spec(acceptance_criteria=[])
        result = _result()

        report = self.gate.evaluate_task(spec, result)

        self.assertIs(report.verdict, QualityVerdict.NOT_VERIFIED)

    def test_exit_code_0_alone_is_insufficient_no_evidence_supplied(self):
        spec = _spec(acceptance_criteria=["creates file X"])
        result = _result()

        report = self.gate.evaluate_task(spec, result)  # no criteria_evidence at all

        self.assertIs(report.verdict, QualityVerdict.NOT_VERIFIED)
        self.assertIn("creates file X", report.rationale)

    def test_partial_evidence_is_not_verified_not_a_partial_pass(self):
        spec = _spec(acceptance_criteria=["a", "b"])
        result = _result()

        report = self.gate.evaluate_task(spec, result, criteria_evidence={"a": True})

        self.assertIs(report.verdict, QualityVerdict.NOT_VERIFIED)

    def test_all_evidence_confirmed_is_pass(self):
        spec = _spec(acceptance_criteria=["a", "b"])
        result = _result()

        report = self.gate.evaluate_task(spec, result, criteria_evidence={"a": True, "b": True})

        self.assertIs(report.verdict, QualityVerdict.PASS)

    def test_any_evidence_contradicted_is_fail(self):
        spec = _spec(acceptance_criteria=["a", "b"])
        result = _result()

        report = self.gate.evaluate_task(spec, result, criteria_evidence={"a": True, "b": False})

        self.assertIs(report.verdict, QualityVerdict.FAIL)

    def test_report_is_persisted_and_queryable(self):
        spec = _spec(acceptance_criteria=["a"])
        result = _result()
        self.gate.evaluate_task(spec, result, criteria_evidence={"a": True})

        latest = self.gate.latest("task_a", level="task")

        self.assertIsNotNone(latest)
        self.assertIs(latest.verdict, QualityVerdict.PASS)


class IntegrationLevelTests(QualityGateTestCase):
    def test_no_required_commands_is_not_verified(self):
        report = self.gate.evaluate_integration("task_a", str(self.tmp_path), [])
        self.assertIs(report.verdict, QualityVerdict.NOT_VERIFIED)

    def test_all_commands_pass_is_pass_with_real_exit_codes_captured(self):
        report = self.gate.evaluate_integration(
            "task_a", str(self.tmp_path), ["python3 -c \"import sys; sys.exit(0)\""]
        )
        self.assertIs(report.verdict, QualityVerdict.PASS)
        self.assertEqual(report.command_results[0]["exit_code"], 0)

    def test_one_command_fails_despite_the_others_passing(self):
        report = self.gate.evaluate_integration(
            "task_a",
            str(self.tmp_path),
            [
                "python3 -c \"import sys; sys.exit(0)\"",
                "python3 -c \"import sys; sys.exit(1)\"",
            ],
        )
        self.assertIs(report.verdict, QualityVerdict.FAIL)
        exit_codes = [r["exit_code"] for r in report.command_results]
        self.assertEqual(exit_codes, [0, 1])

    def test_two_individually_passing_tasks_can_still_fail_after_integration(self):
        # Set up a real scenario: task_a and task_b each independently
        # pass their own task-level Quality Gate. Neither fact is read
        # by evaluate_integration — only the project's own declared
        # command, run for real against the combined repository state,
        # decides the integration-level verdict.
        spec = _spec(acceptance_criteria=["a"])
        self.gate.evaluate_task(spec, _result("task_a"), criteria_evidence={"a": True})
        self.gate.evaluate_task(spec, _result("task_b"), criteria_evidence={"a": True})
        self.assertIs(self.gate.latest("task_a", level="task").verdict, QualityVerdict.PASS)
        self.assertIs(self.gate.latest("task_b", level="task").verdict, QualityVerdict.PASS)

        # The combined state breaks something only visible once both
        # changes are present together.
        (self.tmp_path / "from_task_a.marker").write_text("a")
        (self.tmp_path / "from_task_b.marker").write_text("b")
        check_script = self.tmp_path / "check_combined.py"
        check_script.write_text(
            "import sys, pathlib\n"
            "p = pathlib.Path(__file__).parent\n"
            "if (p / 'from_task_a.marker').exists() and (p / 'from_task_b.marker').exists():\n"
            "    sys.exit(1)  # the combination is what breaks\n"
            "sys.exit(0)\n"
        )

        integration_report = self.gate.evaluate_integration(
            "task_a", str(self.tmp_path), [f"python3 {check_script}"]
        )

        self.assertIs(integration_report.verdict, QualityVerdict.FAIL)

    def test_integration_report_is_independent_of_task_level_reports(self):
        spec = _spec(acceptance_criteria=["a"])
        self.gate.evaluate_task(spec, _result("task_a"), criteria_evidence={"a": True})

        integration_report = self.gate.evaluate_integration(
            "task_a", str(self.tmp_path), ["python3 -c \"import sys; sys.exit(1)\""]
        )

        self.assertIs(integration_report.verdict, QualityVerdict.FAIL)
        # Confirms the two levels are stored and retrieved separately.
        self.assertIs(self.gate.latest("task_a", level="task").verdict, QualityVerdict.PASS)
        self.assertIs(self.gate.latest("task_a", level="integration").verdict, QualityVerdict.FAIL)


if __name__ == "__main__":
    unittest.main()
