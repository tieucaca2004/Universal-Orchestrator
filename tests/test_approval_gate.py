"""Milestone 6.3 — Approval Gate.

Real Git repositories throughout (see ROADMAP.md §10.5); no mocked
decisions — every "approval" or "denial" here is a real row recorded
through `ApprovalGate.record_decision`, read back through
`require_approval`/`get_decisions`, never assumed.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.core.state import StateManager
from universal_orchestrator.errors import ApprovalRequiredError
from universal_orchestrator.execution.approval import ApprovalDecisionType, ApprovalOperationError
from universal_orchestrator.execution.approval_manager import ApprovalGate
from universal_orchestrator.execution.conflict_manager import ConflictManager
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
    (path / "README.md").write_text("original\n")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-q", "-m", "initial"], path)


class ApprovalGateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_path = self.tmp_path / "project"
        _init_repo(self.repo_path)
        self.state = StateManager(self.tmp_path / "orchestrator.sqlite3")
        self.worktrees = WorktreeManager(self.state)
        self.conflicts = ConflictManager(self.state, self.worktrees)
        self.approvals = ApprovalGate(self.state)

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def _clean_proposal(self, task_id: str = "task_a", quality_verdict: str = "PASS"):
        handle = self.worktrees.acquire("proj_x", task_id, str(self.repo_path))
        (Path(handle.path) / "agent_output.txt").write_text("agent work\n")
        _run(["git", "add", "agent_output.txt"], Path(handle.path))
        _run(["git", "commit", "-q", "-m", "agent change"], Path(handle.path))
        report = self.conflicts.check(str(self.repo_path), task_id)
        proposal = self.approvals.build_integration_proposal(
            str(self.repo_path), self.worktrees.get(task_id), report, quality_verdict
        )
        return handle, report, proposal


class ProposalContentTests(ApprovalGateTestCase):
    def test_proposal_presents_task_worktree_diff_and_reports(self):
        handle, report, proposal = self._clean_proposal()

        self.assertEqual(proposal.task_id, "task_a")
        self.assertEqual(proposal.worktree_path, handle.path)
        self.assertEqual(proposal.conflict_report_id, report.report_id)
        self.assertEqual(proposal.quality_verdict, "PASS")
        self.assertFalse(proposal.conflict_blocks)

    def test_diff_summary_never_contains_full_file_content(self):
        handle = self.worktrees.acquire("proj_x", "task_a", str(self.repo_path))
        secret_looking_line = "api_key: sk-this-should-never-appear-in-a-report"
        (Path(handle.path) / "config.txt").write_text(secret_looking_line + "\n")
        _run(["git", "add", "config.txt"], Path(handle.path))
        _run(["git", "commit", "-q", "-m", "agent change"], Path(handle.path))
        report = self.conflicts.check(str(self.repo_path), "task_a")

        proposal = self.approvals.build_integration_proposal(
            str(self.repo_path), handle, report, "PASS"
        )

        # --stat output names the file and a change count, never the
        # actual line content.
        self.assertIn("config.txt", proposal.diff_summary)
        self.assertNotIn(secret_looking_line, proposal.diff_summary)
        self.assertNotIn("sk-this-should-never-appear", proposal.diff_summary)

    def test_diff_identity_is_the_real_branch_commit_sha(self):
        handle, _, proposal = self._clean_proposal()
        real_sha = _run(["git", "-C", str(self.repo_path), "rev-parse", handle.branch], self.repo_path).strip()
        self.assertEqual(proposal.diff_identity, real_sha)


class GateEnforcementTests(ApprovalGateTestCase):
    def test_integration_cannot_proceed_without_any_decision(self):
        _, _, proposal = self._clean_proposal()
        with self.assertRaises(ApprovalRequiredError):
            self.approvals.require_approval(
                proposal.task_id, proposal.action, proposal.diff_identity
            )

    def test_denial_blocks_integration(self):
        _, _, proposal = self._clean_proposal()
        self.approvals.record_decision(
            proposal, ApprovalDecisionType.DENIED, reason="not ready"
        )
        with self.assertRaises(ApprovalRequiredError):
            self.approvals.require_approval(
                proposal.task_id, proposal.action, proposal.diff_identity
            )

    def test_approval_authorizes_the_exact_proposal(self):
        _, _, proposal = self._clean_proposal()
        self.approvals.record_decision(proposal, ApprovalDecisionType.APPROVED, reason="looks good")

        decision = self.approvals.require_approval(
            proposal.task_id, proposal.action, proposal.diff_identity
        )
        self.assertIs(decision.decision, ApprovalDecisionType.APPROVED)

    def test_approval_for_one_proposal_does_not_authorize_a_different_diff(self):
        _, _, proposal = self._clean_proposal()
        self.approvals.record_decision(proposal, ApprovalDecisionType.APPROVED)

        # A new commit changes the branch's SHA -> a genuinely different
        # diff_identity, even though it is still the same task_id.
        handle = self.worktrees.get(proposal.task_id)
        (Path(handle.path) / "more_work.txt").write_text("more\n")
        _run(["git", "add", "more_work.txt"], Path(handle.path))
        _run(["git", "commit", "-q", "-m", "more work after approval"], Path(handle.path))
        new_sha = _run(
            ["git", "-C", str(self.repo_path), "rev-parse", handle.branch], self.repo_path
        ).strip()
        self.assertNotEqual(new_sha, proposal.diff_identity)

        with self.assertRaises(ApprovalRequiredError):
            self.approvals.require_approval(proposal.task_id, proposal.action, new_sha)

    def test_destructive_action_requires_its_own_approval_not_integrate(self):
        _, _, proposal = self._clean_proposal()
        self.approvals.record_decision(proposal, ApprovalDecisionType.APPROVED)

        # Approving "integrate" must never satisfy a check for a
        # different, destructive action on the same diff_identity.
        with self.assertRaises(ApprovalRequiredError):
            self.approvals.require_approval(
                proposal.task_id, "discard_worktree", proposal.diff_identity
            )

    def test_cannot_approve_a_conflict_blocked_proposal(self):
        handle_a = self.worktrees.acquire("proj_x", "task_a", str(self.repo_path))
        handle_b = self.worktrees.acquire("proj_x", "task_b", str(self.repo_path))
        (Path(handle_a.path) / "README.md").write_text("a's edit\n")
        _run(["git", "commit", "-q", "-am", "a edits"], Path(handle_a.path))
        (Path(handle_b.path) / "README.md").write_text("b's edit\n")
        _run(["git", "commit", "-q", "-am", "b edits"], Path(handle_b.path))
        _run(["git", "merge", "--no-ff", "-q", "-m", "integrate b", handle_b.branch], self.repo_path)

        report = self.conflicts.check(str(self.repo_path), "task_a")
        self.assertTrue(report.blocks_integration)
        proposal = self.approvals.build_integration_proposal(
            str(self.repo_path), handle_a, report, "PASS"
        )

        with self.assertRaises(ApprovalOperationError):
            self.approvals.record_decision(proposal, ApprovalDecisionType.APPROVED)

    def test_cannot_approve_a_non_pass_quality_verdict(self):
        _, _, proposal = self._clean_proposal(quality_verdict="FAIL")
        with self.assertRaises(ApprovalOperationError):
            self.approvals.record_decision(proposal, ApprovalDecisionType.APPROVED)

    def test_denial_is_always_recordable_even_when_blocked(self):
        # Denying is never refused — only an APPROVED decision is
        # subject to the defense-in-depth checks above.
        _, _, proposal = self._clean_proposal(quality_verdict="FAIL")
        decision = self.approvals.record_decision(proposal, ApprovalDecisionType.DENIED)
        self.assertIs(decision.decision, ApprovalDecisionType.DENIED)


class AuditabilityTests(ApprovalGateTestCase):
    def test_decisions_are_queryable_in_order(self):
        _, _, proposal = self._clean_proposal()
        self.approvals.record_decision(proposal, ApprovalDecisionType.DENIED, reason="first pass")
        self.approvals.record_decision(proposal, ApprovalDecisionType.APPROVED, reason="fixed now")

        decisions = self.approvals.get_decisions(proposal.task_id)

        self.assertEqual(len(decisions), 2)
        self.assertIs(decisions[0].decision, ApprovalDecisionType.DENIED)
        self.assertIs(decisions[1].decision, ApprovalDecisionType.APPROVED)

    def test_latest_decision_for_the_same_diff_governs(self):
        _, _, proposal = self._clean_proposal()
        self.approvals.record_decision(proposal, ApprovalDecisionType.APPROVED)
        self.approvals.record_decision(proposal, ApprovalDecisionType.DENIED, reason="changed my mind")

        with self.assertRaises(ApprovalRequiredError):
            self.approvals.require_approval(
                proposal.task_id, proposal.action, proposal.diff_identity
            )


if __name__ == "__main__":
    unittest.main()
