# Registries

Three independent in-memory registries (`registry/projects.py`,
`registry/agents.py`, `registry/providers.py`). None of them contact a
network, a file system beyond an explicit config load, or a machine —
registering something only ever records what the caller said, nothing
more.

## ProjectProfile schema (`core/models.py`)

| Field | Type | Meaning |
|---|---|---|
| `project_id` | `str` | Stable id, auto-generated unless supplied. |
| `name` | `str` | Required, non-empty. |
| `repository_path` | `str` | Required, non-empty. Never hardcoded in this codebase — see `registry.projects.load_from_config`. |
| `repository_url` | `str` | Optional. |
| `main_branch` | `str` | Defaults to `"main"`. |
| `technology_stack` | `list[str]` | Optional. |
| `instructions` | `list[str]` | Project-specific instructions. |
| `allowed_operations` | `list[str]` | If non-empty, an allow-list — anything not listed is denied. |
| `restricted_operations` | `list[str]` | Always denied, regardless of the allow-list. Cannot overlap `allowed_operations` (`ProjectRegistry.register` raises `ValueError` if it does). |
| `acceptance_criteria` | `list[str]` | Optional. |
| `required_testing_commands` | `list[str]` | Optional. |
| `security_constraints` | `list[str]` | Optional. |
| `metadata` | `dict` | Free-form; the only field `update_metadata()` touches. |

`registry.projects.load_from_config(path)` reads a Founder-supplied JSON
file shaped `{"projects": [{"name": ..., "repository_path": ..., ...}]}`
and returns `ProjectProfile` instances — the one place a project's real
path enters the system, and it is never written into this repository's
source.

## AgentDescriptor schema and status semantics

| Field | Type | Meaning |
|---|---|---|
| `agent_id` | `str` | Stable id, auto-generated unless supplied. |
| `name`, `role` | `str` | `role` required, non-empty (e.g. `"planner"`, `"coder"`). |
| `status` | `AgentStatus` | See below. |
| `capabilities` | `list[str]` | What the agent can do. |
| `supported_task_types` | `list[str]` | |
| `permissions` | `list[str]` | What the agent is allowed to do. |
| `machine` | `str \| None` | A **logical label** ("NUC", "RTX3070") — never a hostname, IP, or path. |
| `execution_environment` | `str \| None` | e.g. `"claude-cli"`, `"hermes-agent"`. |
| `provider_id` | `str \| None` | Links to a `ProviderDescriptor`. |

`AgentStatus`: `REGISTERED → AVAILABLE | BUSY | OFFLINE | UNVERIFIED |
DISABLED | ERROR`. `AgentRegistry.register()` **always** forces the new
agent to `REGISTERED`, overwriting whatever status the caller passed —
existing in the registry is not evidence of being operational. Only a
later, separate `set_status(agent_id, AgentStatus.AVAILABLE)` call (which
this milestone does not call automatically from anywhere) can promote an
agent, and `AgentRegistry.is_eligible()` / the Agent Router treat every
status other than `AVAILABLE` — including hypothetical unknown values —
as not available.

## ProviderDescriptor schema and verification semantics

| Field | Type | Meaning |
|---|---|---|
| `provider_id` | `str` | Stable id. |
| `name`, `kind` | `str` | Both required, non-empty (e.g. `kind="api"`/`"cli"`). |
| `verified` | `bool` | Whether an adapter's `verify()` has actually confirmed this provider works. Forced `False` on `register()`. |
| `status` | `ProviderStatus` | Reachability: `UNKNOWN \| AVAILABLE \| UNAVAILABLE \| ERROR`. Forced `UNKNOWN` on `register()`. |
| `model_id`, `execution_method` | `str \| None` | |
| `capabilities` | `list[str]` | |
| `cost` | `CostInfo` | See below. |

`verified` and `status` are deliberately separate: a provider can be
reachable (`status=AVAILABLE`) but never actually authenticated
(`verified=False`), or was verified once and is now unreachable.
`ProviderRegistry.is_routable()` requires **both**.

`ProviderDescriptor` has **no field for an API key, token, or any other
credential** — there is nothing for a log line or a registry dump to
leak, by construction, not by convention.

`CostInfo`: `confidence` is one of `VERIFIED | ESTIMATED | UNKNOWN`
(default `UNKNOWN`), plus `input_per_1k_tokens` /
`output_per_1k_tokens` (both `None` by default). The Agent Router only
ever enforces a cost constraint when `confidence is VERIFIED` — see
docs/ROUTING.md.

## Known limitations

- No registry is seeded with real NUC/RTX3070 agents or real provider
  credentials/pricing — both start empty.
- No persistence: registries are in-memory and rebuilt on process
  restart. Tasks and routing decisions persist (SQLite); registries do
  not, yet — not required by this milestone's Definition of Done, and
  deferred rather than built speculatively.
- `ProjectRegistry.load_from_config` only reads JSON (this host has no
  YAML library available without a `pip install` it cannot perform
  without a Founder-approved system change — see M1's "Running tests").
