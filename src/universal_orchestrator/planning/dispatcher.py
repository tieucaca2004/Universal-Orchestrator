"""Dispatcher interface — superseded, never implemented.

Routes a planned `Task` to a verified `ExecutionAdapter`. This
docstring originally said concrete dispatch logic would land in
Milestone 2 "once the orchestration engine exists to drive it" — the
orchestration engine (`core.engine.OrchestrationEngine`) now exists
and explicitly documents that it drives dispatch itself, with "no
Dispatcher" by design. Real dispatch/governance logic lives in
`core.engine.OrchestrationEngine.execute()` and
`core.governed_pipeline.GovernedPipeline` instead.

Audit Finding C-11/16-B (2026-11-14): confirmed, across all of M1-M7,
that no concrete subclass of `Dispatcher` exists anywhere in this
codebase or its tests — the only reference anywhere is
`tests/test_interfaces.py`'s check that this ABC cannot be
instantiated directly. Kept rather than deleted so this superseded
interface stays visible rather than silently disappearing; not on
any real execution path.
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
        `verify()` has not returned `VerificationStatus.AVAILABLE`.
        """
        raise NotImplementedError
