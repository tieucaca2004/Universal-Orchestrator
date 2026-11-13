"""M6 remediation, Item 2 — Approval CLI.

Includes real subprocess invocations of `python3 -m
universal_orchestrator.cli.approval_cli ...` (not just calling
`ApprovalCLI`'s methods directly) against a real, disposable SQLite
database and a real Git repository, so the actual command-line entry
point is genuinely exercised, not only the Python class behind it.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.core.state import StateManager
from universal_orchestrator.execution.approval import ApprovalDecisionType
from universal_orchestrator.execution.approval_manager import ApprovalGate
from universal_orchestrator.execution.conflict_manager import ConflictManager
from universal_orchestrator.execution.worktree_manager import WorktreeManager
from universal_orchestrator.cli.approval_cli import ApprovalCLI, build_parser, main


def _run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)
    return result.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["git", "init", "-q", "-b", "main"], path)
    _run_git(["git", "config", "user.email", "test@example.com"], path)
    _run_git(["git", "config", "user.name", "Test"], path)
    (path / "README.md").write_text("original\n")
    _run_git(["git", "add", "README.md"], path)
    _run_git(["git", "commit", "-q", "-m", "initial"], path)


def _run_cli_subprocess(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    return subprocess.run(
        [sys.executable, "-m", "universal_orchestrator.cli.approval_cli", "--db", str(db_path), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


class ApprovalCLITestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_path = self.tmp_path / "project"
        _init_repo(self.repo_path)
        self.db_path = self.tmp_path / "orchestrator.sqlite3"

        self.state = StateManager(self.db_path)
        self.worktrees = WorktreeManager(self.state)
        self.conflicts = ConflictManager(self.state, self.worktrees)
        self.approvals = ApprovalGate(self.state)

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def _build_real_proposal(self, task_id: str = "task_a"):
        handle = self.worktrees.acquire("proj_x", task_id, str(self.repo_path))
        (Path(handle.path) / "work.txt").write_text("agent output\n")
        _run_git(["git", "add", "work.txt"], Path(handle.path))
        _run_git(["git", "commit", "-q", "-m", "agent work"], Path(handle.path))
        report = self.conflicts.check(str(self.repo_path), task_id)
        proposal = self.approvals.build_integration_proposal(
            str(self.repo_path), handle, report, "PASS"
        )
        return proposal


class ClassLevelTests(ApprovalCLITestCase):
    def test_list_shows_a_pending_proposal(self):
        self._build_real_proposal()
        cli = ApprovalCLI(self.state)

        output = cli.format_list()

        self.assertIn("task_a", output)
        self.assertIn("1 pending", output)

    def test_list_excludes_a_resolved_proposal(self):
        proposal = self._build_real_proposal()
        self.approvals.record_decision(proposal, ApprovalDecisionType.APPROVED)
        cli = ApprovalCLI(self.state)

        output = cli.format_list()

        self.assertIn("No pending", output)

    def test_show_never_prints_full_diff_content_only_stat(self):
        handle = self.worktrees.acquire("proj_x", "task_a", str(self.repo_path))
        secret_line = "token: ghp_this_should_never_appear_in_cli_output"
        (Path(handle.path) / "config.txt").write_text(secret_line + "\n")
        _run_git(["git", "add", "config.txt"], Path(handle.path))
        _run_git(["git", "commit", "-q", "-m", "agent work"], Path(handle.path))
        report = self.conflicts.check(str(self.repo_path), "task_a")
        self.approvals.build_integration_proposal(str(self.repo_path), handle, report, "PASS")
        cli = ApprovalCLI(self.state)

        output = cli.format_detail("task_a")

        self.assertIn("config.txt", output)
        self.assertNotIn(secret_line, output)
        self.assertNotIn("ghp_this_should_never_appear", output)

    def test_approve_records_a_real_decision(self):
        self._build_real_proposal()
        cli = ApprovalCLI(self.state)

        result = cli.approve("task_a", "looks good")

        self.assertIn("APPROVED", result)
        decisions = self.approvals.get_decisions("task_a")
        self.assertEqual(len(decisions), 1)
        self.assertIs(decisions[0].decision, ApprovalDecisionType.APPROVED)

    def test_reject_records_a_real_decision_and_blocks_require_approval(self):
        from universal_orchestrator.errors import ApprovalRequiredError

        proposal = self._build_real_proposal()
        cli = ApprovalCLI(self.state)

        result = cli.reject("task_a", "not ready")

        self.assertIn("REJECTED", result)
        with self.assertRaises(ApprovalRequiredError):
            self.approvals.require_approval(proposal.task_id, proposal.action, proposal.diff_identity)

    def test_approve_unknown_task_raises_cleanly_never_defaults_to_approval(self):
        cli = ApprovalCLI(self.state)
        with self.assertRaises(SystemExit):
            cli.approve("no_such_task", "")

    def test_history_shows_every_decision_in_order(self):
        proposal = self._build_real_proposal()
        self.approvals.record_decision(proposal, ApprovalDecisionType.DENIED, reason="first look")
        self.approvals.record_decision(proposal, ApprovalDecisionType.APPROVED, reason="fixed now")
        cli = ApprovalCLI(self.state)

        output = cli.format_history("task_a")

        self.assertIn("DENIED", output)
        self.assertIn("APPROVED", output)
        self.assertLess(output.index("DENIED"), output.index("APPROVED"))


class RealCLISubprocessTests(ApprovalCLITestCase):
    """These invoke the actual `python3 -m ...` entry point as a real
    subprocess — the genuine command-line interface, not a shortcut
    through the Python class."""

    def test_cli_list_subprocess(self):
        self._build_real_proposal()
        self.state.close()  # release the sqlite file before a second process opens it

        result = _run_cli_subprocess(self.db_path, "list")

        self.assertEqual(result.returncode, 0)
        self.assertIn("task_a", result.stdout)
        self.state = StateManager(self.db_path)  # reopen for tearDown

    def test_cli_approve_then_list_shows_no_longer_pending(self):
        self._build_real_proposal()
        self.state.close()

        approve_result = _run_cli_subprocess(self.db_path, "approve", "task_a", "--reason", "cli test")
        self.assertEqual(approve_result.returncode, 0)
        self.assertIn("APPROVED", approve_result.stdout)

        list_result = _run_cli_subprocess(self.db_path, "list")
        self.assertIn("No pending", list_result.stdout)

        self.state = StateManager(self.db_path)
        decisions = ApprovalGate(self.state).get_decisions("task_a")
        self.assertEqual(len(decisions), 1)
        self.assertIs(decisions[0].decision, ApprovalDecisionType.APPROVED)

    def test_cli_show_unknown_task_exits_cleanly(self):
        self.state.close()

        result = _run_cli_subprocess(self.db_path, "show", "no_such_task")

        self.assertEqual(result.returncode, 0)
        self.assertIn("No Integration Proposal", result.stdout)
        self.state = StateManager(self.db_path)

    def test_cli_requires_an_explicit_subcommand(self):
        self.state.close()
        result = _run_cli_subprocess(self.db_path)
        self.assertNotEqual(result.returncode, 0)
        self.state = StateManager(self.db_path)


class ArgumentParsingTests(unittest.TestCase):
    def test_parser_rejects_no_subcommand(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_parser_accepts_approve_with_reason(self):
        parser = build_parser()
        args = parser.parse_args(["approve", "task_a", "--reason", "x"])
        self.assertEqual(args.command, "approve")
        self.assertEqual(args.task_id, "task_a")
        self.assertEqual(args.reason, "x")


if __name__ == "__main__":
    unittest.main()
