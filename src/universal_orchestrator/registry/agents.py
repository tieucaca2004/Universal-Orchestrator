"""Agent Registry.

Registers `AgentDescriptor` entries and tracks their operational status.
Registering an agent never makes it eligible for routing by itself —
only an explicit `set_status(..., AgentStatus.AVAILABLE)` call does, and
nothing in this module performs that check on an agent's behalf. This
registry does not contact any machine, CLI or API; "available" here
means "a later milestone's verification step said so", nothing more.
"""

from __future__ import annotations

from universal_orchestrator.core.models import AgentDescriptor, AgentStatus
from universal_orchestrator.errors import DuplicateError, NotFoundError


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentDescriptor] = {}

    def register(self, agent: AgentDescriptor) -> AgentDescriptor:
        if agent.agent_id in self._agents:
            raise DuplicateError("Agent", agent.agent_id)
        if not agent.role.strip():
            raise ValueError("AgentDescriptor.role must not be empty")
        # Existing in configuration is never evidence of being
        # operational — force REGISTERED regardless of what the caller
        # passed in.
        agent.status = AgentStatus.REGISTERED
        self._agents[agent.agent_id] = agent
        return agent

    def get(self, agent_id: str) -> AgentDescriptor:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise NotFoundError("Agent", agent_id)
        return agent

    def list(
        self, *, role: str | None = None, status: AgentStatus | None = None
    ) -> list[AgentDescriptor]:
        agents = self._agents.values()
        if role is not None:
            agents = (a for a in agents if a.role == role)
        if status is not None:
            agents = (a for a in agents if a.status == status)
        return list(agents)

    def set_status(self, agent_id: str, status: AgentStatus) -> AgentDescriptor:
        agent = self.get(agent_id)
        agent.status = status
        return agent

    def is_eligible(
        self,
        agent_id: str,
        *,
        required_capabilities: frozenset[str] = frozenset(),
        required_permissions: frozenset[str] = frozenset(),
        required_role: str | None = None,
    ) -> bool:
        """True only if the agent is AVAILABLE and satisfies every given
        requirement. Any other status — including values not in this
        enum — is never treated as available."""
        agent = self.get(agent_id)
        if agent.status is not AgentStatus.AVAILABLE:
            return False
        if required_role is not None and agent.role != required_role:
            return False
        if not required_capabilities.issubset(agent.capabilities):
            return False
        if not required_permissions.issubset(agent.permissions):
            return False
        return True
