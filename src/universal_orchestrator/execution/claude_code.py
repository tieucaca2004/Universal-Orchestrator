"""Claude Code CLI Adapter — the first concrete `ExecutionAdapter`.

Everything this module assumes about the CLI was verified against the
actual installed binary on the development host (Claude Code 2.1.221,
`/usr/local/bin/claude`) before being written — see
docs/EXECUTION_CLAUDE_CODE.md for the exact commands run and their real
output, including the M4 audit remediation's verification steps.
Nothing here is invented from memory of an older CLI version.

Security posture — read docs/EXECUTION_CLAUDE_CODE.md's "Security
policy" section for the full, honest boundary of what this enforces.
Summary: `execute()` disables built-in tools (`--tools ""`), disables
CLAUDE.md/skills/plugins/hooks/MCP servers/custom commands and agents
(`--safe-mode`), independently forces zero MCP servers
(`--strict-mcp-config`), independently disables skills again
(`--disable-slash-commands`), and restricts which settings files load
to user-level only (`--setting-sources user`) — excluding whatever a
task's `working_directory` might itself contain. `--bare` is
deliberately never used: its own `--help` text states it reads
authentication strictly from `ANTHROPIC_API_KEY`/`apiKeyHelper` and
never from OAuth or keychain, which would break authentication on an
OAuth-authenticated host — a real, verified incompatibility, not an
assumption. `--dangerously-skip-permissions` and
`--allow-dangerously-skip-permissions` are never used.

None of this is filesystem, process, or network sandboxing — it is CLI
configuration only, verified against this one CLI version's documented
flags. See "Known limitations" in the docs for exactly what is and is
not covered.
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
# Extended in the M4 audit remediation to cover the configuration-
# isolation flags added for Finding C-1.
_REQUIRED_HELP_MARKERS = (
    "-p, --print",
    "--output-format",
    "--tools",
    "--no-session-persistence",
    "--safe-mode",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--setting-sources",
)

# The configuration-isolation flags applied to every real execution.
# `--bare` is deliberately excluded — see module docstring.
_ISOLATION_FLAGS = (
    "--safe-mode",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--setting-sources",
    "user",
)

# Minimal explicit environment allowlist — never the full parent
# environment. Empirically verified (M4 audit remediation) that
# `claude auth status` still reports `loggedIn: true` for this host's
# OAuth authentication with only these keys present; authentication is
# settings-file-driven (read by the `claude` process itself from
# `~/.claude`), not dependent on any credential being in this process's
# environment. A host that instead authenticates via an exported
# `ANTHROPIC_API_KEY` or similar shell variable would need that
# variable added here — `verify()` will correctly report
# `UNAUTHENTICATED` rather than silently failing if it's missing.
_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")

_AUTH_CHECK_TIMEOUT_SECONDS = 15
_HELP_CHECK_TIMEOUT_SECONDS = 10
_VERSION_CHECK_TIMEOUT_SECONDS = 10
_PROCESS_GROUP_GRACE_SECONDS = 3

_VERIFICATION_ERROR_CLASSIFICATION = {
    VerificationStatus.UNAVAILABLE: "adapter_unavailable",
    VerificationStatus.UNAUTHENTICATED: "adapter_unauthenticated",
    VerificationStatus.UNSUPPORTED: "adapter_unsupported",
    VerificationStatus.ERROR: "adapter_verification_error",
}


def _build_subprocess_env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}


def _truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    """Bound `text` to at most `max_bytes` bytes of UTF-8, never
    producing invalid Unicode. A character-count slice (`text[:n]`)
    would under- or over-count real bytes for any non-ASCII content;
    this encodes first, slices the real byte string, and decodes back,
    dropping a partial trailing multi-byte sequence rather than raising
    or emitting replacement characters."""
    encoded = text.encode("utf-8", errors="surrogateescape")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


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
        # Pinned on first successful resolution — see `_pinned_executable`.
        # Never re-searched via PATH after that, closing the PATH-hijack
        # window between verify() and execute() (audit Finding, Priority 3).
        self._resolved_path: str | None = None

    # -- availability verification --------------------------------------------

    def verify(self) -> VerificationStatus:
        resolved = self._pinned_executable()
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
        """One-time PATH/absolute-path lookup. Called only from
        `_pinned_executable` the first time; never called again for the
        lifetime of this adapter instance — see the class docstring."""
        if "/" in self._executable:
            path = Path(self._executable)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
            return None
        return shutil.which(self._executable)

    def _pinned_executable(self) -> str | None:
        if self._resolved_path is not None:
            # Re-validate the SAME absolute path — never re-search PATH.
            # A previously resolved path is not a guarantee of binary
            # integrity (its contents could change); this adapter does
            # not attempt to verify integrity beyond existence and the
            # executable bit, and documents that limitation rather than
            # claiming more than it checks.
            path = Path(self._resolved_path)
            if path.is_file() and os.access(path, os.X_OK):
                return self._resolved_path
            self._resolved_path = None
            return None

        resolved = self._resolve_executable()
        if resolved is not None:
            self._resolved_path = resolved
        return resolved

    def _check_version(self, resolved: str) -> str | None:
        try:
            result = self._run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=_VERSION_CHECK_TIMEOUT_SECONDS,
                env=_build_subprocess_env(),
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
                env=_build_subprocess_env(),
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
                env=_build_subprocess_env(),
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
            return self._cache_and_return(
                ExecutionResult(
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
            )

        # Fail closed before ever constructing a command: do not invent
        # successful verification evidence. This also pins the
        # executable path and populates self._cli_version.
        verification_status = self.verify()
        if verification_status is not VerificationStatus.AVAILABLE:
            return self._cache_and_return(
                ExecutionResult(
                    execution_id=request.execution_id,
                    task_id=request.task_id,
                    outcome=ExecutionOutcome.ERROR,
                    cli_version=self._cli_version,
                    verification_status=verification_status,
                    error_classification=_VERIFICATION_ERROR_CLASSIFICATION[verification_status],
                )
            )

        resolved = self._pinned_executable()
        if resolved is None:
            # Disappeared between verify() returning AVAILABLE and here —
            # a narrow TOCTOU window; fail closed rather than re-searching
            # PATH (see _pinned_executable).
            return self._cache_and_return(
                ExecutionResult(
                    execution_id=request.execution_id,
                    task_id=request.task_id,
                    outcome=ExecutionOutcome.ERROR,
                    cli_version=self._cli_version,
                    error_classification="executable_not_found",
                )
            )

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
            *_ISOLATION_FLAGS,
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
                env=_build_subprocess_env(),
            )
        except OSError as exc:
            return self._cache_and_return(
                ExecutionResult(
                    execution_id=request.execution_id,
                    task_id=request.task_id,
                    outcome=ExecutionOutcome.ERROR,
                    error_classification="process_start_failed",
                    stderr=str(exc),
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc),
                    cli_version=self._cli_version,
                    verification_status=verification_status,
                )
            )

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
        stdout = _truncate_utf8_bytes(stdout, max_bytes)
        stderr = _truncate_utf8_bytes(stderr, max_bytes)

        if outcome is None:
            outcome, error_classification, structured_output, structured_output_valid = (
                _classify_completed_process(exit_code, stdout)
            )
        else:
            error_classification = "timed_out" if timed_out else "cancelled"
            structured_output = None
            structured_output_valid = None

        return self._cache_and_return(
            ExecutionResult(
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
                verification_status=verification_status,
                error_classification=error_classification,
            )
        )

    def _cache_and_return(self, result: ExecutionResult) -> ExecutionResult:
        self._completed[result.execution_id] = result
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
