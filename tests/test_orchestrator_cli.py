"""M6 remediation (round 2), Item 3 — real entrypoint-level test.

Every step here is a *separate, real subprocess* invocation of the
actual command-line entry points
(`python3 -m universal_orchestrator.cli.orchestrator_cli ...` and
`...approval_cli ...`) — never a direct Python construction of
`GovernedPipeline`, never a standalone demonstration script. This is
the proof that the real application entrypoint, not merely a wrapper
class a unit test happens to call, drives the M6 pipeline end to end.
"""

import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

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
    open("agent_output.txt", "w").write("fake agent output\\n")
    print(json.dumps({"is_error": False, "result": f"claude:{prompt}"})); sys.exit(0)
    """
)


def _write_fake_claude(tmp_dir: Path) -> str:
    path = tmp_dir / "fake_claude"
    path.write_text(_FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


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


def _cli_env() -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    return env


def _run_orchestrator_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "universal_orchestrator.cli.orchestrator_cli", "--db", str(db_path), *args],
        capture_output=True, text=True, env=_cli_env(), timeout=30,
    )


def _run_approval_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "universal_orchestrator.cli.approval_cli", "--db", str(db_path), *args],
        capture_output=True, text=True, env=_cli_env(), timeout=30,
    )


def _extract_task_id(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("TASK_ID="):
            return line.split("=", 1)[1]
    raise AssertionError(f"TASK_ID= not found in stdout: {stdout!r}")


class OrchestratorEntrypointTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_path = self.tmp_path / "project"
        _init_repo(self.repo_path)
        self.claude_bin = _write_fake_claude(self.tmp_path)
        self.db_path = self.tmp_path / "orchestrator.sqlite3"
        # The CLI itself opens/creates the database on first use.

    def tearDown(self):
        self._tmpdir.cleanup()


class RealEntrypointHappyPathTest(OrchestratorEntrypointTestCase):
    def test_submit_propose_approve_integrate_all_via_real_subprocess_commands(self):
        submit = _run_orchestrator_cli(
            self.db_path, "submit",
            "--repo", str(self.repo_path), "--project-id", "proj_x", "--title", "entrypoint test",
            "--criterion", "creates agent_output.txt",
            "--adapter", "claude", "--adapter-target", self.claude_bin,
        )
        self.assertEqual(submit.returncode, 0, submit.stderr)
        self.assertIn("EXECUTION_OUTCOME=completed", submit.stdout)
        task_id = _extract_task_id(submit.stdout)

        # The real dispatch really happened through GovernedPipeline --
        # verify from the persisted state, not from the subprocess's own
        # claim.
        state = StateManager(self.db_path)
        try:
            results = state.get_execution_results(task_id)
            self.assertEqual(len(results), 1)
            handle = state.get_worktree(task_id)
            self.assertIsNotNone(handle)
            self.assertEqual(handle.status.value, "quality_check")
        finally:
            state.close()

        propose = _run_orchestrator_cli(
            self.db_path, "propose", task_id,
            "--repo", str(self.repo_path), "--project-id", "proj_x",
            "--evidence", "creates agent_output.txt=true",
        )
        self.assertEqual(propose.returncode, 0, propose.stderr)
        self.assertIn("PROPOSAL diff_identity=", propose.stdout)

        approve = _run_approval_cli(self.db_path, "approve", task_id, "--reason", "entrypoint test")
        self.assertEqual(approve.returncode, 0, approve.stderr)
        self.assertIn("APPROVED", approve.stdout)

        integrate = _run_orchestrator_cli(
            self.db_path, "integrate", task_id,
            "--repo", str(self.repo_path), "--project-id", "proj_x",
            "--required-test", "python3 -c \"import sys; sys.exit(0)\"",
        )
        self.assertEqual(integrate.returncode, 0, integrate.stderr)
        self.assertIn("POST_INTEGRATION_VERDICT=PASS", integrate.stdout)

        # The real target branch really has the Agent's real file.
        self.assertTrue((self.repo_path / "agent_output.txt").exists())
        state = StateManager(self.db_path)
        try:
            handle = state.get_worktree(task_id)
            self.assertEqual(handle.status.value, "released")
            task = state.get(task_id)
            self.assertEqual(task.status.value, "succeeded")
        finally:
            state.close()


class RealEntrypointBypassPreventionTest(OrchestratorEntrypointTestCase):
    def test_integrate_via_real_cli_fails_without_a_real_approval(self):
        submit = _run_orchestrator_cli(
            self.db_path, "submit",
            "--repo", str(self.repo_path), "--project-id", "proj_x", "--title", "no approval test",
            "--criterion", "creates agent_output.txt",
            "--adapter", "claude", "--adapter-target", self.claude_bin,
        )
        task_id = _extract_task_id(submit.stdout)
        _run_orchestrator_cli(
            self.db_path, "propose", task_id,
            "--repo", str(self.repo_path), "--project-id", "proj_x",
            "--evidence", "creates agent_output.txt=true",
        )

        # No approval step at all -- the real CLI's integrate must fail.
        integrate = _run_orchestrator_cli(
            self.db_path, "integrate", task_id,
            "--repo", str(self.repo_path), "--project-id", "proj_x",
        )
        self.assertNotEqual(integrate.returncode, 0)
        self.assertIn("ApprovalRequiredError", integrate.stderr)
        self.assertFalse((self.repo_path / "agent_output.txt").exists())

    def test_propose_via_real_cli_fails_without_sufficient_evidence(self):
        submit = _run_orchestrator_cli(
            self.db_path, "submit",
            "--repo", str(self.repo_path), "--project-id", "proj_x", "--title", "no evidence test",
            "--criterion", "creates agent_output.txt",
            "--adapter", "claude", "--adapter-target", self.claude_bin,
        )
        task_id = _extract_task_id(submit.stdout)

        propose = _run_orchestrator_cli(
            self.db_path, "propose", task_id,
            "--repo", str(self.repo_path), "--project-id", "proj_x",
        )  # no --evidence at all

        self.assertEqual(propose.returncode, 1)
        self.assertIn("did not PASS", propose.stdout)

        state = StateManager(self.db_path)
        try:
            self.assertEqual(state.get(task_id).status.value, "failed")
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()
