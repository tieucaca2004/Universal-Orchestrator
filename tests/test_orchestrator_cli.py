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

from universal_orchestrator.cli.orchestrator_cli import (
    PathTraversalError,
    _safe_worktree_target,
    _write_agent_file,
    cmd_extract_claude_files,
)
from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.models import ProjectProfile, TaskRequest
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.execution.adapter import ExecutionOutcome, ExecutionResult

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


class RealEntrypointWorktreeWriteTest(OrchestratorEntrypointTestCase):
    """M6 completion, Item 1/8 — the real CLI actually wires
    `--permission-mode worktree_write` through to a real, sandboxed
    dispatch (the sandbox's own real containment is proven at the
    adapter unit-test level, tests/test_claude_code_adapter.py;
    here the entrypoint-level concern is that the flag really reaches
    the real pipeline and that two concurrent tasks each get their
    own, non-shared Worktree)."""

    def _bwrap_available(self) -> bool:
        import shutil

        return shutil.which("bwrap") is not None

    def test_submit_with_worktree_write_dispatches_through_the_sandbox(self):
        if not self._bwrap_available():
            self.skipTest("bwrap not installed on this host")
        submit = _run_orchestrator_cli(
            self.db_path, "submit",
            "--repo", str(self.repo_path), "--project-id", "proj_x", "--title", "worktree write test",
            "--criterion", "creates agent_output.txt",
            "--adapter", "claude", "--adapter-target", self.claude_bin,
            "--permission-mode", "worktree_write",
        )
        self.assertEqual(submit.returncode, 0, submit.stderr)
        self.assertIn("EXECUTION_OUTCOME=completed", submit.stdout)
        self.assertIn("permission_mode=worktree_write", submit.stdout)

        worktree_path = None
        for line in submit.stdout.splitlines():
            if line.startswith("WORKTREE_PATH="):
                worktree_path = line.split("=", 1)[1]
        self.assertIsNotNone(worktree_path)
        self.assertTrue((Path(worktree_path) / "agent_output.txt").exists())

    def test_two_concurrent_worktree_write_tasks_get_isolated_worktrees(self):
        if not self._bwrap_available():
            self.skipTest("bwrap not installed on this host")

        def submit_one(title: str) -> str:
            result = _run_orchestrator_cli(
                self.db_path, "submit",
                "--repo", str(self.repo_path), "--project-id", "proj_x", "--title", title,
                "--criterion", "creates agent_output.txt",
                "--adapter", "claude", "--adapter-target", self.claude_bin,
                "--permission-mode", "worktree_write",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for line in result.stdout.splitlines():
                if line.startswith("WORKTREE_PATH="):
                    return line.split("=", 1)[1]
            raise AssertionError("WORKTREE_PATH= not found")

        worktree_a = submit_one("agent A task")
        worktree_b = submit_one("agent B task")

        self.assertNotEqual(worktree_a, worktree_b)
        self.assertTrue((Path(worktree_a) / "agent_output.txt").exists())
        self.assertTrue((Path(worktree_b) / "agent_output.txt").exists())
        # Neither execution's working tree holds anything beyond what
        # that same execution itself wrote -- no cross-contamination.
        self.assertEqual(
            sorted(p.name for p in Path(worktree_a).glob("*") if not p.name.startswith(".git")),
            sorted(p.name for p in Path(worktree_b).glob("*") if not p.name.startswith(".git")),
        )


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


class SafeWorktreeTargetTests(unittest.TestCase):
    """Audit Finding C-2 regression tests: `extract-claude-files`
    writes an Agent's own, untrusted text reply into real files. Every
    case here would have written outside `self.root` under the
    pre-fix code, which did `worktree_path / relative_name` with no
    validation at all."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self._tmpdir.name)
        self.root = self.base / "worktree"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_simple_traversal_is_rejected(self):
        with self.assertRaises(PathTraversalError):
            _safe_worktree_target(self.root, "../outside.txt")

    def test_nested_traversal_is_rejected(self):
        with self.assertRaises(PathTraversalError):
            _safe_worktree_target(self.root, "a/../../outside.txt")

    def test_absolute_path_is_rejected(self):
        with self.assertRaises(PathTraversalError):
            _safe_worktree_target(self.root, str(self.outside / "x.txt"))

    def test_windows_style_traversal_is_rejected(self):
        with self.assertRaises(PathTraversalError):
            _safe_worktree_target(self.root, "..\\..\\outside.txt")

    def test_empty_or_whitespace_name_is_rejected(self):
        with self.assertRaises(PathTraversalError):
            _safe_worktree_target(self.root, "   ")

    def test_leading_dot_slash_is_harmless_and_normalized(self):
        # pathlib itself drops a leading "./" -- this never reaches
        # the traversal check at all and lands safely inside root,
        # exactly like a bare "x.txt" would.
        target = _safe_worktree_target(self.root, "./x.txt")
        self.assertEqual(target, self.root.resolve() / "x.txt")

    def test_encoded_traversal_is_treated_as_a_literal_harmless_name(self):
        # No percent-decoding happens anywhere in this pipeline, so an
        # encoded traversal attempt is just an odd file name, still
        # strictly inside the Worktree -- never a bypass.
        target = _safe_worktree_target(self.root, "%2e%2e%2foutside.txt")
        self.assertEqual(target.parent, self.root.resolve())

    def test_valid_nested_file_is_allowed(self):
        target = _safe_worktree_target(self.root, "src/pkg/module.py")
        self.assertEqual(target, self.root.resolve() / "src" / "pkg" / "module.py")

    def test_symlinked_directory_pointing_outside_is_rejected(self):
        (self.root / "escape").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(PathTraversalError):
            _safe_worktree_target(self.root, "escape/x.txt")

    def test_chained_symlinks_pointing_outside_is_rejected(self):
        far = self.base / "far"
        far.mkdir()
        (self.outside / "hop2").symlink_to(far, target_is_directory=True)
        (self.root / "hop1").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(PathTraversalError):
            _safe_worktree_target(self.root, "hop1/hop2/x.txt")

    def test_preexisting_symlink_pointing_outside_at_the_final_component_is_rejected(self):
        (self.root / "evil.txt").symlink_to(self.outside / "secret.txt")
        with self.assertRaises(PathTraversalError):
            _safe_worktree_target(self.root, "evil.txt")

    def test_write_refuses_to_follow_a_preexisting_symlink_even_if_it_points_inside(self):
        real_file = self.root / "real.txt"
        real_file.write_text("original\n")
        link = self.root / "link.txt"
        link.symlink_to(real_file)
        with self.assertRaises(OSError):
            _write_agent_file(link, "attacker-controlled\n")
        self.assertEqual(real_file.read_text(), "original\n")

    def test_overwriting_an_existing_regular_file_succeeds(self):
        target = _safe_worktree_target(self.root, "file.txt")
        _write_agent_file(target, "v1\n")
        target_again = _safe_worktree_target(self.root, "file.txt")
        _write_agent_file(target_again, "v2\n")
        self.assertEqual(target.read_text(), "v2\n")

    def test_valid_nested_write_creates_intermediate_directories(self):
        target = _safe_worktree_target(self.root, "a/b/c.txt")
        _write_agent_file(target, "hello\n")
        self.assertEqual(target.read_text(), "hello\n")


class ExtractClaudeFilesAdversarialTests(unittest.TestCase):
    """Drives the real `cmd_extract_claude_files` CLI handler
    in-process against a malicious Agent reply and proves no file is
    ever created outside the Worktree, and nothing at all is written
    on rejection (all-or-nothing)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self._tmpdir.name)
        self.db_path = self.base / "orchestrator.sqlite3"
        self.worktree = self.base / "worktree"
        _init_repo(self.worktree)  # a real git repo: the benign case must `git add`/`commit` for real
        (self.base / "outside_marker.txt").write_text("should never be touched\n")

        self.state = StateManager(self.db_path)
        engine = OrchestrationEngine.bootstrap(self.state)
        self.task = engine.submit(
            TaskRequest(title="extract test"), ProjectProfile(name="p", repository_path=str(self.worktree))
        )

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def _record_reply(self, reply_text: str) -> None:
        self.state.save_execution_result(
            ExecutionResult(
                execution_id="exec_1", task_id=self.task.task_id,
                outcome=ExecutionOutcome.COMPLETED, exit_code=0,
                structured_output={"result": reply_text},
            )
        )

    def _args(self):
        import argparse
        return argparse.Namespace(
            db=str(self.db_path), task_id=self.task.task_id, worktree=str(self.worktree)
        )

    def test_malicious_reply_writes_nothing_outside_and_returns_nonzero(self):
        self._record_reply(
            "=== FILE: notes.txt ===\nfine\n"
            "=== FILE: ../../outside_marker.txt ===\nPWNED\n"
        )
        rc = cmd_extract_claude_files(self._args())
        self.assertEqual(rc, 1)
        self.assertEqual((self.base / "outside_marker.txt").read_text(), "should never be touched\n")
        self.assertFalse((self.worktree / "notes.txt").exists())  # all-or-nothing: good block not written either

    def test_benign_reply_writes_exactly_the_declared_files(self):
        self._record_reply("=== FILE: pkg/mod.py ===\nprint('hi')\n")
        rc = cmd_extract_claude_files(self._args())
        self.assertEqual(rc, 0)
        self.assertEqual((self.worktree / "pkg" / "mod.py").read_text(), "print('hi')\n")


if __name__ == "__main__":
    unittest.main()
