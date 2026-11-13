"""Execution Adapter interface.

Every concrete integration (Claude Code, Gemini CLI, DeepSeek, Kimi,
OpenClaw, Hermes, a future RTX 3070 worker) implements this contract.
Concrete adapters land in Milestone 5; none exist yet, so nothing in this
codebase can claim a provider is operational.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from universal_orchestrator.core.models import Task


class VerificationStatus(enum.Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"


@dataclass(slots=True)
class ExecutionResult:
    success: bool
    output: str = ""
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ExecutionAdapter(ABC):
    """Contract every concrete execution-environment adapter must satisfy.

    `verify()` must perform a real execution + authentication check
    against the live provider and return the actual outcome — never an
    assumed or hard-coded result.
    """

    @abstractmethod
    def verify(self) -> VerificationStatus:
        raise NotImplementedError

    @abstractmethod
    def execute(self, task: Task) -> ExecutionResult:
        raise NotImplementedError
