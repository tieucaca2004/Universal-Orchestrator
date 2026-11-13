"""Hermes Docker workspace copy-in/copy-out — M6 remediation, Item 3.

Makes a host-side Git Worktree reachable inside the live Hermes
container, following the M6 Final Consolidated Implementation
Blueprint's recommended option (controlled copy-in/copy-out) in
preference to a per-worktree bind mount — this module never
reconfigures, restarts, or stops the container, and never touches its
other live responsibilities (e.g. a Telegram bridge sharing it).

**The host Worktree is authoritative at every step:**
- `copy_in()` only ever reads from the host and writes into a fresh,
  unique, adapter-generated staging directory inside the container
  (never a caller-supplied name, to avoid any path-injection
  surface) — never the reverse at this stage.
- `copy_in()` verifies completeness (file count + per-file size) before
  returning; an incomplete copy raises and nothing further proceeds.
- `copy_out()` re-snapshots the host Worktree immediately before
  overwriting it and compares that against the snapshot `copy_in()`
  took — if the host changed in between (it should not, under the
  single-task-per-worktree exclusivity rule, but this is the actual
  tripwire, not an assumption), copy-out is refused rather than
  blindly overwriting a host-side edit with stale container content.
- `copy_out()` verifies completeness the same way `copy_in()` does.
  **The in-container staging directory is removed only after a
  verified-complete copy-out** — on any failure path, it is left in
  place, since it may be the only remaining copy of unrecovered work.

**Known, disclosed limitation — not solved by this module:** if the
container itself crashes between `copy_in()` and `copy_out()`, any
edits that exist only inside the staging directory at that moment are
unrecoverable. This module shortens that exposure window (one real
Agent call's duration) but does not eliminate it. Never claim this
risk is eliminated.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

from universal_orchestrator.errors import OrchestratorError

_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")
_TIMEOUT_SECONDS = 60.0


def _env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}


class HermesTransferError(OrchestratorError):
    """Raised when a copy-in or copy-out step cannot be verified
    complete, or an underlying `docker` command fails. Never raised
    for the normal case — a verified-complete transfer returns
    normally."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Hermes workspace transfer failed: {reason}")


def _new_staging_path() -> str:
    return f"/tmp/uo_exec_{uuid.uuid4().hex[:12]}"


def _host_manifest(host_path: str) -> dict[str, int]:
    """path (relative to host_path) -> size in bytes, for every real
    file. The one source of truth `copy_in`/`copy_out` compare against."""
    root = Path(host_path)
    manifest: dict[str, int] = {}
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            manifest[str(path.relative_to(root))] = path.stat().st_size
    return manifest


def _container_manifest(docker_bin: str, container: str, staging_path: str) -> dict[str, int]:
    result = subprocess.run(
        [docker_bin, "exec", container, "find", staging_path, "-type", "f", "-printf", "%P\t%s\n"],
        env=_env(),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise HermesTransferError(f"could not list container staging directory: {result.stderr.strip()}")
    manifest: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        rel_path, _, size = line.rpartition("\t")
        if ".git" in Path(rel_path).parts:
            continue
        manifest[rel_path] = int(size)
    return manifest


class HermesWorkspaceTransfer:
    """One instance per execution. `copy_in()` then, after the real
    Agent call, exactly one `copy_out()` — never reused across two
    different executions, since the staging path is generated fresh
    each time `copy_in()` runs."""

    def __init__(self, container: str, docker_bin: str = "docker") -> None:
        self._container = container
        self._docker_bin = docker_bin
        self._staging_path: str | None = None
        self._host_manifest_at_copy_in: dict[str, int] | None = None

    def copy_in(self, host_worktree_path: str) -> str:
        if self._staging_path is not None:
            raise HermesTransferError("copy_in() already called for this transfer instance")

        staging_path = _new_staging_path()
        mkdir = subprocess.run(
            [self._docker_bin, "exec", self._container, "mkdir", "-p", staging_path],
            env=_env(), capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
        )
        if mkdir.returncode != 0:
            raise HermesTransferError(f"could not create staging directory: {mkdir.stderr.strip()}")

        host_manifest = _host_manifest(host_worktree_path)
        copy = subprocess.run(
            [self._docker_bin, "cp", f"{host_worktree_path}/.", f"{self._container}:{staging_path}"],
            env=_env(), capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
        )
        if copy.returncode != 0:
            raise HermesTransferError(f"docker cp (copy-in) failed: {copy.stderr.strip()}")

        container_manifest = _container_manifest(self._docker_bin, self._container, staging_path)
        if container_manifest != host_manifest:
            raise HermesTransferError(
                f"copy-in incomplete: host has {len(host_manifest)} file(s), "
                f"container staging has {len(container_manifest)}"
            )

        self._staging_path = staging_path
        self._host_manifest_at_copy_in = host_manifest
        return staging_path

    def copy_out(self, host_worktree_path: str) -> None:
        if self._staging_path is None:
            raise HermesTransferError("copy_in() was never called for this transfer instance")

        current_host_manifest = _host_manifest(host_worktree_path)
        if current_host_manifest != self._host_manifest_at_copy_in:
            # The host Worktree changed since copy_in() -- never
            # overwrite it with stale container content. Staging is
            # left in place for inspection.
            raise HermesTransferError(
                "refusing copy-out: host Worktree changed since copy-in "
                "(would overwrite newer host content with stale staging data)"
            )

        container_manifest = _container_manifest(self._docker_bin, self._container, self._staging_path)
        copy = subprocess.run(
            [self._docker_bin, "cp", f"{self._container}:{self._staging_path}/.", host_worktree_path],
            env=_env(), capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
        )
        if copy.returncode != 0:
            # Staging left in place -- it may be the only remaining copy.
            raise HermesTransferError(f"docker cp (copy-out) failed: {copy.stderr.strip()}")

        host_manifest_after = _host_manifest(host_worktree_path)
        if host_manifest_after != container_manifest:
            # Incomplete -- do NOT clean up staging; it is the recovery path.
            raise HermesTransferError(
                "copy-out incomplete: host and container manifests differ after "
                "copy; staging directory preserved for recovery, not cleaned up"
            )

        # Verified complete -- only now may the staging directory be removed.
        subprocess.run(
            [self._docker_bin, "exec", self._container, "rm", "-rf", self._staging_path],
            env=_env(), capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
        )
