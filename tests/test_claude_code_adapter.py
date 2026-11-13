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
    import sys
    import time

    args = sys.argv[1:]

    if args == ["--version"]:
        print("9.9.9 (Fake Claude)")
        sys.exit(0)

    if args == ["--help"]:
        print("-p, --print\\n--output-format\\n--tools\\n--no-session-persistence\\n")
        sys.exit(0)

    if len(args) >= 2 and args[0:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": True, "authMethod": "oauth_token"}))
        sys.exit(0)

    # Execution path: argv[0] is always the prompt (adapter puts it first).
    prompt = args[0] if args else ""
    print(json.dumps(args), file=sys.stderr)  # lets tests inspect the exact command

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
    elif prompt.startswith("TRIGGER_CRASH"):
        sys.exit(139)
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
                print("-p, --print --output-format --tools --no-session-persistence"); sys.exit(0)
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
        adapter = ClaudeCodeAdapter(executable=str(self.tmp_path / "gone"))
        result = adapter.execute(self._request("hello"))

        self.assertIs(result.outcome, ExecutionOutcome.ERROR)
        self.assertEqual(result.error_classification, "executable_not_found")

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


if __name__ == "__main__":
    unittest.main()
