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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_alert_id() -> str:
    return f"alert_{uuid.uuid4().hex[:12]}"


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


class AlertKind(enum.Enum):
    """Milestone 7, Component B (State-aware Stall Detector). Every
    kind maps to one concrete, structural condition — never a vague
    "something seems wrong." `BLOCKED_CHAIN` and `RUNNING_TOO_LONG`
    are soft (`recovery_suspected`-style) signals that can still
    resolve on their own; `DEADLOCKED`, `CONFIRMED_INTERRUPTED`, and
    `INCONSISTENT_STATE` are confirmed findings that will not."""

    BLOCKED_CHAIN = "blocked_chain"
    DEADLOCKED = "deadlocked"
    RUNNING_TOO_LONG = "running_too_long"
    CONFIRMED_INTERRUPTED = "confirmed_interrupted"
    INCONSISTENT_STATE = "inconsistent_state"
    STUCK_QUALITY_CHECK = "stuck_quality_check"


class AlertSeverity(enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(slots=True)
class SchedulerAlert:
    """Pure bookkeeping — never read by `ApprovalGate` or
    `GovernedPipeline`, and recording or acknowledging one never
    changes a `Task` or `WorktreeHandle` row. See
    `core.stall_detector`'s module docstring for the detection rules
    that produce these."""

    task_id: str
    kind: AlertKind
    severity: AlertSeverity
    detail: str
    alert_id: str = field(default_factory=_new_alert_id)
    detected_at: datetime = field(default_factory=_utcnow)
    acknowledged: bool = False
    acknowledged_at: datetime | None = None
    acknowledged_reason: str = ""
