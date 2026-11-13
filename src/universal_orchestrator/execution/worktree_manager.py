"""Git Worktree Manager — Milestone 6.1 (ROADMAP.md §10.1).

Gives every task its own isolated Git worktree, so multiple Agents can
execute concurrently within the same Project (Founder-approved Option
B — Multi-Agent Concurrent Execution) without ever sharing or
overwriting each other's files, and without the project's own real
checkout ever being touched directly.

Exclusive ownership is enforced at the persistence layer: `task_id` is
the `worktrees` table's primary key (`core.state.StateManager`), so a
second `acquire()` for the same task raises `errors.DuplicateError` —
the same `sqlite3.IntegrityError` -> typed-error translation already
used for `tasks` (Milestone 2) and `execution_results` (Milestone 4
remediation), not a new validation style.

Hardware-independence (ROADMAP.md §10.5): every path here is derived
from the caller-supplied `repository_path` (itself sourced from
`ProjectProfile.repository_path`) and an optional `worktrees_root`
override — nothing is hardcoded to this NUC's filesystem layout. Tests
use `tempfile.TemporaryDirectory()` real repositories, never a fixed
path.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from universal_orchestrator.core.state import StateManager
from universal_orchestrator.errors import DuplicateError, NotFoundError
from universal_orchestrator.execution.worktree import (
    WorktreeHandle,
    WorktreeOperationError,
    WorktreeStatus,
    validate_transition,
)

_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")
_GIT_TIMEOUT_SECONDS = 30.0


def _env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorktreeManager:
    """Acquire, track, and release per-task Git worktrees. Never touches
    a project's real checkout directly — every operation targets the
    task's own worktree path, created by `git worktree add` from the
    real repository given to `acquire()`."""

    def __init__(self, state: StateManager, *, worktrees_root: str | Path | None = None) -> None:
        self._state = state
        # Caller-supplied or derived from the repository's own parent
        # directory — never a path hardcoded to one machine. See
        # ROADMAP.md §10.5.
        self._worktrees_root = Path(worktrees_root) if worktrees_root is not None else None

    # -- lifecycle -----------------------------------------------------

    def acquire(self, project_id: str, task_id: str, repository_path: str) -> WorktreeHandle:
        repo = Path(repository_path)
        if not repo.is_dir():
            raise WorktreeOperationError(
                task_id, f"repository_path '{repository_path}' is not a directory"
            )

        if self._state.get_worktree(task_id) is not None:
            raise WorktreeOperationError(
                task_id, "a worktree already exists for this task_id"
            )

        root = self._worktrees_root or (repo.parent / f".{repo.name}-worktrees")
        root.mkdir(parents=True, exist_ok=True)
        worktree_path = root / f"task_{task_id}"
        branch = f"uo/task_{task_id}"

        base_sha = self._run_git(task_id, repo, ["rev-parse", "HEAD"]).strip()
        self._run_git(
            task_id, repo, ["worktree", "add", "-b", branch, str(worktree_path), base_sha]
        )

        handle = WorktreeHandle(
            task_id=task_id,
            project_id=project_id,
            path=str(worktree_path),
            branch=branch,
            base_sha=base_sha,
            status=WorktreeStatus.ACQUIRED,
        )
        try:
            self._state.save_worktree(handle)
        except DuplicateError:
            # Race: another acquire() for the same task_id won between our
            # existence check and save_worktree() — never silently reuse
            # the worktree we just created; clean it up and refuse.
            self._run_git(
                task_id, repo, ["worktree", "remove", "--force", str(worktree_path)]
            )
            raise WorktreeOperationError(
                task_id, "a worktree already exists for this task_id (race)"
            ) from None
        return handle

    def transition(self, task_id: str, target: WorktreeStatus) -> WorktreeHandle:
        handle = self._require(task_id)
        validate_transition(handle.status, target)
        handle.status = target
        handle.updated_at = _utcnow()
        self._state.update_worktree(handle)
        return handle

    def get(self, task_id: str) -> WorktreeHandle:
        return self._require(task_id)

    def list_active(self, project_id: str | None = None) -> list[WorktreeHandle]:
        return [
            h
            for h in self._state.list_worktrees(project_id=project_id)
            if h.status is not WorktreeStatus.RELEASED
        ]

    def release(
        self, task_id: str, repository_path: str, *, confirm_discard: bool = False
    ) -> None:
        """Delete the task's worktree and mark it RELEASED.

        Safe by default from `INTEGRATED` (the changes are already in
        `main` — nothing is lost). From `NEEDS_REVIEW`, the caller must
        pass `confirm_discard=True` explicitly — this is a destructive
        path (uncommitted Agent work may still exist only in the
        worktree) that a higher layer (the Approval Gate, Milestone
        6.3) must authorize before ever calling this with
        `confirm_discard=True`; `WorktreeManager` itself never decides
        that on its own.
        """
        handle = self._require(task_id)
        if handle.status is WorktreeStatus.INTEGRATED:
            pass
        elif handle.status is WorktreeStatus.NEEDS_REVIEW and confirm_discard:
            pass
        else:
            raise WorktreeOperationError(
                task_id,
                f"refusing to release: status is '{handle.status.value}' — only "
                "INTEGRATED (safe: already merged) or NEEDS_REVIEW with "
                "confirm_discard=True (explicit, authorized discard) may release",
            )

        repo = Path(repository_path)
        self._run_git(task_id, repo, ["worktree", "remove", "--force", handle.path])
        validate_transition(handle.status, WorktreeStatus.RELEASED)
        handle.status = WorktreeStatus.RELEASED
        handle.updated_at = _utcnow()
        self._state.update_worktree(handle)

    # -- recovery --------------------------------------------------------

    def recover(self, repository_path: str) -> list[WorktreeHandle]:
        """Reconcile persisted worktree state against the real Git
        worktree state after an Orchestrator restart. Mirrors
        `TaskManager.recover()`'s caution exactly: never assumes, never
        silently deletes anything, only reclassifies an uncertain
        worktree to `NEEDS_REVIEW` for an explicit later decision."""
        repo = Path(repository_path)
        real_paths = set(self._list_real_worktree_paths(repo))
        reconciled: list[WorktreeHandle] = []
        for handle in self._state.list_worktrees():
            if handle.status is WorktreeStatus.RELEASED:
                continue
            if handle.status is WorktreeStatus.INTEGRATED:
                # Already safely merged; a lingering directory is cleanup
                # debt, not a data-loss risk. Leave it for an explicit
                # release() call — never auto-advance state here.
                reconciled.append(handle)
                continue
            path_exists = handle.path in real_paths
            if not path_exists or handle.status is not WorktreeStatus.NEEDS_REVIEW:
                # Either persisted state claims a worktree that no longer
                # exists on disk (a real inconsistency — never trusted
                # blindly either way), or the task was interrupted
                # mid-flight. Both require a human decision, not a guess.
                if handle.status is not WorktreeStatus.NEEDS_REVIEW:
                    handle.status = WorktreeStatus.NEEDS_REVIEW
                    handle.updated_at = _utcnow()
                    self._state.update_worktree(handle)
            reconciled.append(handle)
        return reconciled

    def _list_real_worktree_paths(self, repo: Path) -> list[str]:
        output = self._run_git("<recover>", repo, ["worktree", "list", "--porcelain"])
        paths = []
        for line in output.splitlines():
            if line.startswith("worktree "):
                paths.append(line[len("worktree "):].strip())
        return paths

    # -- internals -----------------------------------------------------

    def _require(self, task_id: str) -> WorktreeHandle:
        handle = self._state.get_worktree(task_id)
        if handle is None:
            raise NotFoundError("Worktree", task_id)
        return handle

    def _run_git(self, task_id: str, repo: Path, args: list[str]) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            env=_env(),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise WorktreeOperationError(
                task_id, f"git {' '.join(args)} failed: {result.stderr.strip()}"
            )
        return result.stdout
