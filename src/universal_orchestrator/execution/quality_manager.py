"""Quality Gate — Milestone 6.4 (ROADMAP.md §10.4).

Judges whether a completed execution actually met its acceptance
criteria — never merely that the process exited 0 (`ExecutionResult
.success` has always been documented, since Milestone 4, as a
process-level signal only; this module is what finally enforces that
distinction instead of just disclosing it).
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from universal_orchestrator.core.models import TaskSpecification
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.execution.adapter import ExecutionOutcome, ExecutionResult
from universal_orchestrator.execution.quality import QualityReport, QualityVerdict

_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")
_COMMAND_TIMEOUT_SECONDS = 300.0


def _env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}


class QualityGate:
    def __init__(self, state: StateManager) -> None:
        self._state = state

    # -- task-level --------------------------------------------------------

    def evaluate_task(
        self,
        specification: TaskSpecification | None,
        result: ExecutionResult,
        *,
        criteria_evidence: dict[str, bool] | None = None,
    ) -> QualityReport:
        """Never infers correctness from `result.outcome` alone. A
        completed process (`ExecutionOutcome.COMPLETED`) is necessary
        but not sufficient — it only clears the process-level bar
        `ExecutionResult.success` has always represented.

        `criteria_evidence` must be supplied by whoever actually
        checked the work (a human today). A criterion with no
        corresponding entry is **not** assumed to have passed — it
        produces NOT_VERIFIED, exactly like having no evidence at
        all, because that is what it is."""
        if result.outcome is not ExecutionOutcome.COMPLETED:
            report = QualityReport(
                task_id=result.task_id,
                level="task",
                verdict=QualityVerdict.FAIL,
                rationale=(
                    f"execution outcome was '{result.outcome.value}', not "
                    "COMPLETED — a non-completed process cannot pass "
                    "regardless of acceptance criteria"
                ),
            )
            self._state.save_quality_report(report)
            return report

        criteria = list(specification.acceptance_criteria) if specification else []
        if not criteria:
            report = QualityReport(
                task_id=result.task_id,
                level="task",
                verdict=QualityVerdict.NOT_VERIFIED,
                rationale="task declares no acceptance_criteria to verify against",
            )
            self._state.save_quality_report(report)
            return report

        evidence = criteria_evidence or {}
        per_criterion: dict[str, bool | None] = {c: evidence.get(c) for c in criteria}
        missing = [c for c, v in per_criterion.items() if v is None]
        if missing:
            report = QualityReport(
                task_id=result.task_id,
                level="task",
                verdict=QualityVerdict.NOT_VERIFIED,
                rationale=f"no evidence supplied for: {', '.join(missing)}",
                criteria_evidence=per_criterion,
            )
            self._state.save_quality_report(report)
            return report

        if all(per_criterion.values()):
            verdict = QualityVerdict.PASS
            rationale = "all declared acceptance criteria have confirming evidence"
        else:
            verdict = QualityVerdict.FAIL
            failed = [c for c, v in per_criterion.items() if not v]
            rationale = f"evidence contradicts: {', '.join(failed)}"

        report = QualityReport(
            task_id=result.task_id,
            level="task",
            verdict=verdict,
            rationale=rationale,
            criteria_evidence=per_criterion,
        )
        self._state.save_quality_report(report)
        return report

    # -- integration-level -----------------------------------------------

    def evaluate_integration(
        self, task_id: str, repository_path: str, required_testing_commands: list[str]
    ) -> QualityReport:
        """Runs the project's own declared testing commands against the
        real, post-integration repository state — never an invented
        universal test, never just the pre-integration worktree's own
        result. Two independently-PASSing tasks are never assumed to
        imply this passes; this always actually runs."""
        if not required_testing_commands:
            report = QualityReport(
                task_id=task_id,
                level="integration",
                verdict=QualityVerdict.NOT_VERIFIED,
                rationale="project declares no required_testing_commands",
            )
            self._state.save_quality_report(report)
            return report

        repo = Path(repository_path)
        results: list[dict[str, object]] = []
        all_passed = True
        for command in required_testing_commands:
            try:
                proc = subprocess.run(
                    shlex.split(command),
                    cwd=str(repo),
                    env=_env(),
                    capture_output=True,
                    text=True,
                    timeout=_COMMAND_TIMEOUT_SECONDS,
                )
                exit_code: int | None = proc.returncode
                stdout, stderr = proc.stdout, proc.stderr
            except (OSError, subprocess.TimeoutExpired) as exc:
                exit_code = None
                stdout, stderr = "", str(exc)
            results.append(
                {"command": command, "exit_code": exit_code, "stdout": stdout, "stderr": stderr}
            )
            if exit_code != 0:
                all_passed = False

        verdict = QualityVerdict.PASS if all_passed else QualityVerdict.FAIL
        rationale = (
            "all required testing commands exited 0"
            if all_passed
            else "at least one required testing command failed or could not run"
        )
        report = QualityReport(
            task_id=task_id,
            level="integration",
            verdict=verdict,
            rationale=rationale,
            commands_run=list(required_testing_commands),
            command_results=results,
        )
        self._state.save_quality_report(report)
        return report

    # -- queries ---------------------------------------------------------

    def latest(self, task_id: str, level: str) -> QualityReport | None:
        return self._state.latest_quality_report(task_id, level)

    def get_reports(self, task_id: str) -> list[QualityReport]:
        return self._state.get_quality_reports(task_id)
