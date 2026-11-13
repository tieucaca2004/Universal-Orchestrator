"""Execution Adapter interface.

Every concrete integration (Claude Code CLI, Hermes, a future DeepSeek
Harness CLI, ...) implements this contract. `ClaudeCodeAdapter`
(`execution/claude_code.py`, Milestone 4) and `HermesAdapter`
(`execution/hermes.py`, Milestone 5) are the two concrete
implementations so far — neither this file nor `core/engine.py`,
`routing/router.py`, or `registry/providers.py` contains any
runtime-specific code; both adapters are selected purely by
`provider_id` through `execution/registry.py`'s `AdapterRegistry`.

Interface change from Milestone 1, justified (grep confirmed zero
callers of either symbol anywhere in this repository before this
change — this is the same kind of safe, necessary extension as
Milestone 2's `TaskStatus` rename):

- `VerificationStatus` had `UNVERIFIED/VERIFIED/FAILED` — three values
  that cannot distinguish "CLI not installed" from "installed but not
  authenticated" from "installed, authenticated, but this version is
  missing a flag we need." Milestone 4's real CLI inspection needs all
  three told apart, so the enum is now `AVAILABLE/UNAVAILABLE/
  UNAUTHENTICATED/UNSUPPORTED/ERROR` — exactly the five values Milestone
  4's brief specifies, no more.
- `execute()` took a bare `Task`, with nowhere to put an execution id,
  a working directory, a timeout, or an execution policy — all of which
  Milestone 4 needs and none of which `Task` carries. It now takes an
  `ExecutionRequest` built from those fields explicitly.
"""

from __future__ import annotations

import enum
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from universal_orchestrator.core.models import TaskSpecification


def _new_execution_id() -> str:
    return f"exec_{uuid.uuid4().hex[:12]}"


class VerificationStatus(enum.Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNAUTHENTICATED = "unauthenticated"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class ExecutionOutcome(enum.Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    ERROR = "error"


class PermissionMode(enum.Enum):
    """`READ_ONLY` was the only mode Milestone 4 implemented — see
    execution/claude_code.py and docs/EXECUTION_CLAUDE_CODE.md for what
    "read-only" is verified to actually mean for this adapter.

    `WORKTREE_WRITE` (M6 completion) enables the Agent's own file
    tools (Read/Write/Edit/Bash) *and* wraps the real subprocess in a
    real OS-level sandbox (`bwrap`/user namespaces) that makes every
    path except the task's own Worktree read-only to the Agent —
    enforced by the kernel, not by `working_directory`/`cwd` alone.
    An adapter that cannot provide real containment for this mode must
    fail closed rather than silently running with write tools enabled
    and no sandbox — see `execution.claude_code`'s own check."""

    READ_ONLY = "read_only"
    WORKTREE_WRITE = "worktree_write"


@dataclass(slots=True)
class ExecutionPolicy:
    permission_mode: PermissionMode = PermissionMode.READ_ONLY
    max_output_bytes: int = 65536
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionRequest:
    """Everything an adapter needs to run one task. Every field here is
    either supplied explicitly by the caller or defaulted — nothing is
    inferred from task text (see analysis/analyzer.py's precedent)."""

    task_id: str
    project_id: str
    agent_id: str
    provider_id: str
    agent_role: str
    working_directory: str
    execution_id: str = field(default_factory=_new_execution_id)
    specification: TaskSpecification | None = None
    input_context: str = ""
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    timeout_seconds: float = 60.0
    correlation_id: str | None = None


@dataclass(slots=True)
class ExecutionResult:
    execution_id: str
    task_id: str
    outcome: ExecutionOutcome
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    cli_version: str | None = None
    output_format: str | None = None
    structured_output: dict[str, Any] | None = None
    structured_output_valid: bool | None = None
    verification_status: VerificationStatus | None = None
    error_classification: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """`COMPLETED` alone — never exit code alone. A completed
        process can still have produced the wrong answer; this property
        says nothing about correctness, only that the process itself
        ran to a normal finish with no error signal."""
        return self.outcome is ExecutionOutcome.COMPLETED


class ExecutionAdapter(ABC):
    """Contract every concrete execution-environment adapter must satisfy.

    `verify()` must perform a real check against the live provider and
    return the actual outcome — never an assumed or hard-coded result.
    """

    @abstractmethod
    def verify(self) -> VerificationStatus:
        raise NotImplementedError

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        raise NotImplementedError
