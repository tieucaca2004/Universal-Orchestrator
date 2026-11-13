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
| 1 | Orchestration Engine | `core/engine.py` | **Done** (Analyzer → Manager → Router → Execution Adapter; no Dispatcher) |
| 1a | Task Analyzer | `analysis/analyzer.py` | **Done** |
| 1b | Task Manager | `core/task_manager.py`, `core/lifecycle.py` | **Done** |
| 2 | Project Profile Registry | `registry/projects.py` | **Done** — see [docs/REGISTRIES.md](docs/REGISTRIES.md) |
| 3 | Agent Registry | `registry/agents.py` | **Done** — see [docs/REGISTRIES.md](docs/REGISTRIES.md) |
| 4 | Provider Registry | `registry/providers.py` | **Done** — see [docs/REGISTRIES.md](docs/REGISTRIES.md) |
| 4a | Agent Router | `routing/router.py` | **Done** — see [docs/ROUTING.md](docs/ROUTING.md) |
| 5 | Task Lifecycle + persistent state | `core/models.py`, `core/lifecycle.py`, `core/state.py` (SQLite) | **Done** — see [docs/TASK_LIFECYCLE.md](docs/TASK_LIFECYCLE.md) |
| 6 | Task Planner interface | `planning/planner.py` | **Done** (interface) |
| 7 | Dispatcher interface | `planning/dispatcher.py` | **Done** (interface) |
| 8 | Execution Adapter interface + Claude Code Adapter + Hermes Adapter | `execution/adapter.py`, `execution/claude_code.py`, `execution/hermes.py` | **Done** — see [docs/EXECUTION_CLAUDE_CODE.md](docs/EXECUTION_CLAUDE_CODE.md) and [docs/EXECUTION_HERMES.md](docs/EXECUTION_HERMES.md). Dispatcher itself remains unimplemented; a future DeepSeek adapter is not implemented (none installed to verify against — see docs/EXECUTION_CLAUDE_CODE.md "DeepSeek compatibility review"). |
| 8a | Adapter Registry (multi-engine selection) | `execution/registry.py` | **Done**, now proven with two real adapters — see [docs/EXECUTION_HERMES.md](docs/EXECUTION_HERMES.md) "Multi-runtime architecture" and `tests/test_multi_runtime.py` |
| 9 | Structured logging / audit trail | `audit/logger.py` (logging, done), full audit trail in a later milestone |
| 10 | Configuration management | `config/settings.py` | **Done** |
| 11 | Error handling & recovery | `errors.py` | **Done** |
| 12 | Unit / integration tests | `tests/` | Ongoing; 257/257 passing as of Milestone 5 |

Components marked "Done" are real, tested code. Everything else is either a
genuine interface (7 — Dispatcher, explicitly scoped as an interface) or an
empty, clearly-labelled placeholder module naming the milestone that
implements it (the Approval Gate). Nothing claims to work before it does.

The Task Manager explicitly does **not** decide which agent or provider
executes a task — that is the Agent Router's job. The Router only ever
*selects*: it never calls a provider, starts a Git worktree, or contacts
a machine. `OrchestrationEngine.execute()` is the one place a real
provider call happens, and only after a `RoutingOutcome.SELECTED`
decision exists — see [docs/ROUTING.md](docs/ROUTING.md) and
[docs/EXECUTION_CLAUDE_CODE.md](docs/EXECUTION_CLAUDE_CODE.md).

## Provider verification policy

`ProviderDescriptor.verified` (in `core/models.py`) defaults to `False`.
`ExecutionAdapter.verify()` must perform a real execution + authentication
check against the live provider and return the actual outcome. No code
path in this repository may dispatch a task to, or report as operational,
a provider or agent while it is unverified — see
`errors.ProviderNotVerifiedError`.

## Approval boundaries

The engine must route any sensitive action through `approval/gate.py`
(M6, per the roadmap below) before it executes — in particular anything
touching existing Docker infrastructure, existing project repositories,
credentials/secrets, production systems, or the RTX 3070 Desktop. Until
M6 lands, the only execution path that exists
(`OrchestrationEngine.execute()` → `ClaudeCodeAdapter`) enforces its own
safety boundary directly: zero-write, zero-tool execution only (see
[docs/EXECUTION_CLAUDE_CODE.md](docs/EXECUTION_CLAUDE_CODE.md)) — not a
substitute for the real Approval Gate, but a fail-closed default in its
absence.

## Roadmap

This is the Founder-approved milestone sequence, corrected here per the
Milestone 3 brief — earlier reports in this repository's history used a
different, shorter 6-item roadmap that this section now supersedes. This
sequence must not change without Founder approval.

1. **M1 — Repository Foundation.** **Done.**
2. **M2 — Task Analyzer, Task Manager, State Manager.** **Done.**
3. **M3 — Project Registry, Agent Registry, Provider Registry, Agent Router.** **Done.**
4. **M4 — Claude CLI Adapter and non-interactive execution verification.** **Done.**
5. **M5 — Hermes Adapter and execution verification.** **Done.**
6. **M6 — Git Worktree, Conflict Manager, Approval Gate and Quality Gate.**
7. **M7 — Worker Protocol and RTX 3070 integration.**
8. **M8 — End-to-end testing, backup, recovery and final acceptance.**

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
3. **Path naming in the Milestone 3 brief does not match this
   repository.** The brief specifies `src/orchestrator/registry/*.py`
   and `src/orchestrator/routing/router.py`; this repository's package
   has been `src/universal_orchestrator/` since Milestone 1
   (`pyproject.toml`'s `name = "universal-orchestrator"`, 20+ existing
   files importing it). Renaming the package to match would touch every
   file in the repository for no functional benefit and is exactly the
   kind of change Milestone 2's directive warned against ("do not
   redesign without demonstrated technical necessity"). This milestone's
   four components were implemented at
   `src/universal_orchestrator/registry/{projects,agents,providers}.py`
   and `src/universal_orchestrator/routing/router.py` instead. Flagging
   for Founder confirmation rather than silently picking one path.
4. **NUC/RTX-3070 agent role assignments are architecture, not proof.**
   This milestone's brief repeats the Claude-on-NUC /
   Hermes-on-RTX3070 role split as established fact. Per open question 1
   above, neither machine's availability is verified, and this
   repository ships **no pre-registered agents or providers** — the
   `AgentRegistry` and `ProviderRegistry` start empty; any NUC/RTX3070
   agent descriptors in the test suite are illustrative test data only,
   never seeded into production code, and `AgentDescriptor.machine` is a
   logical label chosen by whoever calls `register()`, never a verified
   fact about real hardware.
5. **Claude CLI has no literal "read-only" permission mode — narrowed,
   not resolved, by the M4 audit remediation.** Verified against real
   `claude --help` output: `--permission-mode` accepts `acceptEdits,
   auto, bypassPermissions, manual, dontAsk, plan` — none named
   "read-only". `ClaudeCodeAdapter` implements read-only execution as
   `--tools ""` (every built-in tool disabled), and the remediation
   added `--safe-mode`/`--strict-mcp-config`/`--disable-slash-commands`/
   `--setting-sources user` to also disable plugins, hooks, MCP servers
   and skills — closing the specific gap an independent audit found
   (this host's real settings had 15+ enabled plugins that the original
   `--tools ""`-only policy never accounted for). What remains
   explicitly unenforced — filesystem isolation, process isolation
   beyond grouping, and network isolation — is documented, not claimed,
   in [docs/EXECUTION_CLAUDE_CODE.md](docs/EXECUTION_CLAUDE_CODE.md)'s
   "Security posture" table. Planner/Architect/Reviewer roles still
   cannot browse the repository in this milestone, only reason from
   supplied text. Founder decision still needed before loosening
   `--tools ""` to an allow-list, once this CLI version's exact
   tool-name list is verified.
6. **No DeepSeek Harness CLI is installed anywhere on this host**
   (Milestone 4 addendum compatibility review). Only an HTTP API
   provider plugin inside OpenClaw exists under that name — not a CLI
   harness. DeepSeek compatibility with `ExecutionAdapter` therefore
   remains unverified, and no DeepSeek adapter is implemented. See
   [docs/EXECUTION_CLAUDE_CODE.md](docs/EXECUTION_CLAUDE_CODE.md).
7. **Hermes's zero-tool enforcement relies on an unrecognized-toolset
   name, not an officially documented flag value** (Milestone 5).
   Verified by tracing Hermes's own source and by two real executions
   (`cli.py --list_tools` and, separately, a real `hermes chat` call)
   that an unrecognized `-t/--toolsets` value yields zero enabled
   tools — but this is observed behavior of the current version
   (v0.21.0), not a contract Hermes documents or promises to keep. See
   [docs/EXECUTION_HERMES.md](docs/EXECUTION_HERMES.md) "The zero-tool
   mechanism" and "Known limitations". Also note: killing the local
   `docker exec` client does not stop the in-container process by
   itself — verified empirically — so `HermesAdapter`'s timeout path
   does additional, targeted in-container PID termination that
   `ClaudeCodeAdapter` never needed. Founder awareness requested before
   this mechanism is relied on for anything beyond the scratch-sandbox
   execution this milestone verified.
