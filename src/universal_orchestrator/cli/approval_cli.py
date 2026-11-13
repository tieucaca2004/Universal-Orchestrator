"""Approval CLI — M6 remediation, Item 2 (ROADMAP.md §10.3).

A real command-line interface, not just a Python function a caller
happens to invoke directly:

    python3 -m universal_orchestrator.cli.approval_cli list
    python3 -m universal_orchestrator.cli.approval_cli show <task_id>
    python3 -m universal_orchestrator.cli.approval_cli approve <task_id> --reason "..."
    python3 -m universal_orchestrator.cli.approval_cli reject <task_id> --reason "..."
    python3 -m universal_orchestrator.cli.approval_cli history <task_id>

Every subcommand is explicit; there is no default subcommand and no
default decision. `approve`/`reject` always require the Founder to
type the exact word — nothing here ever infers consent from silence,
a timeout (none exists), or an error path. An unexpected error always
prints a message and exits non-zero; it never falls through to a
decision of any kind.

**Security:** only the fields `IntegrationProposal`, `QualityReport`,
and `ConflictReport` already carry are ever displayed — `diff_summary`
is `git diff --stat` output only (file names and change counts, never
full patch content, by construction since Milestone 6.3). No
credential, token, password, or environment secret is read or
displayed by this module; it only reads the orchestrator's own SQLite
state.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from universal_orchestrator.config.settings import Settings, load_settings
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.execution.approval import ApprovalDecisionType, IntegrationProposal
from universal_orchestrator.execution.approval_manager import ApprovalGate
from universal_orchestrator.execution.conflict_manager import ConflictManager
from universal_orchestrator.execution.quality_manager import QualityGate
from universal_orchestrator.execution.worktree_manager import WorktreeManager


class ApprovalCLI:
    """The actual logic behind every subcommand, kept separate from
    argument parsing so it can be exercised directly in tests without
    spawning a subprocess — but every one of these methods is exactly
    what the CLI entry point below calls, nothing more."""

    def __init__(self, state: StateManager) -> None:
        self.state = state
        self.worktrees = WorktreeManager(state)
        self.conflicts = ConflictManager(state, self.worktrees)
        self.approvals = ApprovalGate(state)
        self.quality = QualityGate(state)

    def list_pending(self) -> list[IntegrationProposal]:
        return [p for p in self.approvals.list_proposals() if self.approvals.is_pending(p)]

    def format_list(self) -> str:
        pending = self.list_pending()
        if not pending:
            return "No pending Integration Proposals."
        lines = [f"{len(pending)} pending Integration Proposal(s):", ""]
        for p in pending:
            lines.append(
                f"  task_id={p.task_id}  project_id={p.project_id}  action={p.action}  "
                f"diff_identity={p.diff_identity[:12]}  conflict_blocks={p.conflict_blocks}  "
                f"quality_verdict={p.quality_verdict}  proposed_at={p.proposed_at.isoformat()}"
            )
        return "\n".join(lines)

    def format_detail(self, task_id: str) -> str:
        proposal = self.approvals.get_proposal(task_id)
        if proposal is None:
            return f"No Integration Proposal on record for task '{task_id}'."

        lines = [
            f"Task ID         : {proposal.task_id}",
            f"Project ID      : {proposal.project_id}",
            f"Action          : {proposal.action}",
            f"Worktree path   : {proposal.worktree_path}",
            f"Diff identity   : {proposal.diff_identity}",
            f"Target base SHA : {proposal.target_base_sha}",
            f"Proposed at     : {proposal.proposed_at.isoformat()}",
            "",
            "Diff summary (git diff --stat only — never full file content):",
            proposal.diff_summary or "(no changes)",
            "",
            f"Task-level Quality Gate verdict : {proposal.quality_verdict}",
        ]
        quality_report = self.quality.latest(task_id, level="task")
        if quality_report is not None:
            lines.append(f"  Rationale: {quality_report.rationale}")
            for criterion, satisfied in quality_report.criteria_evidence.items():
                lines.append(f"  - {criterion}: {satisfied}")

        lines.append("")
        lines.append(f"Conflict Manager report ({proposal.conflict_report_id}):")
        lines.append(f"  Base SHA checked against : {proposal.target_base_sha}")
        lines.append(f"  Blocks integration       : {proposal.conflict_blocks}")
        matching_reports = [
            r for r in self.conflicts.get_reports(task_id) if r.report_id == proposal.conflict_report_id
        ]
        if matching_reports:
            report = matching_reports[-1]
            if report.path_overlap:
                lines.append("  Path overlap (tier 1, informational):")
                for other_task, paths in report.path_overlap.items():
                    lines.append(f"    - overlaps with {other_task}: {', '.join(paths)}")
            if report.tier2_conflicting:
                lines.append(f"  Tier-2 conflicting files: {', '.join(report.tier2_conflicting_files)}")

        lines.append("")
        lines.append("Decision history:")
        decisions = self.approvals.get_decisions(task_id)
        if not decisions:
            lines.append("  (none yet)")
        for d in decisions:
            lines.append(
                f"  {d.decided_at.isoformat()}  {d.decision.value.upper()}  "
                f"diff={d.diff_identity[:12]}  reason={d.reason!r}"
            )
        is_pending = self.approvals.is_pending(proposal)
        lines.append("")
        lines.append(
            "This proposal is PENDING — no decision recorded for its exact diff."
            if is_pending
            else "This proposal already has a recorded decision for its exact diff."
        )
        return "\n".join(lines)

    def approve(self, task_id: str, reason: str) -> str:
        proposal = self.approvals.get_proposal(task_id)
        if proposal is None:
            raise SystemExit(f"No Integration Proposal on record for task '{task_id}'.")
        decision = self.approvals.record_decision(
            proposal, ApprovalDecisionType.APPROVED, reason=reason
        )
        return (
            f"APPROVED task '{task_id}' (diff {decision.diff_identity[:12]}) "
            f"at {decision.decided_at.isoformat()}."
        )

    def reject(self, task_id: str, reason: str) -> str:
        proposal = self.approvals.get_proposal(task_id)
        if proposal is None:
            raise SystemExit(f"No Integration Proposal on record for task '{task_id}'.")
        decision = self.approvals.record_decision(
            proposal, ApprovalDecisionType.DENIED, reason=reason
        )
        return (
            f"REJECTED task '{task_id}' (diff {decision.diff_identity[:12]}) "
            f"at {decision.decided_at.isoformat()}."
        )

    def format_history(self, task_id: str) -> str:
        decisions = self.approvals.get_decisions(task_id)
        if not decisions:
            return f"No decisions recorded for task '{task_id}'."
        lines = [f"Decision history for task '{task_id}':"]
        for d in decisions:
            lines.append(
                f"  {d.decided_at.isoformat()}  {d.decision.value.upper()}  "
                f"action={d.action}  diff={d.diff_identity[:12]}  reason={d.reason!r}"
            )
        return "\n".join(lines)


def _open_state(db_path: str | None) -> StateManager:
    if db_path is not None:
        return StateManager(Path(db_path))
    settings: Settings = load_settings()
    return StateManager(settings.state_db_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="approval_cli",
        description="Review and decide on pending M6 Integration Proposals.",
    )
    parser.add_argument(
        "--db", default=None, help="Path to the orchestrator SQLite database (default: config settings)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List pending Integration Proposals.")

    show = subparsers.add_parser("show", help="Show full detail for one task's proposal.")
    show.add_argument("task_id")

    approve = subparsers.add_parser("approve", help="Approve a task's current proposal.")
    approve.add_argument("task_id")
    approve.add_argument("--reason", default="", help="Why this is being approved.")

    reject = subparsers.add_parser("reject", help="Reject a task's current proposal.")
    reject.add_argument("task_id")
    reject.add_argument("--reason", default="", help="Why this is being rejected.")

    history = subparsers.add_parser("history", help="Show the full decision history for a task.")
    history.add_argument("task_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    state = _open_state(args.db)
    try:
        cli = ApprovalCLI(state)
        if args.command == "list":
            print(cli.format_list())
        elif args.command == "show":
            print(cli.format_detail(args.task_id))
        elif args.command == "approve":
            print(cli.approve(args.task_id, args.reason))
        elif args.command == "reject":
            print(cli.reject(args.task_id, args.reason))
        elif args.command == "history":
            print(cli.format_history(args.task_id))
        else:  # pragma: no cover - argparse's `required=True` prevents this
            parser.print_help()
            return 2
        return 0
    finally:
        state.close()


if __name__ == "__main__":
    sys.exit(main())
