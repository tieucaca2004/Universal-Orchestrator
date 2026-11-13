"""Milestone 5, Priority 9 — multi-runtime verification.

Proves the combined architecture using the two *real* concrete adapter
classes (ClaudeCodeAdapter, HermesAdapter) against minimal deterministic
fake executables — never real Claude or Hermes calls. Confirms: both
adapters satisfy the same ExecutionAdapter contract, both are
independently verifiable, both are selectable purely by provider_id,
one being unavailable never disables the other, and both produce the
same normalized ExecutionResult shape.
"""

import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from universal_orchestrator.core.engine import OrchestrationEngine
from universal_orchestrator.core.models import (
    AgentDescriptor,
    AgentStatus,
    ProjectProfile,
    ProviderDescriptor,
    ProviderStatus,
    TaskRequest,
)
from universal_orchestrator.core.state import StateManager
from universal_orchestrator.execution.adapter import ExecutionAdapter, ExecutionOutcome, VerificationStatus
from universal_orchestrator.execution.claude_code import ClaudeCodeAdapter
from universal_orchestrator.execution.hermes import HermesAdapter
from universal_orchestrator.execution.registry import AdapterRegistry
from universal_orchestrator.registry.agents import AgentRegistry
from universal_orchestrator.registry.providers import ProviderRegistry
from universal_orchestrator.routing.router import AgentRouter

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
    print(json.dumps({"is_error": False, "result": f"claude:{prompt}"})); sys.exit(0)
    """
)

_BROKEN_CLAUDE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import sys
    sys.exit(1)
    """
)

_FAKE_DOCKER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, sys
    args = sys.argv[1:]
    if args[0] == "inspect":
        fmt, target = args[2], args[3]
        if target not in ("hermes", "fake_container_id"):
            sys.exit(1)
        if fmt == "{{.Id}} {{.State.Running}}":
            print("fake_container_id true")
        else:
            print("true")
        sys.exit(0)
    if args[0] == "exec":
        idx = 1
        workdir = None
        if args[idx] == "-w":
            workdir = args[idx + 1]; idx += 2
        container = args[idx]; idx += 1
        inner = args[idx:]
        if workdir is not None and not os.path.isdir(workdir):
            sys.exit(127)
        if inner[0] == "hermes":
            if inner[1] == "--version":
                print("Hermes Agent v9.9.9 (fake)"); sys.exit(0)
            if inner[1] == "chat" and inner[2] == "--help":
                print(
                    "-q, --query QUERY\\n-Q, --quiet\\n--oneshot\\n--safe-mode\\n"
                    "-t, --toolsets TOOLSETS\\n"
                )
                sys.exit(0)
            if inner[1] == "status":
                print("API Keys\\n  OpenAI \\u2713 sk-f...ake1\\nTerminal Backend\\n"); sys.exit(0)
        if inner[0] == "sh" and inner[1] == "-c":
            rest = inner[4:]
            prompt = rest[rest.index("-q") + 1] if "-q" in rest else ""
            print(f"hermes:{prompt}"); sys.exit(0)
        if inner[0] in ("cat", "kill", "rm"):
            sys.exit(0)
    sys.exit(1)
    """
)


def _write_fake(tmp_dir: Path, source: str, name: str) -> str:
    path = tmp_dir / name
    path.write_text(source)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class MultiRuntimeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.workdir = self.tmp_path / "workdir"
        self.workdir.mkdir()
        self.claude_bin = _write_fake(self.tmp_path, _FAKE_CLAUDE, "fake_claude")
        self.docker_bin = _write_fake(self.tmp_path, _FAKE_DOCKER, "fake_docker")

    def tearDown(self):
        self._tmpdir.cleanup()


class ContractConformanceTests(MultiRuntimeTestCase):
    def test_both_adapters_are_execution_adapters(self):
        claude = ClaudeCodeAdapter(executable=self.claude_bin)
        hermes = HermesAdapter(container="hermes", docker_bin=self.docker_bin)
        self.assertIsInstance(claude, ExecutionAdapter)
        self.assertIsInstance(hermes, ExecutionAdapter)

    def test_both_are_independently_verifiable_and_available(self):
        claude = ClaudeCodeAdapter(executable=self.claude_bin)
        hermes = HermesAdapter(container="hermes", docker_bin=self.docker_bin)
        self.assertIs(claude.verify(), VerificationStatus.AVAILABLE)
        self.assertIs(hermes.verify(), VerificationStatus.AVAILABLE)

    def test_claude_unavailable_does_not_affect_hermes(self):
        broken_claude_bin = _write_fake(self.tmp_path, _BROKEN_CLAUDE, "broken_claude")
        claude = ClaudeCodeAdapter(executable=broken_claude_bin)
        hermes = HermesAdapter(container="hermes", docker_bin=self.docker_bin)

        self.assertIs(claude.verify(), VerificationStatus.ERROR)
        self.assertIs(hermes.verify(), VerificationStatus.AVAILABLE)

    def test_hermes_unavailable_does_not_affect_claude(self):
        claude = ClaudeCodeAdapter(executable=self.claude_bin)
        hermes = HermesAdapter(container="no-such-container", docker_bin=self.docker_bin)

        self.assertIs(hermes.verify(), VerificationStatus.UNAVAILABLE)
        self.assertIs(claude.verify(), VerificationStatus.AVAILABLE)

    def test_both_results_share_the_same_normalized_shape(self):
        from universal_orchestrator.execution.adapter import ExecutionRequest

        claude = ClaudeCodeAdapter(executable=self.claude_bin)
        hermes = HermesAdapter(container="hermes", docker_bin=self.docker_bin)
        base = dict(
            task_id="task_x",
            project_id="proj_x",
            agent_id="agent_x",
            agent_role="coder",
            working_directory=str(self.workdir),
            input_context="hello",
        )
        claude_result = claude.execute(ExecutionRequest(provider_id="prov_claude", **base))
        hermes_result = hermes.execute(ExecutionRequest(provider_id="prov_hermes", **base))

        for result in (claude_result, hermes_result):
            self.assertIs(result.outcome, ExecutionOutcome.COMPLETED)
            self.assertIsInstance(result.stdout, str)
            self.assertIsNotNone(result.verification_status)
            self.assertEqual(result.task_id, "task_x")


class EngineDispatchByProviderIdTests(MultiRuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.agents = AgentRegistry()
        self.providers = ProviderRegistry()
        self.state = StateManager(self.tmp_path / "orchestrator.sqlite3")

    def tearDown(self):
        self.state.close()
        super().tearDown()

    def _verified_provider(self, name):
        provider = self.providers.register(ProviderDescriptor(name=name, kind="cli"))
        self.providers.set_verified(provider.provider_id, True)
        self.providers.set_status(provider.provider_id, ProviderStatus.AVAILABLE)
        return provider

    def _available_agent(self, name, role, provider_id):
        agent = self.agents.register(AgentDescriptor(name=name, role=role, provider_id=provider_id))
        self.agents.set_status(agent.agent_id, AgentStatus.AVAILABLE)
        return agent

    def test_engine_dispatches_claude_and_hermes_tasks_to_the_correct_adapter(self):
        claude_provider = self._verified_provider("anthropic")
        hermes_provider = self._verified_provider("nous-hermes")
        claude_agent = self._available_agent("claude-reviewer", "reviewer", claude_provider.provider_id)
        self._available_agent("hermes-coder", "coder", hermes_provider.provider_id)

        adapters = AdapterRegistry()
        adapters.register(claude_provider.provider_id, ClaudeCodeAdapter(executable=self.claude_bin))
        adapters.register(
            hermes_provider.provider_id, HermesAdapter(container="hermes", docker_bin=self.docker_bin)
        )

        router = AgentRouter(self.agents, self.providers)
        engine = OrchestrationEngine.bootstrap(
            self.state, router=router, adapters=adapters, agents=self.agents, providers=self.providers
        )
        projects_profile = ProjectProfile(name="demo", repository_path="/workspace/demo")

        # Route a task requiring the "reviewer" role -> must select Claude.
        task_a = engine.submit(
            TaskRequest(title="review", metadata={"acceptance_criteria": ["x"], "expected_deliverables": ["y"]}),
            projects_profile,
        )
        task_a_obj = engine.manager.get(task_a.task_id)
        task_a_obj.payload["required_role"] = "reviewer"
        decision_a = engine.route(task_a.task_id, projects_profile)
        self.assertEqual(decision_a.selected_provider_id, claude_provider.provider_id)
        result_a = engine.execute(task_a.task_id, working_directory=str(self.workdir))
        self.assertIn("claude:", result_a.stdout)

        # Route a task requiring the "coder" role -> must select Hermes.
        task_b = engine.submit(
            TaskRequest(title="code", metadata={"acceptance_criteria": ["x"], "expected_deliverables": ["y"]}),
            projects_profile,
        )
        task_b_obj = engine.manager.get(task_b.task_id)
        task_b_obj.payload["required_role"] = "coder"
        decision_b = engine.route(task_b.task_id, projects_profile)
        self.assertEqual(decision_b.selected_provider_id, hermes_provider.provider_id)
        result_b = engine.execute(task_b.task_id, working_directory=str(self.workdir))
        self.assertIn("hermes:", result_b.stdout)

        # Both persisted independently, same table, same shape.
        self.assertEqual(len(engine.manager.get_execution_results(task_a.task_id)), 1)
        self.assertEqual(len(engine.manager.get_execution_results(task_b.task_id)), 1)


if __name__ == "__main__":
    unittest.main()
