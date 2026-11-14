"""Task Scheduler CLI — Milestone 7, Component C.

    python3 -m universal_orchestrator.cli.scheduler_cli run \\
        --projects-config projects.json --adapter claude

`projects.json` is the same `{"projects": [...]}` shape
`registry.projects.load_from_config` already reads (Milestone 3,
unchanged) — every task's `project_id` must resolve to one entry in
it, or that task is logged and skipped rather than crashing the whole
scheduling loop.

SIGINT/SIGTERM both request a graceful shutdown (`Scheduler.stop()`):
stop claiming new work, wait up to `--drain-timeout` for in-flight
workers, release the lease only if every one of them actually
finished. See `core.scheduler.Scheduler.shutdown`'s own docstring for
why a stuck worker is never force-killed and never silently assumed
finished.
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from universal_orchestrator.config.scheduler_settings import load_scheduler_settings
from universal_orchestrator.config.settings import load_settings
from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.governed_pipeline import GovernedPipeline
from universal_orchestrator.core.scheduler import Scheduler
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.execution.adapter import ExecutionAdapter
from universal_orchestrator.execution.approval_manager import ApprovalGate
from universal_orchestrator.execution.claude_code import ClaudeCodeAdapter
from universal_orchestrator.execution.conflict_manager import ConflictManager
from universal_orchestrator.execution.hermes import HermesAdapter
from universal_orchestrator.execution.hermes_transfer import HermesWorkspaceTransfer
from universal_orchestrator.execution.quality_manager import QualityGate
from universal_orchestrator.execution.registry import AdapterRegistry
from universal_orchestrator.execution.worktree_manager import WorktreeManager
from universal_orchestrator.registry.agents import AgentRegistry
from universal_orchestrator.registry.projects import ProjectRegistry, load_from_config
from universal_orchestrator.core.models import AgentDescriptor, AgentStatus, ProviderDescriptor, ProviderStatus
from universal_orchestrator.registry.providers import ProviderRegistry
from universal_orchestrator.routing.router import AgentRouter


def _build_adapter(kind: str, target: str) -> ExecutionAdapter:
    if kind == "claude":
        return ClaudeCodeAdapter(executable=target or "claude")
    if kind == "hermes":
        return HermesAdapter(container=target or "hermes")
    raise SystemExit(f"Unknown --adapter '{kind}' (expected 'claude' or 'hermes').")


def _build_pipeline(state: StateManager, adapter_kind: str, adapter_target: str):
    agents = AgentRegistry()
    providers = ProviderRegistry()
    adapters = AdapterRegistry()
    adapter = _build_adapter(adapter_kind, adapter_target)

    provider = providers.register(ProviderDescriptor(name=adapter_kind, kind="cli"))
    providers.set_verified(provider.provider_id, True)
    providers.set_status(provider.provider_id, ProviderStatus.AVAILABLE)
    adapters.register(provider.provider_id, adapter)

    agent = agents.register(
        AgentDescriptor(name=f"{adapter_kind}-coder", role="coder", provider_id=provider.provider_id)
    )
    agents.set_status(agent.agent_id, AgentStatus.AVAILABLE)

    router = AgentRouter(agents, providers)
    engine = OrchestrationEngine.bootstrap(
        state, router=router, adapters=adapters, agents=agents, providers=providers
    )
    worktrees = WorktreeManager(state)
    conflicts = ConflictManager(state, worktrees)
    approvals = ApprovalGate(state)
    quality = QualityGate(state)
    pipeline = GovernedPipeline(engine, worktrees, conflicts, approvals, quality)
    return engine, pipeline


def _open_state(db_path: str | None) -> StateManager:
    if db_path is not None:
        return StateManager(Path(db_path))
    return StateManager(load_settings().state_db_path)


def cmd_run(args: argparse.Namespace) -> int:
    state = _open_state(args.db)
    try:
        engine, pipeline = _build_pipeline(state, args.adapter, args.adapter_target)
        projects = ProjectRegistry()
        for profile in load_from_config(Path(args.projects_config)):
            projects.register(profile)

        settings = load_scheduler_settings()
        if args.max_concurrency is not None:
            settings.max_concurrency = args.max_concurrency
        if args.drain_timeout is not None:
            settings.drain_timeout_seconds = args.drain_timeout

        workspace_transfer = None
        if args.adapter == "hermes":
            workspace_transfer = HermesWorkspaceTransfer(container=args.adapter_target or "hermes")

        scheduler = Scheduler(
            state, engine, pipeline, projects,
            settings=settings, workspace_transfer=workspace_transfer,
        )

        def _request_stop(signum, _frame):
            print(f"Received signal {signum}; requesting graceful shutdown...", file=sys.stderr)
            scheduler.stop()

        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)

        print(f"HOLDER_ID={scheduler.holder_id}")
        print(f"MAX_CONCURRENCY={settings.max_concurrency}")
        scheduler.run_forever()
        print("Scheduler stopped.")
        return 0
    finally:
        state.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scheduler_cli", description="The M7 long-running Task Scheduler."
    )
    parser.add_argument("--db", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the Scheduler until stopped (SIGINT/SIGTERM).")
    run.add_argument("--projects-config", required=True, help="Path to a projects.json (registry.projects format).")
    run.add_argument("--adapter", choices=["claude", "hermes"], default="claude")
    run.add_argument("--adapter-target", default="")
    run.add_argument("--max-concurrency", type=int, default=None, help="Overrides UO_SCHEDULER_MAX_CONCURRENCY.")
    run.add_argument("--drain-timeout", type=float, default=None, help="Seconds; overrides UO_SCHEDULER_DRAIN_TIMEOUT_SECONDS.")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
