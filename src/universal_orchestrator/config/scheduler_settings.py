"""Milestone 7 Scheduler/Stall-Detector configuration.

Same pattern as `config.settings`: environment-variable overrides
only, with defaults chosen for the actual NUC target (2 physical
cores / 4 threads, ~7 GiB RAM) — never a hardcoded assumption baked
into the Scheduler or Stall Detector's own logic, which take a
`SchedulerSettings` instance as a parameter and never read `os.environ`
themselves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MAX_CONCURRENCY_ENV = "UO_SCHEDULER_MAX_CONCURRENCY"
LEASE_SECONDS_ENV = "UO_SCHEDULER_LEASE_SECONDS"
HEARTBEAT_SECONDS_ENV = "UO_SCHEDULER_HEARTBEAT_SECONDS"
POLL_SECONDS_ENV = "UO_SCHEDULER_POLL_SECONDS"
DRAIN_TIMEOUT_SECONDS_ENV = "UO_SCHEDULER_DRAIN_TIMEOUT_SECONDS"
DISPATCH_TIMEOUT_SECONDS_ENV = "UO_SCHEDULER_DISPATCH_TIMEOUT_SECONDS"
RUNNING_STALL_THRESHOLD_ENV = "UO_STALL_RUNNING_THRESHOLD_SECONDS"
BLOCKED_STALL_THRESHOLD_ENV = "UO_STALL_BLOCKED_THRESHOLD_SECONDS"
QUALITY_CHECK_STALL_THRESHOLD_ENV = "UO_STALL_QUALITY_CHECK_THRESHOLD_SECONDS"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass(slots=True)
class SchedulerSettings:
    # Default of 1 is deliberate (ROADMAP.md / M7 Blueprint): never
    # raised without real adapter resource measurements justifying it.
    max_concurrency: int = 1
    lease_seconds: float = 30.0
    heartbeat_seconds: float = 10.0
    poll_seconds: float = 2.0
    drain_timeout_seconds: float = 120.0
    # Audit Finding C-4: distinct from drain_timeout_seconds (how long
    # *shutdown* waits for workers already running) -- this is how
    # long a single dispatched Agent execution itself is allowed to
    # run before GovernedPipeline.dispatch()/the adapter forcibly
    # terminates it. Default matches the prior hardcoded value
    # exactly, so existing deployments see no behavior change unless
    # they opt into a longer one.
    dispatch_timeout_seconds: float = 60.0
    running_stall_threshold_seconds: float = 1800.0
    blocked_stall_threshold_seconds: float = 3600.0
    quality_check_stall_threshold_seconds: float = 900.0


def load_scheduler_settings() -> SchedulerSettings:
    return SchedulerSettings(
        max_concurrency=_int_env(MAX_CONCURRENCY_ENV, 1),
        lease_seconds=_float_env(LEASE_SECONDS_ENV, 30.0),
        heartbeat_seconds=_float_env(HEARTBEAT_SECONDS_ENV, 10.0),
        poll_seconds=_float_env(POLL_SECONDS_ENV, 2.0),
        drain_timeout_seconds=_float_env(DRAIN_TIMEOUT_SECONDS_ENV, 120.0),
        dispatch_timeout_seconds=_float_env(DISPATCH_TIMEOUT_SECONDS_ENV, 60.0),
        running_stall_threshold_seconds=_float_env(RUNNING_STALL_THRESHOLD_ENV, 1800.0),
        blocked_stall_threshold_seconds=_float_env(BLOCKED_STALL_THRESHOLD_ENV, 3600.0),
        quality_check_stall_threshold_seconds=_float_env(
            QUALITY_CHECK_STALL_THRESHOLD_ENV, 900.0
        ),
    )
