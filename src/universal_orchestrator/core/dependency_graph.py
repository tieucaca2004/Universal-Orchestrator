"""Cycle & Deadlock Detector — Milestone 7, Component A.

Operates purely on the structural dependency graph `TaskManager` already
owns (`Task.depends_on`) — never inspects agents, providers, or
execution state. Two jobs, kept deliberately separate:

  - prevention: `would_create_cycle()` is called by
    `TaskManager.add_dependency` *before* an edge is persisted, so a
    cycle can never actually enter the graph through the one API that
    creates edges.
  - diagnosis: `find_cycles()` and `diagnose_blocked()` are read-only
    checks a caller (the Stall Detector, a CLI, a test) can run at any
    time against whatever graph currently exists — defense in depth,
    and the actionable detail behind a BLOCKED task.

Neither function inspects `Task.status` to decide whether a *cycle*
exists — a cycle is a structural fact about `depends_on` edges,
independent of what state its tasks happen to be in. Status only
matters for `diagnose_blocked()`, which must tell "still legitimately
waiting on something that will finish" apart from "can never finish."
"""

from __future__ import annotations

from dataclasses import dataclass, field

from universal_orchestrator.core.models import Task, TaskStatus


def would_create_cycle(
    tasks: dict[str, Task], task_id: str, depends_on_id: str
) -> tuple[str, ...] | None:
    """Returns the cycle chain that adding the edge `task_id ->
    depends_on_id` would close, or `None` if adding it is safe. A task
    depending on itself is the length-1 case, reported as
    `(task_id, task_id)`. Walks forward from `depends_on_id` along
    existing `depends_on` edges looking for `task_id` — if found,
    `depends_on_id` already (transitively) depends on `task_id`, so
    the new edge would close a loop."""
    if task_id == depends_on_id:
        return (task_id, depends_on_id)

    visited: set[str] = set()
    stack: list[tuple[str, tuple[str, ...]]] = [(depends_on_id, (depends_on_id,))]
    while stack:
        current, path = stack.pop()
        if current == task_id:
            return (task_id, *path)
        if current in visited:
            continue
        visited.add(current)
        current_task = tasks.get(current)
        if current_task is None:
            continue
        for dep in current_task.depends_on:
            stack.append((dep, (*path, dep)))
    return None


def find_cycles(tasks: dict[str, Task]) -> list[tuple[str, ...]]:
    """Full-graph scan for any cycle currently present in `depends_on`
    edges. A graph built exclusively through `TaskManager.add_dependency`
    should always yield `[]` here — this exists as a defense-in-depth
    check, independent of that prevention, over whatever graph actually
    exists right now. Each distinct cycle is reported once; rotations
    of the same cycle (e.g. `(A, B, C, A)` vs `(B, C, A, B)`) are
    deduplicated."""
    color: dict[str, int] = {}  # 0 = unseen, 1 = on the current path, 2 = fully explored
    found: list[tuple[str, ...]] = []

    def dfs(node: str, path: list[str]) -> None:
        color[node] = 1
        path.append(node)
        task = tasks.get(node)
        for dep in task.depends_on if task is not None else ():
            if dep not in tasks:
                continue
            state = color.get(dep, 0)
            if state == 1:
                idx = path.index(dep)
                found.append(tuple(path[idx:]) + (dep,))
            elif state == 0:
                dfs(dep, path)
        path.pop()
        color[node] = 2

    for task_id in tasks:
        if color.get(task_id, 0) == 0:
            dfs(task_id, [])

    return _dedupe_cycles(found)


def _dedupe_cycles(cycles: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    seen: set[frozenset[str]] = set()
    unique: list[tuple[str, ...]] = []
    for cycle in cycles:
        key = frozenset(cycle)
        if key in seen:
            continue
        seen.add(key)
        unique.append(cycle)
    return unique


@dataclass(slots=True)
class BlockedDiagnosis:
    """Actionable detail for one BLOCKED task: which dependencies are
    actually holding it back right now, the full transitive chain, and
    whether any part of that chain is a cycle — which would mean the
    task is not merely waiting, but permanently deadlocked and will
    never become READY on its own, no matter how long it waits."""

    task_id: str
    unmet_direct: tuple[str, ...] = field(default_factory=tuple)
    unmet_chain: tuple[str, ...] = field(default_factory=tuple)
    cycle: tuple[str, ...] | None = None

    @property
    def deadlocked(self) -> bool:
        return self.cycle is not None


def diagnose_blocked(tasks: dict[str, Task], task_id: str) -> BlockedDiagnosis:
    """Walks `depends_on` outward from `task_id`, following only
    dependencies that have not reached SUCCEEDED, and reports the full
    chain plus whether `task_id` participates in a structural cycle. A
    dependency id missing from `tasks` entirely is included in the
    chain (it can never succeed) but is not walked further."""
    root = tasks.get(task_id)
    if root is None:
        return BlockedDiagnosis(task_id=task_id)

    def _unmet(dep_id: str) -> bool:
        return dep_id not in tasks or tasks[dep_id].status is not TaskStatus.SUCCEEDED

    direct_unmet = tuple(dep_id for dep_id in root.depends_on if _unmet(dep_id))

    chain: list[str] = []
    visited: set[str] = {task_id}
    queue: list[str] = list(direct_unmet)
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        chain.append(current)
        current_task = tasks.get(current)
        if current_task is None:
            continue
        for dep_id in current_task.depends_on:
            if _unmet(dep_id):
                queue.append(dep_id)

    cycle = next((c for c in find_cycles(tasks) if task_id in c), None)

    return BlockedDiagnosis(
        task_id=task_id,
        unmet_direct=direct_unmet,
        unmet_chain=tuple(chain),
        cycle=cycle,
    )
