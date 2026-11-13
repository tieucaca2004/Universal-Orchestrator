"""M6 remediation, Item 1 — Governed Execution Pipeline.

Proves the full automatic sequence (submit -> route -> Worktree ->
dispatch -> Quality Gate -> Conflict check -> Proposal -> STOP ->
Approval -> revalidate -> lock -> integrate -> post-integration
Quality Gate -> Worktree resolution) using the *real* `ClaudeCodeAdapter`
against a minimal, deterministic fake executable (never a real Claude
call — same pattern as tests/test_multi_runtime.py) and real Git
repositories.
"""

import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.governed_pipeline import GovernedPipeline, IntegrationBlockedError
from universal_orchestrator.core.models import (
    AgentDescriptor,
    AgentStatus,
    ProjectProfile,
    ProviderDescriptor,
    ProviderStatus,
    TaskRequest,
)
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.errors import ApprovalRequiredError, OrchestratorError
from universal_orchestrator.execution.approval import ApprovalDecisionType
from universal_orchestrator.execution.approval_manager import ApprovalGate
from universal_orchestrator.execution.claude_code import ClaudeCodeAdapter
from universal_orchestrator.execution.conflict_manager import ConflictManager
from universal_orchestrator.execution.quality import QualityVerdict
from universal_orchestrator.execution.quality_manager import QualityGate
from universal_orchestrator.execution.registry import AdapterRegistry
from universal_orchestrator.execution.worktree import WorktreeStatus
from universal_orchestrator.execution.worktree_manager import WorktreeManager
from universal_orchestrator.registry.agents import AgentRegistry
from universal_orchestrator.registry.providers import ProviderRegistry
from universal_orchestrator.routing.router import AgentRouter

# Writes a real file into its own cwd (the Worktree, since
# ClaudeCodeAdapter passes cwd=request.working_directory) — simulates
# the Agent's actual work product, never a real Claude call.
_FAKE_CLAUDE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys
    args = sys.argv[1:]
    if args == ["--version"]:
        print("9.9.9 (Fake Claude)"); sys.exit(0)
    if args == ["--help"]:
        print(
            "-p, --print\\n--output-format\\n--tools\\n--no-session-persistence\\n"
            "--safe-mode\\n--strict-mcp-config\\n--disable-slash-commands\\n--setting-sources\\n"
        )
        sys.exit(0)
    if len(args) >= 2 and args[0:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": True})); sys.exit(0)
    prompt = args[0] if args else ""
    open("agent_output.txt", "w").write("fake agent output\\n")
    print(json.dumps({"is_error": False, "result": f"claude:{prompt}"})); sys.exit(0)
    """
)


def _write_fake_claude(tmp_dir: Path) -> str:
    path = tmp_dir / "fake_claude"
    path.write_text(_FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)
    return result.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], path)
    _run(["git", "config", "user.email", "test@example.com"], path)
    _run(["git", "config", "user.name", "Test"], path)
    (path / "README.md").write_text("original\n")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-q", "-m", "initial"], path)


class GovernedPipelineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_path = self.tmp_path / "project"
        _init_repo(self.repo_path)
        self.claude_bin = _write_fake_claude(self.tmp_path)

        self.state = StateManager(self.tmp_path / "orchestrator.sqlite3")
        self.agents = AgentRegistry()
        self.providers = ProviderRegistry()
        self.adapters = AdapterRegistry()
        self.adapters.register("prov_claude", ClaudeCodeAdapter(executable=self.claude_bin))

        provider = self.providers.register(ProviderDescriptor(name="anthropic", kind="cli"))
        self.providers.set_verified(provider.provider_id, True)
        self.providers.set_status(provider.provider_id, ProviderStatus.AVAILABLE)
        self.provider_id = provider.provider_id
        self.adapters.register(provider.provider_id, ClaudeCodeAdapter(executable=self.claude_bin))

        agent = self.agents.register(
            AgentDescriptor(name="claude-coder", role="coder", provider_id=provider.provider_id)
        )
        self.agents.set_status(agent.agent_id, AgentStatus.AVAILABLE)
        self.agent_id = agent.agent_id

        router = AgentRouter(self.agents, self.providers)
        self.engine = OrchestrationEngine.bootstrap(
            self.state, router=router, adapters=self.adapters, agents=self.agents, providers=self.providers
        )

        self.worktrees = WorktreeManager(self.state)
        self.conflicts = ConflictManager(self.state, self.worktrees)
        self.approvals = ApprovalGate(self.state)
        self.quality = QualityGate(self.state)
        self.pipeline = GovernedPipeline(
            self.engine, self.worktrees, self.conflicts, self.approvals, self.quality
        )

        self.project = ProjectProfile(
            name="demo",
            repository_path=str(self.repo_path),
            project_id="proj_x",
            required_testing_commands=[
                f"python3 -c \"import pathlib,sys; "
                f"sys.exit(0 if pathlib.Path('{self.repo_path}/agent_output.txt').exists() else 1)\""
            ],
        )

    def tearDown(self):
        self.state.close()
        self._tmpdir.cleanup()

    def _submit_and_route(self, title: str = "write agent_output.txt"):
        task = self.engine.submit(
            TaskRequest(
                title=title,
                metadata={
                    "acceptance_criteria": ["creates agent_output.txt"],
                    "expected_deliverables": ["agent_output.txt"],
                },
            ),
            self.project,
        )
        task_obj = self.engine.manager.get(task.task_id)
        task_obj.payload["required_role"] = "coder"
        self.engine.route(task.task_id, self.project)
        return task.task_id


class WorkspaceTransferWiringTests(GovernedPipelineTestCase):
    """M6 completion, Item 2 — `dispatch()` must actually call
    `workspace_transfer.copy_in()`/`copy_out()` when one is supplied,
    in the right order, and dispatch against the *staging* path it
    returns, not the host Worktree path directly. The transfer
    mechanism's own real correctness (manifest verification, host-drift
    refusal, etc.) is proven separately in test_hermes_transfer.py
    against a real filesystem stand-in; this is purely the wiring."""

    def test_dispatch_uses_the_staging_path_and_calls_copy_in_then_copy_out(self):
        import unittest.mock as mock

        task_id = self._submit_and_route()
        handle_path_holder = {}
        # A real directory stands in for the in-container staging path
        # here -- the fake Claude executable still needs a real `cwd`
        # to run in; `HermesWorkspaceTransfer`'s own real correctness
        # (manifest verification, host-drift refusal, real `docker`
        # calls) is proven separately in test_hermes_transfer.py. This
        # test is purely about dispatch()'s own wiring/call order.
        staging_dir = self.tmp_path / "fake_staging"
        staging_dir.mkdir()

        transfer = mock.Mock()
        call_order = []

        def fake_copy_in(host_path):
            handle_path_holder["host"] = host_path
            call_order.append("copy_in")
            return str(staging_dir)

        def fake_copy_out(host_path):
            call_order.append("copy_out")
            self.assertEqual(host_path, handle_path_holder["host"])

        transfer.copy_in.side_effect = fake_copy_in
        transfer.copy_out.side_effect = fake_copy_out

        result = self.pipeline.dispatch(task_id, self.project, workspace_transfer=transfer)

        self.assertEqual(call_order, ["copy_in", "copy_out"])
        self.assertEqual(result.outcome.value, "completed")

    def test_copy_in_failure_never_dispatches_and_preserves_the_worktree(self):
        import unittest.mock as mock

        from universal_orchestrator.execution.hermes_transfer import HermesTransferError

        task_id = self._submit_and_route()
        transfer = mock.Mock()
        transfer.copy_in.side_effect = HermesTransferError("simulated copy-in failure")

        with self.assertRaises(HermesTransferError):
            self.pipeline.dispatch(task_id, self.project, workspace_transfer=transfer)

        transfer.copy_out.assert_not_called()
        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.NEEDS_REVIEW)
        # The real Agent call never happened.
        self.assertEqual(self.engine.manager.get_execution_results(task_id), [])

    def test_copy_out_failure_surfaces_rather_than_being_hidden(self):
        import unittest.mock as mock

        from universal_orchestrator.execution.hermes_transfer import HermesTransferError

        task_id = self._submit_and_route()
        transfer = mock.Mock()
        transfer.copy_in.return_value = "/fake/staging/path"
        transfer.copy_out.side_effect = HermesTransferError("simulated copy-out failure")

        with self.assertRaises(HermesTransferError):
            self.pipeline.dispatch(task_id, self.project, workspace_transfer=transfer)

        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.NEEDS_REVIEW)
        # The real Agent call *did* happen -- only the copy-out failed.
        self.assertEqual(len(self.engine.manager.get_execution_results(task_id)), 1)


class HappyPathTests(GovernedPipelineTestCase):
    def test_full_pipeline_submit_to_integrated(self):
        task_id = self._submit_and_route()

        proposal = self.pipeline.submit_for_approval(
            task_id, self.project, criteria_evidence={"creates agent_output.txt": True}
        )

        self.assertIsNotNone(proposal)
        self.assertFalse(proposal.conflict_blocks)
        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.AWAITING_APPROVAL)
        self.assertEqual(self.engine.manager.get(task_id).status.value, "succeeded")

        # Approval Gate is the only way past this point.
        self.approvals.record_decision(proposal, ApprovalDecisionType.APPROVED, reason="looks good")

        integration_report = self.pipeline.integrate_after_approval(task_id, self.project)

        self.assertIs(integration_report.verdict, QualityVerdict.PASS)
        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.RELEASED)
        self.assertTrue((self.repo_path / "agent_output.txt").exists())
        self.assertIsNone(self.conflicts.current_integration_lock_holder())


class EvidenceLifecycleTests(GovernedPipelineTestCase):
    """M6 remediation (round 2), Item 1: evidence must be collected
    strictly *after* the real ExecutionResult exists, never fabricated
    ahead of dispatch. `dispatch()` and `evaluate_and_propose()` are
    the two separate calls that make this possible."""

    def test_evidence_is_collected_from_the_real_result_after_dispatch(self):
        task_id = self._submit_and_route()

        # dispatch() runs the real (fake-executable) Claude call and
        # returns the real ExecutionResult -- no evidence exists yet.
        result = self.pipeline.dispatch(task_id, self.project)
        self.assertEqual(result.outcome.value, "completed")
        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.QUALITY_CHECK)

        # Evidence is built only now, by inspecting what the real
        # (fake-executable) call actually produced in the Worktree --
        # never supplied before dispatch() ran.
        file_exists = (Path(self.worktrees.get(task_id).path) / "agent_output.txt").exists()
        real_evidence = {"creates agent_output.txt": file_exists}

        proposal = self.pipeline.evaluate_and_propose(
            task_id, self.project, criteria_evidence=real_evidence
        )

        self.assertIsNotNone(proposal)
        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.AWAITING_APPROVAL)
        self.assertEqual(self.engine.manager.get(task_id).status.value, "succeeded")

    def test_evaluate_and_propose_refuses_without_a_prior_dispatch(self):
        task_id = self._submit_and_route()
        with self.assertRaises(OrchestratorError):
            self.pipeline.evaluate_and_propose(task_id, self.project, criteria_evidence={"x": True})

    def test_evaluate_and_propose_refuses_if_called_twice(self):
        task_id = self._submit_and_route()
        self.pipeline.dispatch(task_id, self.project)
        self.pipeline.evaluate_and_propose(
            task_id, self.project, criteria_evidence={"creates agent_output.txt": True}
        )
        # Worktree is now AWAITING_APPROVAL, not QUALITY_CHECK -- a
        # second evaluate_and_propose() call must be refused, not
        # silently re-run.
        with self.assertRaises(OrchestratorError):
            self.pipeline.evaluate_and_propose(
                task_id, self.project, criteria_evidence={"creates agent_output.txt": True}
            )

    def test_missing_evidence_after_dispatch_still_produces_not_verified_never_pass(self):
        task_id = self._submit_and_route()
        self.pipeline.dispatch(task_id, self.project)

        proposal = self.pipeline.evaluate_and_propose(task_id, self.project)  # no evidence at all

        self.assertIsNone(proposal)
        self.assertEqual(self.engine.manager.get(task_id).status.value, "failed")
        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.NEEDS_REVIEW)

    def test_submit_for_approval_wrapper_still_works_unchanged(self):
        """Backward compatibility: the single-call wrapper is still
        available for callers whose evidence does not depend on
        inspecting a real result after the fact."""
        task_id = self._submit_and_route()
        proposal = self.pipeline.submit_for_approval(
            task_id, self.project, criteria_evidence={"creates agent_output.txt": True}
        )
        self.assertIsNotNone(proposal)
        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.AWAITING_APPROVAL)


class BypassPreventionTests(GovernedPipelineTestCase):
    def test_cannot_integrate_without_approval(self):
        task_id = self._submit_and_route()
        self.pipeline.submit_for_approval(
            task_id, self.project, criteria_evidence={"creates agent_output.txt": True}
        )

        with self.assertRaises(ApprovalRequiredError):
            self.pipeline.integrate_after_approval(task_id, self.project)

        # Refusal must not have touched main or the Worktree.
        self.assertFalse((self.repo_path / "agent_output.txt").exists())
        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.AWAITING_APPROVAL)

    def test_cannot_succeed_without_quality_gate_pass(self):
        task_id = self._submit_and_route()

        # No criteria_evidence supplied at all -> NOT_VERIFIED, never PASS.
        proposal = self.pipeline.submit_for_approval(task_id, self.project)

        self.assertIsNone(proposal)
        self.assertEqual(self.engine.manager.get(task_id).status.value, "failed")
        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.NEEDS_REVIEW)

    def test_cannot_integrate_with_a_real_conflict_on_revalidation(self):
        task_id = self._submit_and_route()
        proposal = self.pipeline.submit_for_approval(
            task_id, self.project, criteria_evidence={"creates agent_output.txt": True}
        )
        self.approvals.record_decision(proposal, ApprovalDecisionType.APPROVED)

        # Simulate a different, already-approved integration landing on
        # main first, creating the *same* file with different content —
        # a real add/add conflict with this task's own branch.
        (self.repo_path / "agent_output.txt").write_text("a different agent's output\n")
        _run(["git", "add", "agent_output.txt"], self.repo_path)
        _run(["git", "commit", "-q", "-m", "a different integration landed first"], self.repo_path)

        with self.assertRaises(IntegrationBlockedError):
            self.pipeline.integrate_after_approval(task_id, self.project)

        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.NEEDS_REVIEW)
        # main was not forced, not reset — the other integration's
        # content is exactly what's there, untouched by this failure.
        self.assertEqual(
            (self.repo_path / "agent_output.txt").read_text(), "a different agent's output\n"
        )
        self.assertIsNone(self.conflicts.current_integration_lock_holder())

    def test_post_integration_quality_failure_preserves_worktree_and_does_not_revert_main(self):
        task_id = self._submit_and_route()
        proposal = self.pipeline.submit_for_approval(
            task_id, self.project, criteria_evidence={"creates agent_output.txt": True}
        )
        self.approvals.record_decision(proposal, ApprovalDecisionType.APPROVED)

        # A required testing command that will fail for real, regardless
        # of the merge outcome.
        broken_project = ProjectProfile(
            name="demo",
            repository_path=str(self.repo_path),
            project_id="proj_x",
            required_testing_commands=["python3 -c \"import sys; sys.exit(1)\""],
        )

        integration_report = self.pipeline.integrate_after_approval(task_id, broken_project)

        self.assertIs(integration_report.verdict, QualityVerdict.FAIL)
        self.assertEqual(self.worktrees.get(task_id).status, WorktreeStatus.NEEDS_REVIEW)
        # The merge itself already happened (that's what post-integration
        # verification checks) — main is not reverted.
        self.assertTrue((self.repo_path / "agent_output.txt").exists())
        # The Worktree itself is not deleted — still inspectable.
        self.assertTrue(Path(self.worktrees.get(task_id).path).exists())


if __name__ == "__main__":
    unittest.main()
