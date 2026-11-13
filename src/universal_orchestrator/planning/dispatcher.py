"""Dispatcher interface.

Routes a planned `Task` to a verified `ExecutionAdapter`. Concrete
dispatch logic (including the approval-gate check) lands in Milestone 2,
once the orchestration engine exists to drive it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from universal_orchestrator.core.models import Task
from universal_orchestrator.execution.adapter import ExecutionAdapter, ExecutionResult


class Dispatcher(ABC):
    """Contract every concrete dispatcher implementation must satisfy."""

    @abstractmethod
    def dispatch(self, task: Task, adapter: ExecutionAdapter) -> ExecutionResult:
        """Hand `task` to `adapter` and return its result.

        Implementations must refuse to dispatch to an adapter whose
        `verify()` has not returned `VerificationStatus.VERIFIED`.
        """
        raise NotImplementedError
