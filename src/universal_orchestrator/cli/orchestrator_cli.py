"""The real Orchestrator entrypoint — M6 remediation (round 2), Item 3.

Before this module existed, there was no application entrypoint at
all for submitting a task; every exercise of the M6 pipeline was
either a unit test or a one-off script. This is the genuine,
`python3 -m`-invocable command the application actually uses to go
from a Founder's request to a real, governed integration:

    python3 -m universal_orchestrator.cli.orchestrator_cli submit ...
    python3 -m universal_orchestrator.cli.orchestrator_cli extract-claude-files ...
    python3 -m universal_orchestrator.cli.orchestrator_cli propose ...
    python3 -m universal_orchestrator.cli.orchestrator_cli integrate ...

`submit` is the line that matters for Item 3: it builds a real
`GovernedPipeline` and calls `pipeline.dispatch(...)` — the exact same
class and method every unit test already exercised, now reachable
from a real command a Founder would actually type, not only from
`tests/`. `propose` and `integrate` call `pipeline.evaluate_and_propose`
and `pipeline.integrate_after_approval` the same way, each as its own
process invocation against the same SQLite state — the realistic
shape of a human using this across time (submit now, review evidence
later, approve via `approval_cli`, integrate after that).

`extract-claude-files` exists only because `ClaudeCodeAdapter` and
`HermesAdapter` both enforce zero-tool, read-only execution by design
(unchanged since Milestones 4/5 — not weakened here, as explicitly
forbidden): neither adapter can write a file through its own tool use.
To let a real coding task still produce a real, non-empty Git diff
without touching that security restriction, this command reads the
Agent's own real text reply — `ExecutionResult.structured_output
["result"]` when the adapter has a JSON mode (Claude CLI), falling
back to raw `stdout` when it does not (Hermes has no JSON mode at
all) — and writes exactly that text, unmodified, into real files in
the task's Worktree, then commits it as a second, clearly-separate
commit. Works identically for either adapter's real reply; the
Agent's own tool-use permissions are never loosened for either one —
only the orchestration layer, which already fully controls this
Worktree, performs the save.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from universal_orchestrator.audit.logger import get_logger
from universal_orchestrator.config.settings import load_settings
from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.governed_pipeline import GovernedPipeline
from universal_orchestrator.core.models import (
    AgentDescriptor,
    AgentStatus,
    ProjectProfile,
    ProviderDescriptor,
    ProviderStatus,
    TaskRequest,
)
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.execution.adapter import ExecutionPolicy, PermissionMode
from universal_orchestrator.execution.approval_manager import ApprovalGate
from universal_orchestrator.execution.claude_code import ClaudeCodeAdapter
from universal_orchestrator.execution.conflict_manager import ConflictManager
from universal_orchestrator.execution.hermes import HermesAdapter
from universal_orchestrator.execution.hermes_transfer import HermesWorkspaceTransfer
from universal_orchestrator.execution.quality_manager import QualityGate
from universal_orchestrator.execution.registry import AdapterRegistry
from universal_orchestrator.execution.worktree_manager import WorktreeManager
from universal_orchestrator.registry.agents import AgentRegistry
from universal_orchestrator.registry.providers import ProviderRegistry
from universal_orchestrator.routing.router import AgentRouter

_ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")

_FILE_BLOCK_RE = re.compile(
    r"=== FILE: (?P<name>[^\s=]+) ===\n(?P<content>.*?)(?=\n=== FILE: |\Z)", re.DOTALL
)


class PathTraversalError(ValueError):
    """Raised by `_safe_worktree_target` — an Agent-generated
    '=== FILE: <name> ===' name would write outside its own
    Worktree (Audit Finding C-2)."""


def _safe_worktree_target(worktree_root: Path, relative_name: str) -> Path:
    """Resolves an Agent-generated file name to a real location
    strictly inside `worktree_root`, or raises `PathTraversalError`.

    The Agent's own text reply is untrusted input: this rejects an
    empty name, an absolute path, any literal `.`/`..` component
    (Windows-style `\\` separators are normalized to `/` first, so a
    traversal attempt spelled either way is caught identically), and
    — by resolving the full candidate path and checking containment
    against the Worktree's own resolved real path — any symlink
    anywhere along the way, existing or not, that would otherwise
    redirect the write outside the Worktree. Never decodes percent-
    encoding or any other escaping scheme, so an encoded traversal
    attempt (e.g. '%2e%2e') is just a literal, harmless file name,
    not a bypass."""
    name = relative_name.strip()
    if not name:
        raise PathTraversalError("empty file name")

    candidate = Path(name.replace("\\", "/"))
    if candidate.is_absolute():
        raise PathTraversalError(f"absolute path not allowed: {relative_name!r}")

    parts = candidate.parts
    if not parts or any(part in ("..", ".", "") for part in parts):
        raise PathTraversalError(f"path traversal not allowed: {relative_name!r}")

    root = worktree_root.resolve(strict=True)
    target = root.joinpath(*parts)
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise PathTraversalError(f"escapes the Worktree: {relative_name!r}")
    return target


def _write_agent_file(target: Path, content: str) -> None:
    """Writes `content` to `target`, refusing to follow a symlink at
    the final path component (`O_NOFOLLOW`) — an Agent-controlled
    name must never cause a write through a symlink planted earlier
    in the Worktree, whether by a prior extraction or by committed
    Agent content. `_safe_worktree_target` has already confirmed
    `target` itself resolves inside the Worktree; this additionally
    refuses to overwrite a pre-existing symlink even if that symlink
    happens to point back inside the Worktree."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)


def _open_state(db_path: str) -> StateManager:
    return StateManager(Path(db_path)) if db_path else StateManager(load_settings().state_db_path)


def _build_pipeline_full(
    state: StateManager, adapter_kind: str, adapter_target: str
) -> tuple[OrchestrationEngine, GovernedPipeline, str]:
    """Full stack, including a real adapter — used by `submit`, which
    is the one subcommand that actually dispatches an Agent."""
    agents = AgentRegistry()
    providers = ProviderRegistry()
    adapters = AdapterRegistry()

    if adapter_kind == "claude":
        adapter = ClaudeCodeAdapter(executable=adapter_target or "claude")
    elif adapter_kind == "hermes":
        adapter = HermesAdapter(container=adapter_target or "hermes")
    else:
        raise SystemExit(f"Unknown --adapter '{adapter_kind}' (expected 'claude' or 'hermes').")

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
    pipeline = _wrap(engine, state)
    return engine, pipeline, agent.agent_id


def _build_pipeline_light(state: StateManager) -> tuple[OrchestrationEngine, GovernedPipeline]:
    """No adapters/router — enough for `propose`/`integrate`, which
    never dispatch an Agent, only evaluate and integrate what `submit`
    already produced."""
    engine = OrchestrationEngine.bootstrap(state)
    return engine, _wrap(engine, state)


def _wrap(engine: OrchestrationEngine, state: StateManager) -> GovernedPipeline:
    worktrees = WorktreeManager(state)
    conflicts = ConflictManager(state, worktrees)
    approvals = ApprovalGate(state)
    quality = QualityGate(state)
    return GovernedPipeline(engine, worktrees, conflicts, approvals, quality)


def cmd_submit(args: argparse.Namespace) -> int:
    state = _open_state(args.db)
    try:
        engine, pipeline, agent_id = _build_pipeline_full(state, args.adapter, args.adapter_target)
        project = ProjectProfile(
            name=args.project_name, repository_path=args.repo, project_id=args.project_id
        )
        task = engine.submit(
            TaskRequest(title=args.title, metadata={"acceptance_criteria": args.criterion}), project
        )
        task_obj = engine.manager.get(task.task_id)
        task_obj.payload["required_role"] = "coder"
        engine.route(task.task_id, project)

        policy = None
        if args.permission_mode == "worktree_write":
            policy = ExecutionPolicy(permission_mode=PermissionMode.WORKTREE_WRITE)

        # Hermes runs in a container with no bind mount for a project
        # workspace (docs/EXECUTION_HERMES.md) — the host Worktree path
        # means nothing inside it. The transfer makes the Worktree
        # reachable via controlled copy-in/copy-out instead of ever
        # reconfiguring the live container. Claude (and any adapter
        # that runs natively on this host) needs no transfer at all.
        workspace_transfer = None
        if args.adapter == "hermes":
            workspace_transfer = HermesWorkspaceTransfer(container=args.adapter_target or "hermes")

        print(f"TASK_ID={task.task_id}")
        print(
            f"Dispatching real {args.adapter} Agent (agent_id={agent_id}, "
            f"permission_mode={args.permission_mode}) ..."
        )
        result = pipeline.dispatch(
            task.task_id, project, input_context=args.input_context,
            timeout_seconds=args.timeout, policy=policy, workspace_transfer=workspace_transfer,
        )
        print(f"EXECUTION_OUTCOME={result.outcome.value}")
        print(f"EXECUTION_ID={result.execution_id}")
        print(f"STDOUT_LENGTH={len(result.stdout)}")
        print(f"WORKTREE_PATH={pipeline.worktrees.get(task.task_id).path}")
        return 0
    finally:
        state.close()


def cmd_extract_claude_files(args: argparse.Namespace) -> int:
    """Reads the real Agent's own text reply and writes exactly that
    text into real files — because neither adapter can write files
    through its own (deliberately zero-tool) execution. Expects the
    reply to contain one or more blocks of the form:

        === FILE: <relative/path> ===
        <exact file content>

    Any reply not matching this format extracts zero files — this
    command never invents file content itself."""
    state = _open_state(args.db)
    try:
        engine, _ = _build_pipeline_light(state)
        results = engine.manager.get_execution_results(args.task_id)
        if not results:
            print(f"No ExecutionResult recorded for task '{args.task_id}'.")
            return 1
        result = results[-1]
        text = None
        if isinstance(result.structured_output, dict):
            text = result.structured_output.get("result")
        if text is None:
            text = result.stdout
        if not isinstance(text, str):
            print("Real Agent reply has no extractable text 'result'. Zero files written.")
            return 1

        matches = list(_FILE_BLOCK_RE.finditer(text))
        if not matches:
            print("Real Agent reply did not contain any '=== FILE: <name> ===' block. Zero files written.")
            return 1

        worktree_path = Path(args.worktree)

        # Validate every block's destination *before* writing anything --
        # one bad name must abort the whole extraction, never a partial,
        # inconsistent write (Audit Finding C-2).
        planned: list[tuple[str, Path, str]] = []
        for match in matches:
            relative_name = match.group("name").strip()
            content = match.group("content")
            if not content.endswith("\n"):
                content += "\n"
            try:
                target = _safe_worktree_target(worktree_path, relative_name)
            except PathTraversalError as exc:
                print(f"Refused: real Agent reply named an unsafe file path: {exc}. Zero files written.")
                return 1
            planned.append((relative_name, target, content))

        written = []
        for relative_name, target, content in planned:
            _write_agent_file(target, content)
            written.append(relative_name)
            print(f"Wrote real Agent-generated content to: {relative_name} ({len(content)} bytes)")

        subprocess.run(
            ["git", "-C", str(worktree_path), "add", "-A"], env=_env(), check=True, timeout=30.0
        )
        subprocess.run(
            ["git", "-C", str(worktree_path), "commit", "-q", "-m",
             f"apply real Agent-generated files for {args.task_id}"],
            env=_env(), check=True, timeout=30.0,
        )
        print(f"FILES_WRITTEN={','.join(written)}")
        return 0
    finally:
        state.close()


def cmd_propose(args: argparse.Namespace) -> int:
    state = _open_state(args.db)
    try:
        engine, pipeline = _build_pipeline_light(state)
        project = ProjectProfile(
            name=args.project_name, repository_path=args.repo, project_id=args.project_id
        )
        evidence = {}
        for item in args.evidence:
            criterion, _, value = item.partition("=")
            evidence[criterion] = value.strip().lower() in ("1", "true", "yes", "pass")

        results = engine.manager.get_execution_results(args.task_id)
        if results:
            print(f"Evaluating against EXECUTION_ID={results[-1].execution_id}")
        proposal = pipeline.evaluate_and_propose(
            args.task_id, project, criteria_evidence=evidence or None,
            expected_execution_id=args.execution_id,
        )
        if proposal is None:
            print("Quality Gate did not PASS. No Integration Proposal created.")
            print(f"Task status: {engine.manager.get(args.task_id).status.value}")
            return 1
        print(f"PROPOSAL diff_identity={proposal.diff_identity}")
        print(f"Conflict blocks: {proposal.conflict_blocks}")
        print(f"Use the Approval CLI to review and decide: "
              f"python3 -m universal_orchestrator.cli.approval_cli show {args.task_id}")
        return 0
    finally:
        state.close()


def cmd_recover_proposal(args: argparse.Namespace) -> int:
    """Milestone 7, Component D. Never builds an adapter/router --
    `_build_pipeline_light` is the same "never dispatches an Agent"
    stack `propose`/`integrate` already use; `recover_proposal` itself
    never calls `engine.execute()` either (see its own docstring)."""
    logger = get_logger("recover_proposal")
    state = _open_state(args.db)
    try:
        engine, pipeline = _build_pipeline_light(state)
        project = ProjectProfile(
            name=args.project_name, repository_path=args.repo, project_id=args.project_id
        )
        try:
            proposal = pipeline.recover_proposal(args.task_id, project)
        except Exception as exc:
            logger.warning(
                "recover-proposal refused",
                extra={"fields": {"task_id": args.task_id, "reason": str(exc)}},
            )
            print(f"Refused: {exc}")
            return 1

        logger.info(
            "recover-proposal succeeded",
            extra={"fields": {
                "task_id": args.task_id,
                "diff_identity": proposal.diff_identity,
                "conflict_blocks": proposal.conflict_blocks,
            }},
        )
        print(f"RECOVERED_PROPOSAL diff_identity={proposal.diff_identity}")
        print(f"Conflict blocks: {proposal.conflict_blocks}")
        print(f"Worktree status: {pipeline.worktrees.get(args.task_id).status.value}")
        return 0
    finally:
        state.close()


def cmd_integrate(args: argparse.Namespace) -> int:
    state = _open_state(args.db)
    try:
        engine, pipeline = _build_pipeline_light(state)
        project = ProjectProfile(
            name=args.project_name, repository_path=args.repo, project_id=args.project_id,
            required_testing_commands=args.required_test,
        )
        report = pipeline.integrate_after_approval(args.task_id, project)
        print(f"POST_INTEGRATION_VERDICT={report.verdict.value}")
        print(f"Task status: {engine.manager.get(args.task_id).status.value}")
        return 0
    finally:
        state.close()


def _env() -> dict[str, str]:
    import os
    return {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestrator_cli", description="The real Orchestrator entrypoint (M6)."
    )
    parser.add_argument("--db", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="Submit, route, and dispatch a real Agent for a new task.")
    submit.add_argument("--repo", required=True)
    submit.add_argument("--project-id", required=True)
    submit.add_argument("--project-name", default="project")
    submit.add_argument("--title", required=True)
    submit.add_argument("--criterion", action="append", default=[])
    submit.add_argument("--input-context", default="")
    submit.add_argument("--adapter", choices=["claude", "hermes"], default="claude")
    submit.add_argument("--adapter-target", default="")
    submit.add_argument("--timeout", type=float, default=60.0)
    submit.add_argument(
        "--permission-mode", choices=["read_only", "worktree_write"], default="read_only",
        help="'worktree_write' grants the Agent its own coding tools, sandboxed to this task's "
             "Worktree only (requires 'bwrap' on PATH; fails closed if unavailable).",
    )
    submit.set_defaults(func=cmd_submit)

    extract = sub.add_parser("extract-claude-files", help="Write the Agent's real reply text into real files.")
    extract.add_argument("task_id")
    extract.add_argument("--worktree", required=True)
    extract.set_defaults(func=cmd_extract_claude_files)

    propose = sub.add_parser("propose", help="Evaluate real evidence and build an Integration Proposal.")
    propose.add_argument("task_id")
    propose.add_argument("--repo", required=True)
    propose.add_argument("--project-id", required=True)
    propose.add_argument("--project-name", default="project")
    propose.add_argument("--evidence", action="append", default=[], help="criterion=true|false")
    propose.add_argument(
        "--execution-id", default=None,
        help="The execution_id this evidence was actually collected against. If the task's "
             "latest execution is a different one by the time this runs, propose refuses "
             "rather than pairing stale evidence with it. Omit to use whichever is latest "
             "(prior behavior, unchanged).",
    )
    propose.set_defaults(func=cmd_propose)

    recover = sub.add_parser(
        "recover-proposal",
        help="Rebuild a missing Integration Proposal for an already-SUCCEEDED task "
             "(never re-runs the Agent or the Quality Gate).",
    )
    recover.add_argument("task_id")
    recover.add_argument("--repo", required=True)
    recover.add_argument("--project-id", required=True)
    recover.add_argument("--project-name", default="project")
    recover.set_defaults(func=cmd_recover_proposal)

    integrate = sub.add_parser("integrate", help="Integrate an approved proposal and run post-integration checks.")
    integrate.add_argument("task_id")
    integrate.add_argument("--repo", required=True)
    integrate.add_argument("--project-id", required=True)
    integrate.add_argument("--project-name", default="project")
    integrate.add_argument("--required-test", action="append", default=[])
    integrate.set_defaults(func=cmd_integrate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
