"""Shared exception hierarchy for Universal-Orchestrator."""

from __future__ import annotations


class OrchestratorError(Exception):
    """Base class for all orchestrator-raised errors."""


class ApprovalRequiredError(OrchestratorError):
    def __init__(self, action: str, reason: str = "") -> None:
        self.action = action
        self.reason = reason
        super().__init__(
            f"Approval required for '{action}': {reason or 'no reason given'}"
        )


class ProviderNotVerifiedError(OrchestratorError):
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        super().__init__(
            f"Provider '{provider_id}' has not been verified as operational; "
            "refusing to dispatch."
        )


class NotFoundError(OrchestratorError):
    def __init__(self, kind: str, identifier: str) -> None:
        self.kind = kind
        self.identifier = identifier
        super().__init__(f"{kind} '{identifier}' not found")


class DuplicateError(OrchestratorError):
    def __init__(self, kind: str, identifier: str) -> None:
        self.kind = kind
        self.identifier = identifier
        super().__init__(f"{kind} '{identifier}' already registered")


class AgentNotEligibleError(OrchestratorError):
    """Raised when the agent a RoutingDecision selected is no longer
    AVAILABLE by the time execution is attempted — the decision has
    gone stale since routing (M4 audit remediation, Finding F-1)."""

    def __init__(self, agent_id: str, status: str) -> None:
        self.agent_id = agent_id
        self.status = status
        super().__init__(
            f"Agent '{agent_id}' is '{status}', not AVAILABLE; refusing to dispatch "
            "a stale routing decision."
        )


class AdapterUnavailableError(OrchestratorError):
    """Raised when `ExecutionAdapter.verify()` does not return AVAILABLE
    immediately before dispatch — never assumed, always checked fresh
    (M4 audit remediation, Finding B-1)."""

    def __init__(self, provider_id: str, status: str) -> None:
        self.provider_id = provider_id
        self.status = status
        super().__init__(
            f"Adapter for provider '{provider_id}' reported '{status}', not "
            "AVAILABLE; refusing to dispatch."
        )


class QualityGateRequiredError(OrchestratorError):
    """Raised by `TaskManager.succeed()` (Milestone 6.4) when no
    recorded task-level Quality Gate verdict of PASS exists for this
    task — a completed process is never, by itself, evidence that the
    task's acceptance criteria were met."""

    def __init__(self, task_id: str, verdict: str) -> None:
        self.task_id = task_id
        self.verdict = verdict
        super().__init__(
            f"Task '{task_id}' cannot be marked SUCCEEDED without a recorded "
            f"Quality Gate PASS tied to its own acceptance criteria; latest "
            f"task-level verdict is '{verdict}'."
        )


class InvalidStateTransitionError(OrchestratorError):
    def __init__(self, entity: str, current: str, target: str) -> None:
        self.entity = entity
        self.current = current
        self.target = target
        super().__init__(
            f"{entity}: cannot transition from '{current}' to '{target}'"
        )
