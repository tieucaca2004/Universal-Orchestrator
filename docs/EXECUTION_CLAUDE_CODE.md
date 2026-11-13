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
| Config isolation (safe mode) | `claude --help` (M4 audit remediation) | `--safe-mode`: "Start with all customizations (CLAUDE.md, skills, plugins, hooks, MCP servers, custom commands and agents, output styles, workflows, custom themes, keybindings, and more) disabled... Auth, model selection, built-in tools, and permissions work normally." |
| MCP isolation | `claude --help` (M4 audit remediation) | `--strict-mcp-config`: "Only use MCP servers from --mcp-config, ignoring all other MCP configurations" |
| Skills isolation | `claude --help` (M4 audit remediation) | `--disable-slash-commands`: "Disable all skills" |
| Settings-source restriction | `claude --help` (M4 audit remediation) | `--setting-sources <sources>`: "Comma-separated list of setting sources to load (user, project, local)" — adapter uses `user` only |
| Minimal mode — evaluated, rejected | `claude --help` (M4 audit remediation) | `--bare`: "...Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings (OAuth and keychain are never read)..." — **incompatible with this host's OAuth authentication**, confirmed by this host's own `auth status` reporting `authMethod: oauth_token`; never used |
| Restricted-environment auth check (real, free) | `subprocess.run(["claude","auth","status"], env={"HOME","PATH","LANG"})` (M4 audit remediation) | exit `0`, `loggedIn: true`, `authMethod: oauth_token` — confirms the environment allowlist does not break authentication on this host |

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

**Revised by the M4 audit remediation.** An independent audit (Finding
C-1) found that the original `--tools ""`-only policy was described as
"read-only execution" without distinguishing *which* isolation boundary
that phrase actually covers. The six boundaries below are now handled,
documented, and tested separately — conflating them is exactly the
mistake the audit flagged, so this section is deliberately explicit
about which ones this adapter enforces and which it does not.

| Boundary | Enforced? | How |
|---|---|---|
| **Built-in tool restriction** | **Yes** | `--tools ""` — every built-in tool (Read, Write, Edit, Bash, ...) disabled. Unchanged from the original M4 implementation. |
| **Plugin / hook / MCP restriction** | **Yes, added in remediation** | `--safe-mode` (disables CLAUDE.md, skills, plugins, hooks, MCP servers, custom commands/agents, output styles, workflows, themes) + `--strict-mcp-config` (forces zero MCP servers independently of `--safe-mode`) + `--disable-slash-commands` (disables skills independently too) + `--setting-sources user` (excludes project/local settings files — e.g. anything a task's `working_directory` might itself contain). All four verified present in `--help` before every real execution (`verify()`'s `_check_supported_options`); `execute()` fails closed (`UNSUPPORTED`) if a CLI version lacks any of them. |
| **Filesystem isolation** | **No** | Not a sandbox, container, or chroot. Nothing here stops the `claude` process itself (as opposed to Claude's own tool use) from touching the filesystem if it chose to — this adapter relies entirely on tool/plugin/hook/MCP restriction above, not OS-level filesystem confinement. Real project execution still needs Git Worktree isolation (M6). |
| **Process isolation** | **Partial** | `start_new_session=True` gives the invocation its own process group, so timeout/cancellation can terminate it and any children together (`os.killpg`). This is grouping for clean teardown, not a security sandbox — it does not prevent the process from spawning something outside that group. |
| **Network isolation** | **No** | The process can still reach the network (it needs to, to reach the Anthropic API). Nothing in this adapter restricts outbound network access. |
| **Environment restriction** | **Yes, added in remediation** | `_build_subprocess_env()` passes an explicit allowlist (`HOME, PATH, LANG, LC_ALL, TERM, TMPDIR`) to every subprocess call this adapter makes — never the full parent environment. Empirically verified: `claude auth status` with only this allowlist still reports `loggedIn: true` on this host (OAuth authentication is settings-file-driven, read by the `claude` process itself from `~/.claude`, not dependent on any credential being in this process's environment). A host authenticating via an exported `ANTHROPIC_API_KEY` or similar shell variable would need that name added to the allowlist — `verify()` will correctly report `UNAUTHENTICATED` rather than silently misbehaving if it's missing, never a false `AVAILABLE`. |

Additional, unchanged-by-remediation guarantees, all still verified:

- `execute()` fails closed (`ExecutionOutcome.ERROR`,
  `error_classification="unsupported_permission_mode"`) for any
  `permission_mode` other than `READ_ONLY` — there is no silent
  fallback to a more permissive mode.
- `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`,
  and **`--bare`** are never constructed into any command, anywhere in
  this module. `--bare` specifically was evaluated and rejected: its own
  `--help` text states "Anthropic auth is strictly `ANTHROPIC_API_KEY`
  or `apiKeyHelper`... OAuth and keychain are never read" — this host
  authenticates via OAuth, so `--bare` would have broken authentication
  outright. A real, verified incompatibility, not a guess — see
  `SecurityTests.test_bare_flag_is_never_used`.
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
  this adapter never reads, writes, or overwrites that configuration,
  and never reads `~/.claude/settings.json` or any other credential-
  bearing file directly.

### Executable resolution (pinning)

Also added in remediation, addressing a separate audit finding: a bare
executable name (`"claude"`, the default) is resolved via `PATH`
**exactly once per adapter instance** — on whichever of `verify()` or
`execute()` runs first — and the resulting absolute path is pinned
(`self._resolved_path`). Every later call re-validates that the *same*
path still exists and is still executable; it never searches `PATH`
again. This closes the window where a `PATH` change between `verify()`
and `execute()` could silently route execution to a different binary.
If the pinned path disappears, resolution fails closed
(`UNAVAILABLE`/`executable_not_found`) rather than falling back to a
fresh `PATH` search. This does **not** verify binary integrity (e.g., a
checksum) — only existence and the executable bit; that limitation is
intentional, not an oversight, and is unchanged from before.

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

**`verify()` runs again inside `execute()`, every time** (remediation,
Finding B-1 at the adapter layer — the Engine-layer fix is below). Before
ever constructing a command, `execute()` calls `self.verify()` and
populates `ExecutionResult.verification_status` with whatever it
returns; anything other than `AVAILABLE` short-circuits to
`ExecutionOutcome.ERROR` with a matching `error_classification`
(`adapter_unavailable`/`adapter_unauthenticated`/`adapter_unsupported`/
`adapter_verification_error`) and never constructs a command at all.
This never invents a successful verification — the status recorded is
always the actual, fresh result of that call.

**Output truncation is byte-accurate** (remediation; previously
character-count slicing, which could keep up to 2x the intended bytes
for non-ASCII output or silently undercount). `stdout`/`stderr` are each
bounded independently to `policy.max_output_bytes` by encoding to UTF-8,
slicing the real byte string, and decoding back with a partial trailing
multi-byte sequence dropped rather than raising or emitting replacement
characters — see `_truncate_utf8_bytes` and
`OutputTruncationTests`.

**Duplicate-execution protection**: re-calling `execute()` with an
`execution_id` already seen returns the cached first result, never
re-running — including when that first result was `TIMED_OUT` (an
uncertain outcome is still never silently retried). This guarantee holds
for the lifetime of one `ClaudeCodeAdapter` instance only — it is **not**
crash-persistent. A process restart loses the cache; `StateManager.
save_execution_result` now raises `errors.DuplicateError` on a repeated
`execution_id` (remediation, Finding E-2 — previously a raw
`sqlite3.IntegrityError` would have surfaced), matching how `tasks.
create()` already behaves. Nothing in this milestone automatically
retries a task found with an uncertain prior execution result on
restart (consistent with `TaskManager.recover()`'s existing rule for
`INTERRUPTED` tasks), and this remediation deliberately does not add a
database-backed check to prevent a *fresh* `execute()` call (with a new
`execution_id`) from re-running the same task after a restart — doing
so would be a new persistence architecture, out of scope here.

**Cancellation**: `cancel(execution_id)` sends the same
graceful-then-forceful process-group termination used for timeouts.
Best-effort and cooperative — it cannot interrupt the CLI mid-API-call
any faster than the process itself can be signalled, and returns `False`
if nothing matching is currently running.

## Engine integration

`OrchestrationEngine.execute(task_id, working_directory, ...)`:

1. Requires `self.adapters`, `self.agents`, **and `self.providers`**
   (an `AdapterRegistry`, `AgentRegistry`, `ProviderRegistry`) all
   configured — `RuntimeError` otherwise. `providers` is new in the
   remediation; `agents` graduated from optional to required for the
   same reason.
2. Fetches the task's most recent routing decision. No decision, or a
   `NO_ELIGIBLE_AGENT` decision → `OrchestratorError`. Routing is never
   bypassed.
3. **Revalidates, every time** (remediation, Findings B-1/F-1 — a
   `RoutingDecision` can go stale between `route()` and `execute()`):
   - the selected agent's current `status` must be `AgentStatus.
     AVAILABLE` — else `errors.AgentNotEligibleError`;
   - the selected provider must still be `verified` and `AVAILABLE` —
     else `errors.ProviderNotVerifiedError` (reused rather than adding
     a near-duplicate type);
   - the selected adapter's own `verify()` must return
     `VerificationStatus.AVAILABLE` right now — else
     `errors.AdapterUnavailableError`.
   Any failure raises before an `ExecutionRequest` is even built —
   `adapter.execute()` is never called (verified by
   `DispatchRevalidationTests.test_refused_dispatch_never_calls_adapter_execute`).
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

Unchanged by the remediation:

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
  record but is not itself consulted to prevent a *fresh* `execute()`
  call from re-running the same task after a restart.

New, from the remediation itself — residual risks the Founder has
acknowledged rather than this document silently papering over:

- **Filesystem, process, and network isolation remain unenforced** (see
  the "Security posture" table). The plugin/MCP/hook restriction added
  this round closes a real gap, but it is still CLI configuration, not
  a sandbox, container, or network policy. Real project execution
  still requires Git Worktree isolation (M6).
- **The exposed OAuth token from the audit was not rotated as part of
  this remediation** — the Founder directive explicitly deferred that
  decision; it remains an acknowledged residual risk outside this
  codebase's scope to fix.
- **Executable pinning checks existence and the executable bit only**,
  never binary integrity (no checksum/signature verification). A
  pinned path that is replaced in-place with different content between
  checks would not be detected.
- **Process-group termination has not been verified against the real
  Claude CLI's own child-process behavior** (e.g., any MCP server
  process it might start before `--strict-mcp-config` takes effect, or
  before this remediation at all) — only against single-process fake
  executables in the automated tests, per the audit's own no-paid-call
  constraint on real verification.
