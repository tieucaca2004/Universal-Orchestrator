# Claude Code CLI Adapter

`execution/claude_code.py` — the first concrete `ExecutionAdapter`
(`execution/adapter.py`). Everything below was verified against the
real, installed CLI on the development host before being written into
code; nothing here is assumed from memory of an older version.

## Machine identity (verified again for this milestone)

Same finding as Phase 0 and every milestone since: this host is
`tieu`, an ASUS laptop, **not** the Intel NUC the approved architecture
names as the central Orchestrator host. `hostnamectl` confirms "Chassis:
laptop". Milestone 4's code makes no assumption about which machine it
runs on — the adapter shells out to whatever `claude` executable it's
given — but the Claude CLI itself was only verified running *here*, not
on the NUC.

## Verified Claude CLI capabilities

Commands actually run, with their actual output, on this host:

| Check | Command | Result |
|---|---|---|
| Executable path | `which claude` | `/usr/local/bin/claude` |
| Version | `claude --version` | `2.1.221 (Claude Code)` |
| Non-interactive invocation | `claude --help` | `-p, --print` — "Print response and exit (useful for pipes)" |
| Structured output format | `claude --help` | `--output-format <format>`: `text` (default), `json` (single result), `stream-json` — only with `--print` |
| Permission modes | `claude --help` | `--permission-mode <mode>`: `acceptEdits, auto, bypassPermissions, manual, dontAsk, plan` — **no literal "read-only" mode exists**; see "Security posture" below for what the adapter uses instead |
| Zero-tool mode | `claude --help` | `--tools <tools...>`: `""` disables all tools, `"default"` enables all, or a specific comma/space list |
| Session persistence control | `claude --help` | `--no-session-persistence` — "sessions will not be saved to disk... (only works with --print)" |
| Authentication check | `claude auth status` | `{"loggedIn": true, "authMethod": "oauth_token", "apiProvider": "firstParty"}` — no credential value, just status |
| Installation health | `claude doctor` | version/commit/platform confirmed, "No installation issues found" |
| Trust prompt behavior | `claude --help` | "The workspace trust dialog is skipped when Claude is run in non-interactive mode (via -p...). Only use this in directories you trust." |

**Real argument-order bug found and worked around:** `--tools` is
variadic (`--tools <tools...>`). Passing the prompt *after* `--tools ""`
causes the CLI's parser to swallow the prompt into `--tools`'s value
list, leaving no prompt argument at all (`Error: Input must be provided
either through stdin or as a prompt argument when using --print`). The
adapter always puts the prompt **first**, before any flags — verified
to work.

## Real smoke test (one bounded, paid call — not a mock)

Run once, in an isolated empty scratch directory
(`/tmp/uo-m4-smoke-sandbox`, created solely for this test, not any
project's repository):

```
claude "Reply with exactly this JSON and nothing else: {...ack...}" \
  -p --output-format json --tools ""
```

Result: exit code `0`, real JSON response (`is_error: false`,
`stop_reason: "end_turn"`, `subtype: "success"`, `result` containing the
requested echoed text), `total_cost_usd: 0.0357` (a real, observed cost —
not invented), and the sandbox directory verified **unchanged**
(`ls -la` before and after: identical, empty) — proving `--tools ""`
really does prevent every filesystem write, not just in theory. No
credential value appeared anywhere in the output.

This satisfies "operationally ready" for: CLI detection, authentication,
non-interactive invocation, JSON output parsing, and zero-file-write
enforcement. It does **not** by itself prove timeout or cancellation
behavior at scale — those are verified instead with a fake slow
executable in `tests/test_claude_code_adapter.py`, to avoid a second
unnecessary paid call.

## Security posture

`PermissionMode` currently defines exactly one value, `READ_ONLY`,
implemented as `--tools ""`. This was chosen over an allow-list such as
`--allowedTools Read,Grep,Glob` because this repository has not verified
this CLI version's exact tool-name list, and a wrong guess could under-
or over-restrict; `--tools ""` is the one option directly and
unambiguously documented by `--help`. Consequences, all verified by the
real smoke test above:

- Claude cannot read, write, or edit any file — including the task's
  own project repository. Planner/Architect/Reviewer roles in this
  milestone can only reason from `ExecutionRequest.input_context` (text
  the caller supplies), not from browsing the repository. Broader read
  access is deferred to a future milestone once the real tool-name list
  for this CLI version is verified.
- `execute()` fails closed (`ExecutionOutcome.ERROR`,
  `error_classification="unsupported_permission_mode"`) for any
  `permission_mode` other than `READ_ONLY` — there is no silent
  fallback to a more permissive mode.
- `--dangerously-skip-permissions` and
  `--allow-dangerously-skip-permissions` are never constructed into any
  command, anywhere in this module — verified by
  `SecurityTests.test_dangerous_permission_bypass_flags_are_never_used`.
- The prompt is always passed as a single `argv` element to
  `subprocess.Popen` (never through a shell), so shell metacharacters in
  a task's input cannot be interpreted — verified with a payload
  containing `; touch ... ; echo pwned #`.
- `auth status`'s JSON is parsed only for the `loggedIn` boolean;
  `authMethod` and `apiProvider` are read but never stored, logged, or
  returned by this adapter — verified by
  `AuthenticationTests.test_verify_never_exposes_raw_auth_json`.
- No API key is ever placed in a command-line argument — authentication
  is entirely the pre-existing `claude auth login` state on this host;
  this adapter never reads, writes, or overwrites that configuration.

## Execution lifecycle

`verify()`: resolve executable → `--version` → `--help` (checks the 4
flags this adapter depends on are present — "Unsupported" if any are
missing) → `auth status` → one of `AVAILABLE / UNAVAILABLE /
UNAUTHENTICATED / UNSUPPORTED / ERROR`.

`execute(request)`: refuses non-`READ_ONLY` policy and a missing
executable up front (both `ERROR`, fail closed); otherwise runs
`claude <prompt> -p --output-format json --no-session-persistence
--tools ""` in `request.working_directory`, in its own process group
(`start_new_session=True`), bounded by `request.timeout_seconds`. On
timeout: `SIGTERM` to the whole group, up to 3 seconds grace, then
`SIGKILL` — a child process is never left running. Output is truncated
to `policy.max_output_bytes` (default 64 KiB).

Outcome classification never trusts exit code `0` alone: a `0` exit
with the CLI's own `is_error: true` field is still `FAILED`
(`error_classification="cli_reported_error"`) — see
`ExecutionTests.test_cli_reported_error_with_zero_exit_is_failed`.
`ExecutionResult.success` is a process-level signal only ("the process
ran to a normal finish with no error signal"), never a claim that the
task's actual objective was met.

**Duplicate-execution protection**: re-calling `execute()` with an
`execution_id` already seen returns the cached first result, never
re-running — including when that first result was `TIMED_OUT` (an
uncertain outcome is still never silently retried). This guarantee holds
for the lifetime of one `ClaudeCodeAdapter` instance only — it is **not**
crash-persistent. A process restart loses the cache; the Task Manager's
own `record_execution_result`/`get_execution_results` (SQLite-backed)
is the durable record, and nothing in this milestone automatically
retries a task found with an uncertain prior execution result on
restart (consistent with `TaskManager.recover()`'s existing rule for
`INTERRUPTED` tasks).

**Cancellation**: `cancel(execution_id)` sends the same
graceful-then-forceful process-group termination used for timeouts.
Best-effort and cooperative — it cannot interrupt the CLI mid-API-call
any faster than the process itself can be signalled, and returns `False`
if nothing matching is currently running.

## Engine integration

`OrchestrationEngine.execute(task_id, working_directory, ...)`:

1. Requires `self.adapters` (an `AdapterRegistry`) to be configured —
   `RuntimeError` otherwise.
2. Fetches the task's most recent routing decision. No decision, or a
   `NO_ELIGIBLE_AGENT` decision → `OrchestratorError`. Routing is never
   bypassed.
3. Looks up the `ExecutionAdapter` for `decision.selected_provider_id`
   via the Adapter Registry, and the agent's role via
   `AgentRegistry` (if wired).
4. Builds an `ExecutionRequest` carrying `task_id`, `project_id`,
   `agent_id`, `provider_id`, `agent_role`, the task's
   `TaskSpecification`, and `decision.decision_id` as the correlation id
   — full traceability from Task → Routing Decision → Execution Result.
5. Calls `adapter.execute(request)` and persists the result through
   `TaskManager.record_execution_result` (its own `execution_results`
   SQLite table — never touches the `tasks` row or its history, same
   pattern as `routing_decisions`).

**`working_directory` has no default anywhere in this chain.** The
caller supplies it explicitly every time — `Engine.execute()` never
falls back to `ProjectProfile.repository_path`, specifically so the live
repository is never touched just because routing and the adapter happen
to succeed. A real integration that runs Claude against an actual
project's files needs a Git Worktree Manager (Milestone 6) to provide an
isolated, disposable directory first; until then, every real invocation
must point at a scratch sandbox the caller already trusts, exactly like
the smoke test above.

`execute()` does **not** change `Task.status`. A completed execution is
not evidence the task's business objective was met — that judgement is
a future Quality Gate's job, not this adapter's.

## Multi-engine architecture (addendum)

- **Engine Registry / Engine Selection**: deliberately not a new
  abstraction. `registry.providers.ProviderRegistry` already is the
  engine inventory (kind, capabilities, verification/availability
  status); `execution.registry.AdapterRegistry` is the one missing
  mechanical piece — given a `provider_id` the Router selected, which
  `ExecutionAdapter` instance to call. Nothing in `core/engine.py` or
  `routing/router.py` changes to add a second engine.
- **Manual/Automatic mode**: out of scope for this milestone per the
  addendum ("M4 only needs to establish architectural compatibility").
  `AdapterRegistry` + `ProviderRegistry` together are what a future
  selection policy would read; no policy engine exists yet.
- **Execution isolation**: `ClaudeCodeAdapter` has no notion of any
  other engine, and nothing in this milestone allows two adapters to
  write to the same working tree concurrently — mechanically true today
  because the only implemented policy is zero-write (`--tools ""`).

### DeepSeek Harness CLI compatibility review

**No DeepSeek Harness CLI is installed on this host** — a filesystem
search found no such binary anywhere (checked `which`, and a repo-wide
search including `/opt` and the OpenClaw installation). What exists
instead is OpenClaw's DeepSeek **API** provider plugin
(`~/openclaw/extensions/deepseek/`), which talks to DeepSeek over HTTP,
not a CLI harness at all. Because there is nothing installed to run
`--help` against, this review cannot verify real invocation, flags,
output format, or timeout/cancellation behavior for a DeepSeek CLI —
and this document does not guess them. **No DeepSeek adapter is
implemented.** When a real DeepSeek Harness CLI is available, its
compatibility with `ExecutionAdapter` should be verified the same way
this document verified Claude's: real `--help` output, a real bounded
smoke test, before any code is written.

## Known limitations

- Only zero-tool (`--tools ""`) execution is implemented. No read-access
  policy, no write policy, no Git Worktree integration.
- `execute()` is synchronous (blocks the calling thread for the
  subprocess's lifetime); cancellation works from another thread, but
  there is no async/concurrent execution model.
- No cost or token budgeting is enforced — the adapter surfaces
  whatever the CLI's own JSON reports (this milestone observed a real
  `total_cost_usd` field) but invents no pricing model and enforces no
  budget cap. `--max-budget-usd` exists per `--help` but is not wired in,
  since Founder-approved cost policy doesn't exist yet.
- No automated retry of failed or timed-out executions.
- No distributed/queued execution — one process per `execute()` call.
- Execution-result caching for duplicate protection is in-memory only
  (see "Duplicate-execution protection" above) — not durable across a
  process restart; the SQLite `execution_results` table is the durable
  record but is not itself consulted to prevent a re-run.
