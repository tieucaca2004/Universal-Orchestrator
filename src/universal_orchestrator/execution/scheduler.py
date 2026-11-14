"""Scheduler lease & claim data model — Milestone 7 (Task Scheduler),
Phase 2: fencing and persistence primitives.

Kept free of any dependency on `core.state.StateManager`, the same
reason `execution.worktree` is kept free of it (see that module's own
docstring) — `core.state` imports these dataclasses, not the other
way around.

Two kinds of row, one SQLite file, two different jobs:

- `SchedulerLease` (single row, `scheduler_lease` table): which
  Scheduler process is currently authoritative, and since when.
  `epoch` is the fencing token — it only ever increases, once per
  successful acquisition, and is what every other protected mutation
  compares itself against before being allowed to happen.
- `TaskClaim` (one row per claimed task, `scheduler_claims` table):
  a Scheduler's reservation of exactly one task, tagged with the
  epoch that was authoritative when the claim was made. `task_id` is
  the primary key at the persistence layer (see `core.state`), so two
  claims for the same task can never both succeed — the same
  IntegrityError -> DuplicateError translation already used for
  `tasks` (Milestone 2) and `worktrees` (Milestone 6.1).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class ClaimStatus(enum.Enum):
    """CLAIMED never auto-advances. COMPLETED means the claiming
    holder's own fenced write confirmed its epoch was still
    authoritative when the claimed work finished. QUARANTINED means a
    *different*, later-authoritative holder found this claim still
    CLAIMED during recovery and could not positively confirm whether
    the original worker ever finished — never silently discarded,
    never silently reused."""

    CLAIMED = "claimed"
    COMPLETED = "completed"
    QUARANTINED = "quarantined"


@dataclass(slots=True)
class SchedulerLease:
    """The single authoritative-Scheduler row. `holder_id is None`
    means free. `epoch` is the fencing token: it increases by exactly
    one on every successful `acquire`, never on a `renew`, and never
    resets on `release` — so a holder from any earlier epoch can
    never again appear "current" no matter how many times the lease
    is subsequently acquired and released."""

    holder_id: str | None
    epoch: int
    acquired_at: datetime | None = None
    renewed_at: datetime | None = None
    expires_at: datetime | None = None

    @property
    def is_held(self) -> bool:
        return self.holder_id is not None


@dataclass(slots=True)
class TaskClaim:
    task_id: str
    epoch: int
    holder_id: str
    status: ClaimStatus
    claimed_at: datetime
    updated_at: datetime
