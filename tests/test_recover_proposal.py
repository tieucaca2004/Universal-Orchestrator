"""Milestone 7, Component D — Proposal Recovery CLI.

`GovernedPipeline.recover_proposal()` is tested directly (no adapter
or router ever constructed — proof by construction that it cannot
call `engine.execute()`: there is nothing registered for it to call),
plus a real-subprocess test of `orchestrator_cli recover-proposal`
itself, matching tests/test_approval_cli.py's discipline.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.governed_pipeline import GovernedPipeline
from universal_orchestrator.core.models import ProjectProfile, TaskRequest
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.errors import OrchestratorError
from universal_orchestrator.execution.adapter import ExecutionOutcome, ExecutionResult
from universal_orchestrator.execution.approval_manager import ApprovalGate
from universal_orchestrator.execution.conflict_manager import ConflictManager
from universal_orchestrator.execution.quality import QualityReport, QualityVerdict
from universal_orchestrator.execution.quality_manager import QualityGate
from universal_orchestrator.execution.worktree import WorktreeStatus
from universal_orchestrator.execution.worktree_manager import WorktreeManager


def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], path)
    _run(["git", "config", "user.email", "test@example.com"], path)
    _run(["git", "config", "user.name", "Test"], path)
    (path / "README.md").write_text("original\n")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-q", "-m", "initial"], path)


class RecoverProposalTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_path = self.tmp_path / "project"
        _init_repo(self.repo_path)
        self.state = StateManager(self.tmp_path / "orchestrator.sqlite3")
        # Deliberately no adapters/agents/providers/router registered --
        # recover_proposal() must never need one.
        self.engine = OrchestrationEngine.bootstrap(self.state)
        self.worktrees = WorktreeManager(self.state)
        self.conflicts = ConflictManager(self.state, self.worktrees)
        self.approvals = ApprovalGate(self.state)
        self.quality = QualityGate(self.state)
        self.pipeline = GovernedPipeline(
            self.engine, self.worktrees, self.conflicts, self.approvals, self.quality
        )
        self.project = ProjectProfile(
            name="proj", repository_path=str(self.repo_path), project_id="proj"
        )

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def _submit(self) -> str:
        task = self.engine.submit(TaskRequest(title="t"), self.project)
        return task.task_id

    def _acquire_and_start(self, task_id: str) -> None:
        self.worktrees.acquire(self.project.project_id, task_id, str(self.repo_path))
        self.worktrees.transition(task_id, WorktreeStatus.EXECUTING)
        self.engine.manager.start(task_id)

    def _record_execution_result(self, task_id: str) -> None:
        self.engine.manager.record_execution_result(
            ExecutionResult(
                execution_id=f"exec_{task_id}", task_id=task_id,
                outcome=ExecutionOutcome.COMPLETED, exit_code=0,
            )
        )

    def _pass_quality_gate(self, task_id: str) -> None:
        self.state.save_quality_report(
            QualityReport(task_id=task_id, level="task", verdict=QualityVerdict.PASS, rationale="t")
        )

    def _reach_succeeded_with_worktree_still_quality_check(self, task_id: str) -> None:
        """Simulates the crash window: a task that reached SUCCEEDED
        but never got as far as building its own Integration
        Proposal (a real, reachable gap -- see
        GovernedPipeline.recover_proposal's own docstring)."""
        self._acquire_and_start(task_id)
        self._record_execution_result(task_id)
        self.worktrees.transition(task_id, WorktreeStatus.QUALITY_CHECK)
        self._pass_quality_gate(task_id)
        self.engine.manager.succeed(task_id, result={"execution_id": f"exec_{task_id}"})


class HappyPathTests(RecoverProposalTestCase):
    def test_recovers_a_missing_proposal_for_an_already_succeeded_task(self):
        task_id = self._submit()
        self._reach_succeeded_with_worktree_still_quality_check(task_id)

        proposal = self.pipeline.recover_proposal(task_id, self.project)

        self.assertIsNotNone(proposal)
        self.assertEqual(self.approvals.get_proposal(task_id).proposal_id, proposal.proposal_id)
        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.AWAITING_APPROVAL)
        self.assertEqual(self.engine.manager.get(task_id).status.value, "succeeded")


class PreconditionRefusalTests(RecoverProposalTestCase):
    def test_refuses_when_task_is_not_succeeded(self):
        task_id = self._submit()
        self._acquire_and_start(task_id)
        self._record_execution_result(task_id)
        self.worktrees.transition(task_id, WorktreeStatus.QUALITY_CHECK)
        # Deliberately never calls succeed() -- task stays RUNNING.
        with self.assertRaisesRegex(OrchestratorError, "not SUCCEEDED"):
            self.pipeline.recover_proposal(task_id, self.project)
        self.assertIsNone(self.approvals.get_proposal(task_id))

    def test_refuses_when_no_execution_result_is_recorded(self):
        task_id = self._submit()
        self._acquire_and_start(task_id)
        self.worktrees.transition(task_id, WorktreeStatus.QUALITY_CHECK)
        self._pass_quality_gate(task_id)
        self.engine.manager.succeed(task_id)  # no ExecutionResult ever recorded
        with self.assertRaisesRegex(OrchestratorError, "no persisted ExecutionResult"):
            self.pipeline.recover_proposal(task_id, self.project)

    def test_refuses_when_worktree_is_not_quality_check(self):
        task_id = self._submit()
        self._acquire_and_start(task_id)  # Worktree left at EXECUTING
        self._record_execution_result(task_id)
        self._pass_quality_gate(task_id)
        self.engine.manager.succeed(task_id, result={"x": 1})
        with self.assertRaisesRegex(OrchestratorError, "not\\s+QUALITY_CHECK"):
            self.pipeline.recover_proposal(task_id, self.project)

    def test_refuses_and_prevents_duplicate_proposal_when_one_already_exists(self):
        task_id = self._submit()
        self._reach_succeeded_with_worktree_still_quality_check(task_id)

        # Seed a pre-existing proposal directly (Worktree deliberately
        # left at QUALITY_CHECK, isolating "a proposal already exists"
        # from "the Worktree already moved on" -- recover_proposal's
        # own successful run always advances the Worktree too, so
        # calling it twice in a row would hit that check first).
        handle = self.worktrees.get(task_id)
        conflict_report = self.conflicts.check(str(self.repo_path), task_id)
        self.approvals.build_integration_proposal(
            str(self.repo_path), handle, conflict_report, "PASS"
        )

        with self.assertRaisesRegex(OrchestratorError, "already has an integration proposal"):
            self.pipeline.recover_proposal(task_id, self.project)

        # Still exactly the one proposal -- never duplicated.
        self.assertEqual(
            len([p for p in self.approvals.list_proposals() if p.task_id == task_id]), 1
        )

    def test_never_invokes_engine_execute_or_an_adapter(self):
        """No adapters/agents/providers/router were ever registered in
        setUp -- if recover_proposal tried to call engine.execute(),
        this would raise a RuntimeError ('no adapter registry
        configured'), not merely fail an assertion. Reaching a
        successful, correct result is itself the proof."""
        task_id = self._submit()
        self._reach_succeeded_with_worktree_still_quality_check(task_id)
        proposal = self.pipeline.recover_proposal(task_id, self.project)
        self.assertIsNotNone(proposal)


def _run_cli_subprocess(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    return subprocess.run(
        [sys.executable, "-m", "universal_orchestrator.cli.orchestrator_cli", "--db", str(db_path), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


class RecoverProposalCLITestCase(RecoverProposalTestCase):
    def test_cli_recovers_a_missing_proposal(self):
        task_id = self._submit()
        self._reach_succeeded_with_worktree_still_quality_check(task_id)
        self.state.close()  # release the file lock before the subprocess opens it

        result = _run_cli_subprocess(
            self.tmp_path / "orchestrator.sqlite3", "recover-proposal", task_id,
            "--repo", str(self.repo_path), "--project-id", "proj",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RECOVERED_PROPOSAL", result.stdout)

        self.state = StateManager(self.tmp_path / "orchestrator.sqlite3")
        self.approvals = ApprovalGate(self.state)
        self.assertIsNotNone(self.approvals.get_proposal(task_id))

    def test_cli_refuses_cleanly_for_a_non_succeeded_task(self):
        task_id = self._submit()
        self._acquire_and_start(task_id)
        self._record_execution_result(task_id)
        self.worktrees.transition(task_id, WorktreeStatus.QUALITY_CHECK)
        self.state.close()

        result = _run_cli_subprocess(
            self.tmp_path / "orchestrator.sqlite3", "recover-proposal", task_id,
            "--repo", str(self.repo_path), "--project-id", "proj",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Refused", result.stdout)
        self.state = StateManager(self.tmp_path / "orchestrator.sqlite3")


if __name__ == "__main__":
    unittest.main()
