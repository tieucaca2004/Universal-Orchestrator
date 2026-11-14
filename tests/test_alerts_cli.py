"""Milestone 7, Component B — Alerts CLI.

Real subprocess invocations of `python3 -m
universal_orchestrator.cli.alerts_cli ...` against a real, disposable
SQLite database, matching tests/test_approval_cli.py's own discipline.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from universal_orchestrator.core.state import StateManager
from universal_orchestrator.core.task_manager import TaskManager
from universal_orchestrator.core.models import TaskSpecification


def _run_cli_subprocess(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    return subprocess.run(
        [sys.executable, "-m", "universal_orchestrator.cli.alerts_cli", "--db", str(db_path), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


class AlertsCLITestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "orchestrator.sqlite3"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_stalled_running_task(self) -> str:
        state = StateManager(self.db_path)
        try:
            tasks = TaskManager(state)
            task = tasks.create_task("t", "proj")
            tasks.attach_specification(task.task_id, TaskSpecification(objective="o", scope="s"))
            tasks.start(task.task_id)
            # Backdate far enough that even the CLI's real default
            # threshold (hours) is exceeded, without touching settings.
            import datetime as dt

            stale_task = tasks.get(task.task_id)
            stale_task.updated_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
            state.update(stale_task)
            return task.task_id
        finally:
            state.close()

    def test_scan_with_no_tasks_reports_no_new_alerts(self):
        StateManager(self.db_path).close()
        result = _run_cli_subprocess(self.db_path, "scan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No new alerts", result.stdout)

    def test_scan_persists_an_alert_list_shows_it_and_it_can_be_acknowledged(self):
        # Each CLI subprocess is its own process: `AlertsCLI.__init__`
        # bootstraps a fresh `OrchestrationEngine`, whose `recover()`
        # immediately flags any RUNNING task left by a *previous*
        # process as INTERRUPTED -- before `scan()` ever runs. So a
        # task created RUNNING in this test's setup, then scanned by a
        # brand-new `alerts_cli` process, is correctly reported as
        # CONFIRMED_INTERRUPTED, not RUNNING_TOO_LONG -- the real,
        # expected end-to-end behavior of a genuinely separate process.
        task_id = self._make_stalled_running_task()

        scan = _run_cli_subprocess(self.db_path, "scan")
        self.assertEqual(scan.returncode, 0, scan.stderr)
        self.assertIn("1 new alert", scan.stdout)
        self.assertIn(task_id, scan.stdout)

        listing = _run_cli_subprocess(self.db_path, "list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn(task_id, listing.stdout)
        self.assertIn("confirmed_interrupted", listing.stdout)

        alert_id = listing.stdout.split("id=")[1].split()[0]

        show = _run_cli_subprocess(self.db_path, "show", alert_id)
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn("resolve_interrupted", show.stdout)
        self.assertIn("Acknowledged   : False", show.stdout)

        ack = _run_cli_subprocess(self.db_path, "acknowledge", alert_id, "--reason", "known slow agent")
        self.assertEqual(ack.returncode, 0, ack.stderr)
        self.assertIn("Acknowledged alert", ack.stdout)

        after = _run_cli_subprocess(self.db_path, "list")
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertIn("No open alerts", after.stdout)

        all_alerts = _run_cli_subprocess(self.db_path, "list", "--all")
        self.assertIn(alert_id, all_alerts.stdout)

    def test_acknowledge_unknown_alert_id_fails_cleanly(self):
        StateManager(self.db_path).close()
        result = _run_cli_subprocess(self.db_path, "acknowledge", "alert_does_not_exist")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
