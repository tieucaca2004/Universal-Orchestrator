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
from dataclasses import asdict
from datetime import datetime
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
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def _initialize(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)

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
