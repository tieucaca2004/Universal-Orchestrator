# Agent Router

`routing/router.py` — decides **who** should perform a task. It never
executes anything: no Claude CLI call, no Hermes call, no Git worktree,
no network contact with a machine, no provider call. It only reads the
`AgentRegistry` and `ProviderRegistry` and returns a `RoutingDecision`.

## Inputs the router reads — nothing is guessed

| Source | Field | Used for |
|---|---|---|
| `Task.specification.required_capabilities` | `list[str]` | Capability match |
| `Task.payload["required_role"]` | `str` | Role match |
| `Task.payload["required_permissions"]` | `list[str]` | Permission match |
| `Task.payload["required_machine"]` | `str` | Machine/resource match |
| `Task.payload["max_cost_per_1k_tokens"]` | `float` | Cost constraint |
| `ProjectProfile.restricted_operations` | `list[str]` | Project permission boundary |

Every one of these is optional; an absent key simply means that
dimension imposes no constraint. None of them are inferred from task
text — only from explicit, caller-supplied structured fields.

## Algorithm

For every registered agent, sorted by `agent_id` (for determinism), in
order:

1. **Status.** Must be exactly `AgentStatus.AVAILABLE`. Everything else
   — `REGISTERED`, `BUSY`, `OFFLINE`, `UNVERIFIED`, `DISABLED`, `ERROR`,
   or any value this enum doesn't even have — is rejected. Unknown is
   never available.
2. **Role**, if `required_role` was given: `agent.role` must match.
3. **Capabilities**: `required_capabilities` must be a subset of
   `agent.capabilities`.
4. **Permissions**: `required_permissions` must be a subset of
   `agent.permissions`, **and** must not intersect
   `project_profile.restricted_operations`.
5. **Machine**, if `required_machine` was given: `agent.machine` must
   match exactly.
6. **Provider compatibility**, only if the agent has a `provider_id`
   (an agent with none is not disqualified on that basis — capabilities
   belong to the agent, not its provider):
   - the provider must be registered (`ProviderRegistry.get` must not
     raise `NotFoundError`),
   - `provider.verified` must be `True`,
   - `provider.status` must be `AVAILABLE`,
   - **cost**, only when `max_cost_per_1k_tokens` was given *and*
     `provider.cost.confidence is CostConfidence.VERIFIED`: reject if
     `provider.cost.input_per_1k_tokens > max_cost_per_1k_tokens`.
     `ESTIMATED` or `UNKNOWN` cost data never blocks routing — it isn't
     reliable enough to reject anyone on.

Any agent that fails one or more checks is recorded in
`RoutingDecision.rejected` as a `RejectedCandidate(agent_id, reasons)`
with every reason that applied, not just the first. Agents that pass all
checks are scored (next section); the highest-scoring one is selected,
ties broken by the lower `agent_id` (string comparison) — this is what
makes routing deterministic for identical registry state.

If no agent passes every check, the router returns
`RoutingOutcome.NO_ELIGIBLE_AGENT` with every candidate's rejection
reasons still attached. It never silently settles for an incompatible
agent, and it never falls back to a "best effort" pick.

## Scoring (the only formula this milestone defines)

```
score = len(matched_capabilities) / len(required_capabilities)
```

`1.0` when no capabilities were required at all. This is deliberately
the simplest formula that satisfies "rank by capability match" — task
priority and cost are not folded into the score (see "Known
limitations" below).

## RoutingDecision fields

`task_id`, `project_id`, `outcome` (`SELECTED` /
`NO_ELIGIBLE_AGENT`), `explanation` (human-readable), `decision_id`,
`selected_agent_id`, `selected_provider_id`, `matched_capabilities`,
`rejected` (list of `RejectedCandidate`), `score`, `decided_at`.

## Persistence

The router itself never touches storage. `OrchestrationEngine.route()`
calls `router.route(...)` and then
`TaskManager.record_routing_decision(decision)` — the only path a
decision reaches `StateManager.save_routing_decision`, which writes to
its own `routing_decisions` table (see `docs/TASK_LIFECYCLE.md`'s
sibling schema in `core/state.py`). A routing decision never touches the
`tasks` row or its `history` — tested explicitly in
`test_state_manager.RoutingDecisionPersistenceTests`.

Routing does **not** change `Task.status`. `READY` stays `READY` whether
or not a routing decision has been made — dispatch (a later milestone)
is what would actually act on a decision.

## Known limitations

- **Task priority** is read nowhere in this milestone's routing logic.
  The brief asks the router to "evaluate task priority"; with a single
  `route()` call per task and no queue to reorder, there is nothing
  for priority to affect yet. It becomes meaningful once a Dispatcher
  (M4/M5) has multiple ready tasks competing for the same busy agent.
  Recording this as a limitation rather than fabricating a
  priority-weighted score with nothing real behind it.
- **No preemption.** A `BUSY` agent is simply ineligible; the router
  does not bump a lower-priority running task.
- **No multi-agent plans.** One decision selects one agent for one task;
  composing several agents for one task is Agent Router scope for a
  later milestone, not this one.
