"""Milestone 6 — integrated multi-Agent workflow test.

Exercises all four M6 components together, in the order the Founder's
revised integration sequence specifies, against real Git repositories:

    acquire worktree -> execute -> task-level Quality Gate (PASS)
    -> TaskManager.succeed() (now gated on that PASS)
    -> Conflict Manager check (tagged with base_sha)
    -> Approval Gate (explicit decision bound to that exact diff)
    -> integration lock -> revalidate -> real Git integration
    -> post-integration Quality Gate (project's own test command)
    -> release the worktree

Two Agents, same Project, two separate Worktrees (Option B) — the
second Agent's integration is checked against the *new* main left by
the first, proving the components compose across more than one task,
not just in isolation.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.core.models import TaskSpecification
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.core.task_manager import TaskManager
from universal_orchestrator.errors import ApprovalRequiredError, QualityGateRequiredError
from universal_orchestrator.execution.adapter import ExecutionOutcome, ExecutionResult
from universal_orchestrator.execution.approval import ApprovalDecisionType
from universal_orchestrator.execution.approval_manager import ApprovalGate
from universal_orchestrator.execution.conflict_manager import ConflictManager
from universal_orchestrator.execution.quality_manager import QualityGate
from universal_orchestrator.execution.worktree import WorktreeStatus
from universal_orchestrator.execution.worktree_manager import WorktreeManager


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], path)
    _run(["git", "config", "user.email", "test@example.com"], path)
    _run(["git", "config", "user.name", "Test"], path)
    (path / "project.txt").write_text("line1\nline2\nline3\n")
    _run(["git", "add", "project.txt"], path)
    _run(["git", "commit", "-q", "-m", "initial"], path)


class M6IntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_path = self.tmp_path / "project"
        _init_repo(self.repo_path)

        self.state = StateManager(self.tmp_path / "orchestrator.sqlite3")
        self.tasks = TaskManager(self.state)
        self.worktrees = WorktreeManager(self.state)
        self.conflicts = ConflictManager(self.state, self.worktrees)
        self.approvals = ApprovalGate(self.state)
        self.quality = QualityGate(self.state)

        # The Project's own declared required testing command — never
        # invented by any M6 component. A trivial, real, deterministic
        # check: the integration marker file left by a prior task must
        # still be present after further integrations.
        self.required_testing_commands = [
            f"python3 -c \"import pathlib,sys; "
            f"sys.exit(0 if pathlib.Path('{self.repo_path}/project.txt').exists() else 1)\""
        ]

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def _run_task_through_pipeline(
        self, title: str, criteria: list[str], filename: str, content: str
    ):
        """One Agent's full path from task creation through a real,
        approved Git integration — the scenario the M6 Final
        Consolidated Implementation Blueprint specified. Returns the
        real, Task-Manager-assigned task_id along with the handle,
        conflict report, and proposal."""
        task = self.tasks.create_task(title, "proj_x")
        task_id = task.task_id
        spec = TaskSpecification(
            objective=f"write {filename}", scope="single file", acceptance_criteria=criteria
        )
        self.tasks.attach_specification(task_id, spec)
        self.tasks.start(task_id)

        # -- Git Worktree Manager: isolated environment per Agent --------
        handle = self.worktrees.acquire("proj_x", task_id, str(self.repo_path))
        self.worktrees.transition(task_id, WorktreeStatus.EXECUTING)

        # -- the Agent "executes" (real commit in its own worktree) ------
        (Path(handle.path) / filename).write_text(content)
        _run(["git", "add", filename], Path(handle.path))
        _run(["git", "commit", "-q", "-m", f"agent work for {task_id}"], Path(handle.path))
        result = ExecutionResult(
            execution_id=f"exec_{task_id}", task_id=task_id, outcome=ExecutionOutcome.COMPLETED
        )

        # -- Task-level Quality Gate --------------------------------------
        evidence = {c: True for c in criteria}
        quality_report = self.quality.evaluate_task(spec, result, criteria_evidence=evidence)
        self.assertEqual(quality_report.verdict.value, "PASS")
        self.worktrees.transition(task_id, WorktreeStatus.QUALITY_CHECK)

        # TaskManager.succeed() is now gated on exactly this report.
        self.tasks.succeed(task_id, result={"file": filename})

        # -- Conflict Manager ----------------------------------------------
        conflict_report = self.conflicts.check(str(self.repo_path), task_id)

        # -- Approval Gate ---------------------------------------------------
        proposal = self.approvals.build_integration_proposal(
            str(self.repo_path), handle, conflict_report, quality_report.verdict.value
        )
        return task_id, handle, conflict_report, proposal

    def test_full_multi_agent_workflow_two_agents_integrate_successfully(self):
        # ---- Agent A: Planner/Coder writes file_a.txt --------------------
        task_a, handle_a, report_a, proposal_a = self._run_task_through_pipeline(
            "task A", ["creates file_a.txt"], "file_a.txt", "a's work\n"
        )
        self.assertFalse(report_a.blocks_integration)
        self.worktrees.transition(task_a, WorktreeStatus.AWAITING_APPROVAL)

        # Founder approves exactly this proposal.
        self.approvals.record_decision(proposal_a, ApprovalDecisionType.APPROVED, reason="looks good")
        decision_a = self.approvals.require_approval(
            proposal_a.task_id, proposal_a.action, proposal_a.diff_identity
        )
        self.assertEqual(decision_a.decision.value, "approved")

        # Integration lock + revalidation against the real current main.
        self.conflicts.acquire_integration_lock(task_a)
        self.assertFalse(self.conflicts.is_stale(str(self.repo_path), report_a))
        self.worktrees.transition(task_a, WorktreeStatus.INTEGRATING)
        _run(["git", "merge", "--no-ff", "-q", "-m", "integrate task A", handle_a.branch], self.repo_path)
        self.conflicts.release_integration_lock(task_a)

        # Post-integration Quality Gate — real command, real exit code.
        integration_report_a = self.quality.evaluate_integration(
            task_a, str(self.repo_path), self.required_testing_commands
        )
        self.assertEqual(integration_report_a.verdict.value, "PASS")
        self.worktrees.transition(task_a, WorktreeStatus.INTEGRATED)
        self.worktrees.release(task_a, str(self.repo_path))
        self.assertEqual(self.worktrees.get(task_a).status, WorktreeStatus.RELEASED)
        self.assertTrue((self.repo_path / "file_a.txt").exists())

        # ---- Agent B: a second, independent Agent, same Project ---------
        task_b, handle_b, report_b, proposal_b = self._run_task_through_pipeline(
            "task B", ["creates file_b.txt"], "file_b.txt", "b's work\n"
        )
        # Checked against the *new* main (which now contains A's change) —
        # proves the components compose across sequential integrations,
        # not just in isolation.
        self.assertFalse(report_b.blocks_integration)
        self.worktrees.transition(task_b, WorktreeStatus.AWAITING_APPROVAL)
        self.approvals.record_decision(proposal_b, ApprovalDecisionType.APPROVED)
        self.approvals.require_approval(proposal_b.task_id, proposal_b.action, proposal_b.diff_identity)

        self.conflicts.acquire_integration_lock(task_b)
        self.assertFalse(self.conflicts.is_stale(str(self.repo_path), report_b))
        self.worktrees.transition(task_b, WorktreeStatus.INTEGRATING)
        _run(["git", "merge", "--no-ff", "-q", "-m", "integrate task B", handle_b.branch], self.repo_path)
        self.conflicts.release_integration_lock(task_b)

        integration_report_b = self.quality.evaluate_integration(
            task_b, str(self.repo_path), self.required_testing_commands
        )
        self.assertEqual(integration_report_b.verdict.value, "PASS")
        self.worktrees.transition(task_b, WorktreeStatus.INTEGRATED)
        self.worktrees.release(task_b, str(self.repo_path))

        # Both Agents' work is really on main; the original single file
        # from `_init_repo` is also untouched.
        self.assertTrue((self.repo_path / "file_a.txt").exists())
        self.assertTrue((self.repo_path / "file_b.txt").exists())
        self.assertEqual((self.repo_path / "project.txt").read_text(), "line1\nline2\nline3\n")
        self.assertEqual(self.tasks.get(task_a).status.value, "succeeded")
        self.assertEqual(self.tasks.get(task_b).status.value, "succeeded")

    def test_full_workflow_blocks_without_approval_and_without_quality_pass(self):
        task = self.tasks.create_task("task C", "proj_x")
        task_id = task.task_id
        spec = TaskSpecification(objective="o", scope="s", acceptance_criteria=["x"])
        self.tasks.attach_specification(task_id, spec)
        self.tasks.start(task_id)
        handle = self.worktrees.acquire("proj_x", task_id, str(self.repo_path))
        self.worktrees.transition(task_id, WorktreeStatus.EXECUTING)
        (Path(handle.path) / "file_c.txt").write_text("c's work\n")
        _run(["git", "add", "file_c.txt"], Path(handle.path))
        _run(["git", "commit", "-q", "-m", "agent work"], Path(handle.path))

        # No Quality Gate evidence recorded at all -> succeed() must refuse.
        with self.assertRaises(QualityGateRequiredError):
            self.tasks.succeed(task_id)
        self.assertEqual(self.tasks.get(task_id).status.value, "running")

        # Even once Quality Gate passes, integration must still wait on an
        # actual recorded Approval Gate decision — never inferred.
        result = ExecutionResult(execution_id="exec_c", task_id=task_id, outcome=ExecutionOutcome.COMPLETED)
        quality_report = self.quality.evaluate_task(spec, result, criteria_evidence={"x": True})
        self.tasks.succeed(task_id)
        self.assertEqual(self.tasks.get(task_id).status.value, "succeeded")

        conflict_report = self.conflicts.check(str(self.repo_path), task_id)
        proposal = self.approvals.build_integration_proposal(
            str(self.repo_path), handle, conflict_report, quality_report.verdict.value
        )
        with self.assertRaises(ApprovalRequiredError):
            self.approvals.require_approval(proposal.task_id, proposal.action, proposal.diff_identity)

        # Integration never happened; the worktree is still exactly as
        # the Agent left it, not silently discarded.
        self.assertTrue(Path(handle.path).exists())
        self.assertEqual((Path(handle.path) / "file_c.txt").read_text(), "c's work\n")


if __name__ == "__main__":
    unittest.main()
