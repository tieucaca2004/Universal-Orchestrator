"""Provider Registry — implemented in Milestone 3.

Will register `core.models.ProviderDescriptor` entries for Claude Code,
Gemini CLI, DeepSeek, Kimi, OpenClaw, Hermes and the future RTX 3070
worker. A provider stays `verified=False`, and therefore undispatchable
(see `errors.ProviderNotVerifiedError`), until an actual execution and
authentication check against the live provider has succeeded.
"""
