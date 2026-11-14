# Architecture — Universal-Orchestrator

**This document answers: how does the system actually work.**
[ROADMAP.md](ROADMAP.md) answers what and when, and is the
authoritative source for milestone status and architecture *decisions*;
where this document's description of current behavior and ROADMAP.md's
decision disagree, ROADMAP.md's decision governs and this document is
corrected to match it, not the other way around.

Status vocabulary (IMPLEMENTED / TESTED / VERIFIED / PARTIALLY
IMPLEMENTED / PLANNED / NOT IMPLEMENTED / NOT APPROVED / LOCKED /
SUPERSEDED / UNKNOWN) is used exactly as defined in
[ROADMAP.md](ROADMAP.md)'s header. In particular: IMPLEMENTED never
substitutes for VERIFIED, and a component's documentation existing is
never read as the component existing.

**STATUS UPDATE (2026-11-14, audit Finding C-11) — this document
predates Milestone 6's closure and was never updated afterward.**
Per this document's own precedence rule above, ROADMAP.md governs:
M6 is **IMPLEMENTED, TESTED, VERIFIED, COMPLETE / ACCEPTED / CLOSED**
(ROADMAP.md §9–10). Specifically, every place below that still marks
**Approval Gate**, **Quality Gate**, **Git Worktree Manager**, or
**Conflict Manager** as NOT IMPLEMENTED or PLANNED — including §B's
component table, §D's "Security posture" table, and §H's concurrency
table — is **stale**; all four are real, implemented, tested modules
(`execution/approval_manager.py`, `execution/quality_manager.py`,
`execution/worktree_manager.py`, `execution/conflict_manager.py`)
wired together by `core/governed_pipeline.py`. Separately, M7 (Cycle
& Deadlock Detector, scheduler lease fencing, Stall Detector, Task
Scheduler, Proposal Recovery CLI) now also exists in code — see
ROADMAP.md §11's governance-status note (audit Finding C-10) for why
that is an implementation fact, not a milestone-acceptance fact.
This document's detailed per-component tables below are left as
historical record rather than rewritten table-by-table (avoiding an
unnecessary full rewrite); this note is the authoritative correction
for all of them at once.

---

## A. System Context

**Problem this system solves:** the Founder has several independent
projects, more than one AI agent/runtime capable of doing work, and no
single place that takes one goal, breaks it down, decides who/what
should do each piece, runs it, and tracks what happened. Universal-
Orchestrator is that single place.

**How the Founder interacts with it:** the Founder gives a goal to
`OrchestrationEngine.submit()`, reached in practice through one of
four real, `python3 -m`-invocable CLI entrypoints under
`src/universal_orchestrator/cli/` — `orchestrator_cli` (submit /
extract-claude-files / propose / recover-proposal / integrate),
`approval_cli`, `alerts_cli`, and `scheduler_cli` (audit Finding
C-11: this sentence previously, incorrectly, said no such entrypoint
existed — these four were added across the M6/M7 remediation
commits and are the real, documented way this system is actually
run; still no separate API or UI front-end). The Founder does not
call an adapter directly, does not pick a Git branch strategy per
task, and does not manually track task state — those are this
system's job once a goal is submitted.

**System boundary:** Universal-Orchestrator contains its own task
model, state store, registries, routing logic, and execution adapters.
It does **not** contain, absorb, or duplicate the configuration,
memory, secrets, or Git history of any project it orchestrates, nor of
Claude Code or Hermes themselves — those remain independent systems
reached only through the `ExecutionAdapter` interface (§E).

**Explicitly out of scope for this document and this codebase:**
- **Orchestrator B ("Orchestrator 3070")** — a separate, independent
  orchestration system. **LOCKED / FROZEN / NOT STARTED / OUT OF
  SCOPE.** This repository contains no code, configuration, or design
  that references, connects to, or assumes its existence. See
  [ROADMAP.md](ROADMAP.md) §7.
- **RTX 3070 (hardware)** — untouched, not integrated, **not a Worker
  Runtime**, no role in this architecture. See [ROADMAP.md](ROADMAP.md) §8.
- PSH (pshop-music), Việc Nha Trang, FOOD/Agent-Research, OpenClaw, and
  Hermes as *projects/runtimes* — each keeps its own repository,
  configuration, memory, secrets, and Git governance. OpenClaw and
  Hermes are reached only through execution adapters, never copied in.

---

## B. Component Architecture

Only components with real code in this repository are listed as
existing. Nothing below is invented.

**Status-label note:** `VERIFIED` is reserved for a real, live call
against an external system (a real Claude CLI call, a real Hermes
Docker call) — see the Claude Code Adapter and Hermes Adapter rows
below. The pure-logic rows below carry no such external call to
verify against; `IMPLEMENTED, TESTED` (matching [ROADMAP.md](ROADMAP.md)
§D's wording for the same components) is the accurate claim for them,
re-confirmed by this session's fresh full-suite run (257/257).

| Component | Module | Status |
|---|---|---|
| Core data models | `core/models.py` (265 lines) | **IMPLEMENTED, TESTED** |
| Task lifecycle rules | `core/lifecycle.py` (56 lines) | **IMPLEMENTED, TESTED** |
| Task Analyzer | `analysis/analyzer.py` (110 lines) | **IMPLEMENTED, TESTED** |
| Task Manager | `core/task_manager.py` (282 lines) | **IMPLEMENTED, TESTED** |
| State Manager (SQLite) | `core/state.py` (357 lines) | **IMPLEMENTED, TESTED** |
| Project Registry | `registry/projects.py` (102 lines) | **IMPLEMENTED, TESTED** |
| Agent Registry | `registry/agents.py` (74 lines) | **IMPLEMENTED, TESTED** |
| Provider Registry | `registry/providers.py` (62 lines) | **IMPLEMENTED, TESTED** |
| Agent Router | `routing/router.py` (177 lines) | **IMPLEMENTED, TESTED** |
| Orchestration Engine | `core/engine.py` (199 lines) | **IMPLEMENTED, TESTED**, single task at a time |
| ExecutionAdapter interface | `execution/adapter.py` (138 lines) | **IMPLEMENTED, TESTED** |
| Claude Code Adapter — **kept, not removed from this architecture** | `execution/claude_code.py` (456 lines) | **IMPLEMENTED; TESTED** (unit/integration, re-verified this session, 257/257); **VERIFIED (historical)** — real smoke test on record in `docs/EXECUTION_CLAUDE_CODE.md`, Milestone 4, not re-run this session |
| Hermes Adapter — **kept, not removed from this architecture** (resolves Founder Review Finding 3) | `execution/hermes.py` (557 lines) | **IMPLEMENTED; TESTED** (unit/integration, re-verified this session, 257/257); **VERIFIED (historical)** — real smoke test on record in `docs/EXECUTION_HERMES.md`, Milestone 5, not re-run this session |
| Adapter Registry | `execution/registry.py` (43 lines) | **IMPLEMENTED, TESTED** |
| Error hierarchy | `errors.py` (77 lines) | **IMPLEMENTED, TESTED** |
| Task Planner interface | `planning/planner.py` (36 lines) | **IMPLEMENTED** as an abstract interface only — `PlanStep`/`Plan` dataclasses plus an abstract `TaskPlanner.plan()`; **NOT IMPLEMENTED** as a working planner |
| Dispatcher interface | `planning/dispatcher.py` (26 lines) | **IMPLEMENTED** as an abstract interface only — abstract `Dispatcher.dispatch()`; **NOT IMPLEMENTED** as a working dispatcher |
| Approval Gate | `approval/gate.py` (7 lines) | **NOT IMPLEMENTED.** The file is a docstring-only placeholder with no code. Its own docstring is stale (says "implemented in Milestone 4," which is inaccurate, and mentions "RTX 3070 Desktop"); flagged for Founder decision in a separate task, not edited here (see Appendix item 2 below and [ROADMAP.md](ROADMAP.md) §10.3). |
| Quality Gate | — | **NOT IMPLEMENTED.** No module exists under this name anywhere in the repository. |
| Structured audit logging | `audit/logger.py` | **PARTIALLY IMPLEMENTED** — basic logging exists; a full audit trail is M6/M8-era scope, not yet built |
| Configuration management | `config/settings.py` | **IMPLEMENTED** |

**Components that do not exist and are not described elsewhere in this
document:** Git Worktree Manager, Conflict Manager, project memory
store, any DeepSeek adapter. These are **NOT IMPLEMENTED**; see
[ROADMAP.md](ROADMAP.md) §10 and §D.

---

## C. Execution Lifecycle

**Implemented today**, for one task at a time:

```
TaskRequest (Founder goal)
  → TaskAnalyzer.analyze()              [IMPLEMENTED, TESTED]
      → TaskSpecification
  → TaskManager.create_task() / decompose() / add_dependency()
                                          [IMPLEMENTED, TESTED]
      → Task (CREATED → ANALYZED → PLANNED → READY|BLOCKED)
  → AgentRouter.route()                  [IMPLEMENTED, TESTED]
      → RoutingDecision (SELECTED | REJECTED, with reasons)
  → OrchestrationEngine.execute()        [IMPLEMENTED, TESTED]
      - revalidates agent AVAILABLE status
      - revalidates provider verified + AVAILABLE status
      - calls adapter.verify() fresh (never trusts a cached verify)
      → AdapterRegistry[provider_id].execute()
                                          [IMPLEMENTED, TESTED]
                                          (dispatches to ClaudeCodeAdapter or
                                           HermesAdapter — see §B for each
                                           concrete adapter's own historical
                                           real-call VERIFIED status)
      → ExecutionResult
  → TaskManager.record_execution_result() / succeed() / fail()
                                          [IMPLEMENTED, TESTED]
      → StateManager (SQLite persistence)
                                          [IMPLEMENTED, TESTED]
```

**What is real about this today:** every arrow above is a real method
call with passing tests, and `OrchestrationEngine.execute()` never
dispatches to an agent or provider that fails re-validation at the
moment of execution — it does not trust an earlier, possibly-stale
status. `Task.status` transitions are only ever changed through
`core/lifecycle.py`'s `ALLOWED_TRANSITIONS` table; an illegal
transition raises `InvalidStateTransitionError` rather than silently
succeeding.

**What is planned, not implemented:**
- **Sequential vs. parallel scheduling** across multiple ready tasks —
  the dependency graph (BLOCKED until all `depends_on` SUCCEEDED) makes
  this possible later, but no scheduler exists; today the caller drives
  one task's lifecycle at a time.
- **Automatic agent-to-agent hand-off** (Planner output feeding a
  Coder, Coder output feeding a Reviewer) — each adapter call is
  invoked independently by the caller; there is no pipeline that chains
  them.
- **Result normalization beyond `ExecutionResult`'s shared shape** —
  `ExecutionResult.success` is a process-level signal (did the adapter
  call exit cleanly) only. It is never a claim that the task's actual
  objective was met — that judgment is the planned Quality Gate's job
  (§B, [ROADMAP.md](ROADMAP.md) §10.4), which does not exist yet.
- **Interrupted-task recovery is implemented but conservative:**
  `TaskManager.recover()` reclassifies any task found RUNNING at
  startup as INTERRUPTED (a crash or restart mid-execution leaves no
  other safe assumption); `resolve_interrupted()` requires explicit
  evidence to mark one CONFIRMED_SUCCEEDED and never auto-promotes an
  INTERRUPTED task on its own.

---

## D. Agent Architecture

**Four distinct concepts, not to be conflated (resolves Founder Review
Finding 1):**

| Concept | What it is | Where it lives |
|---|---|---|
| **Workspace** | Founder's organizing concept for the four roles below. Not a module or class — a repository-wide search confirms no file or identifier named "Workspace" exists anywhere in this codebase, and that is correct, not a defect: the four roles already exist without needing one. | Nowhere in code; a Founder-level organizing idea only |
| **Agent Role** | What an agent is *for* — Planner, Coder, Tester, Reviewer. Plain free-text on `AgentDescriptor.role` (`core/models.py`), matched by the Router against task requirements. | `AgentDescriptor.role: str`, `routing/router.py` |
| **Agent Runtime** | Which real AI environment executes a role — Claude Code CLI, Hermes, (future) DeepSeek. A role is never assumed to require one specific runtime or one specific model. | `ProviderDescriptor` (`core/models.py`), `registry/providers.py` |
| **Execution Adapter** | The code layer connecting the Orchestrator to one runtime — `verify()`/`execute()`. | `execution/adapter.py`, `claude_code.py`, `hermes.py` |

The Founder has confirmed the Workspace's four roles are **Planner,
Coder, Tester, Reviewer** and that this structure is not to be rebuilt,
renamed, or expanded with new agents merely because earlier milestones'
documentation named role/runtime pairs differently. The table below's
"Claude Planner/Architect," "Hermes Coder/Tester," "Claude Reviewer"
wording is retained as a historical record of which *runtime* executed
which *role* in the milestones built so far — a Runtime fact layered on
top of a Role, never a rule binding one to the other, and never
evidence that the four roles coordinate automatically.

| Role | Responsibility | Status |
|---|---|---|
| Founder | Decide the goal; approve architecture/roadmap; give final acceptance | Human, not code |
| Universal-Orchestrator | Analyze, decompose, route, coordinate, track state | **IMPLEMENTED, TESTED**, one task at a time |
| Claude Planner / Architect | Plan and design | **PARTIALLY IMPLEMENTED** — `AgentDescriptor.role` can be `"planner"`/`"architect"`; `ClaudeCodeAdapter` is a real, working execution path for whatever such an agent produces. No automatic plan-then-design pipeline exists. |
| Hermes Coder / Tester | Implement and test | **PARTIALLY IMPLEMENTED** — `role="coder"`/`"tester"` plus a real, working `HermesAdapter`. No automatic hand-off from a Planner's output exists. |
| Claude Reviewer | Independent review | **PARTIALLY IMPLEMENTED** — `role="reviewer"` is a label the Router can match against task requirements; no review-specific logic exists beyond that label match. |
| Integration Test | Verify the combination works together | **NOT IMPLEMENTED** |
| Founder Approval (enforced) | Block sensitive actions until a recorded Founder decision | **NOT IMPLEMENTED** — see Approval Gate, §B |

**Important distinction, stated explicitly because it is easy to get
wrong by reading the role names alone:** this table's roles are labels
an `AgentDescriptor` carries and the `AgentRouter` matches against —
real data, really used in routing decisions. They are **not** proof
that the six-stage Founder → Orchestrator → Planner → Coder → Reviewer
→ Integration Test → Founder pipeline runs automatically end-to-end.
Today, each stage is invoked independently by whatever code calls
`engine.execute()`; nothing in this repository chains one agent's
output into the next agent's input. See [ROADMAP.md](ROADMAP.md) §C for
the target workflow this is measured against.

**Machine/role assignment is logical, not verified infrastructure:**
`AgentDescriptor.machine` is a free-text label chosen by whoever calls
`AgentRegistry.register()` — it is never cross-checked against real,
reachable hardware. No agent or provider is pre-registered by this
codebase; `AgentRegistry` and `ProviderRegistry` both start empty.

---

## E. Runtime Architecture

**Provider vs. Adapter, kept deliberately separate:**
- `ProviderDescriptor` (`core/models.py`) is metadata only — name,
  kind, verification/status flags. **It has no credential field, by
  design** — no provider secret is ever stored in this repository's
  data model.
- `ExecutionAdapter` (`execution/adapter.py`) is an ABC with exactly
  two methods, `verify()` and `execute()`. Every concrete adapter
  implements both.
- `AdapterRegistry` (`execution/registry.py`) maps a `provider_id` to
  one adapter instance — this is the whole mechanism by which the
  Engine picks "Claude vs. Hermes vs. (future) something else": by
  provider identity, not by special-casing adapter types anywhere in
  the Engine.

**Verification policy (standing, enforced in code):**
`ProviderDescriptor.verified` defaults to `False`. `ExecutionAdapter
.verify()` must perform a real check against the live runtime (not
assume success) and return one of `VerificationStatus`'s real values
(`AVAILABLE`, `UNAVAILABLE`, `UNAUTHENTICATED`, `UNSUPPORTED`,
`ERROR`). No code path in this repository dispatches to, or reports as
operational, a provider or agent while unverified —
`OrchestrationEngine.execute()` re-checks this at the moment of
execution, not from a cached flag, and raises `ProviderNotVerifiedError`
/ `AgentNotEligibleError` / `AdapterUnavailableError` otherwise.

**Claude Code Adapter** (`execution/claude_code.py`, **IMPLEMENTED**;
**TESTED** by unit/integration suite re-verified this session; **real
smoke-test VERIFIED is historical** — Milestone 4, recorded in
`docs/EXECUTION_CLAUDE_CODE.md`, not re-run this session): executable
path pinned once (resolved via `PATH`,
never re-resolved — only re-checked for continued existence — between
`verify()` and `execute()`, closing a PATH-hijack window); environment
passed to the subprocess is an explicit allowlist (`HOME, PATH, LANG,
LC_ALL, TERM, TMPDIR`), never the parent process's full environment;
isolation flags `--safe-mode --strict-mcp-config
--disable-slash-commands --setting-sources user --tools ""` disable
every built-in tool, every plugin, every hook, every MCP server, and
every skill — this is the "read-only" enforcement mechanism, and it is
explicitly *not* the same thing as filesystem, process, or network
isolation (see §G); process-group SIGTERM-then-SIGKILL on timeout;
output truncated at a byte boundary (`encode → slice → decode
(errors="ignore")`), never a naive character-count slice that could
split a multi-byte UTF-8 sequence.

**Hermes Adapter** (`execution/hermes.py`, **IMPLEMENTED**; **TESTED**
by unit/integration suite re-verified this session; **real smoke-test
VERIFIED is historical** — Milestone 5, recorded in
`docs/EXECUTION_HERMES.md`, not re-run this session; **kept in this
architecture, not removed** — resolves Founder Review Finding 3): no
native host CLI exists for Hermes — every call is
`docker exec <container> hermes ...`; the container ID is resolved once
via `docker inspect` and pinned the same way the Claude executable path
is, closing a container-swap window between `verify()` and `execute()`;
zero-tool enforcement uses a deliberately-unrecognized `-t/--toolsets`
sentinel value (Hermes documents no literal "disable all tools" flag;
this behavior was confirmed by tracing Hermes's own CLI source and by
two real executions, not assumed); `--safe-mode` is also passed;
output is plain text only — Hermes has no JSON output mode, unlike
Claude. **Process termination is adapter-specific:** killing the local
`docker exec` client process does **not** stop the process running
inside the container — verified empirically — so on timeout the
adapter writes a pidfile inside the container
(`sh -c 'echo $$ > pidfile; exec "$@"'`, with an adapter-generated
UUID filename, never caller-supplied text, to avoid shell injection)
and sends a targeted kill to that specific in-container PID.

**Multi-engine proof:** `tests/test_multi_runtime.py` instantiates both
real adapter classes against minimal, deterministic fake executables
(never real Claude or Hermes calls) and proves: both satisfy the same
`ExecutionAdapter` contract; both are independently verifiable; one
being unavailable never affects the other; both produce the same
normalized `ExecutionResult` shape; and `OrchestrationEngine` dispatches
a `"reviewer"`-role task to Claude and a `"coder"`-role task to Hermes
purely by routing through `provider_id` — no adapter-type special-casing
exists in the Engine. This is real and passing, but it demonstrates two
runtimes sharing one interface, not the full automatic engine-selection
policy described next.

**Engine selection — status, precisely:**
- Manual selection (an explicit per-task/per-project runtime config): **NOT IMPLEMENTED.**
- Automatic selection policy (the Engine deciding Claude vs. Hermes vs.
  future runtimes from task characteristics): **NOT IMPLEMENTED.** What
  exists is the Router's role/capability/permission/cost eligibility
  matching (`routing/router.py`), which selects an *agent*, whose
  `provider_id` then determines the adapter — this is real selection
  logic, but it is role-based matching, not a runtime-selection policy
  engine.
- **DeepSeek:** **NOT IMPLEMENTED, NOT INTEGRATED.** No DeepSeek Harness
  CLI is installed anywhere on this host. Only an unrelated HTTP API
  provider plugin inside OpenClaw exists under that name. DeepSeek
  compatibility with `ExecutionAdapter` is **UNKNOWN** — never to be
  declared integrated without a real installed CLI to verify against.

**Claude CLI, Hermes, and DeepSeek each carry independent status
(resolves Founder Review Finding 3).** Founder's stated near-term
runtime direction — Claude CLI now, DeepSeek once it is actually
integrated — describes where new integration effort goes next. It does
**not** remove Hermes from this architecture: Hermes remains a fully
IMPLEMENTED, unit/integration-TESTED, historically-VERIFIED M5 runtime,
on equal footing with Claude CLI as a real, working `ExecutionAdapter`.
None of the three runtimes' status is raised or lowered because of
another's mention in a Founder directive — only real code and real
evidence move a status tag.

---

## F. Data Architecture

| Data | Persisted? | Where |
|---|---|---|
| Task state (`Task`, `TaskStatus`, history) | **Durable — SQLite** | `core/state.py`, `tasks` table |
| Routing decisions | **Durable — SQLite** | `routing_decisions` table |
| Execution results | **Durable — SQLite** | `execution_results` table, keyed by `task_id` |
| Project metadata (`ProjectProfile`) | **In-memory only** | `registry/projects.py` — process-lifetime `ProjectRegistry` |
| Agent metadata (`AgentDescriptor`) | **In-memory only** | `registry/agents.py` |
| Provider metadata (`ProviderDescriptor`) | **In-memory only** | `registry/providers.py` |
| Project memory (prior conversations, learned preferences, etc.) | **NOT IMPLEMENTED** | No module of this kind exists. Do not claim "project memory" exists anywhere else in this repository's documentation. |
| Credentials of any kind | **Never stored in this repository's data model** | `ProviderDescriptor` has no credential field; this is a design choice, not an oversight |

`StateManager`'s `create()`/`update()` methods translate a SQLite
`IntegrityError` into this codebase's own `DuplicateError` — this
applies to both the `tasks` table (since Milestone 2) and the
`execution_results` table (added in the Milestone 4 audit remediation).
All three SQLite tables are keyed consistently by `task_id` and/or
`project_id`; a review for this document found no evidence of
cross-project data mixing.

Registries are per-process, in-memory, and empty on startup — nothing
is seeded. Restarting the process loses all registry contents (not the
SQLite-persisted task/routing/execution-result data, which survives a
restart and feeds `TaskManager.recover()`).

---

## G. Security Architecture

**Stated plainly: this system is not, and does not claim to be,
absolutely secure.** Every limitation below is a real, current gap,
not a hypothetical.

| Control | Status |
|---|---|
| Environment allowlist on every subprocess call (`HOME, PATH, LANG, LC_ALL, TERM, TMPDIR`) | **IMPLEMENTED, VERIFIED** — both adapters pass only this list, never the full parent environment |
| Executable / container pinning (resolved once, re-validated for existence only, never re-resolved) | **IMPLEMENTED, VERIFIED** — closes the PATH-hijack / container-swap window between `verify()` and `execute()` |
| Zero-tool / "safe-mode" policy | **IMPLEMENTED, VERIFIED** for both adapters — Claude via `--tools "" --safe-mode --strict-mcp-config --disable-slash-commands --setting-sources user`; Hermes via an unrecognized `--toolsets` sentinel plus `--safe-mode` |
| Credential handling discipline | **IMPLEMENTED, VERIFIED by practice, not by an automated test** — status checks (`claude auth status`, `hermes status`) are reduced to a single boolean in memory; the raw value is never printed, logged, or stored. (See the standing rule in this session never to open `~/.claude/settings.json` or any other credential-bearing file.) |
| Byte-accurate output truncation | **IMPLEMENTED, TESTED** |
| Filesystem isolation | **NOT IMPLEMENTED.** The zero-tool policy stops the adapter's own built-in file-editing tools from being invoked; it does not sandbox the filesystem. A process running with filesystem access at the OS level is not prevented by this code from reading or writing outside its intended working directory. |
| Process isolation | **PARTIALLY IMPLEMENTED.** Claude: process-group signaling on timeout, not a sandboxed process. Hermes: pidfile-based targeted in-container PID kill, which depends on the container's own isolation (Docker), not on anything this adapter enforces itself. |
| Network isolation | **NOT IMPLEMENTED.** Neither adapter restricts outbound network access from the process/container it invokes. |
| Real Docker-execution isolation guarantees | **NOT IMPLEMENTED beyond what Docker itself provides.** This codebase does not configure Docker security options (seccomp, capabilities, read-only root, etc.) — it only calls `docker exec` against a container Hermes already assumes is running. |
| Approval Gate (sensitive-action interception) | **NOT IMPLEMENTED.** See §B and D. |
| Quality Gate | **NOT IMPLEMENTED.** See §B. |

**What "read-only execution" means today, precisely:** it means the
adapter's own tool-invocation surface is disabled — the model cannot
call a file-write tool, a shell tool, or any plugin/hook/MCP-exposed
tool through the CLI it's driving. It does **not** mean the OS-level
process has no filesystem, process, or network capability; those
remain the responsibility of whatever sandbox (today, none beyond
Docker's own default isolation for Hermes) eventually wraps the
adapter call. This distinction was the specific gap an independent M4
audit found and the M4 remediation narrowed, not fully closed.

**Known non-contractual mechanisms (recorded so they are not
over-trusted):** Hermes's zero-tool behavior relies on an unrecognized
toolset name producing zero enabled tools — observed and verified
against the current installed Hermes version, not a flag Hermes's own
documentation promises to keep behaving this way in a future version.

---

## H. Concurrency and Git Architecture

**Standing rule (Founder-decided):** Claude and Hermes must never
modify the same working tree concurrently.

| Mechanism | Status |
|---|---|
| Enforced by code | **NOT IMPLEMENTED** |
| Enforced by process/discipline (one milestone, one adapter call, reviewed before the next) | **This is the current reality** |
| Git Worktree Manager (per-execution isolated worktree) | **PLANNED** — M6, see [ROADMAP.md](ROADMAP.md) §10.1 |
| Conflict Manager (diff-based overlap detection before merge) | **PLANNED** — M6, see [ROADMAP.md](ROADMAP.md) §10.2 |
| Approval Gate | **PLANNED** — M6, see [ROADMAP.md](ROADMAP.md) §10.3 |
| Quality Gate | **PLANNED** — M6, see [ROADMAP.md](ROADMAP.md) §10.4 |

**Git governance process (standing, applied to every milestone in this
repository's history):** verify branch and `HEAD`; verify `origin/main`
and that it matches the expected baseline; inspect the working tree for
anything pre-existing and uncommitted; keep each change scoped to its
milestone; review the full diff; scan for secrets and runtime data
before staging; run the full automated test suite; commit only on
explicit authorization; push only when the remote has not diverged;
verify the local and remote SHA match after pushing. Never force-push.
Never rewrite history. This process is managed by discipline today —
the planned Quality Gate (§B, M6) is what would make "tests pass" and
"scope is clean" an enforced precondition rather than a manually-checked
one.

---

## I. Deployment and Environment

| Environment claim | Status |
|---|---|
| This repository runs on the ASUS Ubuntu Server (hostname `tieu`) | **VERIFIED this session** — `git status`/`git rev-parse` commands in this session executed against this host |
| Resource constraints: 4 threads, ~7 GB RAM | **UNKNOWN — carried over from Phase 0 discovery, not independently re-measured in this session** |
| A separate NUC Ubuntu Server exists at a given LAN IP | **UNKNOWN** — reported during Phase 0 discovery; not re-verified here, and not relevant to this repository's own code, which assumes no specific machine |
| RTX 3070 (hardware) is part of this deployment | **NOT TRUE.** Not integrated, not a Worker, not part of any deployment environment this repository defines. |
| Orchestrator B ("3070") is part of this deployment or environment | **NOT TRUE, and LOCKED/out of scope** — not recorded as a deployment environment, not connected to Orchestrator A in any way |
| No pip / ensurepip on this host's Python 3.14 install | **VERIFIED (Milestone 1)** — this is why the test suite uses stdlib `unittest` rather than pytest |

Nothing in this section asserts a deployment claim without the evidence
basis stated next to it. Where Phase-0-era discovery produced a claim
this session did not re-check, it is marked **UNKNOWN**, not reasserted
as current fact.

**Hardware-independence (Founder direction — fresh evidence this
session):** a repository-wide search of `.py` files for hardcoded
hostnames, IPs, or resource constants found none in executable code —
`config/settings.py` derives every path from `__file__` and
environment variables; `ClaudeCodeAdapter`/`HermesAdapter` resolve
their target via `PATH`/an explicit override, never a hardcoded
absolute path; `AgentDescriptor.machine` is, and always was, a
free-text label the Router/Task Manager/State Manager never read.
**The Orchestrator itself is not bound to this NUC** — running it on a
stronger machine instead requires no code change. What does not exist
yet is a *remote*-execution adapter (today's two adapters both run
locally relative to wherever the Orchestrator process is); adding one
remains possible through the existing `ExecutionAdapter`/
`AdapterRegistry` mechanism alone, without touching the Engine, Task
Manager, or any M6 component. See [ROADMAP.md](ROADMAP.md) §10.5 for
the full constraint this places on M6's four components — this is a
design discipline on existing scope, not a new component, and does not
authorize connecting a second machine.

---

## J. Architecture Diagrams

```
┌─────────────────────────────── Orchestrator A (this repository) ───────────────────────────────┐
│                                                                                                  │
│  Founder                                                                                        │
│    │  (one goal, one entry point)                                                               │
│    ▼                                                                                             │
│  TaskAnalyzer ──▶ TaskManager ──▶ AgentRouter ──▶ OrchestrationEngine.execute()                  │
│  [IMPLEMENTED]    [IMPLEMENTED]    [IMPLEMENTED]        │                                        │
│                        │                                 ├──▶ AdapterRegistry                   │
│                        ▼                                 │     [IMPLEMENTED]                     │
│                  StateManager (SQLite)                   │      │          │                    │
│                  [IMPLEMENTED]                           │      ▼          ▼                    │
│                                                            │  ClaudeCodeAdapter   HermesAdapter    │
│                                                            │  [IMPLEMENTED,       [IMPLEMENTED,    │
│                                                            │   VERIFIED]           VERIFIED]       │
│                                                            │      │                 │             │
│                                                            │      ▼                 ▼             │
│                                                            │  Claude Code CLI    Docker exec ──▶   │
│                                                            │  (external, real)   Hermes container  │
│                                                            │                      (external, real) │
│                                                            │                                        │
│  [PLANNED — M6, not built yet, shown dashed below]        │                                        │
│  ┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐    │                                        │
│  ┆ Git Worktree Mgr → Conflict Mgr → Approval Gate ┆◀───┘                                        │
│  ┆                          → Quality Gate          ┆                                             │
│  └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘                                             │
│                                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────────────────┘

  Orchestrator B ("Orchestrator 3070")        RTX 3070 (hardware)
  ┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐          ┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
  ┆  LOCKED / FROZEN / NOT STARTED  ┆          ┆  untouched, not a   ┆
  ┆  OUT OF SCOPE                   ┆          ┆  Worker, no role    ┆
  ┆  NO connection to Orchestrator A ┆          ┆  in this system     ┆
  └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘          └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
  (drawn only to show the boundary — never as a worker node, never
   connected by any arrow to anything inside Orchestrator A)
```

**Legend, as required of every diagram in this document:**
- **Solid box, "[IMPLEMENTED]"** — exists in code today, with passing tests.
- **Dashed box, "[PLANNED]"** — scoped (see [ROADMAP.md](ROADMAP.md) §10), no code yet.
- **External system** (Claude Code CLI, Hermes/Docker) — reached only
  through an adapter, never absorbed into this repository.
- **Locked system** (Orchestrator B) — drawn once, outside the
  Orchestrator A boundary, with no arrow in or out, specifically to
  prevent it ever being mistaken for a worker node of this system.
- **This diagram does not draw §K's target mechanisms below.** They
  are PLANNED, not yet designed into the Engine's call graph, and are
  described in §K only — adding them here before they exist would
  imply they run, which they do not.

---

## K. Target Architecture — Dynamic Orchestration, Cost Optimization, and Execution Safety — **PLANNED, NOT IMPLEMENTED**

This section describes how these mechanisms would fit the architecture
above **if and when Founder approves building them** (see
[ROADMAP.md](ROADMAP.md) §G–§K for their scope, dependencies, and
milestone mapping). Nothing here is drawn into §J's diagram, and
nothing here is implemented.

**Dynamic Agent Orchestration (target):** extends the existing
`TaskAnalyzer → TaskManager → AgentRouter → OrchestrationEngine`
pipeline (§C) with three capabilities that do not exist today: (1) a
scheduler that reads the existing dependency graph to decide
sequential-vs-parallel execution, instead of a human driving one task
at a time; (2) on-demand agent-session creation, instead of the
Registries starting and staying empty until a human calls `register()`;
(3) automatic hand-off of one role's output into the next role's input,
instead of each `execute()` call being independently triggered. The
Orchestrator remains the sole holder of routing, retry, budget,
execution-permission, and stop-condition authority in this target —
no agent gains the ability to expand its own scope or chain further
work on its own initiative.

**Token Efficiency and Cost Optimization (target):** would sit between
`AgentRouter.route()` and `OrchestrationEngine.execute()` as a
cost-aware selection layer — reading task risk/difficulty (from
`TaskSpecification`, which exists) to pick a runtime, scoping context
per task/role instead of passing full repository state, and checking a
budget ceiling before dispatch. None of this exists; `AgentRouter`
today selects purely on role/capability/permission/cost-tier
eligibility, with no token/call/time accounting layer anywhere in the
codebase.

**Loop Prevention and Execution Safety (target):** would add (1) a
cycle check at `TaskManager.add_dependency()` time — today this method
has no such check, a real and specific gap, not a hypothetical;
(2) a no-progress heuristic over `Task.history`; (3) a workflow-level
timeout/concurrency limiter above the adapter-level timeouts that
already exist in `ClaudeCodeAdapter`/`HermesAdapter`; (4) a persisted
stop-reason, extending the pattern `TaskManager.recover()`/
`resolve_interrupted()` already establish for refusing to auto-promote
an uncertain task. This group overlaps directly with the planned
Approval Gate (§B) — an out-of-authority action is exactly what that
Gate is meant to intercept.

**Reliability and Recovery (target):** `TaskManager.recover()` and
`resolve_interrupted()` already implement checkpoint/recovery at the
single-task level (M2) — real, tested, re-verified this session. What
is PLANNED, not implemented, is the same discipline at the workflow
level (across multiple tasks/agents), plus agent health monitoring,
model fallback with a recorded reason, idempotency guarantees for
side-effecting actions, and an audit trail extended to cover retries,
fallbacks, and stop reasons (today's `routing_decisions` and
`execution_results` tables already cover routing and results, M2–M4).

**Scope validation (this document mirrors [ROADMAP.md](ROADMAP.md)
§K's per-mechanism allocation, not a looser category-level one):** of
the 22 mechanisms across these four target areas, only **three** are
proposed as milestone scope extensions — each explicitly **PROPOSED
SCOPE EXTENSION, NOT APPROVED** — and each is a modest, specific
addition, not a wholesale expansion: a workflow-level session timeout
plus concurrent-session cap (against M6), a restatement of M6.4
Quality Gate's existing acceptance criteria to name Confidence-based
Review explicitly (against M6, no new component), and workflow-level
checkpoint/recovery (against M8, whose own approved name already
covers "recovery"). The remaining mechanisms are either already
covered by an existing, approved component (so recording them again
would double-count it), genuinely not yet assignable (their own
dependencies don't exist), or deliberately deferred as lower priority.
M6 remains four components; M7 remains untouched and NOT APPROVED; M8
remains gated on M6 and M7 and is not turned into a catch-all for every
remaining mechanism. **None of this section, and no entry in
[ROADMAP.md](ROADMAP.md) §K, constitutes Founder approval of anything.**

---

## Appendix — items noted for Founder decision, not acted on in this document

Per instruction, no source code or test file was modified while
preparing this document. The following source-level issues were found
during evidence-gathering and are recorded here only:

1. `src/universal_orchestrator/execution/__init__.py`'s module
   docstring still lists "a future RTX 3070 worker" among illustrative
   future execution environments. This phrasing predates the
   Orchestrator A/B correction and reads as inconsistent with
   [ROADMAP.md](ROADMAP.md) §8's current decision. **Not edited** —
   this is a `.py` source file; left for the Founder to decide in a
   separate task.
2. `src/universal_orchestrator/approval/gate.py`'s module docstring
   says "implemented in Milestone 4" (inaccurate — the file has no code
   at all, see §B) and mentions "the RTX 3070 Desktop" by name. **Not
   edited** — same reason as above.
3. **Historical package-naming note (not a pending decision — a settled
   fact, restored here for reference):** the Milestone 3 brief
   specified `src/orchestrator/registry/*.py` and
   `src/orchestrator/routing/router.py`; this repository's package has
   been `src/universal_orchestrator/` since Milestone 1 — verified
   fresh this session: `pyproject.toml`'s `name = "universal-orchestrator"`,
   32 files under `src/`/`tests/` import `universal_orchestrator`, and
   no `src/orchestrator/` directory exists. Renaming the package to
   match the brief would touch every file in the repository for no
   functional benefit, so Milestone 3's components were implemented at
   the real, existing path instead. Recorded here so the fact is not
   lost again, not because it awaits a new Founder decision.

Items 1–2 are stale comments in source files, not architecture claims
made by this document. Item 3 is a verified historical fact. None of
the three changes anything described in §A–§J.
