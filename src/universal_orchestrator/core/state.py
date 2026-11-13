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

from universal_orchestrator.core.models import Task, TaskError, TaskSpecification, TaskStatus
from universal_orchestrator.errors import DuplicateError, NotFoundError

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
