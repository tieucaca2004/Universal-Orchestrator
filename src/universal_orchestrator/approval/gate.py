"""Approval Gate — implemented in Milestone 4.

Must sit in front of any action touching existing Docker infrastructure,
existing project repositories, credentials/secrets, production systems,
or the RTX 3070 Desktop, and raise `errors.ApprovalRequiredError` unless
Founder approval for that specific action has been recorded.
"""
