"""Unit tests for ClaudeCodeAdapter — entirely against a deterministic
fake executable, never the real Claude CLI. See
docs/EXECUTION_CLAUDE_CODE.md for the separate, one-off real smoke test
evidence; these tests must pass with no network access, no API key, and
no installed CLI at all.
"""

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from universal_orchestrator.execution.adapter import (
    ExecutionOutcome,
    ExecutionPolicy,
    ExecutionRequest,
    PermissionMode,
    VerificationStatus,
)
from universal_orchestrator.execution.claude_code import ClaudeCodeAdapter

_FAKE_CLAUDE_SOURCE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import signal
    import sys
    import time

    args = sys.argv[1:]

    if args == ["--version"]:
        print("9.9.9 (Fake Claude)")
        sys.exit(0)

    if args == ["--help"]:
        print(
            "-p, --print\\n--output-format\\n--tools\\n--no-session-persistence\\n"
            "--safe-mode\\n--strict-mcp-config\\n--disable-slash-commands\\n--setting-sources\\n"
        )
        sys.exit(0)

    if len(args) >= 2 and args[0:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": True, "authMethod": "oauth_token"}))
        sys.exit(0)

    # Execution path: argv[0] is always the prompt (adapter puts it first).
    prompt = args[0] if args else ""
    print(json.dumps(args), file=sys.stderr)  # line 1: exact command, for tests
    print(json.dumps(sorted(os.environ.keys())), file=sys.stderr)  # line 2: env keys only

    if prompt.startswith("TRIGGER_NONZERO"):
        print(json.dumps({"is_error": False, "result": "partial"}))
        sys.exit(1)
    elif prompt.startswith("TRIGGER_CLI_ERROR"):
        print(json.dumps({"is_error": True, "result": "cli says no"}))
        sys.exit(0)
    elif prompt.startswith("TRIGGER_INVALID_JSON"):
        print("not json at all {{{")
        sys.exit(0)
    elif prompt.startswith("TRIGGER_EMPTY"):
        sys.exit(0)
    elif prompt.startswith("TRIGGER_SLOW"):
        seconds = float(prompt.split(":", 1)[1]) if ":" in prompt else 5.0
        time.sleep(seconds)
        print(json.dumps({"is_error": False, "result": "late"}))
        sys.exit(0)
    elif prompt.startswith("TRIGGER_IGNORE_SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(30)
        sys.exit(0)
    elif prompt.startswith("TRIGGER_CRASH"):
        sys.exit(139)
    elif prompt.startswith("TRIGGER_UNICODE"):
        count = int(prompt.split(":", 1)[1]) if ":" in prompt else 100
        print(json.dumps({"is_error": False, "result": "x" + ("\\u00e9" * count)}))
        sys.exit(0)
    else:
        print(json.dumps({"is_error": False, "result": f"OK:{prompt}"}))
        sys.exit(0)
    """
)

_UNAUTHENTICATED_CLAUDE_SOURCE = _FAKE_CLAUDE_SOURCE.replace(
    '{"loggedIn": True, "authMethod": "oauth_token"}', '{"loggedIn": False}'
)

_UNSUPPORTED_CLAUDE_SOURCE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import sys
    if sys.argv[1:] == ["--version"]:
        print("0.1.0 (Ancient Claude)")
        sys.exit(0)
    if sys.argv[1:] == ["--help"]:
        print("this version predates --tools and --output-format")
        sys.exit(0)
    sys.exit(0)
    """
)


def _write_fake_executable(tmp_dir: Path, source: str, name: str = "fake_claude") -> str:
    path = tmp_dir / name
    path.write_text(source)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class AdapterTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.sandbox = self.tmp_path / "sandbox"
        self.sandbox.mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _fake_adapter(self, source: str = _FAKE_CLAUDE_SOURCE) -> ClaudeCodeAdapter:
        executable = _write_fake_executable(self.tmp_path, source)
        return ClaudeCodeAdapter(executable=executable)

    def _request(self, input_context: str, **overrides) -> ExecutionRequest:
        defaults = dict(
            task_id="task_x",
            project_id="proj_x",
            agent_id="agent_x",
            provider_id="prov_x",
            agent_role="coder",
            working_directory=str(self.sandbox),
            input_context=input_context,
            timeout_seconds=10.0,
        )
        defaults.update(overrides)
        return ExecutionRequest(**defaults)


class CliDetectionTests(AdapterTestCase):
    def test_cli_installed_and_authenticated_is_available(self):
        adapter = self._fake_adapter()
        self.assertIs(adapter.verify(), VerificationStatus.AVAILABLE)

    def test_missing_executable_is_unavailable(self):
        adapter = ClaudeCodeAdapter(executable=str(self.tmp_path / "does_not_exist"))
        self.assertIs(adapter.verify(), VerificationStatus.UNAVAILABLE)

    def test_bare_name_not_on_path_is_unavailable(self):
        adapter = ClaudeCodeAdapter(executable="definitely-not-a-real-cli-binary")
        self.assertIs(adapter.verify(), VerificationStatus.UNAVAILABLE)

    def test_version_is_captured_after_verify(self):
        adapter = self._fake_adapter()
        adapter.verify()
        self.assertEqual(adapter._cli_version, "9.9.9 (Fake Claude)")

    def test_invalid_executable_that_errors_on_version_is_error(self):
        broken = self.tmp_path / "broken_claude"
        broken.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
        broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
        adapter = ClaudeCodeAdapter(executable=str(broken))
        self.assertIs(adapter.verify(), VerificationStatus.ERROR)

    def test_unsupported_version_missing_required_flags_is_unsupported(self):
        adapter = self._fake_adapter(_UNSUPPORTED_CLAUDE_SOURCE)
        self.assertIs(adapter.verify(), VerificationStatus.UNSUPPORTED)


class AuthenticationTests(AdapterTestCase):
    def test_authenticated_cli_is_available(self):
        adapter = self._fake_adapter()
        self.assertIs(adapter.verify(), VerificationStatus.AVAILABLE)

    def test_unauthenticated_cli_is_unauthenticated(self):
        adapter = self._fake_adapter(_UNAUTHENTICATED_CLAUDE_SOURCE)
        self.assertIs(adapter.verify(), VerificationStatus.UNAUTHENTICATED)

    def test_auth_check_crash_is_error(self):
        source = textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            if sys.argv[1:] == ["--version"]:
                print("9.9.9"); sys.exit(0)
            if sys.argv[1:] == ["--help"]:
                print(
                    "-p, --print --output-format --tools --no-session-persistence "
                    "--safe-mode --strict-mcp-config --disable-slash-commands --setting-sources"
                ); sys.exit(0)
            sys.exit(1)
            """
        )
        adapter = self._fake_adapter(source)
        self.assertIs(adapter.verify(), VerificationStatus.ERROR)

    def test_verify_never_exposes_raw_auth_json(self):
        adapter = self._fake_adapter()
        adapter.verify()
        for value in vars(adapter).values():
            self.assertNotIn("oauth_token", str(value))


class ExecutionTests(AdapterTestCase):
    def test_successful_process(self):
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("hello"))

        self.assertIs(result.outcome, ExecutionOutcome.COMPLETED)
        self.assertTrue(result.success)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.structured_output["result"], "OK:hello")
        self.assertTrue(result.structured_output_valid)

    def test_nonzero_exit_code_is_failed(self):
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("TRIGGER_NONZERO"))

        self.assertIs(result.outcome, ExecutionOutcome.FAILED)
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.error_classification, "nonzero_exit")

    def test_cli_reported_error_with_zero_exit_is_failed(self):
        """Exit code 0 alone is never proof of success — the CLI's own
        `is_error` field overrides it."""
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("TRIGGER_CLI_ERROR"))

        self.assertEqual(result.exit_code, 0)
        self.assertIs(result.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(result.error_classification, "cli_reported_error")

    def test_invalid_structured_output(self):
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("TRIGGER_INVALID_JSON"))

        self.assertFalse(result.structured_output_valid)
        self.assertEqual(result.error_classification, "invalid_json_output")
        self.assertIsNone(result.structured_output)

    def test_empty_output(self):
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("TRIGGER_EMPTY"))

        self.assertEqual(result.error_classification, "empty_output")
        self.assertFalse(result.structured_output_valid)

    def test_timeout(self):
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("TRIGGER_SLOW:5", timeout_seconds=0.5))

        self.assertIs(result.outcome, ExecutionOutcome.TIMED_OUT)
        self.assertIsNotNone(result.duration_seconds)
        self.assertLess(result.duration_seconds, 4.0)

    def test_process_crash(self):
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("TRIGGER_CRASH"))

        self.assertIs(result.outcome, ExecutionOutcome.FAILED)
        self.assertEqual(result.exit_code, 139)

    def test_missing_executable_at_execute_time(self):
        # execute() now runs verify() first (fail closed), which reports
        # UNAVAILABLE for a missing executable before ever reaching a
        # bare "not found" check — see claude_code.py's execute().
        adapter = ClaudeCodeAdapter(executable=str(self.tmp_path / "gone"))
        result = adapter.execute(self._request("hello"))

        self.assertIs(result.outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_classification, "adapter_unavailable")
        self.assertIs(result.verification_status, VerificationStatus.UNAVAILABLE)

    def test_permission_mode_other_than_read_only_fails_closed(self):
        adapter = self._fake_adapter()
        # PermissionMode currently only defines READ_ONLY; simulate a
        # hypothetical future mode to prove execute() refuses it rather
        # than silently running with an unverified policy.
        fake_mode = SimpleNamespace(value="write")
        result = adapter.execute(
            self._request("hello", policy=ExecutionPolicy(permission_mode=fake_mode))
        )

        self.assertIs(result.outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_classification, "unsupported_permission_mode")

    def test_unauthorized_working_directory_fails_closed(self):
        adapter = self._fake_adapter()
        result = adapter.execute(
            self._request("hello", working_directory=str(self.tmp_path / "does_not_exist"))
        )

        self.assertIs(result.outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_classification, "process_start_failed")


class CancellationTests(AdapterTestCase):
    def test_cancel_a_running_process_marks_it_cancelled(self):
        import threading

        adapter = self._fake_adapter()
        request = self._request("TRIGGER_SLOW:5", timeout_seconds=10.0)
        result_holder = {}

        def run():
            result_holder["result"] = adapter.execute(request)

        thread = threading.Thread(target=run)
        thread.start()
        # Give the subprocess a moment to actually start before cancelling.
        import time

        time.sleep(0.5)
        cancelled = adapter.cancel(request.execution_id)
        thread.join(timeout=10)

        self.assertTrue(cancelled)
        self.assertIs(result_holder["result"].outcome, ExecutionOutcome.CANCELLED)

    def test_cancel_an_unknown_execution_id_returns_false(self):
        adapter = self._fake_adapter()
        self.assertFalse(adapter.cancel("exec_does_not_exist"))


class SecurityTests(AdapterTestCase):
    def test_read_only_policy_is_enforced_via_empty_tools_flag(self):
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("hello"))
        argv = json.loads(result.stderr.splitlines()[0])

        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")

    def test_dangerous_permission_bypass_flags_are_never_used(self):
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("hello"))
        argv = json.loads(result.stderr.splitlines()[0])

        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--allow-dangerously-skip-permissions", argv)

    def test_prompt_with_shell_metacharacters_is_passed_literally_not_interpreted(self):
        marker = self.tmp_path / "should_never_exist"
        payload = f"; touch {marker} ; echo pwned #"
        adapter = self._fake_adapter()
        result = adapter.execute(self._request(payload))

        argv = json.loads(result.stderr.splitlines()[0])
        self.assertEqual(argv[0], payload)  # received as one literal argument
        self.assertFalse(marker.exists())  # never ran through a shell

    def test_execute_never_modifies_the_working_directory(self):
        before = sorted(p.name for p in self.sandbox.iterdir())
        adapter = self._fake_adapter()
        adapter.execute(self._request("hello"))
        after = sorted(p.name for p in self.sandbox.iterdir())

        self.assertEqual(before, after)
        self.assertEqual(before, [])

    def test_no_session_persistence_flag_is_present(self):
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("hello"))
        argv = json.loads(result.stderr.splitlines()[0])

        self.assertIn("--no-session-persistence", argv)

    def test_configuration_isolation_flags_are_present(self):
        """Audit remediation, Finding C-1: plugins/hooks/MCP/skills are
        disabled, and only user-level settings load."""
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("hello"))
        argv = json.loads(result.stderr.splitlines()[0])

        self.assertIn("--safe-mode", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--disable-slash-commands", argv)
        self.assertIn("--setting-sources", argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")

    def test_bare_flag_is_never_used(self):
        """`--bare` is deliberately rejected: its own --help text says it
        reads auth strictly from ANTHROPIC_API_KEY/apiKeyHelper and never
        from OAuth or keychain, which would break authentication on an
        OAuth-authenticated host — see the module docstring."""
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("hello"))
        argv = json.loads(result.stderr.splitlines()[0])

        self.assertNotIn("--bare", argv)


class ExecutionTrackingTests(AdapterTestCase):
    def test_each_request_has_a_unique_execution_id_by_default(self):
        r1 = self._request("a")
        r2 = self._request("b")
        self.assertNotEqual(r1.execution_id, r2.execution_id)

    def test_result_carries_task_and_execution_id(self):
        adapter = self._fake_adapter()
        request = self._request("hello")
        result = adapter.execute(request)

        self.assertEqual(result.execution_id, request.execution_id)
        self.assertEqual(result.task_id, request.task_id)

    def test_duplicate_execution_id_does_not_re_run(self):
        adapter = self._fake_adapter()
        request = self._request("TRIGGER_NONZERO", execution_id="exec_fixed")
        first = adapter.execute(request)

        different_request = self._request(
            "hello", execution_id="exec_fixed", task_id=request.task_id
        )
        second = adapter.execute(different_request)

        self.assertIs(second, first)  # cached, never re-run with the new prompt
        self.assertEqual(second.error_classification, "nonzero_exit")

    def test_uncertain_timeout_outcome_is_also_cached_not_retried(self):
        import time

        adapter = self._fake_adapter()
        request = self._request("TRIGGER_SLOW:5", timeout_seconds=0.3, execution_id="exec_unsure")
        first = adapter.execute(request)
        self.assertIs(first.outcome, ExecutionOutcome.TIMED_OUT)

        start = time.monotonic()
        second = adapter.execute(request)
        elapsed = time.monotonic() - start

        self.assertIs(second, first)
        self.assertLess(elapsed, 0.2)  # returned from cache, did not re-run and re-wait


class EnvironmentAllowlistTests(AdapterTestCase):
    def test_unrelated_environment_variables_are_not_inherited(self):
        marker_key = "UO_TEST_UNRELATED_SECRET_12345"
        os.environ[marker_key] = "should-not-be-inherited"
        try:
            adapter = self._fake_adapter()
            result = adapter.execute(self._request("hello"))
        finally:
            del os.environ[marker_key]

        child_env_keys = json.loads(result.stderr.splitlines()[1])
        self.assertNotIn(marker_key, child_env_keys)

    def test_allowlisted_variables_that_are_set_are_inherited(self):
        os.environ["HOME"] = os.environ.get("HOME", "/tmp")
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("hello"))

        child_env_keys = json.loads(result.stderr.splitlines()[1])
        self.assertIn("HOME", child_env_keys)
        self.assertIn("PATH", child_env_keys)

    def test_version_help_and_auth_checks_also_use_the_restricted_environment(self):
        marker_key = "UO_TEST_UNRELATED_SECRET_FOR_VERIFY"
        os.environ[marker_key] = "leak-check"
        captured_envs = []
        real_run = subprocess.run

        def spying_run(*args, **kwargs):
            captured_envs.append(kwargs.get("env"))
            return real_run(*args, **kwargs)

        try:
            executable = _write_fake_executable(self.tmp_path, _FAKE_CLAUDE_SOURCE)
            adapter = ClaudeCodeAdapter(executable=executable, run=spying_run)
            adapter.verify()
        finally:
            del os.environ[marker_key]

        self.assertTrue(captured_envs)
        for env in captured_envs:
            self.assertIsNotNone(env)
            self.assertNotIn(marker_key, env)


class ExecutablePinningTests(AdapterTestCase):
    def test_path_change_after_verify_does_not_change_which_binary_execute_uses(self):
        original = _write_fake_executable(self.tmp_path, _FAKE_CLAUDE_SOURCE, name="claude")
        bin_dir = original.rsplit("/", 1)[0]

        adapter = ClaudeCodeAdapter(executable="claude", run=subprocess.run)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = bin_dir + os.pathsep + old_path
        try:
            self.assertIs(adapter.verify(), VerificationStatus.AVAILABLE)
            pinned_path = adapter._resolved_path
            self.assertEqual(pinned_path, original)

            # Put a DIFFERENT fake "claude" earlier on PATH, simulating a
            # PATH change / hijack attempt after verify() already ran.
            hijack_dir = self.tmp_path / "hijack"
            hijack_dir.mkdir()
            hijack_path = _write_fake_executable(hijack_dir, _UNSUPPORTED_CLAUDE_SOURCE, name="claude")
            os.environ["PATH"] = str(hijack_dir) + os.pathsep + bin_dir + os.pathsep + old_path

            result = adapter.execute(self._request("hello"))
        finally:
            os.environ["PATH"] = old_path

        # Still ran the originally pinned binary, not the newly-PATH-shadowing one.
        self.assertIs(result.outcome, ExecutionOutcome.COMPLETED)
        self.assertEqual(adapter._resolved_path, pinned_path)

    def test_pinned_path_disappearing_fails_closed_without_researching_path(self):
        executable = _write_fake_executable(self.tmp_path, _FAKE_CLAUDE_SOURCE)
        adapter = ClaudeCodeAdapter(executable=executable)
        self.assertIs(adapter.verify(), VerificationStatus.AVAILABLE)

        os.remove(executable)

        self.assertIs(adapter.verify(), VerificationStatus.UNAVAILABLE)
        self.assertIsNone(adapter._resolved_path)


class OutputTruncationTests(AdapterTestCase):
    def test_ascii_output_under_the_limit_is_untouched(self):
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("hello", policy=ExecutionPolicy(max_output_bytes=65536)))
        self.assertEqual(result.structured_output["result"], "OK:hello")

    def test_output_is_bounded_to_the_exact_byte_limit_not_character_count(self):
        # Each "é" is 2 bytes in UTF-8; char-count slicing would keep
        # twice as many bytes as a byte-accurate bound should.
        adapter = self._fake_adapter()
        result = adapter.execute(
            self._request("TRIGGER_UNICODE:500", policy=ExecutionPolicy(max_output_bytes=100))
        )

        self.assertLessEqual(len(result.stdout.encode("utf-8")), 100)
        # No UnicodeDecodeError was raised constructing `result.stdout`
        # (it is already a valid `str`); re-encoding must also succeed.
        result.stdout.encode("utf-8")

    def test_stderr_is_truncated_independently_of_stdout(self):
        adapter = self._fake_adapter()
        result = adapter.execute(
            self._request("hello", policy=ExecutionPolicy(max_output_bytes=20))
        )

        self.assertLessEqual(len(result.stderr.encode("utf-8")), 20)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 20)
        result.stderr.encode("utf-8")


class SignalTerminationTests(AdapterTestCase):
    def test_process_ignoring_sigterm_is_escalated_to_sigkill(self):
        adapter = self._fake_adapter()
        start = time.monotonic()
        result = adapter.execute(self._request("TRIGGER_IGNORE_SIGTERM", timeout_seconds=0.5))
        elapsed = time.monotonic() - start

        self.assertIs(result.outcome, ExecutionOutcome.TIMED_OUT)
        # Must have gone through the full SIGTERM grace period before
        # SIGKILL finally stopped it — proves escalation actually ran,
        # not just that TimeoutExpired was caught.
        self.assertGreaterEqual(elapsed, 3.0)
        self.assertLess(elapsed, 10.0)


class VerificationStatusPopulationTests(AdapterTestCase):
    def test_successful_execution_carries_the_available_status(self):
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("hello"))
        self.assertIs(result.verification_status, VerificationStatus.AVAILABLE)

    def test_unauthenticated_adapter_refuses_to_execute_with_that_status(self):
        adapter = self._fake_adapter(_UNAUTHENTICATED_CLAUDE_SOURCE)
        result = adapter.execute(self._request("hello"))

        self.assertIs(result.outcome, ExecutionOutcome.ERROR)
        self.assertIs(result.verification_status, VerificationStatus.UNAUTHENTICATED)
        self.assertEqual(result.error_classification, "adapter_unauthenticated")


class WorktreeWriteModeTests(AdapterTestCase):
    """M6 completion — WORKTREE_WRITE. `bwrap`-dependent tests are
    skipped (not faked) when the real sandbox binary isn't on this
    host's PATH — real kernel-level containment cannot be honestly
    proven any other way, and this adapter refuses to pretend
    otherwise (see the fail-closed test below, which needs no sandbox
    at all)."""

    def _bwrap_available(self) -> bool:
        import shutil

        return shutil.which("bwrap") is not None

    def test_worktree_write_mode_passes_the_expanded_tools_list(self):
        if not self._bwrap_available():
            self.skipTest("bwrap not installed on this host")
        adapter = self._fake_adapter()
        request = self._request(
            "hello", policy=ExecutionPolicy(permission_mode=PermissionMode.WORKTREE_WRITE)
        )
        result = adapter.execute(request)
        argv = json.loads(result.stderr.splitlines()[0])

        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Write,Edit,Bash")

    def test_worktree_write_mode_really_contains_writes_to_the_worktree_only(self):
        """Not a convention check — a real write attempt outside the
        Worktree, from inside the real sandbox, must really fail at
        the OS level."""
        if not self._bwrap_available():
            self.skipTest("bwrap not installed on this host")

        outside_dir = self.tmp_path / "outside"
        outside_dir.mkdir()
        outside_marker = outside_dir / "should_never_be_written.txt"

        source = textwrap.dedent(
            f"""\
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
                print(json.dumps({{"loggedIn": True}})); sys.exit(0)
            with open("inside_worktree.txt", "w") as f:
                f.write("written inside the worktree\\n")
            outside_blocked = False
            try:
                with open({str(outside_marker)!r}, "w") as f:
                    f.write("should never succeed\\n")
            except OSError:
                outside_blocked = True
            print(json.dumps({{"is_error": False, "result": "done", "outside_blocked": outside_blocked}}))
            sys.exit(0)
            """
        )
        adapter = self._fake_adapter(source)
        request = self._request(
            "hello", policy=ExecutionPolicy(permission_mode=PermissionMode.WORKTREE_WRITE)
        )

        result = adapter.execute(request)

        self.assertIs(result.outcome, ExecutionOutcome.COMPLETED)
        self.assertTrue((self.sandbox / "inside_worktree.txt").exists())
        self.assertTrue(result.structured_output["outside_blocked"])
        self.assertFalse(outside_marker.exists())

    def test_worktree_write_mode_fails_closed_when_sandbox_binary_is_missing(self):
        import universal_orchestrator.execution.claude_code as claude_code_module

        original = claude_code_module._SANDBOX_BIN
        claude_code_module._SANDBOX_BIN = "bwrap-does-not-exist-on-this-host-xyz"
        try:
            adapter = self._fake_adapter()
            request = self._request(
                "hello", policy=ExecutionPolicy(permission_mode=PermissionMode.WORKTREE_WRITE)
            )
            result = adapter.execute(request)
        finally:
            claude_code_module._SANDBOX_BIN = original

        self.assertIs(result.outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_classification, "sandbox_unavailable")
        # Never ran the inner command at all -- no side effect exists.
        self.assertFalse((self.sandbox / "inside_worktree.txt").exists())

    def test_build_sandboxed_command_default_keeps_network_shared(self):
        """Audit Finding C-3 regression test: the default must stay
        unchanged -- `claude` itself needs network access, so
        `allow_network` defaults to `True` and `--unshare-net` is
        never added unless explicitly opted into."""
        import universal_orchestrator.execution.claude_code as claude_code_module

        command = claude_code_module._build_sandboxed_command(["echo", "hi"], str(self.sandbox))
        self.assertNotIn("--unshare-net", command)

    def test_build_sandboxed_command_opt_in_adds_unshare_net(self):
        import universal_orchestrator.execution.claude_code as claude_code_module

        command = claude_code_module._build_sandboxed_command(
            ["echo", "hi"], str(self.sandbox), allow_network=False
        )
        self.assertIn("--unshare-net", command)

    def test_worktree_write_mode_network_isolation_blocks_a_real_outbound_connection(self):
        """Not a convention check -- with `allow_network=False`, a
        real outbound TCP connect attempt from inside the real
        sandbox must really fail at the kernel level (ENETUNREACH),
        exactly as empirically verified for this fix. Skips (rather
        than fails) in an environment with no outbound network at
        all, consistent with this module's own 'must pass with no
        network access' baseline -- that baseline describes the test
        *runner's* needs, not a claim this adapter enforces isolation
        by default."""
        if not self._bwrap_available():
            self.skipTest("bwrap not installed on this host")

        import socket

        try:
            socket.create_connection(("8.8.8.8", 53), timeout=2).close()
        except OSError:
            self.skipTest("no outbound network available in this environment")

        source = textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, socket, sys
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
            try:
                socket.create_connection(("8.8.8.8", 53), timeout=3).close()
                reached = True
            except OSError as e:
                reached = False
            print(json.dumps({"is_error": False, "result": "done", "network_reached": reached}))
            sys.exit(0)
            """
        )
        adapter = self._fake_adapter(source)
        request = self._request(
            "hello",
            policy=ExecutionPolicy(permission_mode=PermissionMode.WORKTREE_WRITE, allow_network=False),
        )

        result = adapter.execute(request)

        self.assertIs(result.outcome, ExecutionOutcome.COMPLETED)
        self.assertFalse(result.structured_output["network_reached"])

    def test_worktree_write_mode_wraps_with_bwrap_and_bypass_permissions_and_scratch_binds(self):
        if not self._bwrap_available():
            self.skipTest("bwrap not installed on this host")
        import unittest.mock as mock

        import universal_orchestrator.execution.claude_code as claude_code_module

        adapter = self._fake_adapter()
        request = self._request(
            "hello", policy=ExecutionPolicy(permission_mode=PermissionMode.WORKTREE_WRITE)
        )
        captured = {}
        real_popen = subprocess.Popen

        def spy_popen(command, **kwargs):
            captured["command"] = command
            return real_popen(command, **kwargs)

        with mock.patch.object(subprocess, "Popen", side_effect=spy_popen):
            adapter.execute(request)

        command = captured["command"]
        self.assertEqual(command[0], "bwrap")
        self.assertIn("--permission-mode", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "bypassPermissions")
        self.assertIn(claude_code_module._CLAUDE_SESSION_ENV_DIR, command)
        self.assertIn(claude_code_module._CLAUDE_TMP_SCRATCH_DIR, command)
        # The Worktree itself is bound read-write, named explicitly.
        self.assertIn(str(self.sandbox), command)

    def test_read_only_mode_is_completely_unaffected_by_worktree_write_existing(self):
        adapter = self._fake_adapter()
        result = adapter.execute(self._request("hello"))  # default policy, unchanged
        argv = json.loads(result.stderr.splitlines()[0])

        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("bwrap", argv)


if __name__ == "__main__":
    unittest.main()
