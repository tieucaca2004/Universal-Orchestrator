# Task Lifecycle

Enforced in code by `core/lifecycle.py` (`ALLOWED_TRANSITIONS`,
`validate_transition`) and driven only by `core/task_manager.py`. No other
module may write `Task.status` directly.

## States

| Status | Meaning | Terminal? |
|---|---|---|
| `CREATED` | Task exists; not yet analyzed. | No |
| `ANALYZED` | Task Analyzer has attached a `TaskSpecification`. | No |
| `PLANNED` | An explicit planning step has run (no planner exists yet in Milestone 2 — this is reachable only via `TaskManager.mark_planned`, for forward compatibility). | No |
| `READY` | Dependencies (if any) are all `SUCCEEDED`; a future dispatcher may pick the task up. | No |
| `BLOCKED` | At least one declared dependency has not `SUCCEEDED` yet. | No |
| `RUNNING` | Execution has started (`TaskManager.start`). Milestone 2 has no executor — this is bookkeeping only. | No |
| `SUCCEEDED` | Completed successfully; `Task.result` set. | **Yes** |
| `FAILED` | Completed unsuccessfully; `Task.error` set. | **Yes** |
| `CANCELLED` | Cancelled by an authorized action, from any non-terminal state. | **Yes** |
| `INTERRUPTED` | Was `RUNNING` when the process restarted; outcome unknown until resolved. | No |

## Transition diagram

```
CREATED ──────────────► ANALYZED ──────────────► PLANNED
   │                        │  │                     │  │
   │                        │  └──────► BLOCKED ◄─────┘  │
   │                        │              │ ▲           │
   │                        └──────► READY ◄─┘           │
   │                                   │                  │
   │                                   ▼                  │
   │                                RUNNING                │
   │                                 │  │  │                │
   │                                 │  │  └──► INTERRUPTED │
   │                                 │  ▼         │  │  │   │
   │                                 │ FAILED      │  │  │   │
   │                                 ▼             │  │  │   │
   │                             SUCCEEDED ◄────────┘  │  │
   │                                                 FAILED  │
   │                                                    │    │
   └──────────────────────────────────────────────► CANCELLED
          (from CREATED, ANALYZED, PLANNED, BLOCKED, READY, RUNNING)
```

`INTERRUPTED → SUCCEEDED` only happens through
`TaskManager.resolve_interrupted(..., CONFIRMED_SUCCEEDED, evidence=...)`
with non-empty `evidence` — never automatically.

## Dependency rules

- A task's structural dependencies (`Task.depends_on`) are declared
  explicitly via `TaskManager.add_dependency(task_id, depends_on_id)`,
  before the task leaves `CREATED`/`ANALYZED`/`PLANNED`/`BLOCKED`. Adding a
  dependency once a task is `READY` or later raises `OrchestratorError`.
- Readiness is (re-)evaluated whenever a task is analyzed, planned, or
  one of its dependencies reaches `SUCCEEDED` (cascade from
  `TaskManager.succeed` / `resolve_interrupted(..., CONFIRMED_SUCCEEDED)`).
- A task with *any* unmet dependency is `BLOCKED`, never `READY` —
  "Task C depends on A and B" only becomes `READY` once **both** A and B
  are `SUCCEEDED`.

## Recovery behavior

- `TaskManager.recover()` reloads every task from the `StateManager` and
  reclassifies anything still `RUNNING` as `INTERRUPTED` — the process
  that was running it is gone, so its outcome is unknown. Nothing is
  marked `SUCCEEDED` or `FAILED` automatically.
- Resolving an `INTERRUPTED` task is a separate, explicit operation
  (`TaskManager.resolve_interrupted`) with three outcomes:
  - `RETRY` → `READY` (never straight back to `RUNNING` — a future
    dispatcher decides when and how to actually re-run it).
  - `FAILED` → `FAILED`, with a recorded `TaskError`.
  - `CONFIRMED_SUCCEEDED` → `SUCCEEDED`, but only with non-empty
    `evidence`; without it the call raises `ValueError`.
- There is no retry *count* or backoff policy in Milestone 2 — each
  `resolve_interrupted` call is a one-time, explicit decision. Retry
  boundaries (how many times, how far apart) are an Agent Router /
  Dispatcher concern for a later milestone, once something actually
  executes tasks.

## Invalid transitions

Anything not in `core/lifecycle.ALLOWED_TRANSITIONS` — for example
`CREATED → RUNNING`, `READY → SUCCEEDED`, or any transition away from a
terminal state — raises `errors.InvalidStateTransitionError` and leaves
the task unchanged.
