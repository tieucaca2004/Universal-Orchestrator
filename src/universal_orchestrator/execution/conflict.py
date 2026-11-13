"""Conflict report data model — Milestone 6.2 (ROADMAP.md §10.2).

Kept free of any dependency on `core.state.StateManager`, the same
reason `execution.worktree` is — so `core.state` can import
`ConflictReport` from here without a circular import; the concrete
`ConflictManager` (in `execution.conflict_manager`) is what actually
needs `StateManager`.

Two distinct, separately-reported tiers — never conflated (per the M6
Final Consolidated Implementation Blueprint and the Founder's explicit
instruction not to treat path overlap as proof of an actual conflict):

- **Tier 1 — path-level overlap**: files more than one active Worktree
  touched. Informational only. Never blocks integration by itself.
- **Tier 2 — actual Git merge conflict**: a real, read-only trial merge
  (`git merge-tree --write-tree`, which never touches the working
  tree, the index, or any ref) against the current target-branch SHA.
  Blocks integration when Git itself cannot auto-merge cleanly.

Every report is tagged with the exact `base_sha` it was computed
against — the only way a later, stale report can be told apart from a
fresh one before integration actually proceeds (see
`ConflictManager.revalidate`).

Tier 3 (semantic/integration conflict — changes merge cleanly but
break something no text diff would show) has **no detector in this
codebase**. It is not represented here as a field with a false "safe"
default; it is disclosed as a standing limitation in
ARCHITECTURE.md/ROADMAP.md and partially, indirectly mitigated only by
Quality Gate's post-integration verification (Milestone 6.4) — never
claimed as automatically detected by this module.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from universal_orchestrator.errors import OrchestratorError


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_report_id() -> str:
    return f"conflict_{uuid.uuid4().hex[:12]}"


@dataclass(slots=True)
class ConflictReport:
    task_id: str
    project_id: str
    base_sha: str
    worktree_branch: str
    # other_task_id -> sorted list of file paths both worktrees touched.
    # Tier 1. Informational only — never used to block on its own.
    path_overlap: dict[str, list[str]] = field(default_factory=dict)
    # Tier 2. A real trial-merge verdict against `base_sha`.
    tier2_conflicting: bool = False
    tier2_conflicting_files: list[str] = field(default_factory=list)
    report_id: str = field(default_factory=_new_report_id)
    checked_at: datetime = field(default_factory=_utcnow)

    @property
    def blocks_integration(self) -> bool:
        """Tier 1 alone never blocks — only a real (tier 2) conflict
        does. This is the one property every caller must use instead
        of re-deriving the rule itself."""
        return self.tier2_conflicting


class ConflictOperationError(OrchestratorError):
    """Raised for a failed `git` command or an unusable trial-merge
    result — never for the conflict verdict itself, which is a normal,
    expected outcome represented by `ConflictReport.blocks_integration`."""

    def __init__(self, task_id: str, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"Conflict check failed for task '{task_id}': {reason}")


class StaleConflictReportError(OrchestratorError):
    """Raised when a `ConflictReport.base_sha` no longer matches the
    target branch's current SHA at the moment integration is about to
    proceed — the report must never be relied on past this point; a
    fresh `ConflictManager.check()` is required instead."""

    def __init__(self, task_id: str, expected_base_sha: str, actual_sha: str) -> None:
        self.task_id = task_id
        self.expected_base_sha = expected_base_sha
        self.actual_sha = actual_sha
        super().__init__(
            f"Stale conflict report for task '{task_id}': report was computed "
            f"against '{expected_base_sha}', but the target branch is now at "
            f"'{actual_sha}' — re-check required before integration."
        )


class IntegrationLockHeldError(OrchestratorError):
    """Raised when a caller tries to acquire the integration lock while
    another integration is already in flight — actual integration into
    the target branch must never race."""

    def __init__(self, task_id: str, held_by: str) -> None:
        self.task_id = task_id
        self.held_by = held_by
        super().__init__(
            f"Cannot acquire the integration lock for task '{task_id}': "
            f"already held by task '{held_by}'."
        )
