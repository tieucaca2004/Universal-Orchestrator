"""Hermes Agent Adapter — the second concrete `ExecutionAdapter`.

Everything this module assumes about Hermes was verified against the
actual running installation on the development host (Hermes Agent
v0.21.0 (2026.8.31), upstream 922c0d67, running as the Docker container
`hermes`, image `nousresearch/hermes-agent`) before being written — see
docs/EXECUTION_HERMES.md for the exact commands run and their real
output. Nothing here is invented from memory of another CLI or of
Claude Code's behavior.

Load-bearing difference from `ClaudeCodeAdapter`: there is no native
Hermes binary on this host's PATH. Hermes only runs inside a Docker
container, reached through `docker exec`. Three consequences, each
verified empirically rather than assumed:

1. `working_directory` is a path *inside the container's filesystem
   namespace*, not a host path — the container has exactly one host
   bind mount (its own `~/.hermes` data directory) and no general
   project-workspace mount. Real execution against an actual project
   would need that project's files inside the container, which is a
   Git Worktree Manager (M6) concern, not this adapter's.
2. Killing the local `docker exec` client process does **not** stop
   the process it started inside the container (verified: a `sleep`
   survived the local client being SIGTERM'd). Timeout/cancellation
   therefore write the in-container PID to a file via a small `sh -c`
   wrapper and send a second, targeted `docker exec ... kill` to that
   PID — never a broad `pkill`, because this container also runs a
   live Telegram bridge for unrelated, real usage that a pattern-match
   kill could collaterally disrupt.
3. Hermes has no `--output-format json` equivalent (`--help` on every
   relevant command was checked; none exists). Output is treated as
   plain text, never parsed as structured JSON.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
import uuid
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

# Substrings `hermes chat --help` must contain for the flags this
# adapter relies on — checked literally every time verify() runs, the
# same discipline as ClaudeCodeAdapter's _check_supported_options.
_REQUIRED_HELP_MARKERS = (
    "-q, --query",
    "-Q, --quiet",
    "--oneshot",
    "--safe-mode",
    "-t, --toolsets",
)

# A deliberately-invalid toolset name. Traced through Hermes's own
# source (`cli.py`'s `_init_toolsets`/`_build_cli_from_args`) and
# empirically confirmed via `--list_tools`: an unrecognized toolset
# name results in zero tools being registered ("0 tools · 0 skills").
# This is Hermes's closest equivalent to Claude Code's documented
# `--tools ""` — not an officially documented flag value, so verify()
# cannot simply trust it forever; see docs/EXECUTION_HERMES.md "Known
# limitations" for what remains unverified about this mechanism
# specifically through the `hermes chat` entrypoint (as opposed to the
# `cli.py --list_tools` entrypoint used to discover it).
_ZERO_TOOLSET_SENTINEL = "__uo_disable_all_tools__"

_VERSION_CHECK_TIMEOUT_SECONDS = 10
_HELP_CHECK_TIMEOUT_SECONDS = 10
_STATUS_CHECK_TIMEOUT_SECONDS = 20
_CONTAINER_LOOKUP_TIMEOUT_SECONDS = 10
_PIDFILE_READ_TIMEOUT_SECONDS = 5
_PROCESS_GROUP_GRACE_SECONDS = 3

_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")

_VERIFICATION_ERROR_CLASSIFICATION = {
    VerificationStatus.UNAVAILABLE: "adapter_unavailable",
    VerificationStatus.UNAUTHENTICATED: "adapter_unauthenticated",
    VerificationStatus.UNSUPPORTED: "adapter_unsupported",
    VerificationStatus.ERROR: "adapter_verification_error",
}

# `hermes status`'s model/provider-credential sections, in the order
# they appear in real output — used to scope the "is anything
# authenticated" check to these sections only, so an unrelated
# integration (e.g. a configured Telegram bridge) is never mistaken
# for model-provider authentication.
_STATUS_CREDENTIAL_SECTION_START = "API Keys"
_STATUS_CREDENTIAL_SECTION_END = "Terminal Backend"


def _build_subprocess_env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}


def _has_any_provider_credential(status_text: str) -> bool:
    """True if a ✓ appears within the credential-related sections of
    `hermes status` output. Operates on the text in memory only — the
    caller never stores or returns this text; only this boolean."""
    start = status_text.find(_STATUS_CREDENTIAL_SECTION_START)
    end = status_text.find(_STATUS_CREDENTIAL_SECTION_END)
    if start == -1 or end == -1 or end <= start:
        return False
    return "✓" in status_text[start:end]


def _truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8", errors="surrogateescape")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class HermesAdapter(ExecutionAdapter):
    def __init__(
        self,
        container: str = "hermes",
        *,
        docker_bin: str = "docker",
        run=subprocess.run,
    ) -> None:
        self._container = container
        self._docker_bin = docker_bin
        # Injectable for deterministic unit tests against a fake
        # `docker` script — never swapped out for real executions.
        self._run = run
        self._cli_version: str | None = None
        self._pinned_container_id: str | None = None
        self._running: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._completed: dict[str, ExecutionResult] = {}

    # -- availability verification --------------------------------------------

    def verify(self) -> VerificationStatus:
        docker_path = shutil.which(self._docker_bin) if "/" not in self._docker_bin else (
            self._docker_bin if Path(self._docker_bin).is_file() else None
        )
        if docker_path is None:
            return VerificationStatus.UNAVAILABLE

        container_id = self._pinned_container()
        if container_id is None:
            return VerificationStatus.UNAVAILABLE

        version = self._check_version(docker_path, container_id)
        if version is None:
            return VerificationStatus.ERROR
        self._cli_version = version

        if not self._check_supported_options(docker_path, container_id):
            return VerificationStatus.UNSUPPORTED

        authenticated = self._check_authenticated(docker_path, container_id)
        if authenticated is None:
            return VerificationStatus.ERROR
        if not authenticated:
            return VerificationStatus.UNAUTHENTICATED

        return VerificationStatus.AVAILABLE

    def _pinned_container(self) -> str | None:
        """Resolves `self._container` (a name) to its current container
        ID exactly once per adapter instance, then re-validates only
        that same ID is still running — never re-resolves by name. A
        container removed and recreated under the same name would
        otherwise silently become a different container between
        verify() and execute(); pinning the ID closes that window, the
        same reasoning as ClaudeCodeAdapter's executable pinning."""
        if self._pinned_container_id is not None:
            if self._container_id_is_running(self._pinned_container_id):
                return self._pinned_container_id
            self._pinned_container_id = None
            return None

        resolved = self._resolve_container_id(self._container)
        if resolved is not None:
            self._pinned_container_id = resolved
        return resolved

    def _resolve_container_id(self, name_or_id: str) -> str | None:
        try:
            result = self._run(
                [
                    self._docker_bin,
                    "inspect",
                    "--format",
                    "{{.Id}} {{.State.Running}}",
                    name_or_id,
                ],
                capture_output=True,
                text=True,
                timeout=_CONTAINER_LOOKUP_TIMEOUT_SECONDS,
                env=_build_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        parts = result.stdout.strip().split()
        if len(parts) != 2 or parts[1] != "true":
            return None
        return parts[0]

    def _container_id_is_running(self, container_id: str) -> bool:
        try:
            result = self._run(
                [self._docker_bin, "inspect", "--format", "{{.State.Running}}", container_id],
                capture_output=True,
                text=True,
                timeout=_CONTAINER_LOOKUP_TIMEOUT_SECONDS,
                env=_build_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _check_version(self, docker_path: str, container_id: str) -> str | None:
        try:
            result = self._run(
                [docker_path, "exec", container_id, "hermes", "--version"],
                capture_output=True,
                text=True,
                timeout=_VERSION_CHECK_TIMEOUT_SECONDS,
                env=_build_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        return first_line or None

    def _check_supported_options(self, docker_path: str, container_id: str) -> bool:
        try:
            result = self._run(
                [docker_path, "exec", container_id, "hermes", "chat", "--help"],
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

    def _check_authenticated(self, docker_path: str, container_id: str) -> bool | None:
        """Returns True/False, or None if the check itself failed. Only
        ever reduces `hermes status`'s output to a single boolean — the
        raw text (which can contain masked key previews like
        `sk-p...3lEA`) is never stored, logged, or returned."""
        try:
            result = self._run(
                [docker_path, "exec", container_id, "hermes", "status"],
                capture_output=True,
                text=True,
                timeout=_STATUS_CHECK_TIMEOUT_SECONDS,
                env=_build_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return _has_any_provider_credential(result.stdout)

    # -- execution --------------------------------------------------------------

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.execution_id in self._completed:
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

        docker_path = shutil.which(self._docker_bin) or self._docker_bin
        container_id = self._pinned_container()
        if container_id is None:
            return self._cache_and_return(
                ExecutionResult(
                    execution_id=request.execution_id,
                    task_id=request.task_id,
                    outcome=ExecutionOutcome.ERROR,
                    cli_version=self._cli_version,
                    error_classification="container_not_found",
                )
            )

        # Our own uuid, never caller-controlled text, so it is safe to
        # interpolate into the fixed shell wrapper below — the prompt
        # itself never is (it travels as a separate argv element).
        pidfile = f"/tmp/.uo_hermes_{uuid.uuid4().hex}.pid"
        wrapper_script = f'echo $$ > {pidfile}; exec "$@"'

        command = [
            docker_path,
            "exec",
            "-w",
            request.working_directory,
            container_id,
            "sh",
            "-c",
            wrapper_script,
            "sh",  # becomes $0 in the wrapper; excluded from "$@"
            "hermes",
            "chat",
            "-q",
            request.input_context,
            "-Q",
            "--oneshot",
            "--safe-mode",
            "-t",
            _ZERO_TOOLSET_SENTINEL,
        ]

        started_at = datetime.now(timezone.utc)
        start_perf = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
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
                self._terminate_in_container(docker_path, container_id, pidfile, process)
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
            self._remove_pidfile(docker_path, container_id, pidfile)

        ended_at = datetime.now(timezone.utc)
        duration = time.monotonic() - start_perf

        max_bytes = request.policy.max_output_bytes
        stdout = _truncate_utf8_bytes(stdout, max_bytes)
        stderr = _truncate_utf8_bytes(stderr, max_bytes)

        if outcome is None:
            outcome, error_classification = _classify_completed_process(exit_code, stdout)
        else:
            error_classification = "timed_out" if timed_out else "cancelled"

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
                output_format="text",
                structured_output=None,
                structured_output_valid=None,
                verification_status=verification_status,
                error_classification=error_classification,
            )
        )

    def _cache_and_return(self, result: ExecutionResult) -> ExecutionResult:
        self._completed[result.execution_id] = result
        return result

    def cancel(self, execution_id: str) -> bool:
        process = self._running.get(execution_id)
        if process is None or process.poll() is not None:
            return False
        self._cancelled.add(execution_id)
        # The pidfile/container_id used by the in-flight execute() call
        # are not available here; cancel() stops the local client so
        # execute()'s own TimeoutExpired-equivalent path cannot run.
        # Immediate, best-effort local termination; execute()'s own
        # in-container kill only runs on an actual timeout, not a
        # cooperative cancel — see "Known limitations" in the docs for
        # why a cancel() this way does not guarantee the in-container
        # process stops immediately.
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

    def _terminate_in_container(
        self, docker_path: str, container_id: str, pidfile: str, local_process: subprocess.Popen
    ) -> None:
        """Verified necessary: killing `local_process` alone does not
        stop the work it started inside the container (empirically
        confirmed — a `sleep` survived the local `docker exec` client
        being SIGTERM'd). Reads the in-container PID the wrapper script
        recorded and signals it directly via a second, targeted
        `docker exec`, never a pattern-match `pkill` (this container
        also runs an unrelated, live Telegram bridge a broad kill
        could disrupt)."""
        inner_pid = self._read_pidfile(docker_path, container_id, pidfile)
        if inner_pid is not None:
            self._run_docker_exec(docker_path, container_id, ["kill", "-TERM", str(inner_pid)])
            deadline = time.monotonic() + _PROCESS_GROUP_GRACE_SECONDS
            while time.monotonic() < deadline:
                if not self._inner_pid_alive(docker_path, container_id, inner_pid):
                    break
                time.sleep(0.2)
            else:
                self._run_docker_exec(docker_path, container_id, ["kill", "-KILL", str(inner_pid)])

        # Always also stop the local client's own process group, so
        # this process does not linger even if the pidfile read failed.
        self._terminate_process_group(local_process)

    def _read_pidfile(self, docker_path: str, container_id: str, pidfile: str) -> int | None:
        try:
            result = self._run(
                [docker_path, "exec", container_id, "cat", pidfile],
                capture_output=True,
                text=True,
                timeout=_PIDFILE_READ_TIMEOUT_SECONDS,
                env=_build_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        match = re.match(r"^\s*(\d+)\s*$", result.stdout)
        return int(match.group(1)) if match else None

    def _inner_pid_alive(self, docker_path: str, container_id: str, pid: int) -> bool:
        try:
            result = self._run(
                [docker_path, "exec", container_id, "kill", "-0", str(pid)],
                capture_output=True,
                text=True,
                timeout=_PIDFILE_READ_TIMEOUT_SECONDS,
                env=_build_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _remove_pidfile(self, docker_path: str, container_id: str, pidfile: str) -> None:
        self._run_docker_exec(docker_path, container_id, ["rm", "-f", pidfile])

    def _run_docker_exec(self, docker_path: str, container_id: str, inner_command: list[str]) -> None:
        try:
            self._run(
                [docker_path, "exec", container_id, *inner_command],
                capture_output=True,
                text=True,
                timeout=_PIDFILE_READ_TIMEOUT_SECONDS,
                env=_build_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError):
            pass


def _classify_completed_process(exit_code: int, stdout: str) -> tuple[ExecutionOutcome, str | None]:
    if exit_code == 127:
        # Docker's own convention for "could not even set up/start the
        # exec'd process" (e.g. an invalid -w working directory) as well
        # as the shell's own "command not found" — verified: a
        # nonexistent working directory produces exactly this code.
        return ExecutionOutcome.ERROR, "working_directory_invalid_or_command_not_found"
    if exit_code != 0:
        return ExecutionOutcome.FAILED, "nonzero_exit"
    if not stdout.strip():
        return ExecutionOutcome.COMPLETED, "empty_output"
    return ExecutionOutcome.COMPLETED, None
