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

- **ASUS Ubuntu Server** (hostname `tieu`; LAN address recorded in the
  Founder's private deployment notes, not in this repository) — hosts
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
| 1 | Orchestration Engine | `core/engine.py` | **Done** (Milestone 2 scope: Analyzer → Manager → State only — no Agent Router, Dispatcher, or adapter wiring) |
| 1a | Task Analyzer | `analysis/analyzer.py` | **Done** |
| 1b | Task Manager | `core/task_manager.py`, `core/lifecycle.py` | **Done** |
| 2 | Project Profile Registry | `registry/projects.py` | Milestone 3 |
| 3 | Agent Registry | `registry/agents.py` | Milestone 3 |
| 4 | Provider Registry | `registry/providers.py` | Milestone 3 |
| 5 | Task Lifecycle + persistent state | `core/models.py`, `core/lifecycle.py`, `core/state.py` (SQLite) | **Done** — see [docs/TASK_LIFECYCLE.md](docs/TASK_LIFECYCLE.md) |
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
it (2, 3, 4, and the Approval Gate). Nothing claims to work before it does.

The Task Manager explicitly does **not** decide which agent or provider
executes a task — that is the Agent Router's job (Milestone 3+). It only
asks "is this transition allowed" and "is this task's dependency graph
satisfied".

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

1. Architecture and repository foundation — **done**.
2. Task Analyzer, Task Manager, State Manager (SQLite) — **done**.
3. Project and agent registries.
4. Approval gate and audit trail.
5. Execution adapter interfaces → first concrete adapter(s).
6. Tests, documentation and integration verification.

## Open architectural questions for Founder review

Recorded rather than silently resolved — see the Milestone 2 repository
audit for how each was found.

1. **Machine identity is still unresolved.** Milestone 2's brief describes
   an approved architecture with the NUC Ubuntu Server as the control
   plane and the RTX 3070 Desktop as a Worker Runtime. Phase 0 discovery
   found: the machine actually running this repository (`tieu`) is an
   ASUS laptop, not the Intel NUC; the NUC (last seen at a now-obsolete
   IP) was unreachable when checked; and the RTX 3070 Desktop was
   confirmed powered off, with "no hard dependency on it" as an explicit
   Phase 1 approval boundary. Nothing in this milestone's code assumes a
   specific machine — the Task Analyzer, Task Manager and State Manager
   are plain Python + SQLite and run anywhere — but the two-machine
   Planner/Architect/Reviewer-on-NUC vs. Coder/Tester/Executor-on-RTX3070
   split is a real architectural commitment that hasn't been verified
   against the actual hardware yet. Needs a Founder decision before any
   milestone that implements Worker Protocol or cross-machine execution.
2. **None of the 11 named governance documents exist yet**
   (`FOUNDER_CONSTITUTION.md`, `MASTER_ARCHITECTURE.md`,
   `ORCHESTRATION_ENGINE.md`, `AGENT_WORKFLOW.md`, `PROJECT_PROFILES.md`,
   `SECURITY_AND_APPROVAL.md`, `GIT_GOVERNANCE.md`,
   `MEMORY_AND_PERSISTENCE.md`, `WORKER_PROTOCOL.md`, `ROADMAP.md`,
   `DEFINITION_OF_DONE.md`). This repository does not fabricate their
   contents. The Task Analyzer takes a `Constitution` as an explicit,
   caller-supplied object instead of reading `FOUNDER_CONSTITUTION.md`,
   and a `ProjectProfile` instead of reading `PROJECT_PROFILES.md` —
   if/when those files are written, a loader can be added without
   changing the Analyzer's interface.
