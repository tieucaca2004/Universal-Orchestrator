# Universal-Orchestrator

Independent central orchestration system that coordinates multiple
independent projects, AI agents, providers and execution environments.

It is **not** PSH, **not** OpenClaw, and **not** Hermes. Those are existing
runtimes with their own repositories, configuration, secrets and Git
governance; this Core integrates them only through execution adapters, never
by absorbing or copying them.

## Status

Milestone 6 (Git Worktree Manager, Conflict Manager, Approval Gate,
Quality Gate) is **IMPLEMENTED, TESTED, VERIFIED, COMPLETE / ACCEPTED
/ CLOSED**. Milestone 7 (Cycle & Deadlock Detector, scheduler lease
fencing, State-aware Stall Detector, Task Scheduler, Proposal Recovery
CLI) **exists in code but has no recorded Founder approval** — see
ROADMAP.md §11. A full M1–M7 audit and remediation pass (2026-11-14)
fixed several confirmed defects found in M7 and earlier milestones;
see the repository's commit history for the itemized findings and
fixes. This note corrects a previously stale claim here that the
project was still at Milestone 5 (audit Finding C-11). See
[ROADMAP.md](ROADMAP.md) for the authoritative milestone status,
M8 plans, and the Orchestrator A / Orchestrator B distinction;
[ARCHITECTURE.md](ARCHITECTURE.md) for the component map and the
Appendix noting items still awaiting a Founder decision;
[docs/TASK_LIFECYCLE.md](docs/TASK_LIFECYCLE.md)
for the task state machine; [docs/REGISTRIES.md](docs/REGISTRIES.md) for
the registry schemas; [docs/ROUTING.md](docs/ROUTING.md) for the routing
algorithm; [docs/EXECUTION_CLAUDE_CODE.md](docs/EXECUTION_CLAUDE_CODE.md)
for the real, verified Claude CLI capabilities, security posture, and
smoke-test evidence; and [docs/EXECUTION_HERMES.md](docs/EXECUTION_HERMES.md)
for the same for Hermes, including the real smoke test and the
Docker-specific process-termination design it required.

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
  execution/   execution adapter interface + ClaudeCodeAdapter +
               HermesAdapter (two real ones) + AdapterRegistry for
               future engines (DeepSeek Harness CLI — not installed,
               not implemented; RTX 3070 is not a Worker Runtime and is
               not in scope — see ROADMAP.md)
  approval/    approval gate for sensitive operations (a later milestone)
  audit/       structured logging (full audit trail in a later milestone)
  config/      configuration management
  errors.py    shared exception hierarchy
```

Note: the Milestone 3 brief specified `src/orchestrator/...`; this
repository has used `src/universal_orchestrator/...` since Milestone 1
(`pyproject.toml`'s `name = "universal-orchestrator"`; 32 files under
`src/`/`tests/` import it; no `src/orchestrator/` directory exists).
See [ARCHITECTURE.md](ARCHITECTURE.md)'s Appendix for why that naming
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
environment. `execution.claude_code.ClaudeCodeAdapter.verify()` is the
first real implementation of this policy — it runs `claude --version`,
`claude --help`, and `claude auth status` against the actual installed
CLI and returns `AVAILABLE` only if all three succeed. See
`core.models.ProviderDescriptor.verified`,
`execution.adapter.ExecutionAdapter.verify`,
`errors.ProviderNotVerifiedError`, and
[docs/EXECUTION_CLAUDE_CODE.md](docs/EXECUTION_CLAUDE_CODE.md).
