"""Agent Router.

Selects WHO should perform a `Task` — never executes it, never calls
Claude CLI or Hermes, never opens a Git worktree, never talks to a
machine over the network, and never marks anything operational on its
own authority. See docs/ROUTING.md for the full algorithm, every
rejection reason, and the (deliberately simple, explicitly defined)
scoring formula.

Everything the router evaluates beyond capabilities comes from explicit,
caller-supplied input — `Task.specification.required_capabilities` and a
handful of recognised `Task.payload` keys (`required_role`,
`required_permissions`, `required_machine`,
`max_cost_per_1k_tokens`) — never guessed from task text.
"""

from __future__ import annotations

from universal_orchestrator.core.models import (
    AgentDescriptor,
    AgentStatus,
    CostConfidence,
    ProjectProfile,
    ProviderStatus,
    RejectedCandidate,
    RoutingDecision,
    RoutingOutcome,
    Task,
)
from universal_orchestrator.errors import NotFoundError
from universal_orchestrator.registry.agents import AgentRegistry
from universal_orchestrator.registry.providers import ProviderRegistry


class AgentRouter:
    def __init__(self, agents: AgentRegistry, providers: ProviderRegistry) -> None:
        self._agents = agents
        self._providers = providers

    def route(self, task: Task, project_profile: ProjectProfile) -> RoutingDecision:
        required_capabilities = frozenset(
            task.specification.required_capabilities if task.specification else ()
        )
        required_role = task.payload.get("required_role")
        required_permissions = frozenset(task.payload.get("required_permissions", ()))
        required_machine = task.payload.get("required_machine")
        max_cost = task.payload.get("max_cost_per_1k_tokens")

        rejected: list[RejectedCandidate] = []
        best: AgentDescriptor | None = None
        best_score = -1.0

        for agent in sorted(self._agents.list(), key=lambda a: a.agent_id):
            reasons = self._reasons_to_reject(
                agent,
                project_profile=project_profile,
                required_capabilities=required_capabilities,
                required_role=required_role,
                required_permissions=required_permissions,
                required_machine=required_machine,
                max_cost=max_cost,
            )
            if reasons:
                rejected.append(RejectedCandidate(agent.agent_id, reasons))
                continue

            score = _score(agent, required_capabilities)
            if score > best_score:
                best, best_score = agent, score

        if best is None:
            return RoutingDecision(
                task_id=task.task_id,
                project_id=task.project_id,
                outcome=RoutingOutcome.NO_ELIGIBLE_AGENT,
                explanation="no registered agent satisfied every requirement",
                rejected=rejected,
            )

        matched = sorted(required_capabilities & set(best.capabilities))
        return RoutingDecision(
            task_id=task.task_id,
            project_id=task.project_id,
            outcome=RoutingOutcome.SELECTED,
            explanation=(
                f"selected '{best.agent_id}' (role '{best.role}'): "
                f"matched {len(matched)}/{len(required_capabilities)} required "
                "capabilities, AVAILABLE, all permission and provider checks passed"
            ),
            selected_agent_id=best.agent_id,
            selected_provider_id=best.provider_id,
            matched_capabilities=matched,
            rejected=rejected,
            score=best_score,
        )

    def _reasons_to_reject(
        self,
        agent: AgentDescriptor,
        *,
        project_profile: ProjectProfile,
        required_capabilities: frozenset[str],
        required_role: str | None,
        required_permissions: frozenset[str],
        required_machine: str | None,
        max_cost: float | None,
    ) -> list[str]:
        reasons: list[str] = []

        # Unknown/absent status is never treated as available.
        if agent.status is not AgentStatus.AVAILABLE:
            reasons.append(f"status is '{agent.status.value}', not AVAILABLE")

        if required_role is not None and agent.role != required_role:
            reasons.append(f"role '{agent.role}' does not match required role '{required_role}'")

        missing_capabilities = required_capabilities - set(agent.capabilities)
        if missing_capabilities:
            reasons.append(f"missing capabilities: {sorted(missing_capabilities)}")

        missing_permissions = required_permissions - set(agent.permissions)
        if missing_permissions:
            reasons.append(f"missing permissions: {sorted(missing_permissions)}")

        restricted_hit = required_permissions & set(project_profile.restricted_operations)
        if restricted_hit:
            reasons.append(
                f"operations restricted by project '{project_profile.project_id}': "
                f"{sorted(restricted_hit)}"
            )

        if required_machine is not None and agent.machine != required_machine:
            reasons.append(
                f"machine '{agent.machine}' does not match required machine "
                f"'{required_machine}'"
            )

        # Capabilities belong to the agent, not its provider — an agent
        # with no assigned provider is not disqualified on that basis
        # alone. Provider checks only apply when a provider_id is set.
        if agent.provider_id is not None:
            try:
                provider = self._providers.get(agent.provider_id)
            except NotFoundError:
                reasons.append(f"assigned provider '{agent.provider_id}' is not registered")
            else:
                if not provider.verified:
                    reasons.append(f"provider '{provider.provider_id}' is not verified")
                if provider.status is not ProviderStatus.AVAILABLE:
                    reasons.append(
                        f"provider '{provider.provider_id}' status is "
                        f"'{provider.status.value}', not AVAILABLE"
                    )
                # Cost is only ever a constraint when the data backing it
                # is VERIFIED — ESTIMATED or UNKNOWN cost data is never
                # reliable enough to reject a candidate on.
                if (
                    max_cost is not None
                    and provider.cost.confidence is CostConfidence.VERIFIED
                    and provider.cost.input_per_1k_tokens is not None
                    and provider.cost.input_per_1k_tokens > max_cost
                ):
                    reasons.append(
                        f"provider cost {provider.cost.input_per_1k_tokens}/1k tokens "
                        f"exceeds constraint {max_cost}/1k tokens"
                    )

        return reasons


def _score(agent: AgentDescriptor, required_capabilities: frozenset[str]) -> float:
    """len(matched) / len(required), or 1.0 when nothing was required.
    The only scoring rule this milestone defines — see docs/ROUTING.md."""
    if not required_capabilities:
        return 1.0
    matched = required_capabilities & set(agent.capabilities)
    return len(matched) / len(required_capabilities)
