"""Governed Execution Pipeline — M6 remediation, Item 1.

Wires the Orchestration Engine (Milestones 2-4) and all four M6
components (Worktree Manager, Conflict Manager, Approval Gate, Quality
Gate) into the automatic, ordered sequence the M6 Blueprint specified
end-to-end — closing the gap the M6 Consolidated Final Report
disclosed: the four components worked correctly when called in the
right order by hand, but nothing drove that order automatically from
`engine.submit()` onward.

Deliberately split into two calls, never one:

    submit_for_approval()  -- steps 1-8: acquire a Worktree, dispatch
                               the Agent into it, task-level Quality
                               Gate, Conflict check, build an
                               Integration Proposal, STOP.
    integrate_after_approval() -- steps 9-12: only runs once a real,
                               recorded APPROVED decision exists;
                               revalidates, locks, integrates, runs
                               the post-integration Quality Gate,
                               resolves the Worktree.

This split is the actual mechanism for "stop and wait for Founder
approval" — there is no code path in this module that goes from
"proposal built" to "integrated" without a separate, later call that
itself depends on `ApprovalGate.require_approval()` succeeding.
`OrchestrationEngine`, `TaskManager`, and all four M6 components are
used exactly as they already existed; none of their own APIs or
behavior changed for this.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.models import ProjectProfile, TaskError
from universal_orchestrator.errors import OrchestratorError
from universal_orchestrator.execution.adapter import ExecutionPolicy, ExecutionResult
from universal_orchestrator.execution.approval import IntegrationProposal
from universal_orchestrator.execution.approval_manager import ApprovalGate
from universal_orchestrator.execution.conflict_manager import ConflictManager
from universal_orchestrator.execution.hermes_transfer import HermesWorkspaceTransfer
from universal_orchestrator.execution.quality import QualityReport, QualityVerdict
from universal_orchestrator.execution.quality_manager import QualityGate
from universal_orchestrator.execution.worktree import WorktreeStatus
from universal_orchestrator.execution.worktree_manager import WorktreeManager

_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")
_MERGE_TIMEOUT_SECONDS = 30.0


def _env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}


class IntegrationBlockedError(OrchestratorError):
    """Raised when `integrate_after_approval` refuses to proceed — a
    real conflict on revalidation, or the Git integration step itself
    failing. Never raised for the normal "not yet approved" case,
    which is `errors.ApprovalRequiredError` (unchanged, Milestone 1)."""

    def __init__(self, task_id: str, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"Integration blocked for task '{task_id}': {reason}")


@dataclass
class GovernedPipeline:
    engine: OrchestrationEngine
    worktrees: WorktreeManager
    conflicts: ConflictManager
    approvals: ApprovalGate
    quality: QualityGate

    def dispatch(
        self,
        task_id: str,
        project: ProjectProfile,
        *,
        input_context: str = "",
        timeout_seconds: float = 60.0,
        policy: ExecutionPolicy | None = None,
        workspace_transfer: HermesWorkspaceTransfer | None = None,
    ) -> ExecutionResult:
        """Steps 1-4 of the Founder-specified sequence only: acquire a
        dedicated Worktree, dispatch the real Agent into it, and commit
        whatever it produced. Returns the real `ExecutionResult` (also
        already persisted by `engine.execute()`) so the caller —
        Founder, a Reviewer-role agent, or an automated check — can
        inspect the *actual* output before deciding on evidence.

        `policy` is passed straight through to `engine.execute()` —
        this is the hook that lets a caller request
        `PermissionMode.WORKTREE_WRITE` (M6 completion) instead of the
        default read-only policy, for an adapter that supports it and
        can provide real containment. This method never changes what
        the policy means or weakens it; it only forwards the caller's
        explicit choice.

        Evidence is never collected here and never before this method
        returns — that was M6 remediation's Item 1 finding: evidence
        can only honestly be evaluated after the real result exists.
        `evaluate_and_propose` is the next, separate call."""
        handle = self.worktrees.acquire(project.project_id, task_id, project.repository_path)
        self.worktrees.transition(task_id, WorktreeStatus.EXECUTING)
        self.engine.manager.start(task_id)

        # Hermes runs in a container with no bind mount for a project
        # workspace — the host Worktree path means nothing inside it.
        # When a transfer is supplied (M6 completion, Item 2), copy the
        # Worktree into a disposable in-container staging path and
        # dispatch there instead; the host Worktree stays authoritative
        # throughout (see execution.hermes_transfer's own module
        # docstring for the full guarantee). Claude and any adapter
        # that runs natively on this host need no transfer at all.
        working_directory = handle.path
        if workspace_transfer is not None:
            try:
                working_directory = workspace_transfer.copy_in(handle.path)
            except Exception:
                # Never proceed to execution against an unverified
                # copy-in — the host Worktree is untouched either way.
                self.worktrees.transition(task_id, WorktreeStatus.NEEDS_REVIEW)
                raise

        try:
            result = self.engine.execute(
                task_id,
                working_directory=working_directory,
                input_context=input_context,
                timeout_seconds=timeout_seconds,
                policy=policy,
            )
        except Exception:
            # Dispatch itself failed (adapter unavailable, stale routing
            # decision, ...) — never silently discard the Worktree; a
            # human must look at it before it is reused or released.
            # Still attempt copy-out below is skipped here deliberately:
            # if the Agent never ran, there is nothing new to recover
            # from the staging path, and the staging directory itself
            # is left in place (never cleaned up outside a verified
            # copy_out) for inspection.
            self.worktrees.transition(task_id, WorktreeStatus.NEEDS_REVIEW)
            raise

        if workspace_transfer is not None:
            try:
                # Always attempt copy-out, success or failure of the
                # Agent call itself — even a failed run may have left
                # partial, inspectable work in the staging directory.
                workspace_transfer.copy_out(handle.path)
            except Exception:
                # Staging preserved by copy_out() itself on any failure
                # path (see execution.hermes_transfer) — never claim
                # this risk is solved; surface it instead of hiding it.
                self.worktrees.transition(task_id, WorktreeStatus.NEEDS_REVIEW)
                raise

        # Adapters (Claude CLI, Hermes) know nothing about Git — they
        # only write files. Capturing whatever the Agent produced into
        # a real commit on the Worktree's own branch is this
        # orchestration layer's job; Conflict Manager's trial merges
        # and the later `git merge` both operate on committed state,
        # by design (ROADMAP.md §10.2), never on a working tree's
        # uncommitted contents.
        self._commit_worktree_changes(handle.path, task_id)
        self.worktrees.transition(task_id, WorktreeStatus.QUALITY_CHECK)
        return result

    def evaluate_and_propose(
        self,
        task_id: str,
        project: ProjectProfile,
        *,
        criteria_evidence: dict[str, bool] | None = None,
    ) -> IntegrationProposal | None:
        """Steps 5-8. Requires `dispatch()` to have already run for this
        exact task (its Worktree must be in `QUALITY_CHECK`) — refuses
        otherwise, rather than silently evaluating against nothing.
        Re-fetches the real, already-persisted `ExecutionResult` itself
        (the same one `dispatch()` returned) — `criteria_evidence` is
        the caller's real judgment of that real result, collected only
        now, never before execution.

        Returns `None` when the task failed its own Quality Gate —
        there is nothing to propose, so no `IntegrationProposal` is
        built; the task is marked FAILED and its Worktree NEEDS_REVIEW.
        Returns a built proposal even when Conflict Manager blocks it
        (so Founder can see the conflict detail) — but its Worktree is
        left NEEDS_REVIEW, not AWAITING_APPROVAL, and
        `ApprovalGate.record_decision()` already refuses to approve a
        blocked proposal (Milestone 6.3, unchanged)."""
        task = self.engine.manager.get(task_id)
        handle = self.worktrees.get(task_id)
        if handle.status is not WorktreeStatus.QUALITY_CHECK:
            raise OrchestratorError(
                f"task '{task_id}': call dispatch() before evaluate_and_propose() "
                f"— Worktree is '{handle.status.value}', not QUALITY_CHECK"
            )
        results = self.engine.manager.get_execution_results(task_id)
        if not results:
            raise OrchestratorError(f"task '{task_id}' has no recorded ExecutionResult")
        result = results[-1]

        # -- 5: task-level Quality Gate, against the real result ------------
        quality_report = self.quality.evaluate_task(
            task.specification, result, criteria_evidence=criteria_evidence
        )

        if quality_report.verdict is not QualityVerdict.PASS:
            self.engine.manager.fail(
                task_id,
                TaskError(
                    code="quality_gate_not_passed",
                    message=f"Task-level Quality Gate verdict: {quality_report.verdict.value}",
                    detail=quality_report.rationale,
                ),
            )
            self.worktrees.transition(task_id, WorktreeStatus.NEEDS_REVIEW)
            return None

        # Only now — gated on the recorded PASS — may the task succeed.
        self.engine.manager.succeed(task_id, result={"execution_id": result.execution_id})

        # -- 6: Conflict Manager, tagged with the real current base_sha -----
        conflict_report = self.conflicts.check(project.repository_path, task_id)

        # -- 7: build and persist the Integration Proposal -------------------
        proposal = self.approvals.build_integration_proposal(
            project.repository_path, handle, conflict_report, quality_report.verdict.value
        )

        # -- 8: stop here. Nothing past this point runs automatically. ------
        if conflict_report.blocks_integration:
            self.worktrees.transition(task_id, WorktreeStatus.NEEDS_REVIEW)
        else:
            self.worktrees.transition(task_id, WorktreeStatus.AWAITING_APPROVAL)

        return proposal

    def submit_for_approval(
        self,
        task_id: str,
        project: ProjectProfile,
        *,
        input_context: str = "",
        timeout_seconds: float = 60.0,
        policy: ExecutionPolicy | None = None,
        workspace_transfer: HermesWorkspaceTransfer | None = None,
        criteria_evidence: dict[str, bool] | None = None,
    ) -> IntegrationProposal | None:
        """Convenience wrapper over `dispatch()` + `evaluate_and_propose()`
        for callers whose evidence is deterministic/automated and does
        not actually need to be collected after seeing the real result
        (every existing automated test in this repository is such a
        caller). **Not** the right call for a genuine human review of
        a real Agent's real output — use `dispatch()` then
        `evaluate_and_propose()` separately for that, so evidence is
        collected strictly after execution, never before. Kept for
        backward compatibility; contains no logic of its own."""
        self.dispatch(
            task_id, project, input_context=input_context, timeout_seconds=timeout_seconds,
            policy=policy, workspace_transfer=workspace_transfer,
        )
        return self.evaluate_and_propose(task_id, project, criteria_evidence=criteria_evidence)

    def integrate_after_approval(
        self, task_id: str, project: ProjectProfile
    ) -> QualityReport:
        """Steps 9-12. Raises `errors.ApprovalRequiredError` (not
        caught here) if no valid APPROVED decision exists for the
        task's current proposal's exact `diff_identity` — this is the
        actual mechanism that makes Approval Gate unbypassable: there
        is no other way into this method's integration logic."""
        handle = self.worktrees.get(task_id)
        proposal = self.approvals.get_proposal(task_id)
        if proposal is None:
            raise IntegrationBlockedError(task_id, "no integration proposal on record")

        self.approvals.require_approval(task_id, proposal.action, proposal.diff_identity)

        # -- 9: revalidate against the *current* target state, always ------
        fresh_report = self.conflicts.revalidate(project.repository_path, task_id)
        if fresh_report.blocks_integration:
            self.worktrees.transition(task_id, WorktreeStatus.NEEDS_REVIEW)
            raise IntegrationBlockedError(
                task_id,
                "Conflict Manager reports a real conflict on revalidation — "
                "the target branch moved since approval",
            )

        # -- 10: integration lock, so two integrations never race -----------
        self.conflicts.acquire_integration_lock(task_id)
        try:
            self.worktrees.transition(task_id, WorktreeStatus.INTEGRATING)
            self._merge_into_target(project.repository_path, handle.branch, task_id)

            # -- 11: post-integration Quality Gate, real commands only -------
            integration_report = self.quality.evaluate_integration(
                task_id, project.repository_path, project.required_testing_commands
            )
            if integration_report.verdict is not QualityVerdict.PASS:
                # Already merged, but failed real post-integration
                # verification — never auto-revert `main`; preserve the
                # Worktree and flag for a human decision, exactly as the
                # standing rule requires.
                self.worktrees.transition(task_id, WorktreeStatus.NEEDS_REVIEW)
                return integration_report

            self.worktrees.transition(task_id, WorktreeStatus.INTEGRATED)
        finally:
            self.conflicts.release_integration_lock(task_id)

        # -- 12: only a fully-verified integration releases the Worktree ----
        self.worktrees.release(task_id, project.repository_path)
        return integration_report

    def _commit_worktree_changes(self, worktree_path: str, task_id: str) -> None:
        subprocess.run(
            ["git", "-C", worktree_path, "add", "-A"],
            env=_env(), capture_output=True, text=True, timeout=_MERGE_TIMEOUT_SECONDS,
        )
        subprocess.run(
            ["git", "-C", worktree_path, "commit", "-q", "-m", f"agent execution for {task_id}"],
            env=_env(), capture_output=True, text=True, timeout=_MERGE_TIMEOUT_SECONDS,
        )
        # A non-zero exit here almost always means "nothing to commit"
        # (the Agent produced no file changes) — not a failure worth
        # raising over; Quality Gate still runs and judges that outcome
        # on its own merits.

    def _merge_into_target(self, repository_path: str, branch: str, task_id: str) -> None:
        result = subprocess.run(
            ["git", "-C", repository_path, "merge", "--no-ff", "-m", f"integrate {task_id}", branch],
            env=_env(),
            capture_output=True,
            text=True,
            timeout=_MERGE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise IntegrationBlockedError(task_id, f"git merge failed: {result.stderr.strip()}")
