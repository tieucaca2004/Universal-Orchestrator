"""State Manager: durable Task persistence on SQLite, via the Python
standard-library `sqlite3` module only — no ORM, no extra dependency.

Every write goes through `sqlite3.Connection` as a context manager, so a
failure rolls the transaction back instead of leaving a half-written row
(see `_write`). `task_id` is the table's primary key, so creating a task
whose id already exists raises `errors.DuplicateError` rather than
silently overwriting it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from universal_orchestrator.core.models import (
    RejectedCandidate,
    RoutingDecision,
    RoutingOutcome,
    Task,
    TaskError,
    TaskSpecification,
    TaskStatus,
)
from universal_orchestrator.errors import DuplicateError, NotFoundError
from universal_orchestrator.execution.adapter import (
    ExecutionOutcome,
    ExecutionResult,
    VerificationStatus,
)
from universal_orchestrator.execution.approval import (
    ApprovalDecision,
    ApprovalDecisionType,
    IntegrationProposal,
)
from universal_orchestrator.execution.conflict import ConflictReport
from universal_orchestrator.execution.quality import QualityReport, QualityVerdict
from universal_orchestrator.execution.worktree import WorktreeHandle, WorktreeStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_id TEXT,
    depends_on TEXT NOT NULL DEFAULT '[]',
    priority INTEGER NOT NULL DEFAULT 0,
    owner TEXT,
    agent_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    specification TEXT,
    result TEXT,
    error TEXT,
    history TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent_id ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- Routing decisions are their own table, deliberately: the Agent Router
-- never writes to `tasks`, so a routing decision can never corrupt a
-- task's row or history (see Milestone 3 / docs/ROUTING.md).
CREATE TABLE IF NOT EXISTS routing_decisions (
    decision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    explanation TEXT NOT NULL,
    selected_agent_id TEXT,
    selected_provider_id TEXT,
    matched_capabilities TEXT NOT NULL DEFAULT '[]',
    rejected TEXT NOT NULL DEFAULT '[]',
    score REAL,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routing_decisions_task_id ON routing_decisions(task_id);

-- Same reasoning as routing_decisions: its own table, so an execution
-- result can never corrupt a task's row or history (Milestone 4).
CREATE TABLE IF NOT EXISTS execution_results (
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    exit_code INTEGER,
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    ended_at TEXT,
    duration_seconds REAL,
    cli_version TEXT,
    output_format TEXT,
    structured_output TEXT,
    structured_output_valid INTEGER,
    verification_status TEXT,
    error_classification TEXT
);
CREATE INDEX IF NOT EXISTS idx_execution_results_task_id ON execution_results(task_id);

-- Milestone 6.1: one worktree per task, enforced by making task_id the
-- primary key — the same IntegrityError -> DuplicateError translation
-- pattern already used for tasks/execution_results guarantees "two
-- executions cannot acquire the same Worktree" at the persistence layer,
-- not just in application logic.
CREATE TABLE IF NOT EXISTS worktrees (
    task_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    path TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_worktrees_project_id ON worktrees(project_id);
CREATE INDEX IF NOT EXISTS idx_worktrees_status ON worktrees(status);

-- Milestone 6.2: every conflict check is tagged with the exact target-
-- branch SHA it was run against (`base_sha`) — this is what lets a
-- stale report be detected and rejected before integration, never
-- trusted just because it exists.
CREATE TABLE IF NOT EXISTS conflict_reports (
    report_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    worktree_branch TEXT NOT NULL,
    path_overlap TEXT NOT NULL DEFAULT '{}',
    tier2_conflicting INTEGER NOT NULL,
    tier2_conflicting_files TEXT NOT NULL DEFAULT '[]',
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conflict_reports_task_id ON conflict_reports(task_id);

-- Milestone 6.2: a single-row mutex. Real Git integration into the
-- target branch must be serialized — many Agents may execute and
-- prepare changes concurrently, but only one integration may actually
-- be in flight at a time. `holder_task_id IS NULL` means free.
CREATE TABLE IF NOT EXISTS integration_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    holder_task_id TEXT,
    acquired_at TEXT
);

-- Milestone 6.3: what was actually presented for a decision — never
-- regenerated from memory after the fact, so the decision recorded
-- against it always refers to the exact same diff_identity/base_sha.
CREATE TABLE IF NOT EXISTS integration_proposals (
    proposal_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    action TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    diff_identity TEXT NOT NULL,
    target_base_sha TEXT NOT NULL,
    diff_summary TEXT NOT NULL DEFAULT '',
    quality_verdict TEXT NOT NULL,
    conflict_report_id TEXT NOT NULL,
    conflict_blocks INTEGER NOT NULL,
    proposed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_integration_proposals_task_id ON integration_proposals(task_id);

-- Milestone 6.3: a human decision, bound to (task_id, action,
-- diff_identity) — never inferred from silence, timeout, or a
-- decision recorded for a different action or a different diff.
CREATE TABLE IF NOT EXISTS approval_decisions (
    decision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    action TEXT NOT NULL,
    diff_identity TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_decisions_task_id ON approval_decisions(task_id);

-- Milestone 6.4: task-level and integration-level verdicts share one
-- table, distinguished by `level` — never conflated (a query always
-- filters by level explicitly; see TaskManager.succeed()'s use of
-- level="task" specifically).
CREATE TABLE IF NOT EXISTS quality_reports (
    report_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    level TEXT NOT NULL,
    verdict TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    criteria_evidence TEXT NOT NULL DEFAULT '{}',
    commands_run TEXT NOT NULL DEFAULT '[]',
    command_results TEXT NOT NULL DEFAULT '[]',
    evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_reports_task_id ON quality_reports(task_id);
CREATE INDEX IF NOT EXISTS idx_quality_reports_level ON quality_reports(level);
"""

_COLUMNS = (
    "task_id",
    "title",
    "project_id",
    "status",
    "parent_id",
    "depends_on",
    "priority",
    "owner",
    "agent_id",
    "payload",
    "specification",
    "result",
    "error",
    "history",
    "created_at",
    "updated_at",
)


class StateManager:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False (Milestone 6.2): the integration lock
        # must be provably atomic under real concurrent callers, not just
        # sequential calls, so this connection needs to be usable from
        # more than one thread. The actual serialization guarantee is
        # `_integration_lock_mutex` below, not this flag by itself — see
        # its own comment for why.
        self._conn = sqlite3.connect(
            str(self.db_path), isolation_level=None, check_same_thread=False
        )
        # `check_same_thread=False` only lifts Python's own thread-affinity
        # check — it does not make concurrent `execute()` calls from two
        # threads on one connection safe by itself (discovered by a real,
        # repeatable flaky failure in a genuine multi-thread test of the
        # integration lock below, not assumed from documentation). This
        # lock is the actual serialization guarantee for the one method
        # pair that is deliberately exercised under real concurrency:
        # `acquire_integration_lock`/`release_integration_lock`.
        self._integration_lock_mutex = threading.Lock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def _initialize(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO integration_lock (id, holder_task_id, acquired_at) "
                "VALUES (1, NULL, NULL)"
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateManager":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- writes --------------------------------------------------------------

    def create(self, task: Task) -> None:
        row = _task_to_row(task)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        try:
            with self._conn:
                self._conn.execute(
                    f"INSERT INTO tasks ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                    [row[col] for col in _COLUMNS],
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateError("Task", task.task_id) from exc

    def update(self, task: Task) -> None:
        row = _task_to_row(task)
        assignments = ", ".join(f"{col} = ?" for col in _COLUMNS if col != "task_id")
        values = [row[col] for col in _COLUMNS if col != "task_id"]
        with self._conn:
            cursor = self._conn.execute(
                f"UPDATE tasks SET {assignments} WHERE task_id = ?",
                [*values, task.task_id],
            )
        if cursor.rowcount == 0:
            raise NotFoundError("Task", task.task_id)

    # -- reads ---------------------------------------------------------------

    def get(self, task_id: str) -> Task | None:
        cursor = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cursor.fetchone()
        return _row_to_task(row) if row is not None else None

    def list(
        self, *, project_id: str | None = None, status: TaskStatus | None = None
    ) -> list[Task]:
        query = "SELECT * FROM tasks"
        clauses = []
        params: list[str] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        cursor = self._conn.execute(query, params)
        return [_row_to_task(row) for row in cursor.fetchall()]

    # -- routing decisions -----------------------------------------------------

    def save_routing_decision(self, decision: RoutingDecision) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO routing_decisions (
                    decision_id, task_id, project_id, outcome, explanation,
                    selected_agent_id, selected_provider_id,
                    matched_capabilities, rejected, score, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.task_id,
                    decision.project_id,
                    decision.outcome.value,
                    decision.explanation,
                    decision.selected_agent_id,
                    decision.selected_provider_id,
                    json.dumps(decision.matched_capabilities),
                    json.dumps([asdict(r) for r in decision.rejected]),
                    decision.score,
                    decision.decided_at.isoformat(),
                ),
            )

    def get_routing_decisions(self, task_id: str) -> list[RoutingDecision]:
        cursor = self._conn.execute(
            "SELECT * FROM routing_decisions WHERE task_id = ? ORDER BY decided_at",
            (task_id,),
        )
        return [_row_to_routing_decision(row) for row in cursor.fetchall()]

    # -- execution results -----------------------------------------------------

    def save_execution_result(self, result: ExecutionResult) -> None:
        """Raises `errors.DuplicateError` on a repeated `execution_id` —
        the same `sqlite3.IntegrityError` -> typed-error translation
        `create()` already does for tasks (audit Finding E-2)."""
        params = (
            result.execution_id,
            result.task_id,
            result.outcome.value,
            result.exit_code,
            result.stdout,
            result.stderr,
            result.started_at.isoformat() if result.started_at else None,
            result.ended_at.isoformat() if result.ended_at else None,
            result.duration_seconds,
            result.cli_version,
            result.output_format,
            json.dumps(result.structured_output) if result.structured_output is not None else None,
            None if result.structured_output_valid is None else int(result.structured_output_valid),
            result.verification_status.value if result.verification_status else None,
            result.error_classification,
        )
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO execution_results (
                        execution_id, task_id, outcome, exit_code, stdout, stderr,
                        started_at, ended_at, duration_seconds, cli_version,
                        output_format, structured_output, structured_output_valid,
                        verification_status, error_classification
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateError("ExecutionResult", result.execution_id) from exc

    def get_execution_results(self, task_id: str) -> list[ExecutionResult]:
        cursor = self._conn.execute(
            "SELECT * FROM execution_results WHERE task_id = ? ORDER BY started_at",
            (task_id,),
        )
        return [_row_to_execution_result(row) for row in cursor.fetchall()]

    # -- worktrees (Milestone 6.1) ---------------------------------------------

    def save_worktree(self, handle: WorktreeHandle) -> None:
        """Raises `errors.DuplicateError` on a repeated `task_id` — this
        is the actual enforcement point for "two executions cannot
        acquire the same Worktree" (see `execution.worktree_manager`)."""
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO worktrees (
                        task_id, project_id, path, branch, base_sha, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        handle.task_id,
                        handle.project_id,
                        handle.path,
                        handle.branch,
                        handle.base_sha,
                        handle.status.value,
                        handle.created_at.isoformat(),
                        handle.updated_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateError("Worktree", handle.task_id) from exc

    def update_worktree(self, handle: WorktreeHandle) -> None:
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE worktrees SET
                    project_id = ?, path = ?, branch = ?, base_sha = ?,
                    status = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    handle.project_id,
                    handle.path,
                    handle.branch,
                    handle.base_sha,
                    handle.status.value,
                    handle.updated_at.isoformat(),
                    handle.task_id,
                ),
            )
        if cursor.rowcount == 0:
            raise NotFoundError("Worktree", handle.task_id)

    def get_worktree(self, task_id: str) -> WorktreeHandle | None:
        cursor = self._conn.execute(
            "SELECT * FROM worktrees WHERE task_id = ?", (task_id,)
        )
        row = cursor.fetchone()
        return _row_to_worktree(row) if row is not None else None

    def list_worktrees(
        self, *, project_id: str | None = None, status: WorktreeStatus | None = None
    ) -> list[WorktreeHandle]:
        query = "SELECT * FROM worktrees"
        clauses = []
        params: list[str] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        cursor = self._conn.execute(query, params)
        return [_row_to_worktree(row) for row in cursor.fetchall()]

    # -- conflict reports (Milestone 6.2) --------------------------------------

    def save_conflict_report(self, report: ConflictReport) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO conflict_reports (
                    report_id, task_id, project_id, base_sha, worktree_branch,
                    path_overlap, tier2_conflicting, tier2_conflicting_files,
                    checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.task_id,
                    report.project_id,
                    report.base_sha,
                    report.worktree_branch,
                    json.dumps(report.path_overlap),
                    int(report.tier2_conflicting),
                    json.dumps(report.tier2_conflicting_files),
                    report.checked_at.isoformat(),
                ),
            )

    def get_conflict_reports(self, task_id: str) -> list[ConflictReport]:
        cursor = self._conn.execute(
            "SELECT * FROM conflict_reports WHERE task_id = ? ORDER BY checked_at",
            (task_id,),
        )
        return [_row_to_conflict_report(row) for row in cursor.fetchall()]

    def latest_conflict_report(self, task_id: str) -> ConflictReport | None:
        reports = self.get_conflict_reports(task_id)
        return reports[-1] if reports else None

    # -- integration lock (Milestone 6.2) --------------------------------------

    def acquire_integration_lock(self, holder_task_id: str) -> bool:
        """Atomic: only succeeds if the lock is currently free
        (`holder_task_id IS NULL`). The UPDATE's own WHERE clause makes
        the *decision* atomic at the SQL level, but a real multi-thread
        test of this method against one shared connection (discovered
        empirically, not assumed) showed that `check_same_thread=False`
        alone does not make two threads' `execute()` calls on one
        `sqlite3.Connection` safe to interleave — this method's own
        `_integration_lock_mutex` is what actually serializes callers at
        the Python level; without it, a second thread's call could
        raise instead of cleanly returning `False`."""
        with self._integration_lock_mutex:
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE integration_lock SET holder_task_id = ?, acquired_at = ? "
                    "WHERE id = 1 AND holder_task_id IS NULL",
                    (holder_task_id, datetime.now(timezone.utc).isoformat()),
                )
            return cursor.rowcount == 1

    def release_integration_lock(self, holder_task_id: str) -> bool:
        """Only the actual holder may release — never a bare `release()`
        that could free a lock someone else is legitimately holding."""
        with self._integration_lock_mutex:
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE integration_lock SET holder_task_id = NULL, acquired_at = NULL "
                    "WHERE id = 1 AND holder_task_id = ?",
                    (holder_task_id,),
                )
            return cursor.rowcount == 1

    def current_integration_lock_holder(self) -> str | None:
        with self._integration_lock_mutex:
            cursor = self._conn.execute(
                "SELECT holder_task_id FROM integration_lock WHERE id = 1"
            )
            row = cursor.fetchone()
            return row["holder_task_id"] if row is not None else None

    # -- integration proposals (Milestone 6.3) ---------------------------------

    def save_integration_proposal(self, proposal: IntegrationProposal) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO integration_proposals (
                    proposal_id, task_id, project_id, action, worktree_path,
                    diff_identity, target_base_sha, diff_summary,
                    quality_verdict, conflict_report_id, conflict_blocks,
                    proposed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.proposal_id,
                    proposal.task_id,
                    proposal.project_id,
                    proposal.action,
                    proposal.worktree_path,
                    proposal.diff_identity,
                    proposal.target_base_sha,
                    proposal.diff_summary,
                    proposal.quality_verdict,
                    proposal.conflict_report_id,
                    int(proposal.conflict_blocks),
                    proposal.proposed_at.isoformat(),
                ),
            )

    def latest_integration_proposal(self, task_id: str) -> IntegrationProposal | None:
        cursor = self._conn.execute(
            "SELECT * FROM integration_proposals WHERE task_id = ? "
            "ORDER BY proposed_at DESC LIMIT 1",
            (task_id,),
        )
        row = cursor.fetchone()
        return _row_to_integration_proposal(row) if row is not None else None

    # -- approval decisions (Milestone 6.3) ------------------------------------

    def save_approval_decision(self, decision: ApprovalDecision) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO approval_decisions (
                    decision_id, task_id, project_id, action, diff_identity,
                    decision, reason, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.task_id,
                    decision.project_id,
                    decision.action,
                    decision.diff_identity,
                    decision.decision.value,
                    decision.reason,
                    decision.decided_at.isoformat(),
                ),
            )

    def get_approval_decisions(self, task_id: str) -> list[ApprovalDecision]:
        cursor = self._conn.execute(
            "SELECT * FROM approval_decisions WHERE task_id = ? ORDER BY decided_at",
            (task_id,),
        )
        return [_row_to_approval_decision(row) for row in cursor.fetchall()]

    # -- quality reports (Milestone 6.4) ---------------------------------------

    def save_quality_report(self, report: QualityReport) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO quality_reports (
                    report_id, task_id, level, verdict, rationale,
                    criteria_evidence, commands_run, command_results, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.task_id,
                    report.level,
                    report.verdict.value,
                    report.rationale,
                    json.dumps(report.criteria_evidence),
                    json.dumps(report.commands_run),
                    json.dumps(report.command_results),
                    report.evaluated_at.isoformat(),
                ),
            )

    def get_quality_reports(self, task_id: str) -> list[QualityReport]:
        cursor = self._conn.execute(
            "SELECT * FROM quality_reports WHERE task_id = ? ORDER BY evaluated_at",
            (task_id,),
        )
        return [_row_to_quality_report(row) for row in cursor.fetchall()]

    def latest_quality_report(self, task_id: str, level: str) -> QualityReport | None:
        cursor = self._conn.execute(
            "SELECT * FROM quality_reports WHERE task_id = ? AND level = ? "
            "ORDER BY evaluated_at DESC LIMIT 1",
            (task_id, level),
        )
        row = cursor.fetchone()
        return _row_to_quality_report(row) if row is not None else None


def _task_to_row(task: Task) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "project_id": task.project_id,
        "status": task.status.value,
        "parent_id": task.parent_id,
        "depends_on": json.dumps(list(task.depends_on)),
        "priority": task.priority,
        "owner": task.owner,
        "agent_id": task.agent_id,
        "payload": json.dumps(task.payload, default=str),
        "specification": json.dumps(asdict(task.specification))
        if task.specification is not None
        else None,
        "result": json.dumps(task.result, default=str) if task.result is not None else None,
        "error": json.dumps(asdict(task.error)) if task.error is not None else None,
        "history": json.dumps(task.history),
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"],
        title=row["title"],
        project_id=row["project_id"],
        status=TaskStatus(row["status"]),
        parent_id=row["parent_id"],
        depends_on=tuple(json.loads(row["depends_on"])),
        priority=row["priority"],
        owner=row["owner"],
        agent_id=row["agent_id"],
        payload=json.loads(row["payload"]),
        specification=TaskSpecification(**json.loads(row["specification"]))
        if row["specification"] is not None
        else None,
        result=json.loads(row["result"]) if row["result"] is not None else None,
        error=TaskError(**json.loads(row["error"])) if row["error"] is not None else None,
        history=[tuple(entry) for entry in json.loads(row["history"])],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_routing_decision(row: sqlite3.Row) -> RoutingDecision:
    return RoutingDecision(
        decision_id=row["decision_id"],
        task_id=row["task_id"],
        project_id=row["project_id"],
        outcome=RoutingOutcome(row["outcome"]),
        explanation=row["explanation"],
        selected_agent_id=row["selected_agent_id"],
        selected_provider_id=row["selected_provider_id"],
        matched_capabilities=json.loads(row["matched_capabilities"]),
        rejected=[RejectedCandidate(**entry) for entry in json.loads(row["rejected"])],
        score=row["score"],
        decided_at=datetime.fromisoformat(row["decided_at"]),
    )


def _row_to_execution_result(row: sqlite3.Row) -> ExecutionResult:
    return ExecutionResult(
        execution_id=row["execution_id"],
        task_id=row["task_id"],
        outcome=ExecutionOutcome(row["outcome"]),
        exit_code=row["exit_code"],
        stdout=row["stdout"],
        stderr=row["stderr"],
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
        duration_seconds=row["duration_seconds"],
        cli_version=row["cli_version"],
        output_format=row["output_format"],
        structured_output=json.loads(row["structured_output"])
        if row["structured_output"] is not None
        else None,
        structured_output_valid=None
        if row["structured_output_valid"] is None
        else bool(row["structured_output_valid"]),
        verification_status=VerificationStatus(row["verification_status"])
        if row["verification_status"]
        else None,
        error_classification=row["error_classification"],
    )


def _row_to_worktree(row: sqlite3.Row) -> WorktreeHandle:
    return WorktreeHandle(
        task_id=row["task_id"],
        project_id=row["project_id"],
        path=row["path"],
        branch=row["branch"],
        base_sha=row["base_sha"],
        status=WorktreeStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_conflict_report(row: sqlite3.Row) -> ConflictReport:
    return ConflictReport(
        report_id=row["report_id"],
        task_id=row["task_id"],
        project_id=row["project_id"],
        base_sha=row["base_sha"],
        worktree_branch=row["worktree_branch"],
        path_overlap=json.loads(row["path_overlap"]),
        tier2_conflicting=bool(row["tier2_conflicting"]),
        tier2_conflicting_files=json.loads(row["tier2_conflicting_files"]),
        checked_at=datetime.fromisoformat(row["checked_at"]),
    )


def _row_to_integration_proposal(row: sqlite3.Row) -> IntegrationProposal:
    return IntegrationProposal(
        proposal_id=row["proposal_id"],
        task_id=row["task_id"],
        project_id=row["project_id"],
        action=row["action"],
        worktree_path=row["worktree_path"],
        diff_identity=row["diff_identity"],
        target_base_sha=row["target_base_sha"],
        diff_summary=row["diff_summary"],
        quality_verdict=row["quality_verdict"],
        conflict_report_id=row["conflict_report_id"],
        conflict_blocks=bool(row["conflict_blocks"]),
        proposed_at=datetime.fromisoformat(row["proposed_at"]),
    )


def _row_to_approval_decision(row: sqlite3.Row) -> ApprovalDecision:
    return ApprovalDecision(
        decision_id=row["decision_id"],
        task_id=row["task_id"],
        project_id=row["project_id"],
        action=row["action"],
        diff_identity=row["diff_identity"],
        decision=ApprovalDecisionType(row["decision"]),
        reason=row["reason"],
        decided_at=datetime.fromisoformat(row["decided_at"]),
    )


def _row_to_quality_report(row: sqlite3.Row) -> QualityReport:
    return QualityReport(
        report_id=row["report_id"],
        task_id=row["task_id"],
        level=row["level"],
        verdict=QualityVerdict(row["verdict"]),
        rationale=row["rationale"],
        criteria_evidence=json.loads(row["criteria_evidence"]),
        commands_run=json.loads(row["commands_run"]),
        command_results=json.loads(row["command_results"]),
        evaluated_at=datetime.fromisoformat(row["evaluated_at"]),
    )
