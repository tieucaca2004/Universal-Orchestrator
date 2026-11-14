# ROADMAP — Universal-Orchestrator

**Status of this document: AUTHORITATIVE.** This file is the single
source of truth for project direction, milestone status, and
architecture decisions that affect sequencing. Where this file and any
other document disagree, this file wins; the other document is
corrected to match (see §13). A chat prompt, an agent's suggestion, a
prior assumption, or technical convenience never overrides a decision
recorded here as the Founder's.

`ROADMAP.md` answers **what and when**. [ARCHITECTURE.md](ARCHITECTURE.md)
answers **how the system actually works**. The two must stay
consistent; where either changes, the other is checked for drift.

**Decision precedence**, applied wherever two sources disagree:
1. Founder's direct, most recent decision.
2. Architecture decisions Founder has confirmed earlier in project history.
3. This ROADMAP and other official documents.
4. Source code, tests, and other technical evidence.
5. Agent reports.
6. AI proposal or inference — lowest precedence; never promoted to a
   Founder decision on its own.

**Status vocabulary used throughout — these are not interchangeable:**

| Tag | Means |
|---|---|
| **IMPLEMENTED** | Real code exists for this. Says nothing about whether it's tested or whether reality matches it. |
| **TESTED** | Automated tests exist and pass for this, in addition to being implemented. |
| **VERIFIED** | Independently re-confirmed against the live system (a real command run, a real file read) in the session that wrote this status, not merely reported by a prior turn. |
| **PARTIALLY IMPLEMENTED** | Some of the described capability exists in code; the rest does not. |
| **PLANNED** | Described and scoped, but no code exists yet. |
| **NOT IMPLEMENTED** | No code exists; not even partially. |
| **NOT APPROVED** | Scope has not received a Founder decision. |
| **LOCKED** | Founder has explicitly frozen this; no work may touch it. |
| **SUPERSEDED** | A prior decision this document replaces. Kept for history, not deleted. |
| **UNKNOWN** | Insufficient evidence. Never guessed. |

A capability is never marked IMPLEMENTED when only its documentation or
data structure exists. "TESTED" is never used as a substitute for
"VERIFIED" — passing a fake-executable unit test and independently
re-running a real command are different claims, stated separately.

---

## A. Project Vision — DECIDED

**System goal:** the Founder gives the system exactly **one** goal at
a single entry point. Everything after that — analysis, decomposition,
agent/runtime selection, execution coordination, state tracking, result
collection, testing, quality control, and presenting the outcome back
for approval — is the Orchestrator's job, not something the Founder
manually assigns task-by-task to each agent.

**Founder's role:** decide the goal; approve this Roadmap and any
scope change to it; give final acceptance on delivered work. The
Founder does not operate adapters, write code, or manually wire tasks
to agents — that is what the Orchestrator exists to do.

**Orchestrator's coordination responsibility**, and its real status
today:

| Responsibility | Status |
|---|---|
| Analyze the request | **IMPLEMENTED, TESTED** — `analysis/analyzer.py` |
| Classify the task | **PARTIALLY IMPLEMENTED** — `TaskSpecification` captures objective/scope/constraints/risks/required capabilities; no separate task-type taxonomy exists beyond that |
| Decompose into sub-tasks | **IMPLEMENTED, TESTED** — `TaskManager.decompose()` |
| Determine dependencies | **IMPLEMENTED, TESTED** — `TaskManager.add_dependency()`, BLOCKED/READY gating |
| Decide sequential vs. parallel execution | **NOT IMPLEMENTED** — the dependency graph enables a future scheduler; no scheduler exists |
| Select Agent | **IMPLEMENTED, TESTED** — `routing/router.py` |
| Select runtime (provider/adapter) | **IMPLEMENTED, TESTED** — `AdapterRegistry`, selected by `provider_id` |
| Coordinate execution | **IMPLEMENTED, TESTED**, single task at a time | `OrchestrationEngine.execute()` |
| Track state | **IMPLEMENTED, TESTED** — `TaskManager`/`StateManager`, SQLite |
| Collect results | **IMPLEMENTED, TESTED** — `ExecutionResult`, persisted |
| Test the result | **NOT IMPLEMENTED** — no Quality Gate exists |
| Quality control | **NOT IMPLEMENTED** — no Quality Gate exists |
| Present to Founder for approval | **NOT IMPLEMENTED** — `approval/gate.py` has been a 7-line, empty placeholder since M1 |

**Development principle (standing, applies to every future milestone):**
the Orchestrator has no authority to expand the Founder's stated goal
or to bypass the Approval Gate once it exists. Nothing is marked done
until it is independently re-verified, not merely reported.

---

## B. Approved Architecture Decisions — DECIDED

1. **Orchestrator A** (this repository) is the only system this
   Roadmap governs.
2. **Orchestrator B ("3070") is LOCKED / FROZEN / NOT STARTED / OUT OF
   SCOPE.** See §7.
3. **RTX 3070 (hardware) is untouched, not integrated, not a Worker
   Runtime, and has no role in the current architecture.** See §8.
4. Agent responsibilities, runtime/adapter architecture, project
   isolation, concurrency, Git governance, and the Approval/Quality
   Gate are recorded in §C–§F and §10 below.

---

## C. Agent Workflow — target DECIDED, actual PARTIALLY IMPLEMENTED

**Workspace vs. Role vs. Runtime — terminology clarification (resolves
Founder Review Finding 1, confirmed by Founder):** Founder has
confirmed the system's organizing concept is a **Workspace** containing
exactly four roles — **Planner, Coder, Tester, Reviewer**. "Workspace"
is a Founder-level organizational concept; it is not, and does not
need to be, a module or class in this codebase — a repository-wide
search confirms no file or identifier named "Workspace" exists, and
that is not an error to fix. The four roles already exist today as
plain `AgentDescriptor.role` string values (`"planner"`, `"coder"`,
`"tester"`, `"reviewer"`), matched by the Router against task
requirements — not an enum, and **not required to map 1:1 to one CLI
or one model**. The table below's "Claude Planner/Architect," "Hermes
Coder/Tester," "Claude Reviewer" wording is kept as a historical record
of which runtime happened to execute which role in the milestones
built so far — it is a **Runtime** framing layered on top of the
**Role**, not a rule that a role is permanently bound to a runtime, and
not a rebuild of the four roles. The roles and existing structure are
unchanged by this clarification. No pipeline automatically moves work
from one role to the next today; the table's status column states this
plainly and is not evidence of automatic four-role coordination.

**Target workflow (Founder-decided):**

```
Founder
  → Universal-Orchestrator
    → Claude Planner / Architect
      → Hermes Coder / Tester
        → Claude Reviewer
          → Integration Test
            → Founder Approval
```

| Role | Responsibility | Status |
|---|---|---|
| Founder | Decide goal; approve Roadmap; accept final results | Human, not code |
| Orchestrator | Analyze, decompose, coordinate, track state | **IMPLEMENTED, TESTED** for one task at a time |
| Claude Planner / Architect | Plan and design | **PARTIALLY IMPLEMENTED** — `role="planner"`/`"architect"` and a working `ClaudeCodeAdapter` both exist; no code automatically runs a plan-then-design pipeline |
| Hermes Coder / Tester | Implement and test | **PARTIALLY IMPLEMENTED** — `role="coder"`/`"tester"` and a working `HermesAdapter` exist; no automatic hand-off from a Planner's output exists |
| Claude Reviewer | Independent review | **PARTIALLY IMPLEMENTED** — `role="reviewer"` is a label the Router can match; no review-specific logic or automatic triggering exists |
| Integration Test | Verify the combination works | **NOT IMPLEMENTED** |
| Founder Approval | Final human sign-off, enforced in code | **NOT IMPLEMENTED** |

**This workflow is not automated end-to-end.** The parts it needs
(role-tagged agents, two independent working adapters, task/dependency/
state tracking) are real, implemented, and tested. Driving one Founder
goal through all six pipeline stages without a human manually calling
`submit()`/`route()`/`execute()` at each step does not exist in code.
This is recorded as PARTIALLY IMPLEMENTED, never IMPLEMENTED.

---

## D. Runtime Roadmap

| Component | Status | Evidence |
|---|---|---|
| `ExecutionAdapter` interface | **IMPLEMENTED, TESTED** (re-verified this session, 257/257) | `execution/adapter.py` |
| `ClaudeCodeAdapter` | **IMPLEMENTED; TESTED** — unit/integration suite, re-verified this session (257/257 pass); **VERIFIED** — real smoke test against the live Claude CLI, **historical evidence only**: performed at Milestone 4, recorded in `docs/EXECUTION_CLAUDE_CODE.md`, **not re-run in this review session**, scope limited to that milestone's one bounded real call | `execution/claude_code.py` |
| `HermesAdapter` — **kept, M5-implemented, not removed from this architecture** (resolves Founder Review Finding 3) | **IMPLEMENTED; TESTED** — unit/integration suite, re-verified this session (257/257 pass); **VERIFIED** — real smoke test against the live Hermes container, **historical evidence only**: performed at Milestone 5, recorded in `docs/EXECUTION_HERMES.md`, **not re-run in this review session**, scope limited to that milestone's one bounded real call | `execution/hermes.py` |
| `AdapterRegistry` | **IMPLEMENTED, TESTED** (re-verified this session) | `execution/registry.py` |
| `ProviderRegistry` | **IMPLEMENTED, TESTED** (re-verified this session) | `registry/providers.py` |
| `OrchestrationEngine` | **IMPLEMENTED, TESTED**, single-task dispatch only (re-verified this session) | `core/engine.py` |
| Agent Router | **IMPLEMENTED, TESTED** (re-verified this session) | `routing/router.py` |
| Manual engine selection (per project/task config) | **NOT IMPLEMENTED** | No explicit "pick Claude vs. Hermes" config exists beyond what the Router's own eligibility rules decide |
| Automatic engine selection policy | **NOT IMPLEMENTED** | `AdapterRegistry` + `ProviderRegistry` are what such a policy would read; no policy engine exists |
| **DeepSeek CLI/Harness** | **NOT IMPLEMENTED, NOT INTEGRATED** | A previously-intended future runtime. No DeepSeek Harness CLI is installed anywhere on this host; only an unrelated HTTP API plugin inside OpenClaw exists under that name. **Never to be declared integrated without real evidence.** |

**Claude CLI, Hermes, and DeepSeek carry independent status — one is
never dropped because Founder's stated near-term direction names the
other two.** Founder's near-term runtime direction (Claude CLI now,
DeepSeek once it is actually integrated) describes where new
integration effort goes next; it does not remove Hermes, which remains
a fully IMPLEMENTED, TESTED M5 runtime per the real evidence above.
Unit/integration TESTED status (this session's 257/257 run) and
real-smoke-test VERIFIED status (a live CLI/Docker call) are two
different claims and are never merged into one label going forward.

`tests/test_multi_runtime.py` proves, with both real adapter classes
against fake executables, that two runtimes can share one interface —
this is real and tested, but it is two runtimes working through one
contract, not the full automatic-selection design above.

---

## E. Project Isolation

| Data | Status |
|---|---|
| Project profile | **IMPLEMENTED, TESTED** — `ProjectRegistry`/`ProjectProfile`, one row per project |
| Repository rules | **IMPLEMENTED, TESTED** — `allowed_operations`/`restricted_operations` per profile |
| Project memory | **NOT IMPLEMENTED** — no memory store exists at all |
| Agent assignment | **IMPLEMENTED, TESTED** — `AgentDescriptor`/`AgentRegistry`, no cross-project default |
| Runtime configuration | **IMPLEMENTED, TESTED** — `ProviderDescriptor` per provider, not per project; no project is hard-wired to a runtime |
| Secrets isolation | **IMPLEMENTED by omission** — `ProviderDescriptor` has no credential field at all; nothing in this codebase stores any project's secrets |
| Task history | **IMPLEMENTED, TESTED** — `Task.history`, `routing_decisions` table |
| Execution history | **IMPLEMENTED, TESTED** — `execution_results` SQLite table, keyed by `task_id` |

Every registry is in-memory per process, keyed by its own IDs; the
SQLite tables are keyed by `task_id`/`project_id` throughout. No
evidence of cross-project leakage was found.

---

## F. Concurrency and Git Governance

**Concurrency rule (DECIDED, standing since M5):** Claude and Hermes
must never modify the same working tree concurrently.

| Enforcement level | Status |
|---|---|
| Enforced by code | **NOT IMPLEMENTED** |
| Managed by process/discipline | **This is the current reality** |
| Planned | M6's Git Worktree Manager + Conflict Manager |

**Git Governance (DECIDED, standing process)** — every milestone must,
in order: (1) verify branch/HEAD, (2) verify `origin/main` and that the
baseline matches, (3) inspect the working tree, (4) control scope,
(5) review the full diff, (6) scan for secrets and runtime data,
(7) run the automated test suite, (8) satisfy the Quality Gate (once
it exists — until then, manual per-milestone review substitutes),
(9) commit only when explicitly authorized, (10) push only when the
remote has not diverged, (11) verify the local and remote SHA match
after pushing. Never force-push. Never rewrite history. Never delete
data without explicit instruction. Never commit a secret. Never declare
success without re-checked evidence.

**Approval Gate and Quality Gate** — both **NOT IMPLEMENTED**. See §10.3–§10.4.

---

## G. Target Architecture — Dynamic Agent Orchestration — **PLANNED, NOT IMPLEMENTED**

**This section describes a target architecture, not a pipeline that
exists.** No step below runs automatically today beyond what its
"Current reality" column states. Nothing here authorizes building any
of it in this task; it exists so M6/M7/M8 scoping has a single, honest
reference point instead of being invented ad hoc per milestone.

| # | Target step | Current reality |
|---|---|---|
| 1 | Founder gives one high-level goal | Human action, unchanged |
| 2 | Orchestrator analyzes and decomposes into tasks | **IMPLEMENTED, TESTED** — `TaskAnalyzer`, `TaskManager.decompose()` |
| 3 | Determine dependencies and execution order | **PARTIALLY IMPLEMENTED** — dependency graph and BLOCKED/READY gating exist (`TaskManager.add_dependency()`); no ordering/scheduling policy beyond that exists |
| 4 | Select the appropriate role | **IMPLEMENTED, TESTED** — `AgentRouter.route()` |
| 5 | Select the appropriate runtime | **PARTIALLY IMPLEMENTED** — `provider_id` on the selected agent determines the adapter; no policy chooses *between* available runtimes by task characteristics (§D) |
| 6 | Automatically create an agent session when needed | **NOT IMPLEMENTED** — registries start empty; nothing registers an agent on demand |
| 7 | Automatically assign the task | **PARTIALLY IMPLEMENTED** — routing selects an agent; nothing then hands the task to it without a human calling `execute()` |
| 8 | Run sequentially or in parallel when safe | **NOT IMPLEMENTED** — no scheduler; see §F's concurrency rule, enforced only by discipline today |
| 9 | Track progress and results | **IMPLEMENTED, TESTED** — `StateManager`, `execution_results` |
| 10 | Test, review, and accept the result | **NOT IMPLEMENTED** — no Quality Gate (§10.4) |
| 11 | Handle errors per policy | **PARTIALLY IMPLEMENTED** — `fail()`/`InvalidStateTransitionError`/`InterruptedTask` recovery exist; no retry/fallback *policy* exists (see §I) |
| 12 | Report the final result to Founder | **PARTIALLY IMPLEMENTED** — results are queryable from `StateManager`; no automatic Founder-facing report is generated |

**Standing guardrails for this target architecture (DECIDED, apply to
every future milestone that builds toward it):** an agent never expands
its own scope or self-coordinates without bound. The Orchestrator — not
any agent — retains final authority over task routing, retry, budget,
execution permissions, and stop conditions. Founder is never required
to manually assign each task to each agent once this is built, but
Founder approval remains mandatory for anything this Roadmap's
Approval Gate (§10.3) would require once it exists.

---

## H. Token Efficiency — Standing Principles and Mechanism Allocation — **PLANNED, NOT IMPLEMENTED**

**Standing principle, not deprioritized by this revalidation pass:**
optimize total cost to complete a task at acceptable quality — never
call an unnecessary model or agent, never sacrifice quality to pick the
cheapest model, and keep an explicit budget/execution limit. This
principle stays at full priority; what changed in this pass is only
*how carefully* each mechanism is matched to an existing milestone
before being written down as a proposal.

**Allocation result: none of M6/M7/M8's approved scope covers cost or
token optimization**, so almost nothing here is proposed as a scope
extension — forcing it into M6 or M8 anyway would be exactly the
milestone bloat this pass is checking for.

| Mechanism | Allocation status | Relationship / dependency note |
|---|---|---|
| 6.1 Smart Task Routing | **PROPOSED FUTURE WORK — NOT ASSIGNED** | Needs a real runtime-selection policy (§D/§G), which does not exist; no M6/M7/M8 component owns runtime selection |
| 6.2 Context Management | **PROPOSED FUTURE WORK — NOT ASSIGNED** | Needs a context-scoping layer per task/role; no existing component owns this |
| 6.3 Task Deduplication | **DEFERRED** | No evidence of a duplicate-task problem today; lower near-term need than 6.1/6.2/6.6/6.7 |
| 6.4 Result Reuse and Cache | **DEFERRED** | Cache-invalidation-on-dependency-change is nontrivial; no infrastructure to invalidate against exists yet |
| 6.5 Execution Efficiency | **DEFERRED** | A general principle restated as a mechanism; too broad to allocate as a standalone deliverable without first scoping 6.1/6.6 |
| 6.6 Cost Tracking | **PROPOSED FUTURE WORK — NOT ASSIGNED** | Foundational dependency for 6.7; no existing component tracks token/call/time cost |
| 6.7 Budget Governor | **PROPOSED FUTURE WORK — NOT ASSIGNED** | Blocked on 6.6 (Cost Tracking) not existing. Its "require Founder approval to exceed" behavior *would* relate to M6.3 Approval Gate once both exist — noted for awareness, **not** proposed as an M6 extension while its own prerequisite is unmet |

**No PROPOSED SCOPE EXTENSION is made against M6, M7, or M8 for Token
Efficiency.** Recording a mechanism here is not a Founder approval of
any kind; it only prevents the mechanism from being lost or invented
ad hoc later.

---

## I. Loop Prevention — Standing Principles and Mechanism Allocation — **PLANNED, NOT IMPLEMENTED**

**Standing principle, not deprioritized by this revalidation pass:**
bound every retry and workflow iteration; detect no-progress, cyclic
dependencies, and deadlocks; always have a timeout and a safe stop;
never let an agent self-raise its own limit or self-expand its scope.

**Direct-relationship check against M6's four named components (Git
Worktree Manager, Conflict Manager, Approval Gate, Quality Gate)** —
done per-mechanism this pass, replacing the earlier blanket "add all of
Loop Prevention to M6" framing, which risked exactly the milestone
bloat this directive warns against:

| Mechanism | Allocation status | Relationship / dependency note |
|---|---|---|
| 7.1 Retry Control | **PROPOSED FUTURE WORK — NOT ASSIGNED** | No error-classification taxonomy exists yet; not a direct fit for any one of M6's four components today |
| 7.2 No-progress Detection | **PROPOSED FUTURE WORK — NOT ASSIGNED** | A workflow-level heuristic over `Task.history`; no M6 component owns this |
| 7.3 Cycle and Deadlock Detection | **PROPOSED FUTURE WORK — NOT ASSIGNED** | Real, specific gap confirmed this session: `TaskManager.add_dependency()` has no cycle check. Closer in nature to M2's existing `TaskManager` scope than to M6 — **not** proposed as an M6 addition; flagged for Founder awareness only |
| 7.4 Timeout and Concurrency Limits — *timeout* portion | **EXISTING IMPLEMENTATION (partial)** | Adapter-level execution timeouts already exist (`ClaudeCodeAdapter`/`HermesAdapter`'s `timeout_seconds`), re-verified this session |
| 7.4 Timeout and Concurrency Limits — *workflow-level session timeout* portion | **PROPOSED FOR EXISTING MILESTONE (M6) — PROPOSED SCOPE EXTENSION, NOT APPROVED** | Fits alongside M6.1/M6.2's concurrency concern (see 8.3 below); a genuinely new, modest addition, not already covered |
| 7.5 Adaptive Retry | **PROPOSED FUTURE WORK — NOT ASSIGNED** | Depends on 7.1 (Retry Control) existing first; not independently assignable |
| 7.6 Safe Stop — *out-of-authority trigger* | **NOT REQUIRED (separately)** | This is already M6.3 Approval Gate's exact job (§10.3's acceptance criteria already requires blocking an out-of-authority action); recorded here only to confirm coverage, not as new scope |
| 7.6 Safe Stop — *retry/budget/timeout/loop triggers* | **PROPOSED FUTURE WORK — NOT ASSIGNED** | Each depends on a mechanism above (7.1, §H's Budget Governor, 7.4's workflow timeout, 7.2/7.3) that is itself NOT ASSIGNED; cannot be meaningfully scoped ahead of its own dependencies |

**One PROPOSED SCOPE EXTENSION results from this group: the
workflow-level portion of 7.4 (session timeout), paired with 8.3 below.
It is explicitly NOT APPROVED.** M6's approved scope remains exactly
Git Worktree Manager, Conflict Manager, Approval Gate, and Quality
Gate; nothing here enlarges that list of four components, and nothing
here is treated as Founder approval.

---

## J. Reliability and Optimization — Standing Principles and Mechanism Allocation — **PLANNED, NOT IMPLEMENTED**

**M8 dependency check, per instruction** — M8's own approved scope
already names "end-to-end testing, backup, recovery, and final
acceptance." Only mechanisms with a direct, already-named fit to that
scope are proposed there; this pass deliberately does **not** fold
every remaining mechanism into M8.

| Mechanism | Allocation status | Relationship / dependency note |
|---|---|---|
| 8.1 Agent Health Monitoring | **PROPOSED FUTURE WORK — NOT ASSIGNED** | No agent-session lifecycle tracking exists to monitor; not a direct fit for M8's named scope today |
| 8.2 Checkpoint and Recovery — *task-level* | **EXISTING IMPLEMENTATION** | `TaskManager.recover()`/`resolve_interrupted()` (M2), re-verified this session |
| 8.2 Checkpoint and Recovery — *workflow-level* | **PROPOSED FOR EXISTING MILESTONE (M8) — PROPOSED SCOPE EXTENSION, NOT APPROVED** | Direct fit: M8's own approved scope already names "recovery" by name; this makes it concrete rather than provisional |
| 8.3 Concurrency Control — *Git-working-tree conflict portion* | **NOT REQUIRED (separately)** | Already M6.1 (Git Worktree Manager) + M6.2 (Conflict Manager)'s exact job; recorded here only to confirm coverage |
| 8.3 Concurrency Control — *concurrent-session cap / resource-control portion* | **PROPOSED FOR EXISTING MILESTONE (M6) — PROPOSED SCOPE EXTENSION, NOT APPROVED** | A genuinely new, modest addition coordinating with but not identical to M6.1/M6.2; paired with 7.4's workflow-timeout portion above |
| 8.4 Model Fallback | **PROPOSED FUTURE WORK — NOT ASSIGNED** | A runtime-selection-layer concern; `OrchestrationEngine.execute()` already fails closed on an unavailable adapter (verified this session) but does not pick a replacement — no M6/M7/M8 component owns this |
| 8.5 Confidence-based Review | **PROPOSED FOR EXISTING MILESTONE (M6) — PROPOSED SCOPE EXTENSION, NOT APPROVED** | Direct fit: M6.4 Quality Gate's own stated job is judging whether an execution met its acceptance criteria, not trusting a process-exit-0 signal; "never use an agent's self-reported confidence as sole evidence" is a restatement of that same job, not a new component |
| 8.6 Change Impact Analysis | **DEFERRED** | No dependency graph of code (as opposed to tasks) exists to scope a change's impact against; lowest priority |
| 8.7 Idempotency | **PROPOSED FUTURE WORK — NOT ASSIGNED** | General execution-safety property; no single M6/M7/M8 component owns it today |
| 8.8 Audit Trail — *existing coverage* | **EXISTING IMPLEMENTATION (partial)** | `routing_decisions` and `execution_results` tables already record routing and results (M2/M4), re-verified this session |
| 8.8 Audit Trail — *extension to retries/fallback/approval/stop-reasons* | **NOT REQUIRED (separately)** | Already implied by M6.3 Approval Gate's own acceptance criteria ("decisions persisted and auditable"); no second component needed |
| 8.9 Human Approval Gate | **NOT REQUIRED** | This is M6.3 by another name — restating it here would double-count the same, already-approved component |

**Two PROPOSED SCOPE EXTENSIONS result from this group: the
workflow-level portion of 8.2 (proposed against M8), and the
concurrent-session-cap portion of 8.3 (proposed against M6, paired
with 7.4 above). Both are explicitly NOT APPROVED.** M8 is not turned
into a catch-all: 8.1, 8.4, 8.7 remain NOT ASSIGNED rather than folded
into M8 just because M8 is the nearest "reliability-sounding"
milestone, and 8.6 is DEFERRED rather than assigned anywhere.

---

## K. Consolidated Scope Validation — No Milestone Bloat or Overlap

**M6 — unchanged core scope.** Still exactly four components: Git
Worktree Manager, Conflict Manager, Approval Gate, Quality Gate
(§10.1–§10.4). This pass identifies exactly two **PROPOSED SCOPE
EXTENSIONS — NOT APPROVED**: (a) a workflow-level session timeout and
concurrent-session cap (merging 7.4's workflow portion with 8.3's
session-cap portion — one addition, not two, to avoid double-counting);
(b) a restatement of M6.4 Quality Gate's existing acceptance criteria
to explicitly cover 8.5 Confidence-based Review (no new component, no
scope growth — a clarification of what M6.4 already requires). M6 does
not grow to six or seven components; it remains four, plus these two
flagged, unapproved proposals recorded for Founder's decision.

**M7 — untouched.** Still NOT APPROVED. No scope was invented or
inferred for it in this pass. No RTX 3070 Worker Runtime framing was
introduced.

**M8 — unchanged core scope, one proposed extension, not a catch-all.**
Still "end-to-end testing, backup, recovery, and final acceptance,"
still gated on M6 and M7 per §12. This pass identifies exactly one
**PROPOSED SCOPE EXTENSION — NOT APPROVED**: workflow-level checkpoint/
recovery (8.2), because M8's own approved name already covers
"recovery." Agent Health Monitoring (8.1), Model Fallback (8.4), and
Idempotency (8.7) were deliberately **not** folded into M8 despite
sounding "reliability-related" — each lacks a direct textual tie to
M8's own already-approved scope, and forcing them in would be the
bloat this directive asks to check for.

**Full per-mechanism allocation (22 mechanisms, using the five
required states):**

| State | Mechanisms |
|---|---|
| **EXISTING IMPLEMENTATION** | 7.4 (timeout portion, partial); 8.2 (task-level); 8.3 (Git-conflict portion, via M6.1/M6.2); 8.8 (routing/result recording, partial) |
| **PROPOSED FOR EXISTING MILESTONE** (each explicitly **PROPOSED SCOPE EXTENSION — NOT APPROVED**) | 7.4+8.3 workflow/session-cap addition → M6; 8.5 Confidence-based Review → M6.4; 8.2 workflow-level → M8 |
| **PROPOSED FUTURE WORK — NOT ASSIGNED** | 6.1, 6.2, 6.6, 6.7 (Token Efficiency); 7.1, 7.2, 7.3, 7.5, 7.6 (non-authority triggers) (Loop Prevention); 8.1, 8.4, 8.7 (Reliability) |
| **DEFERRED** | 6.3, 6.4, 6.5 (Token Efficiency); 8.6 (Reliability) |
| **NOT REQUIRED** | 7.6 (out-of-authority trigger, = M6.3); 8.3 (Git-conflict portion, = M6.1/M6.2); 8.8 (retry/fallback/approval extension, = M6.3's own criteria); 8.9 (= M6.3) |

**Recording a mechanism in this Roadmap is never Founder approval.**
Every row marked PROPOSED SCOPE EXTENSION or PROPOSED FOR EXISTING
MILESTONE requires its own, separate, explicit Founder decision before
any code is written against it. No milestone order was changed. No new
milestone was created. No mechanism was forced into M6, M7, or M8
where its dependencies or acceptance criteria were not yet concrete
enough to justify it — those stayed NOT ASSIGNED or DEFERRED instead,
exactly as instructed.

---

## 7. Orchestrator 3070 — Orchestrator B — **LOCKED / FROZEN / NOT STARTED / OUT OF SCOPE** (DECIDED)

Absolute rules, in force until Founder personally requests unlocking:
no inspection of its repository or environment; no access to its
machine or systems; no configuration, installation, startup, or
modification; no task or implementation milestone created for it; no
connection designed between Orchestrator A and Orchestrator B; no
research into its internal architecture; no unlocking on any agent's
own initiative.

This document records only that Orchestrator B exists, is independent,
is locked, has not started, and is out of scope. If Founder later
requests unlocking, this Roadmap must be updated and re-approved
*before* any work begins.

Note (context only, **not a question for Founder, not under
investigation**): whether Orchestrator B is the same machine Phase 0
discovery once called "RTX 3070 Desktop" is not known. This is recorded
once, here, as context — not listed as an open question and not to be
investigated.

## 8. RTX 3070 (hardware) — DECIDED

Untouched. Not integrated. **Not a Worker Runtime.** No role in the
current architecture. Never conflated with Orchestrator B (§7) — one
is a GPU, the other is an independent software project.

`git grep` across every tracked file confirms every mention of "RTX
3070" is documentation describing it as untouched, or an illustrative
string label in test fixtures — zero lines of code touch it.

---

## 9. Milestone History — M1 through M6

**Column note (resolves Founder Review Finding 2):** "Automated tests"
means the unit/integration suite only, re-run fresh in this review
session. "Real-world evidence" means a real call against a live CLI or
container; where that call is historical (a prior milestone's smoke
test, not re-executed this session) it is labeled VERIFIED
(historical) rather than folded into the TESTED label — a previous
run's result is never used as evidence for the *current* session's
claim.

| Milestone | Commit(s) | Code | Automated tests | Real-world evidence |
|---|---|---|---|---|
| M1 — Repository Foundation | `f88ef810` | **IMPLEMENTED** | **TESTED** (suite re-run fresh this session, 257/257) | n/a — no real external call in this milestone's scope |
| Hygiene cleanup | `dcfaccb2` | **IMPLEMENTED** | **TESTED** (re-run fresh this session) | n/a |
| M2 — Task Analyzer, Task Manager, State Manager | `938996c2` | **IMPLEMENTED** | **TESTED** (re-run fresh this session) | n/a |
| M3 — Project/Agent/Provider Registry, Agent Router | `596d30d8` | **IMPLEMENTED** | **TESTED** (re-run fresh this session) | n/a |
| M4 — Claude CLI Adapter | `9396e019` | **IMPLEMENTED** | **TESTED** (unit/integration suite, re-run fresh this session) | **VERIFIED (historical)** — real smoke test recorded in `docs/EXECUTION_CLAUDE_CODE.md` at Milestone 4; not re-executed this session (a real paid call, out of scope for a documentation review) |
| M4 remediation | `9b6e0df8` | **IMPLEMENTED** | **TESTED** (re-run fresh this session) | n/a |
| M5 — Hermes Adapter | `848129197ce312407e946589920b984e9d8aa2b4` | **IMPLEMENTED** | **TESTED** (unit/integration suite, re-run fresh this session) | **VERIFIED (historical)** — real smoke test recorded in `docs/EXECUTION_HERMES.md` at Milestone 5; not re-executed this session |
| M6 — Git Worktree Manager, Conflict Manager, Approval Gate, Quality Gate | `b2c723e` .. `751ab3f` (full list in §10 closure record) | **IMPLEMENTED** | **TESTED** (unit/integration suite, re-run fresh — 360/360 pass) | **VERIFIED** — real Claude `WORKTREE_WRITE` direct-tool-use coding, and real Hermes agent coding (create/test/fail/diagnose/fix/retest cycle) inside a disposable per-task sandbox, both pushed through the full governed pipeline (Worktree → Quality Gate → Approval Gate → integration); see §10 closure record |

**VERIFIED today, for this document:** `HEAD == origin/main ==
751ab3ff236443d58916f38ea0048ba6c657ab7c` (pre-closure-commit), working
tree clean; full suite re-run fresh in this session, twice → **360/360
pass** both times, 0 failed, 0 skipped. No scope, history, or commit of
M1–M5 was changed — this table only classifies what was already there,
and the M4/M5 real-smoke-test VERIFIED claims are explicitly scoped to
historical evidence, not this session's activity. M6's row reflects
real-agent acceptance evidence gathered and independently verified in
this and the immediately preceding sessions (not re-executed again
solely for this documentation pass, consistent with the M4/M5
historical-evidence convention above).

---

## 10. M6 — Execution Governance — **IMPLEMENTED, TESTED, VERIFIED — COMPLETE / ACCEPTED / CLOSED (Founder decision)**

Serves **Orchestrator A's own execution safety only.** Orchestrator B
and RTX 3070 hardware are explicitly out of scope for M6 — no component
below may be implemented in a way that touches either. This remained
true throughout M6's implementation and acceptance: nothing in §10
ever touched Orchestrator B or RTX 3070.

**Closure record.** Founder officially accepted M6 (all four
components below, plus real-Agent coding acceptance for both Claude
and Hermes) and authorized this closure. Commits (`b2c723e` 6.1 Git
Worktree Manager .. `a0dbd75` 6.4 Quality Gate .. `751ab3f` final
completion item — full range on `main`, chronological):
`b2c723e, 2754d86, e70a19e, a0dbd75, 412145d, 2e5d510, 08edea9,
c135a21, 9e27123, d87383c, 0e4be3e, 751ab3f`. Real-Agent acceptance
evidence (gathered across this and the immediately preceding
sessions, independently re-verified rather than taken on an agent's
self-report): Claude `WORKTREE_WRITE` mode creates and edits files
directly via its own Read/Write/Edit/Bash tools inside a real,
kernel-enforced sandbox (`bwrap`); Hermes, dispatched through a
disposable per-task container sandbox (no Docker socket, read-only
root filesystem, Worktree-only write access), independently created
source and test files, hit a genuine seeded application-logic test
failure, diagnosed it, and fixed it via its own `patch` tool before a
real retest passed — both adapters' output then went through the same
governed pipeline (Quality Gate, Approval Gate, integration) with no
step bypassed. Regression: full suite re-run fresh at closure, twice,
**360/360 pass, 0 failed, 0 skipped**
(`PYTHONPATH=src python3 -m unittest discover -s tests`). Hermes's
production container and Telegram Bridge were never modified,
restarted, or recreated at any point during this work.

### 10.1 Git Worktree Manager
- **Objective:** give each execution an isolated, disposable copy of a
  project's real files, replacing today's manually-supplied scratch
  directory.
- **Scope:** create/track/clean up a worktree per execution; refuse two
  concurrent executions against the same worktree.
- **Dependencies:** `ProjectProfile.repository_path` (M3); becomes the
  source of `ExecutionRequest.working_directory` (M4/M5).
- **Deliverables:** `WorktreeManager.acquire(project_id, task_id) -> path`, `WorktreeManager.release(path)`.
- **Tests:** creation/cleanup correctness; concurrent-reuse rejection;
  no modification of the project's real checkout.
- **Security requirements:** never operate on a path outside the
  project's own repository; never leave an orphaned worktree registered
  as in-use.
- **Acceptance criteria:** an execution can run against a real
  project's files without ever touching that project's actual
  checkout.
- **Completion evidence required:** passing tests above, plus one real
  worktree create/execute/discard cycle against a non-production test
  repository.

### 10.2 Conflict Manager
- **Objective:** detect overlapping or contradictory changes across
  worktrees before merge.
- **Scope:** diff comparison; flag conflicts for Founder/Approval Gate
  review; never auto-resolve silently.
- **Dependencies:** §10.1.
- **Deliverables:** a diff-comparison routine returning a clean/
  conflicting verdict with the specific overlapping paths.
- **Tests:** synthetic overlapping and non-overlapping diff fixtures;
  no false positive/negative on deterministic cases.
- **Security requirements:** never merges on its own authority.
- **Acceptance criteria:** this is the first point where "Claude and
  Hermes must never modify the same working tree concurrently" becomes
  enforced by code, not merely documented.
- **Completion evidence required:** passing tests above, plus a
  deliberately-induced conflict scenario that is correctly blocked.

### 10.3 Approval Gate
- **Objective:** require an explicit, recorded Founder decision before
  any sensitive action.
- **Scope:** intercept a defined set of sensitive actions (merge,
  destructive git operation, Docker/credential/production access,
  anything Orchestrator-B- or RTX-3070-related); record the decision.
- **Dependencies:** `errors.ApprovalRequiredError` (exists since M1).
- **Deliverables:** `ApprovalGate.require(action, context) -> ApprovalDecision`, raising `ApprovalRequiredError` when unmet.
- **Tests:** blocked-without-approval; proceeds-with-recorded-approval;
  decisions persisted and auditable.
- **Security requirements:** no timeout-based auto-approval, ever.
- **Acceptance criteria:** `approval/gate.py` (currently a 7-line,
  empty placeholder) stops being a placeholder; every sensitive path in
  M4/M5's adapters that currently relies on its own fail-closed default
  routes through this instead.
- **Completion evidence required:** passing tests above, plus a real
  demonstration that an unapproved sensitive action is blocked and an
  approved one proceeds.

### 10.4 Quality Gate
- **Objective:** judge whether a completed execution actually met
  `TaskSpecification.acceptance_criteria` — not merely that the process
  exited 0.
- **Scope:** evaluate `ExecutionResult` against the task's
  specification; may involve a Reviewer-role agent re-checking a
  Coder-role agent's output.
- **Dependencies:** M2's `TaskSpecification`, M4/M5's `ExecutionResult`.
- **Deliverables:** a pass/fail evaluator plus a recorded rationale.
- **Tests:** deterministic pass/fail fixtures; no false "success"
  claims.
- **Security requirements:** a failed quality check must never be
  silently retried (consistent with M4/M5's existing rule against
  auto-retrying an uncertain execution).
- **Acceptance criteria:** this is what finally makes good on M4/M5's
  own repeated disclosure that "`ExecutionResult.success` is a
  process-level signal only... never a claim that the task's actual
  objective was met."
- **Completion evidence required:** passing tests above, plus one real
  task whose execution result is correctly judged against its own
  acceptance criteria.

### 10.5 Cross-cutting constraint: Hardware-Independent Architecture (Founder direction, applies to all four components above — not a fifth component)

Founder's stated direction: Universal-Orchestrator currently runs on a
resource-constrained Intel NUC; Founder intends, in the future, to run
most Agent execution on stronger hardware. The architecture must not
be rewritten to make that move possible — it must already allow it.

**Evidence gathered this session (fresh, not reused from a prior
claim) — this requirement is, to a large extent, already satisfied by
existing design, not a gap M6 needs to close:**

- `config/settings.py`'s `_repo_root()` derives every path from
  `__file__` and environment variables (`UO_DATA_DIR`, `UO_LOG_DIR`,
  `UO_CONFIG_FILE`, ...) — **no hostname, IP, or machine-specific path
  is hardcoded anywhere in this module.**
- `ClaudeCodeAdapter.__init__(executable: str = "claude", ...)` and
  `HermesAdapter.__init__(..., docker_bin: str = "docker", ...)` both
  resolve their target via `PATH` (or an explicit override), never a
  hardcoded absolute path — re-confirmed by direct source read this
  session.
- A repository-wide search for hardcoded hostnames/IPs/resource
  constants in `.py` files found exactly two hits, both in comments,
  neither in executable code: `core/models.py`'s existing
  `AgentDescriptor.machine` docstring (already documented as "a
  logical label only... never a real hostname, IP or path") and
  `config/settings.py`'s docstring explaining *why* the defaults are
  lightweight — not a hardcoded ceiling anywhere a program could trip
  over.
- `AgentDescriptor.machine` (M3) was already a free-text logical label
  — the Router, Task Manager, and State Manager never read or depend
  on its value.

**What genuinely does not exist yet, and why that's a smaller gap than
it sounds:** there is no adapter today that dispatches to a runtime on
a *different* machine over a network — `ClaudeCodeAdapter` runs a
local `subprocess`, `HermesAdapter` runs a local `docker exec`. Both
are concrete implementations of the same abstract `ExecutionAdapter`
interface (`verify()`/`execute()`), and that interface itself carries
no assumption about locality — `OrchestrationEngine`, `AgentRouter`,
`TaskManager`, and the planned Approval/Quality/Conflict Gate
components (§10.1–§10.4) only ever talk to an adapter through this
contract, never to a machine directly. **The practical, zero-new-code
path to Founder's stated goal is simply running this same Orchestrator
process on the stronger machine instead of the NUC** — nothing in
`core/`, `registry/`, or `routing/` binds it to any specific host.
Moving *only* the execution runtime (keeping the Orchestrator itself on
the NUC, reaching a remote Claude/Hermes over the network) would
require a new concrete adapter — out of scope for M6 and explicitly not
authorized here.

**Binding constraint on M6's four components (§10.1–§10.4), stated so
it is not forgotten once implementation starts — not new scope, a
design discipline applied to the scope that already exists:**
- **Git Worktree Manager (§10.1):** worktree paths must be derived from
  `ProjectProfile.repository_path` and the existing `config/settings.py`
  pattern (env-var/config-driven, relative to a resolved root) —
  **never** a path hardcoded to this NUC's filesystem layout.
- **Conflict Manager, Approval Gate, Quality Gate (§10.2–§10.4):** none
  of the three reads or depends on `AgentDescriptor.machine`,
  hostname, or any hardware characteristic — they operate on worktree
  paths, diffs, `TaskSpecification`, and `ExecutionResult`, all of
  which are already machine-agnostic data.
- **Adding a future remote-execution adapter** (not part of M6) must
  remain possible through the existing `ExecutionAdapter`/
  `AdapterRegistry` mechanism alone — without changing
  `OrchestrationEngine`, `TaskManager`, or any of M6's four components.
  This is a property to preserve, not a component to build now.

**This subsection does not create a fifth M6 component, does not start
a distributed-systems effort, and does not approve connecting a second
machine.** It records a design discipline against the four
already-approved components, and documents that most of the stated
goal is already true of the existing codebase.

**Risk, recorded rather than acted on:** these four components are
substantial; bundling them into one milestone risks scope creep.
Flagged as a risk only — not resequenced without Founder approval.

**Scope validation pass (this document's §K):** M6's component list
above is unchanged at exactly four. Two items from §K are proposed as
**scope extensions to M6 — PROPOSED SCOPE EXTENSION, NOT APPROVED**:
a workflow-level session timeout plus concurrent-session cap (merging
§I's 7.4 and §J's 8.3), and a restatement of this section's own M6.4
acceptance criteria to explicitly name Confidence-based Review (§J's
8.5) — the latter adds no new component, only makes existing wording
more explicit. Neither is implemented, neither is approved, and M6's
four-component scope is not otherwise enlarged.

## 11. M7 — **NOT APPROVED.** Scope requires architecture review and explicit Founder approval.

Not labeled RTX 3070 Worker Protocol. Not assumed to be any particular
scope. No connection between Orchestrator A and Orchestrator B is
designed, implied, or dependency-wired here or anywhere in this
document.

**Governance status, recorded by the M1–M7 audit (2026-11-14) — audit
Finding C-10.** Code implementing a Cycle & Deadlock Detector,
scheduler lease fencing primitives, a State-aware Stall Detector, a
Task Scheduler, and a Proposal Recovery CLI was committed under an
"M7 Component A/B/C/D" / "M7 Phase 2" label with no corresponding
entry ever added to this document, and with no Founder-approval
record this audit could locate anywhere in the repository. This is
an **implementation-authorization gap, not a milestone acceptance**:
the code exists and, as of this same audit/remediation pass, its
most serious defects (a bootstrap/recovery race and several lower-
severity gaps) have been found and fixed, with real regression
tests. That is evidence the *code* is now in better shape — it is
**not** a Founder approval of M7's scope, and this note does not
assert one. §11's status above (**M7 — NOT APPROVED**) stands
unchanged until the Founder explicitly decides to approve the scope
as built, require changes to it, or reject/roll it back. No date or
signature of approval is recorded here because none occurred.

## 12. M8 — End-to-End Acceptance — **PLANNED (provisional shape only), NOT APPROVED, NOT IMPLEMENTED**

- **Objective:** final validation that the full M1–M6 pipeline (and
  whatever M7 becomes) works end-to-end.
- **Scope (provisional):** end-to-end testing, integration
  verification, backup, recovery, data integrity, final acceptance.
- **Dependencies:** all of M6, and whatever M7's approved scope turns
  out to be, must be done and approved first.
- **Deliverables, tests, security requirements, acceptance criteria:**
  **cannot be scoped in detail** until those dependencies are satisfied
  — stated here only as a placeholder shape, not a commitment.
- **Must not start** until M6 and M7 are both done and approved.

**Scope validation pass (this document's §K):** M8's provisional scope
above is unchanged. Exactly one item from §J is proposed as a **scope
extension to M8 — PROPOSED SCOPE EXTENSION, NOT APPROVED**:
workflow-level checkpoint/recovery (8.2), because M8's own name already
covers "recovery." M8 is deliberately not expanded with Agent Health
Monitoring, Model Fallback, or Idempotency (§J) — each lacks a direct
tie to M8's named scope and was left NOT ASSIGNED rather than folded
in here, so M8 does not become a catch-all for every remaining
mechanism.

---

## 13. Documents Superseded by This File

- `ARCHITECTURE.md`'s former `## Roadmap` section (an earlier, shorter
  milestone list, including an early "M7 — Worker Protocol and RTX
  3070 integration" framing) — **SUPERSEDED**, preserved in Git
  history.
- `ARCHITECTURE.md`'s former "Candidate future Worker" framing for the
  RTX 3070 Desktop — **SUPERSEDED**.
- `README.md`'s former "future RTX 3070 worker — M7" note —
  **SUPERSEDED**.
- `docs/EXECUTION_HERMES.md`'s former "(M7 scope)" framing — corrected
  in wording only; the underlying technical fact (Hermes runs in a
  Docker container, not on a dedicated machine) is unchanged.

Each is a documentation wording correction only; no source logic, test,
or runtime behavior changed. Diffs are provided to Founder for review
and have not been committed.

## 14. Outstanding Decisions Needing Founder Input

1. Should M6's four components be resequenced into sub-milestones?
   Not decided — flagged as a risk only (§10), per instruction not to
   resequence unilaterally.
2. Real NUC identity remains **UNKNOWN** (carried over since M2) —
   recorded as context, not an active question requiring investigation.

## 15. Complete Milestone Sequence (summary)

1. **M1 — Repository Foundation.** DECIDED, IMPLEMENTED, TESTED (re-run fresh this session).
2. **M2 — Task Analyzer, Task Manager, State Manager.** DECIDED, IMPLEMENTED, TESTED (re-run fresh this session).
3. **M3 — Project Registry, Agent Registry, Provider Registry, Agent Router.** DECIDED, IMPLEMENTED, TESTED (re-run fresh this session).
4. **M4 — Claude CLI Adapter and non-interactive execution verification.** DECIDED, IMPLEMENTED, TESTED (unit/integration, this session); VERIFIED (historical real smoke test, not re-run this session). **Kept, not removed** — see §D.
5. **M5 — Hermes Adapter and execution verification.** DECIDED, IMPLEMENTED, TESTED (unit/integration, this session); VERIFIED (historical real smoke test, not re-run this session). **Kept, not removed** — see §D.
6. **M6 — Git Worktree Manager, Conflict Manager, Approval Gate, Quality Gate.** IMPLEMENTED, TESTED, VERIFIED. **COMPLETE / ACCEPTED / CLOSED** (Founder decision) — see §9, §10. Serves Orchestrator A only.
7. **M7 — NOT APPROVED.** Scope requires architecture review and explicit Founder approval. Code exists in the repository (Components A–D, Phase 2 fencing) with no corresponding approval record — see §11's governance-status note (audit Finding C-10). Implementation existing is not acceptance.
8. **M8 — End-to-end testing, backup, recovery, final acceptance.** PLANNED (provisional shape only), NOT APPROVED, NOT IMPLEMENTED; gated on M6 and M7.

**Orchestrator B ("3070"): LOCKED / FROZEN / NOT STARTED / OUT OF SCOPE.**
**RTX 3070 hardware: untouched, not integrated, not a Worker Runtime.**

---

*This document was prepared for Founder review and, for this M6
closure update, committed and pushed to `origin/main` per Founder's
explicit M6 acceptance decision. No source code, test, configuration,
secret, or runtime data was changed while preparing it.*
