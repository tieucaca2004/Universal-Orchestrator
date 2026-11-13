# Universal-Orchestrator

Independent central orchestration system that coordinates multiple
independent projects, AI agents, providers and execution environments.

It is **not** PSH, **not** OpenClaw, and **not** Hermes. Those are existing
runtimes with their own repositories, configuration, secrets and Git
governance; this Core integrates them only through execution adapters, never
by absorbing or copying them.

## Status

Milestone 3 — Project Registry, Agent Registry, Provider Registry and
Agent Router. See [ARCHITECTURE.md](ARCHITECTURE.md) for the component
map, the Founder-approved 8-milestone roadmap, and open architectural
questions; [docs/TASK_LIFECYCLE.md](docs/TASK_LIFECYCLE.md) for the task
state machine; [docs/REGISTRIES.md](docs/REGISTRIES.md) for the registry
schemas; and [docs/ROUTING.md](docs/ROUTING.md) for the routing
algorithm.

## Stack

- Python 3.11+, standard library first.
- SQLite (stdlib `sqlite3`) for persistent task state — no external database.
- Standard library `unittest` for tests (see "Running tests" below).
- No web framework, no container runtime, and no hard dependency on any
  single AI provider. The current execution host is a resource-constrained
  machine (4 threads, ~7 GB RAM), so the Core is deliberately lightweight.

## Layout

```
src/universal_orchestrator/
  analysis/    Task Analyzer — TaskRequest -> TaskSpecification
  core/        data models, task lifecycle rules, Task Manager,
               State Manager (SQLite), orchestration engine
  registry/    project, agent and provider registries
  routing/     Agent Router — selects who should perform a task
  planning/    task planner and dispatcher interfaces
  execution/   execution adapter interface (Claude Code, Gemini CLI,
               DeepSeek, Kimi, OpenClaw, Hermes, future RTX 3070 worker)
  approval/    approval gate for sensitive operations (a later milestone)
  audit/       structured logging (full audit trail in a later milestone)
  config/      configuration management
  errors.py    shared exception hierarchy
```

Note: the Milestone 3 brief specified `src/orchestrator/...`; this
repository has used `src/universal_orchestrator/...` since Milestone 1.
See ARCHITECTURE.md's "Open architectural questions" for why that naming
was kept rather than mass-renamed.

## Running tests

This host's Python 3.14 install has no `ensurepip` and no system `pip`
(verified in Milestone 1 — see ARCHITECTURE.md), so there is no `venv`/pip
toolchain available without a system package install, which needs Founder
approval first. Tests therefore run on the standard library's `unittest`
runner, no installation required:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

If `python3-pip`/`python3-venv` are later installed system-wide (with
approval), pytest can be adopted as a drop-in upgrade — the test bodies
use plain `unittest.TestCase`s and don't depend on pytest-only features.

## Provider verification policy

No provider or agent is ever reported as operational until its actual
execution and authentication have been verified against the live
environment. See `core.models.ProviderDescriptor.verified`,
`execution.adapter.ExecutionAdapter.verify`, and
`errors.ProviderNotVerifiedError`.
