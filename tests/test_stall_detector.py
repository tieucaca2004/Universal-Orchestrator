import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from universal_orchestrator.config.scheduler_settings import SchedulerSettings
from universal_orchestrator.core.models import TaskError, TaskSpecification, TaskStatus
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.core.stall_detector import StallDetector
from universal_orchestrator.core.task_manager import TaskManager
from universal_orchestrator.execution.quality import QualityReport, QualityVerdict
from universal_orchestrator.execution.scheduler import AlertKind, AlertSeverity
from universal_orchestrator.execution.worktree import WorktreeHandle, WorktreeStatus
from universal_orchestrator.execution.worktree_manager import WorktreeManager


def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], path)
    _run(["git", "config", "user.email", "test@example.com"], path)
    _run(["git", "config", "user.name", "Test"], path)
    (path / "README.md").write_text("original\n")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-q", "-m", "initial"], path)


_SETTINGS = SchedulerSettings(
    running_stall_threshold_seconds=100.0,
    blocked_stall_threshold_seconds=200.0,
    quality_check_stall_threshold_seconds=50.0,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


class StallDetectorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_path = self.tmp_path / "project"
        _init_repo(self.repo_path)
        self.state = StateManager(self.tmp_path / "orchestrator.sqlite3")
        self.tasks = TaskManager(self.state)
        self.worktrees = WorktreeManager(self.state)
        self.detector = StallDetector(self.state, self.tasks, self.worktrees, _SETTINGS)

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def _pass_quality_gate(self, task_id: str) -> None:
        self.state.save_quality_report(
            QualityReport(task_id=task_id, level="task", verdict=QualityVerdict.PASS, rationale="t")
        )

    def _backdate_task(self, task_id: str, when: datetime) -> None:
        task = self.tasks.get(task_id)
        task.updated_at = when
        self.state.update(task)

    def _backdate_worktree(self, task_id: str, when: datetime) -> None:
        handle = self.worktrees.get(task_id)
        handle.updated_at = when
        self.state.update_worktree(handle)

    def _start(self, task_id: str) -> None:
        """CREATED -> RUNNING requires passing through ANALYZED/READY
        first (`core.lifecycle`'s allow-list has no CREATED -> RUNNING
        edge) — this is the real path every caller must take."""
        self.tasks.attach_specification(task_id, TaskSpecification(objective="o", scope="s"))
        self.tasks.start(task_id)


class RunningTooLongTests(StallDetectorTestCase):
    def test_recently_running_task_is_not_flagged(self):
        task = self.tasks.create_task("t", "proj")
        self._start(task.task_id)
        self._backdate_task(task.task_id, _ago(10))
        alerts = self.detector.scan(now=NOW)
        self.assertEqual(alerts, [])

    def test_running_past_threshold_is_flagged_as_recovery_suspected(self):
        task = self.tasks.create_task("t", "proj")
        self._start(task.task_id)
        self._backdate_task(task.task_id, _ago(500))
        alerts = self.detector.scan(now=NOW)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kind, AlertKind.RUNNING_TOO_LONG)
        self.assertEqual(alerts[0].severity, AlertSeverity.WARNING)
        self.assertIn("recovery_suspected: true", alerts[0].detail)

    def test_persisting_condition_is_not_re_alerted_on_second_scan(self):
        task = self.tasks.create_task("t", "proj")
        self._start(task.task_id)
        self._backdate_task(task.task_id, _ago(500))
        first = self.detector.scan(now=NOW)
        second = self.detector.scan(now=NOW + timedelta(seconds=5))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_acknowledged_alert_can_be_raised_again_on_recurrence(self):
        task = self.tasks.create_task("t", "proj")
        self._start(task.task_id)
        self._backdate_task(task.task_id, _ago(500))
        first = self.detector.scan(now=NOW)
        self.state.acknowledge_alert(first[0].alert_id, NOW, "known slow agent")
        second = self.detector.scan(now=NOW + timedelta(seconds=5))
        self.assertEqual(len(second), 1)


class ConfirmedInterruptedTests(StallDetectorTestCase):
    def test_interrupted_task_is_flagged_as_confirmed(self):
        task = self.tasks.create_task("t", "proj")
        self._start(task.task_id)
        self.tasks.recover()  # flips RUNNING -> INTERRUPTED, simulating a restart
        alerts = self.detector.scan(now=NOW)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kind, AlertKind.CONFIRMED_INTERRUPTED)
        self.assertEqual(alerts[0].severity, AlertSeverity.CRITICAL)


class DeadlockAndBlockedChainTests(StallDetectorTestCase):
    def test_deadlocked_task_is_flagged_immediately_regardless_of_age(self):
        # add_dependency() itself refuses to create a cycle (Component A) --
        # construct one directly, the same way
        # test_dependency_graph.py's DiagnoseBlockedTests prove the
        # detection logic handles a graph it would never have built itself.
        a = self.tasks.create_task("a", "proj")
        b = self.tasks.create_task("b", "proj")
        self.tasks.add_dependency(a.task_id, b.task_id)

        task_b = self.tasks.get(b.task_id)
        task_b.depends_on = (a.task_id,)
        self.state.update(task_b)
        self.tasks._tasks[b.task_id] = task_b

        task_a = self.tasks.get(a.task_id)
        task_a.status = TaskStatus.BLOCKED
        self.state.update(task_a)
        self.tasks._tasks[a.task_id] = task_a

        alerts = self.detector.scan(now=NOW)
        kinds = {a.kind for a in alerts}
        self.assertIn(AlertKind.DEADLOCKED, kinds)
        deadlock_alert = next(a for a in alerts if a.kind == AlertKind.DEADLOCKED)
        self.assertEqual(deadlock_alert.severity, AlertSeverity.CRITICAL)

    def test_newly_blocked_task_is_not_flagged(self):
        a = self.tasks.create_task("a", "proj")
        b = self.tasks.create_task("b", "proj")
        self.tasks.add_dependency(a.task_id, b.task_id)
        self.tasks.attach_specification(a.task_id, TaskSpecification(objective="o", scope="s"))
        self._backdate_task(a.task_id, _ago(5))  # just became BLOCKED
        alerts = self.detector.scan(now=NOW)
        self.assertEqual(alerts, [])

    def test_long_blocked_chain_is_flagged_as_soft_signal(self):
        a = self.tasks.create_task("a", "proj")
        b = self.tasks.create_task("b", "proj")
        self.tasks.add_dependency(a.task_id, b.task_id)
        self.tasks.attach_specification(a.task_id, TaskSpecification(objective="o", scope="s"))
        self._backdate_task(a.task_id, _ago(500))
        alerts = self.detector.scan(now=NOW)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kind, AlertKind.BLOCKED_CHAIN)
        self.assertEqual(alerts[0].severity, AlertSeverity.WARNING)


class WorktreePairingTests(StallDetectorTestCase):
    def _acquire(self, task_id: str) -> WorktreeHandle:
        return self.worktrees.acquire("proj", task_id, str(self.repo_path))

    def test_recent_quality_check_is_not_flagged(self):
        task = self.tasks.create_task("t", "proj")
        self._acquire(task.task_id)
        self.worktrees.transition(task.task_id, WorktreeStatus.EXECUTING)
        self.worktrees.transition(task.task_id, WorktreeStatus.QUALITY_CHECK)
        self._backdate_worktree(task.task_id, _ago(5))
        alerts = self.detector.scan(now=NOW)
        self.assertEqual(alerts, [])

    def test_stuck_quality_check_past_threshold_is_flagged(self):
        task = self.tasks.create_task("t", "proj")
        self._acquire(task.task_id)
        self.worktrees.transition(task.task_id, WorktreeStatus.EXECUTING)
        self.worktrees.transition(task.task_id, WorktreeStatus.QUALITY_CHECK)
        self._backdate_worktree(task.task_id, _ago(500))
        alerts = self.detector.scan(now=NOW)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].kind, AlertKind.STUCK_QUALITY_CHECK)

    def test_awaiting_approval_is_never_flagged_no_matter_how_old(self):
        task = self.tasks.create_task("t", "proj")
        self._acquire(task.task_id)
        self.worktrees.transition(task.task_id, WorktreeStatus.EXECUTING)
        self.worktrees.transition(task.task_id, WorktreeStatus.QUALITY_CHECK)
        self.worktrees.transition(task.task_id, WorktreeStatus.AWAITING_APPROVAL)
        self._backdate_worktree(task.task_id, _ago(999999))
        alerts = self.detector.scan(now=NOW)
        self.assertEqual(alerts, [])

    def test_running_task_with_needs_review_worktree_is_flagged_immediately(self):
        task = self.tasks.create_task("t", "proj")
        self._start(task.task_id)
        self._acquire(task.task_id)
        self.worktrees.transition(task.task_id, WorktreeStatus.NEEDS_REVIEW)
        # No backdating at all -- must be flagged immediately regardless of age.
        alerts = self.detector.scan(now=NOW)
        kinds = {a.kind for a in alerts}
        self.assertIn(AlertKind.INCONSISTENT_STATE, kinds)
        inconsistent = next(a for a in alerts if a.kind == AlertKind.INCONSISTENT_STATE)
        self.assertEqual(inconsistent.severity, AlertSeverity.CRITICAL)

    def test_succeeded_task_with_needs_review_worktree_is_not_flagged_inconsistent(self):
        # Worktree NEEDS_REVIEW is valid once the Task is no longer
        # RUNNING (e.g. it already failed its own Quality Gate) --
        # only the RUNNING + NEEDS_REVIEW combination is inconsistent.
        task = self.tasks.create_task("t", "proj")
        self._start(task.task_id)
        self._acquire(task.task_id)
        self.worktrees.transition(task.task_id, WorktreeStatus.NEEDS_REVIEW)
        self.tasks.fail(task.task_id, TaskError(code="x", message="m"))
        alerts = self.detector.scan(now=NOW)
        self.assertEqual([a for a in alerts if a.kind == AlertKind.INCONSISTENT_STATE], [])


if __name__ == "__main__":
    unittest.main()
