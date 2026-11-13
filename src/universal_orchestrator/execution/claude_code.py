"""Claude Code CLI Adapter — the first concrete `ExecutionAdapter`.

Everything this module assumes about the CLI was verified against the
actual installed binary on the development host (Claude Code 2.1.221,
`/usr/local/bin/claude`) before being written — see
docs/EXECUTION_CLAUDE_CODE.md for the exact commands run and their real
output. Nothing here is invented from memory of an older CLI version.

Security posture (read this before changing the policy): Milestone 4
implements exactly one `PermissionMode`, `READ_ONLY`, as `--tools ""` —
every built-in tool disabled. This was chosen over an allow-list such as
`--allowedTools Read,Grep,Glob` because this repository has not verified
the exact current tool-name list for this CLI version, and guessing
wrong could under- or over-restrict; `--tools ""` is unambiguous and
directly documented by `claude --help`. `execute()` refuses to run (fails
closed, `ExecutionOutcome.ERROR`) for any other `PermissionMode`, because
none other has been implemented or verified yet. `--dangerously-skip-
permissions` and `--allow-dangerously-skip-permissions` are never used.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from universal_orchestrator.execution.adapter import (
    ExecutionAdapter,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionResult,
    PermissionMode,
    VerificationStatus,
)

# Substrings `claude --help` must contain for the flags this adapter
# relies on. Checked literally, not merely assumed present, every time
# verify() runs — see "Inspect actual supported command-line options".
_REQUIRED_HELP_MARKERS = (
    "-p, --print",
    "--output-format",
    "--tools",
    "--no-session-persistence",
)

_AUTH_CHECK_TIMEOUT_SECONDS = 15
_HELP_CHECK_TIMEOUT_SECONDS = 10
_VERSION_CHECK_TIMEOUT_SECONDS = 10
_PROCESS_GROUP_GRACE_SECONDS = 3


class ClaudeCodeAdapter(ExecutionAdapter):
    def __init__(self, executable: str = "claude", *, run=subprocess.run) -> None:
        self._executable = executable
        # Injectable for deterministic unit tests against a fake script —
        # never swapped out for real executions.
        self._run = run
        self._cli_version: str | None = None
        self._running: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._completed: dict[str, ExecutionResult] = {}

    # -- availability verification --------------------------------------------

    def verify(self) -> VerificationStatus:
        resolved = self._resolve_executable()
        if resolved is None:
            return VerificationStatus.UNAVAILABLE

        version = self._check_version(resolved)
        if version is None:
            return VerificationStatus.ERROR
        self._cli_version = version

        if not self._check_supported_options(resolved):
            return VerificationStatus.UNSUPPORTED

        authenticated = self._check_authenticated(resolved)
        if authenticated is None:
            return VerificationStatus.ERROR
        if not authenticated:
            return VerificationStatus.UNAUTHENTICATED

        return VerificationStatus.AVAILABLE

    def _resolve_executable(self) -> str | None:
        if "/" in self._executable:
            path = Path(self._executable)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
            return None
        return shutil.which(self._executable)

    def _check_version(self, resolved: str) -> str | None:
        try:
            result = self._run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=_VERSION_CHECK_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        version = result.stdout.strip()
        return version or None

    def _check_supported_options(self, resolved: str) -> bool:
        try:
            result = self._run(
                [resolved, "--help"],
                capture_output=True,
                text=True,
                timeout=_HELP_CHECK_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False
        help_text = result.stdout
        return all(marker in help_text for marker in _REQUIRED_HELP_MARKERS)

    def _check_authenticated(self, resolved: str) -> bool | None:
        """Returns True/False for logged-in state, or None if the check
        itself failed. Only ever inspects the `loggedIn` boolean — the
        rest of the JSON (`authMethod`, `apiProvider`) is never logged,
        stored, or returned by this adapter."""
        try:
            result = self._run(
                [resolved, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=_AUTH_CHECK_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            status = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        logged_in = status.get("loggedIn")
        return bool(logged_in) if isinstance(logged_in, bool) else None

    # -- execution --------------------------------------------------------------

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.execution_id in self._completed:
            # Same execution_id requested again: return the cached result
            # rather than re-running something whose first outcome is
            # already known. This only holds within this adapter
            # instance's lifetime — see docs/EXECUTION_CLAUDE_CODE.md
            # "Known limitations" for what that does and doesn't cover.
            return self._completed[request.execution_id]

        if request.policy.permission_mode is not PermissionMode.READ_ONLY:
            result = ExecutionResult(
                execution_id=request.execution_id,
                task_id=request.task_id,
                outcome=ExecutionOutcome.ERROR,
                error_classification="unsupported_permission_mode",
                stderr=(
                    f"permission mode '{request.policy.permission_mode}' is not "
                    "implemented by this adapter; failing closed rather than "
                    "running with an unverified policy"
                ),
            )
            self._completed[request.execution_id] = result
            return result

        resolved = self._resolve_executable()
        if resolved is None:
            result = ExecutionResult(
                execution_id=request.execution_id,
                task_id=request.task_id,
                outcome=ExecutionOutcome.ERROR,
                error_classification="executable_not_found",
            )
            self._completed[request.execution_id] = result
            return result

        # Argument order matters: `--tools` is variadic and will swallow
        # the prompt text if the prompt comes after it — verified against
        # the real CLI. The prompt always comes first.
        command = [
            resolved,
            request.input_context,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
        ]

        started_at = datetime.now(timezone.utc)
        start_perf = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                cwd=request.working_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            result = ExecutionResult(
                execution_id=request.execution_id,
                task_id=request.task_id,
                outcome=ExecutionOutcome.ERROR,
                error_classification="process_start_failed",
                stderr=str(exc),
                started_at=started_at,
                ended_at=datetime.now(timezone.utc),
                cli_version=self._cli_version,
            )
            self._completed[request.execution_id] = result
            return result

        self._running[request.execution_id] = process
        try:
            try:
                stdout, stderr = process.communicate(timeout=request.timeout_seconds)
                outcome = ExecutionOutcome.CANCELLED if request.execution_id in self._cancelled else None
                exit_code = process.returncode
                timed_out = False
            except subprocess.TimeoutExpired:
                self._terminate_process_group(process)
                stdout, stderr = process.communicate()
                exit_code = process.returncode
                timed_out = True
                outcome = (
                    ExecutionOutcome.CANCELLED
                    if request.execution_id in self._cancelled
                    else ExecutionOutcome.TIMED_OUT
                )
        finally:
            self._running.pop(request.execution_id, None)
            self._cancelled.discard(request.execution_id)

        ended_at = datetime.now(timezone.utc)
        duration = time.monotonic() - start_perf

        max_bytes = request.policy.max_output_bytes
        stdout = stdout[:max_bytes]
        stderr = stderr[:max_bytes]

        if outcome is None:
            outcome, error_classification, structured_output, structured_output_valid = (
                _classify_completed_process(exit_code, stdout)
            )
        else:
            error_classification = "timed_out" if timed_out else "cancelled"
            structured_output = None
            structured_output_valid = None

        result = ExecutionResult(
            execution_id=request.execution_id,
            task_id=request.task_id,
            outcome=outcome,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration,
            cli_version=self._cli_version,
            output_format="json",
            structured_output=structured_output,
            structured_output_valid=structured_output_valid,
            error_classification=error_classification,
        )
        self._completed[request.execution_id] = result
        return result

    def cancel(self, execution_id: str) -> bool:
        """Best-effort, cooperative cancellation: sends SIGTERM (then
        SIGKILL if needed) to the running process group for
        `execution_id`. Returns False if nothing matching is currently
        running — including if it already finished."""
        process = self._running.get(execution_id)
        if process is None or process.poll() is not None:
            return False
        self._cancelled.add(execution_id)
        self._terminate_process_group(process)
        return True

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen) -> None:
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + _PROCESS_GROUP_GRACE_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.1)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _classify_completed_process(
    exit_code: int, stdout: str
) -> tuple[ExecutionOutcome, str | None, dict | None, bool | None]:
    if not stdout.strip():
        return ExecutionOutcome.FAILED if exit_code != 0 else ExecutionOutcome.COMPLETED, (
            "empty_output"
        ), None, False

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        outcome = ExecutionOutcome.FAILED if exit_code != 0 else ExecutionOutcome.COMPLETED
        return outcome, "invalid_json_output", None, False

    if exit_code != 0:
        return ExecutionOutcome.FAILED, "nonzero_exit", parsed, True
    if isinstance(parsed, dict) and parsed.get("is_error") is True:
        return ExecutionOutcome.FAILED, "cli_reported_error", parsed, True

    return ExecutionOutcome.COMPLETED, None, parsed, True
