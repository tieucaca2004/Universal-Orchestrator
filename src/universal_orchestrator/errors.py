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


class InvalidStateTransitionError(OrchestratorError):
    def __init__(self, entity: str, current: str, target: str) -> None:
        self.entity = entity
        self.current = current
        self.target = target
        super().__init__(
            f"{entity}: cannot transition from '{current}' to '{target}'"
        )
