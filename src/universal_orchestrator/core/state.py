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
import os
import sqlite3
import stat
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
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
from universal_orchestrator.errors import DuplicateError, NotFoundError, SchemaVersionError
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
from universal_orchestrator.execution.scheduler import (
    AlertKind,
    AlertSeverity,
    ClaimStatus,
    SchedulerAlert,
    SchedulerLease,
    TaskClaim,
)
from universal_orchestrator.execution.worktree import WorktreeHandle, WorktreeStatus

# Audit Finding C-8: bumped only when a real, migration-worthy schema
# change ships. Every database ever produced by this codebase so far
# (M1 through M7) has this exact table shape, so version 1 covers all
# of them -- there is nothing to migrate yet, only a guard against a
# future mismatch being silently ignored.
CURRENT_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
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

-- Milestone 7 (Task Scheduler, Phase 2): single-row lease, the same
-- "one checked row, id=1" shape as `integration_lock`, plus a
-- monotonically increasing `epoch` (the fencing token) and an
-- explicit `expires_at` so a crashed holder's lease can be taken over
-- without ever needing a graceful release that will never come.
CREATE TABLE IF NOT EXISTS scheduler_lease (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    holder_id TEXT,
    epoch INTEGER NOT NULL DEFAULT 0,
    acquired_at TEXT,
    renewed_at TEXT,
    expires_at TEXT
);

-- Milestone 7: one row per task actually claimed by some Scheduler
-- epoch. `task_id` is the primary key — exactly the same "second
-- attempt gets IntegrityError" pattern as `worktrees` (Milestone 6.1)
-- — so two claims for the same task_id can never both succeed,
-- regardless of which epoch or process is attempting it.
CREATE TABLE IF NOT EXISTS scheduler_claims (
    task_id TEXT PRIMARY KEY,
    epoch INTEGER NOT NULL,
    holder_id TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduler_claims_status ON scheduler_claims(status);
CREATE INDEX IF NOT EXISTS idx_scheduler_claims_epoch ON scheduler_claims(epoch);

-- Milestone 7 (State-aware Stall Detector, Component B): pure
-- bookkeeping, never consulted by ApprovalGate/GovernedPipeline and
-- never itself mutating a task or worktree row — see
-- `core.stall_detector`'s module docstring.
CREATE TABLE IF NOT EXISTS scheduler_alerts (
    alert_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    detected_at TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acknowledged_at TEXT,
    acknowledged_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scheduler_alerts_task_id ON scheduler_alerts(task_id);
CREATE INDEX IF NOT EXISTS idx_scheduler_alerts_acknowledged ON scheduler_alerts(acknowledged);
CREATE INDEX IF NOT EXISTS idx_scheduler_alerts_kind ON scheduler_alerts(kind);
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
        # Audit Finding C-9: task descriptions, Agent stdout/stderr,
        # and execution evidence all live in this one file -- owner-
        # only, regardless of umask, before any connection is opened.
        os.chmod(self.db_path.parent, stat.S_IRWXU)
        # check_same_thread=False (Milestone 6.2): the integration lock
        # must be provably atomic under real concurrent callers, not just
        # sequential calls, so this connection needs to be usable from
        # more than one thread. The actual serialization guarantee is
        # `_integration_lock_mutex` below, not this flag by itself — see
        # its own comment for why.
        self._conn = sqlite3.connect(
            str(self.db_path), isolation_level=None, check_same_thread=False
        )
        if self.db_path.exists():
            os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)
        # `check_same_thread=False` only lifts Python's own thread-affinity
        # check — it does not make concurrent `execute()` calls from two
        # threads on one connection safe by itself (discovered by a real,
        # repeatable flaky failure in a genuine multi-thread test of the
        # integration lock below, not assumed from documentation). This
        # lock is the actual serialization guarantee for the one method
        # pair that is deliberately exercised under real concurrency:
        # `acquire_integration_lock`/`release_integration_lock`.
        self._integration_lock_mutex = threading.Lock()
        # Milestone 7: the same empirically-required Python-level lock
        # as `_integration_lock_mutex` above, for the same reason —
        # `check_same_thread=False` does not by itself make concurrent
        # `execute()` calls from multiple worker threads (the
        # Scheduler's own concurrency model) safe to interleave on one
        # `sqlite3.Connection`. Separate from the integration-lock
        # mutex because it guards a different resource (the scheduler
        # lease and its claims, never `integration_lock`).
        self._scheduler_mutex = threading.Lock()
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
            self._conn.execute(
                "INSERT OR IGNORE INTO scheduler_lease (id, holder_id, epoch, acquired_at, "
                "renewed_at, expires_at) VALUES (1, NULL, 0, NULL, NULL, NULL)"
            )
            # Audit Finding C-8: a database from before this table
            # existed has no row yet -- every such database has the
            # exact version-1 shape (nothing has ever changed it), so
            # backfilling version 1 here is correct for both a brand
            # new database and an upgraded-in-place older one. Only a
            # *mismatched* existing row (a real future migration) is
            # ever refused.
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_meta (id, version) VALUES (1, ?)",
                (CURRENT_SCHEMA_VERSION,),
            )
            row = self._conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
            if row["version"] != CURRENT_SCHEMA_VERSION:
                raise SchemaVersionError(row["version"], CURRENT_SCHEMA_VERSION)

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

    def list_latest_integration_proposals(self) -> list[IntegrationProposal]:
        """One row per `task_id` — its most recent proposal only, never
        a superseded one. Used by the Approval CLI (Milestone 6
        remediation, Item 2) to enumerate what a Founder could review;
        never used to infer which ones still need a decision — that is
        `approval_decisions`' job, checked separately."""
        cursor = self._conn.execute(
            """
            SELECT p.* FROM integration_proposals p
            WHERE p.proposed_at = (
                SELECT MAX(proposed_at) FROM integration_proposals
                WHERE task_id = p.task_id
            )
            ORDER BY p.proposed_at
            """
        )
        return [_row_to_integration_proposal(row) for row in cursor.fetchall()]

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

    # -- scheduler lease (Milestone 7, Phase 2) --------------------------------

    def acquire_scheduler_lease(
        self, holder_id: str, now: datetime, lease_seconds: float
    ) -> int | None:
        """One atomic UPDATE: succeeds only if the lease is free
        (`holder_id IS NULL`) or the previous holder's lease has
        expired (`expires_at < now`) — `epoch = epoch + 1` happens
        inside that same statement, so "decide this acquisition is
        allowed" and "advance the fencing token" can never be
        separated by another writer. Returns the new epoch on
        success, `None` if a live holder already owns the lease."""
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._scheduler_mutex:
            with self._conn:
                cursor = self._conn.execute(
                    """
                    UPDATE scheduler_lease
                    SET holder_id = ?, epoch = epoch + 1, acquired_at = ?,
                        renewed_at = ?, expires_at = ?
                    WHERE id = 1 AND (holder_id IS NULL OR expires_at < ?)
                    """,
                    (holder_id, now_iso, now_iso, expires_iso, now_iso),
                )
                if cursor.rowcount == 0:
                    return None
                row = self._conn.execute(
                    "SELECT epoch FROM scheduler_lease WHERE id = 1"
                ).fetchone()
            return row["epoch"]

    def renew_scheduler_lease(
        self, holder_id: str, epoch: int, now: datetime, lease_seconds: float
    ) -> bool:
        """Heartbeat: extends `expires_at` without touching `epoch` —
        never an acquisition. Fenced to the exact (`holder_id`,
        `epoch`) the caller believes it still holds; returns `False`
        (never raises) the moment that stops being true, so a renewal
        attempt is itself how a holder discovers it has been
        superseded."""
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._scheduler_mutex:
            with self._conn:
                cursor = self._conn.execute(
                    """
                    UPDATE scheduler_lease
                    SET renewed_at = ?, expires_at = ?
                    WHERE id = 1 AND holder_id = ? AND epoch = ?
                    """,
                    (now_iso, expires_iso, holder_id, epoch),
                )
            return cursor.rowcount == 1

    def release_scheduler_lease(self, holder_id: str, epoch: int) -> bool:
        """Only the exact current (`holder_id`, `epoch`) may release —
        never a bare release that could free a lease a later holder
        now legitimately owns. `epoch` itself is left untouched (never
        reset to 0): the next `acquire` still increments from its real
        last value, so monotonicity holds across a clean release too."""
        with self._scheduler_mutex:
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE scheduler_lease SET holder_id = NULL "
                    "WHERE id = 1 AND holder_id = ? AND epoch = ?",
                    (holder_id, epoch),
                )
            return cursor.rowcount == 1

    def current_scheduler_lease(self) -> SchedulerLease:
        with self._scheduler_mutex:
            row = self._conn.execute(
                "SELECT * FROM scheduler_lease WHERE id = 1"
            ).fetchone()
        return _row_to_scheduler_lease(row)

    # -- scheduler claims (Milestone 7, Phase 2) -------------------------------

    def claim_task(self, task_id: str, holder_id: str, epoch: int, now: datetime) -> bool:
        """Atomic INSERT...SELECT: the row is only ever inserted if
        (`holder_id`, `epoch`) still matches the *current* lease at the
        exact moment of the statement — one fenced check-and-write, not
        a separate read followed by a trusting write. Returns `False`
        (the caller must stop, never retry blindly) if the lease has
        already moved on. Raises `errors.DuplicateError` if `task_id`
        is already claimed by anyone, at any epoch — the actual
        enforcement of "prevent duplicate claims," at the persistence
        layer, not just in application logic."""
        now_iso = now.isoformat()
        try:
            with self._scheduler_mutex:
                with self._conn:
                    cursor = self._conn.execute(
                        """
                        INSERT INTO scheduler_claims
                            (task_id, epoch, holder_id, status, claimed_at, updated_at)
                        SELECT ?, epoch, holder_id, 'claimed', ?, ?
                        FROM scheduler_lease
                        WHERE id = 1 AND epoch = ? AND holder_id = ?
                        """,
                        (task_id, now_iso, now_iso, epoch, holder_id),
                    )
            return cursor.rowcount == 1
        except sqlite3.IntegrityError as exc:
            raise DuplicateError("SchedulerClaim", task_id) from exc

    def complete_claim(self, task_id: str, holder_id: str, epoch: int, now: datetime) -> bool:
        """The genuinely atomic fenced write Requirement 2 asks for: the
        claim's own stored `epoch` (fixed at claim time) is compared,
        inside this one UPDATE statement, against whatever epoch is
        *currently* authoritative — never a separately-read value
        trusted after the fact. If a newer lease has since become
        authoritative, this matches zero rows and returns `False`; the
        stale holder can never again mutate this claim no matter how
        many times it retries."""
        now_iso = now.isoformat()
        with self._scheduler_mutex:
            with self._conn:
                cursor = self._conn.execute(
                    """
                    UPDATE scheduler_claims
                    SET status = 'completed', updated_at = ?
                    WHERE task_id = ? AND holder_id = ? AND status = 'claimed'
                      AND epoch = (SELECT epoch FROM scheduler_lease WHERE id = 1)
                    """,
                    (now_iso, task_id, holder_id),
                )
            return cursor.rowcount == 1

    def quarantine_claim(
        self, task_id: str, holder_id: str, epoch: int, now: datetime
    ) -> bool:
        """Recovery/reconciliation action: the *current* authoritative
        holder (checked the same way as `complete_claim`, but against
        the caller's own now-current identity, not the claim's stale
        one) marks a still-CLAIMED row left behind by an earlier epoch
        as QUARANTINED. Never silently discarded, never silently
        reused — see `WorktreeManager`/`TaskManager` callers, which
        must independently decide what to do with the task/Worktree
        itself; this call only ever updates bookkeeping."""
        now_iso = now.isoformat()
        with self._scheduler_mutex:
            with self._conn:
                cursor = self._conn.execute(
                    """
                    UPDATE scheduler_claims
                    SET status = 'quarantined', updated_at = ?
                    WHERE task_id = ? AND status = 'claimed'
                      AND EXISTS (
                          SELECT 1 FROM scheduler_lease
                          WHERE id = 1 AND holder_id = ? AND epoch = ?
                      )
                    """,
                    (now_iso, task_id, holder_id, epoch),
                )
            return cursor.rowcount == 1

    def get_claim(self, task_id: str) -> TaskClaim | None:
        with self._scheduler_mutex:
            row = self._conn.execute(
                "SELECT * FROM scheduler_claims WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _row_to_task_claim(row) if row is not None else None

    def list_claims(self, *, status: ClaimStatus | None = None) -> list[TaskClaim]:
        query = "SELECT * FROM scheduler_claims"
        params: list[str] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)
        query += " ORDER BY claimed_at"
        with self._scheduler_mutex:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_task_claim(row) for row in rows]

    # -- scheduler alerts (Milestone 7, Component B) ---------------------------

    def save_scheduler_alert(self, alert: SchedulerAlert) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO scheduler_alerts (
                    alert_id, task_id, kind, severity, detail, detected_at,
                    acknowledged, acknowledged_at, acknowledged_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.alert_id,
                    alert.task_id,
                    alert.kind.value,
                    alert.severity.value,
                    alert.detail,
                    alert.detected_at.isoformat(),
                    int(alert.acknowledged),
                    alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
                    alert.acknowledged_reason,
                ),
            )

    def get_open_alert(self, task_id: str, kind: AlertKind) -> SchedulerAlert | None:
        """Most recent unacknowledged alert of this exact kind for this
        task, if any — used by the detector to decide whether a new
        scan should raise a fresh alert or leave the existing open one
        alone, so a persisting condition never spams duplicate rows."""
        cursor = self._conn.execute(
            "SELECT * FROM scheduler_alerts WHERE task_id = ? AND kind = ? "
            "AND acknowledged = 0 ORDER BY detected_at DESC LIMIT 1",
            (task_id, kind.value),
        )
        row = cursor.fetchone()
        return _row_to_scheduler_alert(row) if row is not None else None

    def get_alert(self, alert_id: str) -> SchedulerAlert | None:
        cursor = self._conn.execute(
            "SELECT * FROM scheduler_alerts WHERE alert_id = ?", (alert_id,)
        )
        row = cursor.fetchone()
        return _row_to_scheduler_alert(row) if row is not None else None

    def list_alerts(
        self, *, acknowledged: bool | None = None, task_id: str | None = None
    ) -> list[SchedulerAlert]:
        query = "SELECT * FROM scheduler_alerts"
        clauses = []
        params: list[object] = []
        if acknowledged is not None:
            clauses.append("acknowledged = ?")
            params.append(int(acknowledged))
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY detected_at"
        cursor = self._conn.execute(query, params)
        return [_row_to_scheduler_alert(row) for row in cursor.fetchall()]

    def acknowledge_alert(self, alert_id: str, now: datetime, reason: str = "") -> bool:
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE scheduler_alerts
                SET acknowledged = 1, acknowledged_at = ?, acknowledged_reason = ?
                WHERE alert_id = ? AND acknowledged = 0
                """,
                (now.isoformat(), reason, alert_id),
            )
        return cursor.rowcount == 1


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


def _row_to_scheduler_lease(row: sqlite3.Row) -> SchedulerLease:
    return SchedulerLease(
        holder_id=row["holder_id"],
        epoch=row["epoch"],
        acquired_at=datetime.fromisoformat(row["acquired_at"]) if row["acquired_at"] else None,
        renewed_at=datetime.fromisoformat(row["renewed_at"]) if row["renewed_at"] else None,
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
    )


def _row_to_task_claim(row: sqlite3.Row) -> TaskClaim:
    return TaskClaim(
        task_id=row["task_id"],
        epoch=row["epoch"],
        holder_id=row["holder_id"],
        status=ClaimStatus(row["status"]),
        claimed_at=datetime.fromisoformat(row["claimed_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_scheduler_alert(row: sqlite3.Row) -> SchedulerAlert:
    return SchedulerAlert(
        alert_id=row["alert_id"],
        task_id=row["task_id"],
        kind=AlertKind(row["kind"]),
        severity=AlertSeverity(row["severity"]),
        detail=row["detail"],
        detected_at=datetime.fromisoformat(row["detected_at"]),
        acknowledged=bool(row["acknowledged"]),
        acknowledged_at=datetime.fromisoformat(row["acknowledged_at"])
        if row["acknowledged_at"]
        else None,
        acknowledged_reason=row["acknowledged_reason"],
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
