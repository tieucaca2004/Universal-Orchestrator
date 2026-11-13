"""Conflict Manager — Milestone 6.2 (ROADMAP.md §10.2).

Two distinct, separately-reported checks (never conflated — see
`execution.conflict`'s module docstring):

- **Tier 1 — path-level overlap**: `git diff --name-only` between each
  active Worktree's own base and branch, compared pairwise. Purely
  informational; never blocks by itself.
- **Tier 2 — actual Git merge conflict**: `git merge-tree --write-tree
  <target_sha> <worktree_branch>` — a real trial merge that writes a
  dangling, unreferenced tree object into the repository's object
  database and touches **nothing else**: not the working tree, not
  the index, not any branch or ref, not `main`. Empirically verified
  this session against a real repository before being relied on here,
  the same discipline this project has always used for CLI behavior
  that isn't officially documented as a stable contract (see
  docs/EXECUTION_HERMES.md's "zero-tool mechanism" for the precedent).
  Git's own exit codes: `0` = clean merge (no block); `1` = real
  conflict, with "Merge conflict in <path>" lines on stdout naming
  exactly which files; anything else is treated as an operational
  failure, never silently swallowed.

Staleness and serialization: every `ConflictReport` is tagged with the
exact target-branch SHA it was computed against. `revalidate()` always
produces a brand-new report rather than re-checking an old one's
freshness in place — the only reliable way to guarantee a stale
report can never be the thing that actually authorizes integration.
The integration lock (`StateManager.acquire_integration_lock`) is a
single-row SQLite mutex: many Agents may execute and prepare changes
concurrently, but only one may be in the middle of an actual
integration at a time — this is what makes "Agent B integrates between
Agent A's conflict check and A's own integration" a case the caller
*can* detect (stale `base_sha` after acquiring the lock), not a silent
race.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from universal_orchestrator.core.state import StateManager
from universal_orchestrator.execution.conflict import (
    ConflictOperationError,
    ConflictReport,
    IntegrationLockHeldError,
)
from universal_orchestrator.execution.worktree_manager import WorktreeManager

_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")
_GIT_TIMEOUT_SECONDS = 30.0
_CONFLICT_MARKER = "Merge conflict in "


def _env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}


class ConflictManager:
    def __init__(self, state: StateManager, worktrees: WorktreeManager) -> None:
        self._state = state
        self._worktrees = worktrees

    # -- checks ----------------------------------------------------------

    def check(
        self, repository_path: str, task_id: str, target_branch: str = "main"
    ) -> ConflictReport:
        repo = Path(repository_path)
        handle = self._worktrees.get(task_id)
        current_target_sha = self._run_git(
            task_id, repo, ["rev-parse", target_branch]
        ).strip()

        my_changed = self._changed_paths(task_id, repo, handle.base_sha, handle.branch)
        overlap: dict[str, list[str]] = {}
        for other in self._worktrees.list_active(handle.project_id):
            if other.task_id == task_id:
                continue
            other_changed = self._changed_paths(
                task_id, repo, other.base_sha, other.branch
            )
            common = sorted(my_changed & other_changed)
            if common:
                overlap[other.task_id] = common

        conflicting, conflicting_files = self._trial_merge(
            task_id, repo, current_target_sha, handle.branch
        )

        report = ConflictReport(
            task_id=task_id,
            project_id=handle.project_id,
            base_sha=current_target_sha,
            worktree_branch=handle.branch,
            path_overlap=overlap,
            tier2_conflicting=conflicting,
            tier2_conflicting_files=conflicting_files,
        )
        self._state.save_conflict_report(report)
        return report

    def is_stale(
        self, repository_path: str, report: ConflictReport, target_branch: str = "main"
    ) -> bool:
        repo = Path(repository_path)
        current = self._run_git(report.task_id, repo, ["rev-parse", target_branch]).strip()
        return current != report.base_sha

    def revalidate(
        self, repository_path: str, task_id: str, target_branch: str = "main"
    ) -> ConflictReport:
        """Never trusts a previously-held report's freshness — always
        produces a brand-new one, checked against the target branch's
        *current* SHA. This is the one call a caller must make
        immediately before integration, inside the integration lock
        (see `acquire_integration_lock`), to satisfy "reject stale
        conflict reports from authorizing integration."""
        return self.check(repository_path, task_id, target_branch)

    # -- integration lock --------------------------------------------------

    def acquire_integration_lock(self, task_id: str) -> None:
        if not self._state.acquire_integration_lock(task_id):
            held_by = self._state.current_integration_lock_holder()
            raise IntegrationLockHeldError(task_id, held_by or "<unknown>")

    def release_integration_lock(self, task_id: str) -> None:
        self._state.release_integration_lock(task_id)

    def current_integration_lock_holder(self) -> str | None:
        return self._state.current_integration_lock_holder()

    # -- internals -----------------------------------------------------

    def _changed_paths(self, task_id: str, repo: Path, base_sha: str, branch: str) -> set[str]:
        output = self._run_git(task_id, repo, ["diff", "--name-only", base_sha, branch])
        return {line.strip() for line in output.splitlines() if line.strip()}

    def _trial_merge(
        self, task_id: str, repo: Path, target_sha: str, branch: str
    ) -> tuple[bool, list[str]]:
        result = subprocess.run(
            ["git", "-C", str(repo), "merge-tree", "--write-tree", target_sha, branch],
            env=_env(),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            return False, []
        if result.returncode == 1:
            files = sorted(
                {
                    line.split(_CONFLICT_MARKER, 1)[1].strip()
                    for line in result.stdout.splitlines()
                    if _CONFLICT_MARKER in line
                }
            )
            return True, files
        raise ConflictOperationError(
            task_id, f"git merge-tree failed unexpectedly: {result.stderr.strip()}"
        )

    def _run_git(self, task_id: str, repo: Path, args: list[str]) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            env=_env(),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise ConflictOperationError(
                task_id, f"git {' '.join(args)} failed: {result.stderr.strip()}"
            )
        return result.stdout
