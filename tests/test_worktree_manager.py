"""Milestone 6.1 — Git Worktree Manager.

Uses real, disposable Git repositories created fresh per test (never a
fixed path — see ROADMAP.md §10.5, hardware independence) and real
`git worktree` commands — never mocked, consistent with this project's
practice of exercising real subprocess behavior wherever feasible
(see tests/test_claude_code_adapter.py, tests/test_hermes_adapter.py).
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.core.state import StateManager
from universal_orchestrator.errors import (
    DuplicateError,
    InvalidStateTransitionError,
    NotFoundError,
)
from universal_orchestrator.execution.worktree import WorktreeStatus
from universal_orchestrator.execution.worktree_manager import (
    WorktreeManager,
    WorktreeOperationError,
)


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], path)
    _run(["git", "config", "user.email", "test@example.com"], path)
    _run(["git", "config", "user.name", "Test"], path)
    (path / "README.md").write_text("original\n")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-q", "-m", "initial"], path)


class WorktreeManagerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_path = self.tmp_path / "project"
        _init_repo(self.repo_path)
        self.state = StateManager(self.tmp_path / "orchestrator.sqlite3")
        self.manager = WorktreeManager(self.state)

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def _original_checkout_snapshot(self) -> str:
        return (self.repo_path / "README.md").read_text()


class AcquisitionTests(WorktreeManagerTestCase):
    def test_two_tasks_same_project_get_different_worktrees(self):
        handle_a = self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        handle_b = self.manager.acquire("proj_x", "task_b", str(self.repo_path))

        self.assertNotEqual(handle_a.path, handle_b.path)
        self.assertTrue(Path(handle_a.path).is_dir())
        self.assertTrue(Path(handle_b.path).is_dir())
        self.assertEqual(handle_a.status, WorktreeStatus.ACQUIRED)
        self.assertEqual(handle_b.status, WorktreeStatus.ACQUIRED)

    def test_duplicate_task_id_acquisition_is_rejected(self):
        self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        with self.assertRaises(WorktreeOperationError):
            self.manager.acquire("proj_x", "task_a", str(self.repo_path))

    def test_acquire_rejects_invalid_repository_path(self):
        with self.assertRaises(WorktreeOperationError):
            self.manager.acquire("proj_x", "task_a", str(self.tmp_path / "does-not-exist"))

    def test_save_worktree_duplicate_raises_duplicate_error_at_state_layer(self):
        # Exclusive ownership is enforced at the persistence layer itself,
        # not merely by the manager's own pre-check (closes the TOCTOU
        # race the manager's acquire() already defends against).
        handle = self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        with self.assertRaises(DuplicateError):
            self.state.save_worktree(handle)


class IsolationTests(WorktreeManagerTestCase):
    def test_changes_in_one_worktree_do_not_affect_another(self):
        handle_a = self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        handle_b = self.manager.acquire("proj_x", "task_b", str(self.repo_path))

        (Path(handle_a.path) / "only_in_a.txt").write_text("a's work\n")

        self.assertFalse((Path(handle_b.path) / "only_in_a.txt").exists())
        self.assertTrue((Path(handle_a.path) / "only_in_a.txt").exists())

    def test_original_checkout_remains_unchanged_after_full_lifecycle(self):
        before = self._original_checkout_snapshot()

        handle = self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        (Path(handle.path) / "agent_output.txt").write_text("agent wrote this\n")
        for target in (
            WorktreeStatus.EXECUTING,
            WorktreeStatus.QUALITY_CHECK,
            WorktreeStatus.AWAITING_APPROVAL,
            WorktreeStatus.INTEGRATING,
            WorktreeStatus.INTEGRATED,
        ):
            self.manager.transition("task_a", target)
        self.manager.release("task_a", str(self.repo_path))

        after = self._original_checkout_snapshot()
        self.assertEqual(before, after)
        self.assertFalse((self.repo_path / "agent_output.txt").exists())


class StateMachineTests(WorktreeManagerTestCase):
    def test_valid_transition_sequence_updates_status(self):
        self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        handle = self.manager.transition("task_a", WorktreeStatus.EXECUTING)
        self.assertEqual(handle.status, WorktreeStatus.EXECUTING)
        handle = self.manager.transition("task_a", WorktreeStatus.QUALITY_CHECK)
        self.assertEqual(handle.status, WorktreeStatus.QUALITY_CHECK)

    def test_invalid_transition_is_rejected(self):
        self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        with self.assertRaises(InvalidStateTransitionError):
            # ACQUIRED -> INTEGRATED is not a valid direct transition.
            self.manager.transition("task_a", WorktreeStatus.INTEGRATED)

    def test_transition_on_unknown_task_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.manager.transition("no_such_task", WorktreeStatus.EXECUTING)

    def test_released_is_terminal_no_further_transition_allowed(self):
        self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        for target in (
            WorktreeStatus.EXECUTING,
            WorktreeStatus.QUALITY_CHECK,
            WorktreeStatus.AWAITING_APPROVAL,
            WorktreeStatus.INTEGRATING,
            WorktreeStatus.INTEGRATED,
        ):
            self.manager.transition("task_a", target)
        self.manager.release("task_a", str(self.repo_path))
        with self.assertRaises(InvalidStateTransitionError):
            self.manager.transition("task_a", WorktreeStatus.EXECUTING)

    def test_release_refuses_when_not_integrated_or_confirmed(self):
        self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        self.manager.transition("task_a", WorktreeStatus.EXECUTING)
        with self.assertRaises(WorktreeOperationError):
            self.manager.release("task_a", str(self.repo_path))
        # Refusal must not have touched the worktree on disk.
        handle = self.manager.get("task_a")
        self.assertTrue(Path(handle.path).is_dir())

    def test_release_refuses_from_needs_review_without_confirm_discard(self):
        self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        self.manager.transition("task_a", WorktreeStatus.NEEDS_REVIEW)
        with self.assertRaises(WorktreeOperationError):
            self.manager.release("task_a", str(self.repo_path))

    def test_release_succeeds_from_needs_review_with_explicit_confirm_discard(self):
        self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        handle = self.manager.transition("task_a", WorktreeStatus.NEEDS_REVIEW)
        self.manager.release("task_a", str(self.repo_path), confirm_discard=True)
        self.assertFalse(Path(handle.path).exists())
        self.assertEqual(self.manager.get("task_a").status, WorktreeStatus.RELEASED)


class RecoveryTests(WorktreeManagerTestCase):
    def test_interrupted_task_preserves_uncommitted_changes(self):
        handle = self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        self.manager.transition("task_a", WorktreeStatus.EXECUTING)
        (Path(handle.path) / "in_progress.txt").write_text("not yet committed\n")

        # Simulate an Orchestrator restart with no clean shutdown.
        self.manager.recover(str(self.repo_path))

        recovered = self.manager.get("task_a")
        self.assertEqual(recovered.status, WorktreeStatus.NEEDS_REVIEW)
        self.assertTrue((Path(handle.path) / "in_progress.txt").exists())
        self.assertEqual(
            (Path(handle.path) / "in_progress.txt").read_text(), "not yet committed\n"
        )

    def test_recover_detects_missing_worktree_directory_as_needs_review(self):
        handle = self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        self.manager.transition("task_a", WorktreeStatus.EXECUTING)
        # Simulate the directory vanishing without the manager's knowledge
        # (e.g. an external `rm -rf`) — recover() must not trust the
        # persisted record blindly.
        subprocess.run(["rm", "-rf", handle.path], check=True)

        self.manager.recover(str(self.repo_path))

        self.assertEqual(self.manager.get("task_a").status, WorktreeStatus.NEEDS_REVIEW)

    def test_recover_leaves_integrated_worktree_untouched(self):
        self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        for target in (
            WorktreeStatus.EXECUTING,
            WorktreeStatus.QUALITY_CHECK,
            WorktreeStatus.AWAITING_APPROVAL,
            WorktreeStatus.INTEGRATING,
            WorktreeStatus.INTEGRATED,
        ):
            self.manager.transition("task_a", target)

        self.manager.recover(str(self.repo_path))

        self.assertEqual(self.manager.get("task_a").status, WorktreeStatus.INTEGRATED)

    def test_recover_does_not_touch_released_worktrees(self):
        self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        for target in (
            WorktreeStatus.EXECUTING,
            WorktreeStatus.QUALITY_CHECK,
            WorktreeStatus.AWAITING_APPROVAL,
            WorktreeStatus.INTEGRATING,
            WorktreeStatus.INTEGRATED,
        ):
            self.manager.transition("task_a", target)
        self.manager.release("task_a", str(self.repo_path))

        reconciled = self.manager.recover(str(self.repo_path))

        self.assertEqual(reconciled, [])
        self.assertEqual(self.manager.get("task_a").status, WorktreeStatus.RELEASED)


class ListingTests(WorktreeManagerTestCase):
    def test_list_active_excludes_released(self):
        self.manager.acquire("proj_x", "task_a", str(self.repo_path))
        self.manager.acquire("proj_x", "task_b", str(self.repo_path))
        for target in (
            WorktreeStatus.EXECUTING,
            WorktreeStatus.QUALITY_CHECK,
            WorktreeStatus.AWAITING_APPROVAL,
            WorktreeStatus.INTEGRATING,
            WorktreeStatus.INTEGRATED,
        ):
            self.manager.transition("task_a", target)
        self.manager.release("task_a", str(self.repo_path))

        active = self.manager.list_active("proj_x")

        self.assertEqual([h.task_id for h in active], ["task_b"])


if __name__ == "__main__":
    unittest.main()
