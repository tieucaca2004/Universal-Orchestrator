"""Approval Gate data model — Milestone 6.3 (ROADMAP.md §10.3).

Kept free of any dependency on `core.state.StateManager`, the same
reason `execution.worktree`/`execution.conflict` are — so `core.state`
can import these types without a circular import.

**Binding an approval to the exact proposal it was given, not just an
action name:** `diff_identity` is the proposed worktree branch's real
Git commit SHA at the moment the proposal was built — not an
invented token. Any further commit on that branch changes the SHA,
so a decision recorded for one `diff_identity` can never silently
apply to a worktree that has since changed; "approval for one
proposal cannot authorize another" falls directly out of using Git's
own content-addressed identity rather than a separate, forgeable
token.

**`action` is free text, never hardcoded to one case:** "integrate"
and "discard_worktree" (a destructive action — see
`execution.worktree_manager.WorktreeManager.release`'s
`confirm_discard`) are two different actions; a decision recorded for
one never satisfies a `require_approval()` check for the other.

**No credential or sensitive value ever belongs in an
`IntegrationProposal`.** `diff_summary` is built from `git diff
--stat` only — file names and line-count statistics, never full patch
content — specifically so an Agent's output can never leak a secret
into an approval report that may be read, logged, or displayed.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from universal_orchestrator.errors import OrchestratorError


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_proposal_id() -> str:
    return f"proposal_{uuid.uuid4().hex[:12]}"


def _new_decision_id() -> str:
    return f"approval_{uuid.uuid4().hex[:12]}"


class ApprovalDecisionType(enum.Enum):
    APPROVED = "approved"
    DENIED = "denied"


@dataclass(slots=True)
class IntegrationProposal:
    task_id: str
    project_id: str
    action: str
    worktree_path: str
    diff_identity: str
    target_base_sha: str
    diff_summary: str
    quality_verdict: str
    conflict_report_id: str
    conflict_blocks: bool
    proposal_id: str = field(default_factory=_new_proposal_id)
    proposed_at: datetime = field(default_factory=_utcnow)


@dataclass(slots=True)
class ApprovalDecision:
    task_id: str
    project_id: str
    action: str
    diff_identity: str
    decision: ApprovalDecisionType
    reason: str = ""
    decision_id: str = field(default_factory=_new_decision_id)
    decided_at: datetime = field(default_factory=_utcnow)


class ApprovalOperationError(OrchestratorError):
    """Raised for a refused or malformed Approval Gate operation that
    is not itself the normal "not yet approved" outcome
    (`errors.ApprovalRequiredError`, already existing since Milestone
    1, is reused for that) — e.g. attempting to APPROVE a proposal that
    Conflict Manager has already marked as blocking."""

    def __init__(self, task_id: str, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"Approval Gate operation refused for task '{task_id}': {reason}")
