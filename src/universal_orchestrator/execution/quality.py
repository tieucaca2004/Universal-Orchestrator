"""Quality Gate data model — Milestone 6.4 (ROADMAP.md §10.4).

Kept free of any dependency on `core.state.StateManager`, the same
reason every other Milestone 6 data-model module is — so `core.state`
(and `core.task_manager`) can import `QualityVerdict`/`QualityReport`
without a circular import.

Two distinct, separately-evaluated levels — never one standing in for
the other (per the Founder's explicit instruction not to assume two
task-level PASSes imply an integrated PASS):

- **Task-level** (`evaluate_task`): judges one Agent's `ExecutionResult`
  against *that task's own* `TaskSpecification.acceptance_criteria`.
  This module does **not** itself understand free-text acceptance
  criteria — it has no semantic judge, and makes no claim to. It
  requires explicit, per-criterion evidence (`criteria_evidence`,
  supplied by whoever is actually checking the work — a human today;
  a future Reviewer-role agent is a target architecture item, not
  built here) and refuses to default to PASS when that evidence is
  missing. A criterion with no evidence produces `NOT_VERIFIED`, never
  a silent PASS.
- **Integration-level** (`evaluate_integration`): runs the *project's
  own declared* testing commands (`ProjectProfile
  .required_testing_commands`, already existing since Milestone 3 —
  never an invented universal test) against the real, post-integration
  repository state, and records their real exit codes and output.

`QualityVerdict.PASS` from one level is never read as evidence for the
other level's verdict — each is computed, recorded, and retrieved
independently.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from universal_orchestrator.errors import OrchestratorError


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_report_id() -> str:
    return f"quality_{uuid.uuid4().hex[:12]}"


class QualityVerdict(enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_VERIFIED = "NOT_VERIFIED"


@dataclass(slots=True)
class QualityReport:
    task_id: str
    level: str  # "task" | "integration"
    verdict: QualityVerdict
    rationale: str
    # Task-level only: criterion text -> True/False/None (None = no
    # evidence supplied, contributes to NOT_VERIFIED).
    criteria_evidence: dict[str, bool | None] = field(default_factory=dict)
    # Integration-level only: the exact commands actually run, and
    # their real exit codes/output — never fabricated, never a
    # universal test invented by this module.
    commands_run: list[str] = field(default_factory=list)
    command_results: list[dict[str, Any]] = field(default_factory=list)
    report_id: str = field(default_factory=_new_report_id)
    evaluated_at: datetime = field(default_factory=_utcnow)


class QualityOperationError(OrchestratorError):
    """Raised for an operational failure evaluating quality (a
    required testing command could not even be started) — never for
    the verdict itself, which is a normal, expected outcome
    represented by `QualityReport.verdict`."""

    def __init__(self, task_id: str, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"Quality Gate evaluation failed for task '{task_id}': {reason}")
