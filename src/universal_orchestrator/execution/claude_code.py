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

None of the above is filesystem, process, or network sandboxing for
`PermissionMode.READ_ONLY` (the only mode that existed through
Milestone 5) — it is CLI configuration only, verified against this one
CLI version's documented flags.

`PermissionMode.WORKTREE_WRITE` (M6 completion) is a second, separate,
opt-in mode with a materially different, honestly-documented posture:
`--tools "Read,Write,Edit,Bash"` replaces `--tools ""`, and
`--permission-mode bypassPermissions` replaces the absence of that flag
— both `acceptEdits` (tried first) and the complete absence of a
permission-mode flag were empirically found, via real calls this
session, to still deny some real Bash tool calls with no TTY to
confirm against; `bypassPermissions` is what reliably removed that.
This **does** remove the CLI's own interactive tool-confirmation layer
for this mode — stated plainly, not hidden. The actual safety boundary
for `WORKTREE_WRITE` is therefore (1) the four-tool allow-list itself
— nothing beyond Read/Write/Edit/Bash exists, and every other
isolation flag above remains active — and (2) real, kernel-enforced
filesystem containment (`bwrap`/Linux user namespaces, see
`_build_sandboxed_command`): every path is read-only to the process
except the task's own Worktree (plus two small, non-project,
non-secret scratch directories Claude's own Bash tool needs to
initialize at all — never `~/.claude/settings.json`, which stays
read-only). This was verified empirically this session, outside any
test harness, with a real absolute-path write attempt outside the
Worktree failing with a real "Read-only file system" OS error. If
`bwrap` is not on PATH, this adapter fails closed
(`sandbox_unavailable`) rather than ever running with write tools
enabled and no real containment.

Audit Finding C-3, stated plainly, not hidden: this is filesystem
containment only. By default the sandboxed `claude` process keeps
full host network access — the same honest limitation already
documented for `READ_ONLY` mode in docs/EXECUTION_CLAUDE_CODE.md's
"Security posture" table — because `claude` itself needs live
network access to reach the Anthropic API; unsharing the network
namespace unconditionally would make every `WORKTREE_WRITE`
execution fail outright, not more secure. `ExecutionPolicy.
allow_network=False` plumbs through to a real `--unshare-net` (see
`_build_sandboxed_command`) for the rare configuration that
genuinely needs no network at all — never meaningful with either
adapter that exists today.

See "Known limitations" in the docs for exactly what is and is
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

# M6 completion — WORKTREE_WRITE mode. These are the only built-in
# tools enabled when write access is granted: file read/create/edit
# and a shell for running the task's own test commands. Real
# containment (below) is what makes enabling Bash here safe — not the
# tool list by itself.
_WORKTREE_WRITE_TOOLS = "Read,Write,Edit,Bash"

_SANDBOX_BIN = "bwrap"

# Claude Code's own Bash tool needs a small amount of scratch space
# outside the Worktree to function at all (its own session/environment
# bookkeeping) — discovered empirically this session: without these,
# every Bash tool call fails to even start, with a real "Read-only
# file system" error from Claude's own tooling, not from this
# adapter's prompt. Neither path contains project data or secrets;
# `~/.claude/settings.json` (the credential file) is deliberately
# *not* included here and stays read-only, same as every other path
# outside the Worktree.
_CLAUDE_SESSION_ENV_DIR = str(Path.home() / ".claude" / "session-env")
_CLAUDE_TMP_SCRATCH_DIR = f"/tmp/claude-{os.getuid()}"


def _build_sandboxed_command(
    inner_command: list[str], worktree_path: str, *, allow_network: bool = True
) -> list[str]:
    """Wraps `inner_command` so that, enforced by the kernel (Linux user
    namespaces via `bwrap`), every path on the filesystem is read-only
    to the sandboxed process *except* `worktree_path` and Claude's own
    non-project scratch directories (see above) — those are the only
    writable bind mounts. `--unshare-pid` additionally hides the
    host's other processes from the sandboxed one. Empirically verified
    this session, outside any test harness, that an absolute-path
    write attempt to a path other than these bound paths fails with a
    real "Read-only file system" OS error — not a convention, not
    something `cwd=` alone would have provided.

    Audit Finding C-3: filesystem containment here has never implied
    network isolation — `allow_network=True` (the default, unchanged)
    leaves the network namespace shared, because `claude` itself needs
    live network access to reach the Anthropic API; setting it to
    `False` adds `--unshare-net`, empirically verified this session to
    turn a real outbound TCP connect into an immediate kernel-level
    `OSError: [Errno 101] Network is unreachable` — but doing so with
    the real `claude` CLI as its own sandboxed process will break
    every execution, since that CLI cannot then reach its own API."""
    os.makedirs(_CLAUDE_SESSION_ENV_DIR, exist_ok=True)
    os.makedirs(_CLAUDE_TMP_SCRATCH_DIR, exist_ok=True)
    network_flags = [] if allow_network else ["--unshare-net"]
    return [
        _SANDBOX_BIN,
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--bind", worktree_path, worktree_path,
        "--bind", _CLAUDE_SESSION_ENV_DIR, _CLAUDE_SESSION_ENV_DIR,
        "--bind", _CLAUDE_TMP_SCRATCH_DIR, _CLAUDE_TMP_SCRATCH_DIR,
        "--unshare-pid",
        *network_flags,
        "--die-with-parent",
        "--chdir", worktree_path,
        "--",
        *inner_command,
    ]
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

        if request.policy.permission_mode not in (
            PermissionMode.READ_ONLY,
            PermissionMode.WORKTREE_WRITE,
        ):
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

        use_worktree_write = request.policy.permission_mode is PermissionMode.WORKTREE_WRITE
        if use_worktree_write and shutil.which(_SANDBOX_BIN) is None:
            # Never silently fall back to running with write tools
            # enabled and no containment — fail closed instead.
            return self._cache_and_return(
                ExecutionResult(
                    execution_id=request.execution_id,
                    task_id=request.task_id,
                    outcome=ExecutionOutcome.ERROR,
                    error_classification="sandbox_unavailable",
                    stderr=(
                        f"WORKTREE_WRITE requires '{_SANDBOX_BIN}' for real filesystem "
                        "containment; it is not available on PATH, so this adapter "
                        "refuses to grant write tools without a sandbox"
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
            _WORKTREE_WRITE_TOOLS if use_worktree_write else "",
        ]
        if use_worktree_write:
            # Without an explicit permission-mode, the CLI's own
            # non-interactive `-p` mode denies every Write/Edit/Bash
            # tool call outright (there is no TTY to confirm against) —
            # verified empirically this session via a real call's
            # `permission_denials` field. `acceptEdits` was tried first
            # and auto-approved Write/Edit reliably, but still denied
            # some real Bash calls mid-session (also verified
            # empirically, via a real multi-step task) — `bypassPermissions`
            # is what reliably removes interactive confirmation for
            # every tool call in testing. This is a real, honest
            # trade-off, not hidden: it is **not** a substitute for the
            # real safety boundary here, which is (1) the `--tools`
            # allow-list above — only Read/Write/Edit/Bash exist at
            # all, nothing else, no MCP/plugins/computer-use/web —
            # together with the unchanged `_ISOLATION_FLAGS`
            # (--safe-mode etc.), and (2) the kernel-enforced sandbox
            # below, which is what actually bounds what those four
            # tools can do to the filesystem regardless of what the
            # CLI's own permission layer would have decided.
            command = [*command, "--permission-mode", "bypassPermissions"]
            # Real, kernel-enforced containment — not `cwd=` alone.
            command = _build_sandboxed_command(
                command, request.working_directory, allow_network=request.policy.allow_network
            )

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
