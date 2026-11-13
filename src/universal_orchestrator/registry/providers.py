"""Provider Registry.

Registers `ProviderDescriptor` entries for Claude Code, Gemini CLI,
DeepSeek, Kimi, OpenClaw, Hermes and any future provider. Registration
never implies the provider is operational: `verified` starts `False` and
`status` starts `ProviderStatus.UNKNOWN` regardless of what the caller
passes, exactly like `AgentRegistry.register` — existing in this
registry is not evidence of anything working.

`ProviderDescriptor` has no field for API keys, tokens or any other
credential, by design: there is nothing here for a log line or a
registry dump to leak.
"""

from __future__ import annotations

from universal_orchestrator.core.models import ProviderDescriptor, ProviderStatus
from universal_orchestrator.errors import DuplicateError, NotFoundError


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderDescriptor] = {}

    def register(self, provider: ProviderDescriptor) -> ProviderDescriptor:
        if provider.provider_id in self._providers:
            raise DuplicateError("Provider", provider.provider_id)
        if not provider.name.strip() or not provider.kind.strip():
            raise ValueError("ProviderDescriptor.name and .kind must not be empty")
        provider.verified = False
        provider.status = ProviderStatus.UNKNOWN
        self._providers[provider.provider_id] = provider
        return provider

    def get(self, provider_id: str) -> ProviderDescriptor:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise NotFoundError("Provider", provider_id)
        return provider

    def list(self, *, kind: str | None = None) -> list[ProviderDescriptor]:
        providers = self._providers.values()
        if kind is not None:
            providers = (p for p in providers if p.kind == kind)
        return list(providers)

    def set_verified(self, provider_id: str, verified: bool) -> ProviderDescriptor:
        """Flip verification. The caller — a future adapter's
        `verify()`, never this registry — is responsible for having
        actually checked the live provider first."""
        provider = self.get(provider_id)
        provider.verified = verified
        return provider

    def set_status(self, provider_id: str, status: ProviderStatus) -> ProviderDescriptor:
        provider = self.get(provider_id)
        provider.status = status
        return provider

    def is_routable(self, provider_id: str) -> bool:
        provider = self.get(provider_id)
        return provider.verified and provider.status is ProviderStatus.AVAILABLE
