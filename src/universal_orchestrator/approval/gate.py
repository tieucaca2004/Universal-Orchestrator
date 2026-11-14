"""Superseded Milestone-1 placeholder — never implemented here.

Audit Finding C-11/16-C (2026-11-14): this file's prior docstring
claimed the Approval Gate was "implemented in Milestone 4," which was
never true, and referenced "the RTX 3070 Desktop," which this
codebase never connects to. The real, implemented Approval Gate is
`execution.approval_manager.ApprovalGate` (Milestone 6.3) — this
module has no code, is not imported by anything outside
`tests/test_interfaces.py` (which only asserts it "imports cleanly"
as a known-placeholder), and is not on any real execution path. Kept
rather than deleted so the placeholder/real-implementation split is
visible to a future reader, instead of silently disappearing.
"""
