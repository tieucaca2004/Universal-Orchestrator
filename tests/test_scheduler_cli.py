"""Section 16-G audit finding: `scheduler_cli.py` had no dedicated
test file at all, unlike `orchestrator_cli.py`/`alerts_cli.py`/
`approval_cli.py`, each of which has a real-subprocess entrypoint
test. This is that same real, `python3 -m`-invocable subprocess
pattern applied to `scheduler_cli run`.
"""

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.models import ProjectProfile, TaskRequest
from universal_orchestrator.core.state import StateManager

_FAKE_CLAUDE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys
    args = sys.argv[1:]
    if args == ["--version"]:
        print("9.9.9 (Fake Claude)"); sys.exit(0)
    if args == ["--help"]:
        print(
            "-p, --print\\n--output-format\\n--tools\\n--no-session-persistence\\n"
            "--safe-mode\\n--strict-mcp-config\\n--disable-slash-commands\\n--setting-sources\\n"
        )
        sys.exit(0)
    if len(args) >= 2 and args[0:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": True})); sys.exit(0)
    prompt = args[0] if args else ""
    open("agent_output.txt", "w").write("fake output\\n")
    print(json.dumps({"is_error": False, "result": f"claude:{prompt}"})); sys.exit(0)
    """
)


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["git", "init", "-q", "-b", "main"], path)
    _run_git(["git", "config", "user.email", "test@example.com"], path)
    _run_git(["git", "config", "user.name", "Test"], path)
    (path / "README.md").write_text("original\n")
    _run_git(["git", "add", "README.md"], path)
    _run_git(["git", "commit", "-q", "-m", "initial"], path)


def _write_fake_claude(tmp_dir: Path) -> str:
    path = tmp_dir / "fake_claude"
    path.write_text(_FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _cli_env() -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    return env


class SchedulerCliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_path = self.tmp_path / "project"
        _init_repo(self.repo_path)
        self.claude_bin = _write_fake_claude(self.tmp_path)
        self.db_path = self.tmp_path / "orchestrator.sqlite3"

        self.project = ProjectProfile(
            name="proj", repository_path=str(self.repo_path), project_id="proj_x",
            acceptance_criteria=["creates agent_output.txt"],
        )
        self.projects_config = self.tmp_path / "projects.json"
        self.projects_config.write_text(json.dumps({"projects": [{
            "project_id": self.project.project_id,
            "name": self.project.name,
            "repository_path": self.project.repository_path,
        }]}))

    def tearDown(self):
        self._tmpdir.cleanup()

    def _create_ready_task(self, title: str = "scheduler cli entrypoint test") -> str:
        """Leaves the task READY and unrouted -- the real Scheduler
        process started below must do its own routing and dispatch,
        exactly as it would for a task a human submitted without
        ever invoking `orchestrator_cli submit` (which always
        dispatches synchronously itself, bypassing the Scheduler)."""
        state = StateManager(self.db_path)
        try:
            engine = OrchestrationEngine.bootstrap(state)
            task = engine.submit(
                TaskRequest(
                    title=title,
                    metadata={"acceptance_criteria": ["creates agent_output.txt"]},
                ),
                self.project,
            )
            return task.task_id
        finally:
            state.close()


class RealEntrypointTests(SchedulerCliTestCase):
    def test_run_help_exits_cleanly(self):
        result = subprocess.run(
            [sys.executable, "-m", "universal_orchestrator.cli.scheduler_cli", "run", "--help"],
            capture_output=True, text=True, env=_cli_env(), timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--dispatch-timeout", result.stdout)
        self.assertIn("--drain-timeout", result.stdout)

    def test_run_dispatches_a_real_ready_task_and_shuts_down_on_sigterm(self):
        """No --criteria-evidence-provider hook is exposed at the CLI
        layer (an honest, documented Scheduler limitation -- see
        core/scheduler.py's own module docstring), so an automatically
        dispatched task with declared acceptance_criteria always ends
        FAILED/NEEDS_REVIEW, never SUCCEEDED, exactly like
        test_scheduler.py's NoEvidenceEndsFailedTests. This test's
        job is proving the real subprocess entrypoint actually wires
        up and runs a real dispatch end-to-end, not that it reaches
        SUCCEEDED."""
        task_id = self._create_ready_task()

        proc = subprocess.Popen(
            [
                sys.executable, "-m", "universal_orchestrator.cli.scheduler_cli",
                "--db", str(self.db_path), "run",
                "--projects-config", str(self.projects_config),
                "--adapter", "claude", "--adapter-target", self.claude_bin,
                "--max-concurrency", "1", "--drain-timeout", "5",
            ],
            cwd=str(self.tmp_path), env=_cli_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 15.0
            terminal_status = None
            while time.monotonic() < deadline:
                state = StateManager(self.db_path)
                try:
                    task = state.get(task_id)
                    if task is not None and task.status.value in ("succeeded", "failed"):
                        terminal_status = task.status.value
                        break
                finally:
                    state.close()
                time.sleep(0.2)
        finally:
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=10)

        self.assertIsNotNone(terminal_status, f"task never reached a terminal state. stdout={out!r} stderr={err!r}")
        self.assertEqual(proc.returncode, 0, f"stdout={out!r} stderr={err!r}")
        self.assertIn("Scheduler stopped.", out)
        self.assertEqual(terminal_status, "failed")  # no evidence provider -- expected, see docstring above

        state = StateManager(self.db_path)
        try:
            results = state.get_execution_results(task_id)
            self.assertEqual(len(results), 1)  # a real dispatch really happened
            self.assertEqual(results[0].outcome.value, "completed")
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()
