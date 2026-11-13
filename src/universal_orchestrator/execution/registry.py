"""Adapter Registry — the multi-engine "Engine Registry" / "Engine
Selection interface" the Milestone 4 addendum asks for, at the smallest
footprint that doesn't duplicate an existing abstraction.

`registry.providers.ProviderRegistry` already records *which* engines
exist, their kind, capabilities, verification and availability status —
that already is the engine inventory. What was missing is purely
mechanical: given a `provider_id` the Agent Router selected, which
`ExecutionAdapter` instance actually knows how to talk to it. This
module is exactly that lookup, nothing more — it holds no policy, no
selection logic, and no knowledge of Claude or DeepSeek specifically.

A future DeepSeek Harness CLI adapter (not implemented in this
milestone — see docs/EXECUTION_CLAUDE_CODE.md "DeepSeek compatibility
review") registers here the same way `ClaudeCodeAdapter` does: one
`register(provider_id, adapter)` call, keyed by the `ProviderDescriptor`
it corresponds to in `ProviderRegistry`. Nothing in `core/engine.py` or
`routing/router.py` needs to change to add it.
"""

from __future__ import annotations

from universal_orchestrator.execution.adapter import ExecutionAdapter
from universal_orchestrator.errors import DuplicateError, NotFoundError


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ExecutionAdapter] = {}

    def register(self, provider_id: str, adapter: ExecutionAdapter) -> None:
        if provider_id in self._adapters:
            raise DuplicateError("Adapter", provider_id)
        self._adapters[provider_id] = adapter

    def get(self, provider_id: str) -> ExecutionAdapter:
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            raise NotFoundError("Adapter", provider_id)
        return adapter

    def list_provider_ids(self) -> list[str]:
        return list(self._adapters.keys())
