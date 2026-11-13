"""Orchestration Engine.

Ties together the registries, the task-state store, the planner, the
dispatcher and the approval gate. Lands in Milestone 2 once
`core.state` and `registry.*` exist — building it earlier would mean
wiring against components that are not yet real.
"""
