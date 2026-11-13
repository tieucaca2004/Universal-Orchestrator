"""Milestone 6.2 — Conflict Manager.

Every scenario uses a real, disposable Git repository (never mocked,
never a fixed path — see ROADMAP.md §10.5) and real `git merge-tree`
trial merges, empirically verified this session to touch nothing but
a dangling, unreferenced tree object: not the working tree, not the
index, not any branch, not `main`.
"""

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from universal_orchestrator.core.state import StateManager
from universal_orchestrator.execution.conflict import IntegrationLockHeldError
from universal_orchestrator.execution.conflict_manager import ConflictManager
from universal_orchestrator.execution.worktree_manager import WorktreeManager


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo(path: Path, *, lines: int = 1) -> None:
    path.mkdir(parents=True, exist_ok=True)
    # Pin the initial branch name explicitly (ROADMAP.md §10.5,
    # hardware independence) — never rely on this host's own
    # `init.defaultBranch` setting, which may not even be "main" (it
    # defaults to "master" when unset, confirmed on this host).
    _run(["git", "init", "-q", "-b", "main"], path)
    _run(["git", "config", "user.email", "test@example.com"], path)
    _run(["git", "config", "user.name", "Test"], path)
    (path / "f.txt").write_text("\n".join(f"line{i}" for i in range(1, lines + 1)) + "\n")
    _run(["git", "add", "f.txt"], path)
    _run(["git", "commit", "-q", "-m", "initial"], path)


def _commit_in_worktree(worktree_path: Path, filename: str, content: str) -> None:
    (worktree_path / filename).write_text(content)
    _run(["git", "add", filename], worktree_path)
    _run(["git", "commit", "-q", "-m", f"change {filename}"], worktree_path)


def _edit_line_in_worktree(worktree_path: Path, line_number: int, new_text: str) -> None:
    f = worktree_path / "f.txt"
    lines = f.read_text().splitlines()
    lines[line_number - 1] = new_text
    f.write_text("\n".join(lines) + "\n")
    _run(["git", "commit", "-q", "-am", f"edit line {line_number}"], worktree_path)


class ConflictManagerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_path = self.tmp_path / "project"
        _init_repo(self.repo_path, lines=10)
        self.state = StateManager(self.tmp_path / "orchestrator.sqlite3")
        self.worktrees = WorktreeManager(self.state)
        self.conflicts = ConflictManager(self.state, self.worktrees)

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()


class TierDistinctionTests(ConflictManagerTestCase):
    def test_different_files_no_overlap_no_conflict(self):
        handle_a = self.worktrees.acquire("proj_x", "task_a", str(self.repo_path))
        handle_b = self.worktrees.acquire("proj_x", "task_b", str(self.repo_path))
        _commit_in_worktree(Path(handle_a.path), "a_only.txt", "a's file\n")
        _commit_in_worktree(Path(handle_b.path), "b_only.txt", "b's file\n")

        report = self.conflicts.check(str(self.repo_path), "task_a")

        self.assertEqual(report.path_overlap, {})
        self.assertFalse(report.tier2_conflicting)
        self.assertFalse(report.blocks_integration)

    def test_path_overlap_without_actual_conflict_is_reported_but_does_not_block(self):
        handle_a = self.worktrees.acquire("proj_x", "task_a", str(self.repo_path))
        handle_b = self.worktrees.acquire("proj_x", "task_b", str(self.repo_path))
        _edit_line_in_worktree(Path(handle_a.path), 1, "line1-by-a")
        _edit_line_in_worktree(Path(handle_b.path), 10, "line10-by-b")

        report = self.conflicts.check(str(self.repo_path), "task_a")

        # Tier 1: reported, because both touched f.txt.
        self.assertIn("task_b", report.path_overlap)
        self.assertIn("f.txt", report.path_overlap["task_b"])
        # Tier 2: Git can auto-merge non-adjacent line edits cleanly.
        self.assertFalse(report.tier2_conflicting)
        self.assertEqual(report.tier2_conflicting_files, [])
        # The one rule this test exists to prove: tier 1 alone never blocks.
        self.assertFalse(report.blocks_integration)

    def test_actual_git_merge_conflict_blocks_integration(self):
        # Tier 2 checks a worktree's branch against the *target branch's
        # current state* — exactly as the Blueprint specifies. Two
        # sibling worktrees that both edit the same line never conflict
        # with each other until one of them has actually landed on the
        # target branch; this reproduces that real sequencing rather
        # than asserting a conflict that Git itself would not report
        # yet.
        handle_a = self.worktrees.acquire("proj_x", "task_a", str(self.repo_path))
        handle_b = self.worktrees.acquire("proj_x", "task_b", str(self.repo_path))
        _edit_line_in_worktree(Path(handle_a.path), 5, "line5-by-a")
        _edit_line_in_worktree(Path(handle_b.path), 5, "line5-by-b")

        # Simulate task_b having already gone through Approval Gate and
        # landed on main (Milestone 6.3, not yet built) — a real,
        # ordinary fast-forward merge, performed directly here only to
        # construct the scenario this test needs.
        _run(["git", "merge", "--no-ff", "-q", "-m", "integrate b", handle_b.branch], self.repo_path)

        report = self.conflicts.check(str(self.repo_path), "task_a")

        self.assertIn("task_b", report.path_overlap)
        self.assertTrue(report.tier2_conflicting)
        self.assertIn("f.txt", report.tier2_conflicting_files)
        self.assertTrue(report.blocks_integration)

    def test_trial_merge_never_touches_source_worktree_or_main(self):
        handle_a = self.worktrees.acquire("proj_x", "task_a", str(self.repo_path))
        handle_b = self.worktrees.acquire("proj_x", "task_b", str(self.repo_path))
        _edit_line_in_worktree(Path(handle_a.path), 5, "line5-by-a")
        _edit_line_in_worktree(Path(handle_b.path), 5, "line5-by-b")

        before_a = (Path(handle_a.path) / "f.txt").read_text()
        before_main_head = _run(["git", "rev-parse", "main"], self.repo_path).strip()

        self.conflicts.check(str(self.repo_path), "task_a")

        after_a = (Path(handle_a.path) / "f.txt").read_text()
        after_main_head = _run(["git", "rev-parse", "main"], self.repo_path).strip()
        self.assertEqual(before_a, after_a)
        self.assertEqual(before_main_head, after_main_head)

    def test_every_report_is_tagged_with_base_sha(self):
        self.worktrees.acquire("proj_x", "task_a", str(self.repo_path))
        current_main = _run(["git", "rev-parse", "main"], self.repo_path).strip()

        report = self.conflicts.check(str(self.repo_path), "task_a")

        self.assertEqual(report.base_sha, current_main)


class StalenessTests(ConflictManagerTestCase):
    def test_report_is_not_stale_when_main_has_not_moved(self):
        self.worktrees.acquire("proj_x", "task_a", str(self.repo_path))
        report = self.conflicts.check(str(self.repo_path), "task_a")
        self.assertFalse(self.conflicts.is_stale(str(self.repo_path), report))

    def test_report_becomes_stale_once_main_advances(self):
        self.worktrees.acquire("proj_x", "task_a", str(self.repo_path))
        report = self.conflicts.check(str(self.repo_path), "task_a")

        # Simulate another, already-approved integration landing on main.
        (self.repo_path / "unrelated.txt").write_text("someone else's merge\n")
        _run(["git", "add", "unrelated.txt"], self.repo_path)
        _run(["git", "commit", "-q", "-m", "a different integration landed"], self.repo_path)

        self.assertTrue(self.conflicts.is_stale(str(self.repo_path), report))

    def test_revalidate_produces_a_fresh_report_against_the_new_main(self):
        self.worktrees.acquire("proj_x", "task_a", str(self.repo_path))
        stale_report = self.conflicts.check(str(self.repo_path), "task_a")

        (self.repo_path / "unrelated.txt").write_text("someone else's merge\n")
        _run(["git", "add", "unrelated.txt"], self.repo_path)
        _run(["git", "commit", "-q", "-m", "a different integration landed"], self.repo_path)
        new_main = _run(["git", "rev-parse", "main"], self.repo_path).strip()

        fresh_report = self.conflicts.revalidate(str(self.repo_path), "task_a")

        self.assertNotEqual(fresh_report.base_sha, stale_report.base_sha)
        self.assertEqual(fresh_report.base_sha, new_main)


class IntegrationLockTests(ConflictManagerTestCase):
    def test_second_acquire_is_refused_while_first_holds_the_lock(self):
        self.conflicts.acquire_integration_lock("task_a")
        with self.assertRaises(IntegrationLockHeldError):
            self.conflicts.acquire_integration_lock("task_b")

    def test_lock_becomes_available_after_release(self):
        self.conflicts.acquire_integration_lock("task_a")
        self.conflicts.release_integration_lock("task_a")
        self.conflicts.acquire_integration_lock("task_b")  # must not raise
        self.assertEqual(self.conflicts.current_integration_lock_holder(), "task_b")

    def test_release_by_non_holder_does_not_free_the_lock(self):
        self.conflicts.acquire_integration_lock("task_a")
        self.conflicts.release_integration_lock("task_b")  # not the holder — no-op
        self.assertEqual(self.conflicts.current_integration_lock_holder(), "task_a")

    def test_two_integrations_racing_for_main_are_safely_serialized(self):
        """Real concurrency, not a sequential simulation: two threads
        call acquire_integration_lock() against the same StateManager
        at (as close as Python allows to) the same time. Exactly one
        must win; the loser must see IntegrationLockHeldError, never a
        silently-corrupted or double-held lock."""
        results: dict[str, object] = {}
        barrier = threading.Barrier(2)

        def attempt(task_id: str) -> None:
            barrier.wait()
            try:
                self.conflicts.acquire_integration_lock(task_id)
                results[task_id] = "acquired"
            except IntegrationLockHeldError:
                results[task_id] = "refused"

        t1 = threading.Thread(target=attempt, args=("task_a",))
        t2 = threading.Thread(target=attempt, args=("task_b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        outcomes = sorted(results.values())
        self.assertEqual(outcomes, ["acquired", "refused"])
        holder = self.conflicts.current_integration_lock_holder()
        self.assertIn(holder, ("task_a", "task_b"))
        self.assertEqual(results[holder], "acquired")


if __name__ == "__main__":
    unittest.main()
