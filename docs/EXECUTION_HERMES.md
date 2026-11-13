# Hermes Agent Adapter

`execution/hermes.py` — the second concrete `ExecutionAdapter`
(`execution/adapter.py`), alongside `ClaudeCodeAdapter`
(`execution/claude_code.py`, Milestone 4). Everything below was
verified against the real, running Hermes installation on the
development host before being written; nothing is assumed from
Claude's behavior or from another Hermes version's documentation.

## Machine identity (re-verified for this milestone)

Still `tieu`, the ASUS laptop — not the Intel NUC the approved
architecture names as the central Orchestrator host. Hermes runs here
as a Docker container, not on any dedicated Worker machine; the RTX
3070 Desktop remains untouched and unconnected (M7 scope).

## Hermes installation (real, verified)

| Property | Value | Evidence |
|---|---|---|
| Install method | Docker container | `docker ps --filter name=hermes` → `nousresearch/hermes-agent`, `Up` |
| Version | **Hermes Agent v0.21.0 (2026.8.31) · upstream 922c0d67** | `docker exec hermes hermes --version` |
| Native host CLI | **None** | `which hermes` on the host returns nothing — Hermes is reachable only via `docker exec hermes hermes ...` |
| Host bind mount | `~/.hermes` (host) → `/opt/data` (container), read-write | `docker inspect hermes` — Hermes's own data/config dir only; **no general project-workspace mount exists** |
| Network | Container network `hermes_net`, not host networking | `docker inspect hermes` |
| Python | 3.13.5 | `docker exec hermes python3 --version` |

**Load-bearing consequence:** since there is no bind-mounted project
workspace, `ExecutionRequest.working_directory` for this adapter is a
path *inside the container's own filesystem*, not a host path — the
opposite of `ClaudeCodeAdapter`, where it is a host path because
Claude runs natively. A real execution against an actual project's
files needs that project's files reachable inside the container, which
is a Git Worktree Manager (M6) concern.

## Verified Hermes CLI capabilities

The real CLI's command surface is much larger than Claude Code's and
was explored through its actual `--help` output at every level
(`hermes --help`, `hermes chat --help`, `hermes status --help`,
`hermes auth --help`, `hermes tools --help`) — not assumed from any
other version's docs.

| Check | Command | Result |
|---|---|---|
| Non-interactive invocation | `hermes chat --help` | `-q/--query QUERY` + `--oneshot` (or `-Q/--quiet`, or non-TTY) → answers and exits |
| Literal, shell-safe prompt input | `hermes chat --help` | `--query-file PATH`: "Safe for arbitrary text: nothing is shell-interpreted, so quotes, $(...), and backticks are preserved verbatim" — this adapter uses `-q` with a single literal argv element, same guarantee |
| Output mode | `hermes chat --help` | `-Q/--quiet`: "suppress banner, spinner, and tool previews. Only output the final response and session info" — **plain text, no JSON mode exists anywhere in `--help`** (checked at every level) |
| Config/plugin/MCP isolation | `hermes --help` | `--safe-mode`: "disable ALL customizations — user config, AGENTS.md/memory injection, plugins, and MCP servers (implies --ignore-user-config and --ignore-rules)" |
| Dangerous bypass flag — never used | `hermes --help` | `--yolo`: "Bypass all dangerous command approval prompts (use at your own risk)" |
| Auto-approve hooks — never used | `hermes --help` | `--accept-hooks`: auto-approves unseen shell hooks without a TTY prompt |
| Working directory | `hermes --help` | `--in DIR`: "Change into DIR before starting" — this adapter instead uses `docker exec -w DIR` directly, verified to fail cleanly (exit `127`) for a nonexistent path |
| Authentication status (redacted) | `hermes status` | Real output includes a line like `OpenAI ✓ sk-p...3lEA` — Hermes's own convention masks the middle of the key, but this adapter never stores or returns even that masked form; it reduces the whole section to one boolean (see "Security policy") |
| Toolset restriction | `hermes chat --help` | `-t/--toolsets TOOLSETS`: "Comma-separated toolsets to enable" |

### The zero-tool mechanism — verified, not officially documented

Hermes has **14 built-in tools by default** (file read/write/patch,
`code_execution`, `computer_use`, `browser`, `delegate_task`,
`cronjob_manage`, and more) — nothing like Claude Code's documented
`--tools ""`. Tracing `cli.py`'s own source
(`_build_cli_from_args`/`_init_toolsets`) found that an **empty**
`--toolsets` string is indistinguishable from not passing the flag at
all (both are Python-falsy, both fall through to the default toolset
selection) — confirmed empirically: `--toolsets ""` still showed 17
tools. Passing a **deliberately unrecognized** toolset name, however,
produced "**0 tools · 0 skills**" / "(;_;) No tools available" —
confirmed by direct `cli.py --list_tools` output, not guessed. This
adapter uses that mechanism (`_ZERO_TOOLSET_SENTINEL =
"__uo_disable_all_tools__"`), and the real smoke test below confirms
it also works through the actual `hermes chat` entrypoint (not just
`cli.py` directly), at the cost of one benign warning line in stdout:
`Warning: Unknown toolsets: __uo_disable_all_tools__`.

This is **not an officially documented flag value** — it is a verified
behavior of the current version, not a guaranteed contract. If a future
Hermes version changes how unrecognized toolset names are handled,
`verify()`'s help-text check would not catch that on its own (it only
confirms `-t/--toolsets` still exists as a flag, not that invalid names
still zero out tools). This is recorded honestly in "Known limitations"
below rather than silently assumed safe forever.

## Security policy

| Boundary | Enforced? | How |
|---|---|---|
| **Built-in tool restriction** | **Yes** | `-t __uo_disable_all_tools__` — verified (see above) to zero out all 14 built-in tools, both via `cli.py --list_tools` and via a real `hermes chat` call. |
| **Plugin / hook / MCP / user-config restriction** | **Yes** | `--safe-mode` — Hermes's own documented flag, disabling CLAUDE.md-equivalent injection, plugins, MCP servers, and user config in one step. |
| **Filesystem isolation** | **No** | Not a sandbox. The container itself provides process/filesystem separation from the host (standard Docker), but nothing in this adapter adds isolation *within* the container beyond the tool restriction above. |
| **Process isolation** | **Partial, and genuinely different from Claude** | `start_new_session=True` groups the **local** `docker exec` client process for cleanup — but killing that local client does **not** stop the process it started inside the container (verified empirically: a `sleep` survived the local client being SIGTERM'd). This adapter additionally writes the in-container PID to a file via a small `sh -c 'echo $$ > pidfile; exec "$@"'` wrapper and sends a second, *targeted* `docker exec ... kill` to that exact PID on timeout — verified end-to-end in unit tests, and never a broad `pkill` (this container also runs this host's live Telegram bridge; a pattern-match kill could disrupt unrelated, real usage). |
| **Network isolation** | **No** | The container has its own Docker network, but nothing here restricts what the Hermes process itself can reach from inside it. |
| **Environment restriction** | **Yes** | Same allowlist discipline as `ClaudeCodeAdapter` (`HOME, PATH, LANG, LC_ALL, TERM, TMPDIR`) passed to every `docker`/`docker exec` call this adapter makes — never the full parent environment. |
| **Credential handling** | **Yes** | `hermes status`'s output (which contains a masked key preview, e.g. `sk-p...3lEA`) is reduced to a single boolean in memory and never stored, logged, or returned — see `_has_any_provider_credential`. No API key is ever passed as a command-line argument (Hermes's own `-a/--api_key` flag exists but this adapter never uses it, relying entirely on the pre-existing configured credential). |
| **Dangerous flags** | **Never used** | `--yolo`, `--accept-hooks`, `--worktree` never appear anywhere in this module — verified by a source-inspection test, not merely by promise. |

## Execution lifecycle

`verify()`: resolve `docker` → pin the container by ID (not by name —
see "Executable/container pinning" below) → `hermes --version` →
`hermes chat --help` (checks 5 required flags) → `hermes status`
(reduced to one boolean) → one of `AVAILABLE/UNAVAILABLE/
UNAUTHENTICATED/UNSUPPORTED/ERROR`.

`execute(request)`: fails closed for any non-`READ_ONLY` policy; calls
`verify()` again internally first (same defense-in-depth pattern as
`ClaudeCodeAdapter`) and populates `verification_status` with whatever
it actually returns; builds
`docker exec -w <working_directory> <container_id> sh -c 'echo $$ > <pidfile>; exec "$@"' sh hermes chat -q <prompt> -Q --oneshot --safe-mode -t __uo_disable_all_tools__`
and runs it as a local subprocess, in its own process group, bounded by
`request.timeout_seconds`. On timeout: read the pidfile via a separate
`docker exec ... cat`, send `kill -TERM` to that exact PID via another
`docker exec`, poll with `kill -0` for up to the same grace period used
by `ClaudeCodeAdapter`, then `kill -KILL` if still alive — verified by
a unit test using a SIGTERM-ignoring fake process, the same escalation
proof used for Claude.

Output is treated as **plain text** (`output_format="text"`,
`structured_output=None`) — there is no JSON mode to parse. Exit code
`127` is classified distinctly (`working_directory_invalid_or_command_
not_found`) because Docker's own OCI runtime uses it specifically for
"could not even set up the exec'd process" (e.g. a bad `-w` path) —
verified: a nonexistent working directory reliably produces exactly
that code.

### Executable/container pinning

Same reasoning as `ClaudeCodeAdapter`'s executable pinning, adapted to
Docker: `self._container` (a *name*, e.g. `"hermes"`) is resolved to
its current container **ID** exactly once per adapter instance; every
later call re-validates that same ID is still running, never
re-resolving by name. A container removed and recreated under the same
name would otherwise silently become a different container between
`verify()` and `execute()` — pinning the ID closes that window.

## Real smoke test (one bounded, real call — not a mock)

Run via the actual adapter code (not a hand-built command), against an
isolated, **container-local** scratch directory created solely for
this test (`/tmp/uo-m5-smoke-sandbox` — not a bind mount, not any
project's files, removed afterward):

```python
adapter = HermesAdapter(container="hermes")
adapter.verify()  # -> AVAILABLE
adapter.execute(ExecutionRequest(
    ..., working_directory="/tmp/uo-m5-smoke-sandbox",
    input_context="Reply with exactly this text and nothing else: HERMES_SMOKE_TEST_OK",
    timeout_seconds=90.0,
))
```

| Field | Result |
|---|---|
| Hermes version | `Hermes Agent v0.21.0 (2026.8.31) · upstream 922c0d67` |
| Outcome | `COMPLETED` |
| Exit code | `0` |
| Duration | ~13 seconds (adapter-measured) |
| stdout | `Warning: Unknown toolsets: __uo_disable_all_tools__` followed by `HERMES_SMOKE_TEST_OK` — the model answered exactly as asked, and the zero-tool warning is the only side effect |
| stderr | A session-ID line only (`session_id: 20261113_064952_133b25`) — no credential, no secret |
| Scratch directory before/after | `docker exec hermes find /tmp/uo-m5-smoke-sandbox` → identical, empty both times |
| Leftover pidfile | None — `_remove_pidfile` cleaned it up |
| Leftover process | None — only the pre-existing steady-state gateway/session processes remained |
| Credential exposure | None — no API key value, no masked preview, appeared anywhere in the command, output, or this document |

This confirms, for real: CLI detection, authentication, non-interactive
invocation, the zero-tool mechanism working through the actual `chat`
entrypoint (not just `cli.py`), zero filesystem change, and clean
process/pidfile teardown.

## Multi-runtime architecture

`HermesAdapter` and `ClaudeCodeAdapter` share the same
`ExecutionAdapter` contract and are selected purely by `provider_id`
through `execution/registry.py`'s `AdapterRegistry` — grep-confirmed
(again, for this milestone) that `core/engine.py`, `routing/router.py`,
`registry/providers.py`, and `execution/adapter.py` contain no
Claude- or Hermes-specific code. `tests/test_multi_runtime.py` proves,
with both real adapter classes against fake executables: both satisfy
`ExecutionAdapter`, both verify independently, one being unavailable
never affects the other, both produce the same `ExecutionResult` shape,
and `OrchestrationEngine.execute()` dispatches a `reviewer`-role task
to Claude and a `coder`-role task to Hermes correctly in the same test
run.

## Known limitations

- **The zero-tool mechanism is empirically verified, not officially
  documented** — see "The zero-tool mechanism" above. A future Hermes
  version could change unrecognized-toolset-name handling without
  `verify()`'s current checks catching it.
- **In-container process termination depends on the pidfile write
  succeeding.** If the wrapper script's `echo $$ > pidfile` step itself
  were to fail (e.g., an unwritable `/tmp` inside the container), the
  targeted kill has no PID to signal; the local client's own process
  group is still terminated either way, but the in-container work could
  outlive the timeout undetected. Not observed in testing, but not
  provably impossible either.
- **No JSON/structured output exists** for this CLI — every result's
  `structured_output` is always `None`; only `stdout` carries evidence.
- Same unchanged-from-Claude limitations apply: no filesystem/process/
  network sandbox, no cost/token budgeting enforced beyond what the CLI
  itself might report, no automated retry, no distributed execution,
  in-memory-only duplicate-execution protection (not durable across a
  restart).
- **Claude and Hermes sharing a working tree concurrently is prevented
  only by discipline, not by code in this milestone** — M5 does not add
  a lock or Conflict Manager (that is explicitly M6 scope); this
  adapter and `ClaudeCodeAdapter` simply have no shared state that
  would let them collide today, since M5 introduces no concurrent
  dispatch mechanism either.
