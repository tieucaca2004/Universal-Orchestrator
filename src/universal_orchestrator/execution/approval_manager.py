"""Approval Gate — Milestone 6.3 (ROADMAP.md §10.3).

Requires an explicit, recorded Founder decision before any sensitive
action — above all, integrating an Agent's worktree into the
protected target branch. This module never decides anything on its
own: `record_decision()` only ever persists a decision a human
actually made; `require_approval()` only ever succeeds if that exact
decision already exists for the exact proposal being asked about.

No default, no timeout, no inferred consent: `require_approval()` has
no code path that returns successfully without a matching, persisted
`ApprovalDecisionType.APPROVED` row. Silence (no decision recorded),
a timeout (not implemented — there is nothing to time out into), an
ambiguous response (not representable — decisions are a two-value
enum), and a previous approval for a *different* `diff_identity` or
`action` all reach the same place: `errors.ApprovalRequiredError`
(reused from Milestone 1, not reinvented).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from universal_orchestrator.core.state import StateManager
from universal_orchestrator.errors import ApprovalRequiredError
from universal_orchestrator.execution.approval import (
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalOperationError,
    IntegrationProposal,
)
from universal_orchestrator.execution.conflict import ConflictReport
from universal_orchestrator.execution.worktree import WorktreeHandle

_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")
_GIT_TIMEOUT_SECONDS = 30.0


def _env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}


class ApprovalGate:
    def __init__(self, state: StateManager) -> None:
        self._state = state

    # -- building and presenting a proposal -----------------------------

    def build_integration_proposal(
        self,
        repository_path: str,
        handle: WorktreeHandle,
        conflict_report: ConflictReport,
        quality_verdict: str,
    ) -> IntegrationProposal:
        """Build the exact thing a human decision is made against:
        task id, source worktree, a safe (`git diff --stat` only —
        never full patch content, never file contents) diff summary,
        the Quality Gate verdict, and the Conflict Manager report. The
        proposal's `diff_identity` is the worktree branch's real
        current commit SHA — not an invented token."""
        repo = Path(repository_path)
        diff_identity = self._run_git(
            handle.task_id, repo, ["rev-parse", handle.branch]
        ).strip()
        diff_summary = self._run_git(
            handle.task_id,
            repo,
            ["diff", "--stat", conflict_report.base_sha, handle.branch],
        ).strip()

        proposal = IntegrationProposal(
            task_id=handle.task_id,
            project_id=handle.project_id,
            action="integrate",
            worktree_path=handle.path,
            diff_identity=diff_identity,
            target_base_sha=conflict_report.base_sha,
            diff_summary=diff_summary,
            quality_verdict=quality_verdict,
            conflict_report_id=conflict_report.report_id,
            conflict_blocks=conflict_report.blocks_integration,
        )
        self._state.save_integration_proposal(proposal)
        return proposal

    def get_proposal(self, task_id: str) -> IntegrationProposal | None:
        return self._state.latest_integration_proposal(task_id)

    def list_proposals(self) -> list[IntegrationProposal]:
        """Every task's most recent proposal — used by the Approval
        CLI (Milestone 6 remediation, Item 2) to enumerate what exists.
        Does not itself say which ones are still awaiting a decision;
        see `is_pending`."""
        return self._state.list_latest_integration_proposals()

    def is_pending(self, proposal: IntegrationProposal) -> bool:
        """True exactly when no decision has been recorded for this
        proposal's exact `(action, diff_identity)` — never inferred
        from whether a *different* diff for the same task was
        decided."""
        decisions = [
            d
            for d in self._state.get_approval_decisions(proposal.task_id)
            if d.action == proposal.action and d.diff_identity == proposal.diff_identity
        ]
        return not decisions

    # -- recording a decision -----------------------------------------

    def record_decision(
        self,
        proposal: IntegrationProposal,
        decision: ApprovalDecisionType,
        *,
        reason: str = "",
    ) -> ApprovalDecision:
        """Persists a decision a human actually made — never inferred,
        never defaulted. Refuses outright to record an APPROVED
        decision for a proposal Conflict Manager has already marked
        as blocking (`conflict_blocks=True`) or whose Quality Gate
        verdict was not PASS — defense in depth, so a mistaken approval
        can never override what the earlier gates already determined."""
        if decision is ApprovalDecisionType.APPROVED:
            if proposal.conflict_blocks:
                raise ApprovalOperationError(
                    proposal.task_id,
                    "cannot approve: Conflict Manager reports an unresolved "
                    f"conflict for report '{proposal.conflict_report_id}'",
                )
            if proposal.quality_verdict != "PASS":
                raise ApprovalOperationError(
                    proposal.task_id,
                    f"cannot approve: Quality Gate verdict is "
                    f"'{proposal.quality_verdict}', not PASS",
                )

        record = ApprovalDecision(
            task_id=proposal.task_id,
            project_id=proposal.project_id,
            action=proposal.action,
            diff_identity=proposal.diff_identity,
            decision=decision,
            reason=reason,
        )
        self._state.save_approval_decision(record)
        return record

    # -- the actual gate -------------------------------------------------

    def require_approval(
        self, task_id: str, action: str, diff_identity: str
    ) -> ApprovalDecision:
        """The one call every sensitive action must pass before
        proceeding. Matches on `(task_id, action, diff_identity)`
        exactly — a decision for a different action or a different
        (now-stale) diff_identity never satisfies this. If more than
        one decision exists for the exact same triple (a re-decision),
        the most recently recorded one governs."""
        decisions = [
            d
            for d in self._state.get_approval_decisions(task_id)
            if d.action == action and d.diff_identity == diff_identity
        ]
        if not decisions:
            raise ApprovalRequiredError(
                action=f"{action}:{task_id}",
                reason=f"no recorded decision for diff_identity '{diff_identity}'",
            )
        latest = max(decisions, key=lambda d: d.decided_at)
        if latest.decision is not ApprovalDecisionType.APPROVED:
            raise ApprovalRequiredError(
                action=f"{action}:{task_id}",
                reason=f"latest decision was '{latest.decision.value}'",
            )
        return latest

    def get_decisions(self, task_id: str) -> list[ApprovalDecision]:
        return self._state.get_approval_decisions(task_id)

    # -- internals -----------------------------------------------------

    def _run_git(self, task_id: str, repo: Path, args: list[str]) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            env=_env(),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise ApprovalOperationError(
                task_id, f"git {' '.join(args)} failed: {result.stderr.strip()}"
            )
        return result.stdout
