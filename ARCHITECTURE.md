# Architecture

## Scope and non-goals

Universal-Orchestrator coordinates independent projects, agents, providers
and execution environments. It does not replace or absorb any of them:

- Not PSH (pshop-music)
- Not Việc Nha Trang
- Not FOOD / Agent-Research
- Not OpenClaw
- Not Hermes

Each keeps its own repository, configuration, memory, secrets and Git
governance. OpenClaw and Hermes are integrated only through execution
adapters — never by copying their configuration or secrets into this
repository.

## Hardware roles (verified in Phase 0 discovery)

- **ASUS Ubuntu Server** (`tieu`, `192.168.100.209`) — hosts
  Universal-Orchestrator and runs the orchestration engine. Resource
  constrained: 4 threads, ~7 GB RAM. The Core must not assume more than
  this is available.
- **RTX 3070 Desktop** — currently powered off. Candidate future Worker
  for Hermes / local inference. Nothing in this repository accesses,
  configures, or assumes the presence of this machine; it is an optional,
  pluggable execution environment, never a hard dependency.

## Core components

| # | Component | Module | Status |
|---|---|---|---|
| 1 | Orchestration Engine | `core/engine.py` | Milestone 2 |
| 2 | Project Profile Registry | `registry/projects.py` | Milestone 3 |
| 3 | Agent Registry | `registry/agents.py` | Milestone 3 |
| 4 | Provider Registry | `registry/providers.py` | Milestone 3 |
| 5 | Task Lifecycle + persistent state | `core/models.py` (data model, done), `core/state.py` (store, Milestone 2) |
| 6 | Task Planner interface | `planning/planner.py` | **Done** (interface) |
| 7 | Dispatcher interface | `planning/dispatcher.py` | **Done** (interface) |
| 8 | Execution Adapter interface | `execution/adapter.py` | **Done** (interface); concrete adapters in Milestone 5 |
| 9 | Structured logging / audit trail | `audit/logger.py` (logging, done), full audit trail Milestone 4 |
| 10 | Configuration management | `config/settings.py` | **Done** |
| 11 | Error handling & recovery | `errors.py` | **Done** |
| 12 | Unit / integration tests | `tests/` | Ongoing; full coverage at Milestone 6 |

Components marked "Done" are real, tested code. Everything else is either a
genuine interface (6, 7, 8 — explicitly scoped as interfaces) or an empty,
clearly-labelled placeholder module naming the milestone that implements
it. Nothing claims to work before it does.

## Provider verification policy

`ProviderDescriptor.verified` (in `core/models.py`) defaults to `False`.
`ExecutionAdapter.verify()` must perform a real execution + authentication
check against the live provider and return the actual outcome. No code
path in this repository may dispatch a task to, or report as operational,
a provider or agent while it is unverified — see
`errors.ProviderNotVerifiedError`.

## Approval boundaries

The engine must route any sensitive action through `approval/gate.py`
(Milestone 4) before it executes — in particular anything touching
existing Docker infrastructure, existing project repositories,
credentials/secrets, production systems, or the RTX 3070 Desktop. Until
Milestone 4 lands, no adapter may perform such actions at all.

## Roadmap

1. Architecture and repository foundation — **this milestone**.
2. Core orchestration engine + persistent task lifecycle (SQLite).
3. Project and agent registries.
4. Approval gate and audit trail.
5. Execution adapter interfaces → first concrete adapter(s).
6. Tests, documentation and integration verification.
